"""Video processing job model.

Tracks video uploads through the processing lifecycle:
    draft → received → processing → done | failed | cancelled

The worker extracts audio, sends it to Gemini for transcript + keyframe timestamps,
extracts still frames, and stores everything as derived data. The video file is
deleted only after frame extraction completes.
"""

import os
import json
import hashlib
import base64
import subprocess
import logging

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class UnifixVideoJob(models.Model):
    _name = 'unifix.video.job'
    _description = 'Unifix Video Processing Job'
    _order = 'create_date desc'

    name = fields.Char(required=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('received', 'Received'),
        ('processing', 'Processing'),
        ('done', 'Done'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, index=True)

    tmp_path = fields.Char()
    source_filename = fields.Char()
    source_size = fields.Integer()
    source_size_mb = fields.Float(compute='_compute_size_mb')
    source_hash = fields.Char(index=True)

    transcript = fields.Text()
    segment_ids = fields.One2many('unifix.video.segment', 'job_id')
    keyframe_ids = fields.One2many('unifix.video.keyframe', 'job_id')
    keyframe_count = fields.Integer(compute='_compute_keyframe_count')

    # Work-order fields extracted by Gemini
    worker_name = fields.Char()
    company = fields.Char()
    location = fields.Char()
    reported_problem = fields.Text()
    diagnosis_cause = fields.Text()
    work_performed = fields.Text()
    parts_used = fields.Text()
    start_time = fields.Char()
    end_time = fields.Char()
    total_time_spent = fields.Char()
    future_recommendations = fields.Text()
    remaining_tasks = fields.Text()

    error_message = fields.Text()
    retry_count = fields.Integer(default=0)
    processed_at = fields.Datetime()

    @api.depends('source_size')
    def _compute_size_mb(self):
        for rec in self:
            rec.source_size_mb = (rec.source_size or 0) / (1024.0 * 1024.0)

    @api.depends('keyframe_ids')
    def _compute_keyframe_count(self):
        for rec in self:
            rec.keyframe_count = len(rec.keyframe_ids)

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_retry(self):
        """Manually retry a failed job."""
        self.ensure_one()
        if self.state != 'failed':
            raise UserError("Only failed jobs can be retried.")
        if not self.tmp_path or not os.path.exists(self.tmp_path):
            raise UserError(
                "The original video has been cleaned up. Please upload again."
            )
        self.write({'state': 'received', 'error_message': False})

    def action_cancel(self):
        """Cancel a pending/processing job and clean up temp files."""
        self.ensure_one()
        if self.state in ('done', 'cancelled'):
            raise UserError("Cannot cancel a completed or already-cancelled job.")
        self._cleanup_temp_files()
        self.write({'state': 'cancelled'})

    # ── Worker ───────────────────────────────────────────────────────────────

    def _process_video(self):
        """Main processing entry point. Called by the cron worker.

        Ordering is critical: video is deleted ONLY after frames are extracted.
        """
        self.ensure_one()
        audio_path = None
        try:
            self.write({'state': 'processing'})

            tmp_path = self.tmp_path
            if not tmp_path or not os.path.exists(tmp_path):
                raise FileNotFoundError(
                    f"Temp video file not found: {tmp_path}"
                )

            # 1) Integrity hash (streaming — never buffer the whole file)
            self.source_hash = self._compute_sha256(tmp_path)

            # 2) Extract audio — KEEP the video for frame extraction
            audio_path = self._extract_audio(tmp_path)

            # 3) Gemini: transcript + segments + keyframe timestamps + fields
            result = self._call_gemini(audio_path)

            # 4) Audio no longer needed
            os.unlink(audio_path)
            audio_path = None

            # 5) Create segments BEFORE keyframes so we can link them
            self._create_segments(result)

            # 6) Select keyframe timestamps from Gemini's curated list
            timestamps = self._select_keyframe_timestamps(result)

            # 7) Extract frames at those timestamps → store as keyframe records
            frames = self._extract_frames(tmp_path, timestamps)
            self._store_keyframes(frames, result)

            # 8) NOW the video is safe to delete
            os.unlink(tmp_path)
            self.tmp_path = False

            # 9) Persist transcript + work-order fields
            self._write_results(result)

            self.write({
                'state': 'done',
                'processed_at': fields.Datetime.now(),
            })

            _logger.info(
                "Job %s completed: %d segments, %d keyframes",
                self.id, len(self.segment_ids), len(self.keyframe_ids),
            )

        except Exception as e:
            _logger.exception("Unifix job %s failed", self.id)
            self.write({
                'state': 'failed',
                'error_message': str(e),
                'retry_count': self.retry_count + 1,
            })
            self._cleanup_temp_files(audio_path)

    def _compute_sha256(self, path):
        """Stream-hash a file without loading it into memory."""
        sha = hashlib.sha256()
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                sha.update(chunk)
        return sha.hexdigest()

    def _extract_audio(self, video_path):
        """Extract mono 16kHz WAV audio from a video using ffmpeg."""
        audio_path = video_path.rsplit('.', 1)[0] + '.wav'
        proc = subprocess.run(
            ['ffmpeg', '-i', video_path, '-vn',
             '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
             '-y', audio_path],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg audio extraction failed: {proc.stderr[:500]}")
        return audio_path

    def _call_gemini(self, audio_path):
        """Send audio to Gemini and return parsed JSON with transcript + fields."""
        from google import genai
        ICP = self.env['ir.config_parameter'].sudo()
        api_key = ICP.get_param('unifix.gemini_api_key')
        model = ICP.get_param('unifix.gemini_model', 'gemini-2.5-pro')
        prompt = ICP.get_param('unifix.transcript_prompt')

        if not api_key:
            raise RuntimeError("Gemini API key not configured (unifix.gemini_api_key)")

        if not prompt:
            # Load default prompt from module
            prompt_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                'prompts', 'transcript_v1.txt'
            )
            with open(prompt_path, 'r') as f:
                prompt = f.read()

        client = genai.Client(api_key=api_key)
        with open(audio_path, 'rb') as f:
            audio_bytes = f.read()

        response = client.models.generate_content(
            model=model,
            contents=[
                genai.types.Part.from_bytes(
                    data=audio_bytes, mime_type='audio/wav'),
                prompt,
            ],
            config=genai.types.GenerateContentConfig(
                response_mime_type='application/json',
                temperature=0.2,
            ),
        )
        return json.loads(response.text)

    def _select_keyframe_timestamps(self, result):
        """Choose which timestamps to capture as frames.

        Prefers Gemini's curated keyframes list; falls back to segment starts.
        De-duplicates near-identical timestamps (<0.5s apart) and caps at max.
        """
        max_kf = int(self.env['ir.config_parameter'].sudo()
                     .get_param('unifix.max_keyframes', '12'))
        curated = result.get('keyframes') or []
        if curated:
            items = [(float(k['t']), k.get('reason')) for k in curated]
        else:
            items = [
                (float(s['start']), None)
                for s in result.get('segments', [])
            ]
        # De-duplicate and cap
        items.sort(key=lambda x: x[0])
        deduped, last = [], -10.0
        for t, reason in items:
            if t - last >= 0.5:
                deduped.append((t, reason))
                last = t
        return deduped[:max_kf]

    def _extract_frames(self, video_path, timestamps):
        """Extract JPEG frames at the given timestamps via ffmpeg.

        Returns list of (timestamp, jpeg_bytes, reason).
        """
        q = self.env['ir.config_parameter'].sudo().get_param(
            'unifix.frame_quality', '3')
        out = []
        for t, reason in timestamps:
            proc = subprocess.run(
                ['ffmpeg',
                 '-ss', f'{t:.3f}',
                 '-i', video_path,
                 '-frames:v', '1',
                 '-q:v', str(q),
                 '-f', 'image2pipe', '-vcodec', 'mjpeg',
                 'pipe:1'],
                capture_output=True)
            if proc.returncode != 0 or not proc.stdout:
                _logger.warning(
                    "Frame extraction at %.3fs failed: %s",
                    t, proc.stderr[:300] if proc.stderr else 'no output',
                )
                continue
            out.append((t, proc.stdout, reason))
        return out

    def _store_keyframes(self, frames, result):
        """Create unifix.video.keyframe records for each extracted frame."""
        Keyframe = self.env['unifix.video.keyframe']
        segs = sorted(self.segment_ids, key=lambda s: s.start_time or 0.0)
        for i, (t, jpeg_bytes, reason) in enumerate(frames):
            seg = self._nearest_segment(segs, t)
            Keyframe.create({
                'job_id': self.id,
                'segment_id': seg.id if seg else False,
                'sequence': (i + 1) * 10,
                'timestamp': t,
                'timestamp_display': self._fmt_ts(t),
                'image': base64.b64encode(jpeg_bytes),
                'image_filename': f'frame_{int(t * 1000):08d}.jpg',
                'reason': reason,
            })

    def _nearest_segment(self, sorted_segments, timestamp):
        """Find the segment closest to the given timestamp."""
        best, best_dist = None, float('inf')
        for seg in sorted_segments:
            dist = abs((seg.start_time or 0.0) - timestamp)
            if dist < best_dist:
                best_dist = dist
                best = seg
        return best

    def _create_segments(self, result):
        """Create unifix.video.segment records from Gemini's segment list."""
        Segment = self.env['unifix.video.segment']
        for i, seg in enumerate(result.get('segments', [])):
            start = float(seg.get('start', 0))
            end = float(seg.get('end', 0))
            Segment.create({
                'job_id': self.id,
                'sequence': (i + 1) * 10,
                'start_time': start,
                'end_time': end,
                'start_time_display': self._fmt_ts(start),
                'end_time_display': self._fmt_ts(end),
                'text': seg.get('text', ''),
                'confidence': float(seg.get('confidence', 0)),
            })

    def _write_results(self, result):
        """Persist transcript and work-order fields to the job record."""
        self.write({
            'transcript': result.get('transcript', ''),
            'worker_name': result.get('worker', ''),
            'company': result.get('company', ''),
            'location': result.get('location', ''),
            'reported_problem': result.get('reported_problem', ''),
            'diagnosis_cause': result.get('diagnosis_cause', ''),
            'work_performed': result.get('work_performed', ''),
            'parts_used': result.get('parts_used', ''),
            'start_time': result.get('start_time', ''),
            'end_time': result.get('end_time', ''),
            'total_time_spent': result.get('total_time_spent', ''),
            'future_recommendations': result.get('future_recommendations', ''),
            'remaining_tasks': result.get('remaining_tasks', ''),
        })

    def _cleanup_temp_files(self, extra_path=None):
        """Delete temp video and optionally an extra file (e.g. audio)."""
        for path in [self.tmp_path, extra_path]:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    _logger.warning("Could not delete temp file: %s", path)
        if self.tmp_path and extra_path is None:
            self.tmp_path = False

    @staticmethod
    def _fmt_ts(seconds):
        """Format seconds as mm:ss."""
        m, s = divmod(int(seconds), 60)
        return f'{m:02d}:{s:02d}'

    @api.model
    def _cleanup_orphaned_files(self):
        """Cleanup orphaned temp files (called by cron)."""
        import glob
        import time
        import tempfile

        ICP = self.env['ir.config_parameter'].sudo()
        tmp_dir = ICP.get_param('unifix.temp_dir', tempfile.gettempdir())
        age_h = int(ICP.get_param('unifix.cleanup_age_hours', '1'))
        cutoff = time.time() - age_h * 3600

        for pat in ['unifix_*.mp4', 'unifix_*.mkv', 'unifix_*.mov',
                     'unifix_*.avi', 'unifix_*.webm', 'unifix_*.wav']:
            for path in glob.glob(os.path.join(tmp_dir, pat)):
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.unlink(path)
                except OSError:
                    pass

        stale = self.search([
            ('state', 'in', ['received', 'processing']),
            ('create_date', '<',
             fields.Datetime.subtract(fields.Datetime.now(), hours=age_h)),
        ])
        for job in stale:
            if not job.tmp_path or not os.path.exists(job.tmp_path):
                job.write({
                    'state': 'failed',
                    'error_message': 'Temp file expired before processing completed.'
                })
