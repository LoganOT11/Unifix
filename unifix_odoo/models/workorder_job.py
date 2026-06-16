"""Work-order processing job model.

Handles three media kinds through one lifecycle
(draft → received → processing → done | failed | cancelled):

  * audio  — stored (compressed to Opus); work-order fields + transcript.
  * image  — stored (resized JPEG); image pipeline (schema v3, nested tasks).
  * video  — NOT stored; audio is extracted for work-order fields + transcript,
             keyframe stills are extracted at Gemini-chosen cue timestamps, then
             the video is deleted.

Extraction is delegated to the vendored engine under ``unifix_odoo/processing``
(``get_pipeline(mode, cfg, provider).run(ctx)``). OpenCV keyframe selection and
ffmpeg/cv2 compression live in ``processor.keyframes`` / ``processor.compression``;
this model orchestrates I/O, auth, provider selection, and maps results to fields.
"""

import os
import json
import base64
import hashlib
import logging
import tempfile
from pathlib import Path

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Single source of truth for the default model (overridable via unifix.gemini_model).
_DEFAULT_MODEL = "gemini-3.1-flash-lite"


class UnifixWorkorderJob(models.Model):
    _name = 'unifix.workorder.job'
    _description = 'Unifix Work Order Processing Job'
    _order = 'create_date desc'

    name = fields.Char(required=True)
    media_kind = fields.Selection([
        ('audio', 'Audio'),
        ('image', 'Image'),
        ('video', 'Video'),
    ], required=True, default='audio', index=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('received', 'Received'),
        ('processing', 'Processing'),
        ('done', 'Done'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, index=True)

    # Stored media (audio & image only; video is never persisted)
    media_file = fields.Binary(attachment=True)
    media_filename = fields.Char()
    # HTML5 audio player (raw streaming via /web/content — no base64).
    audio_player = fields.Html(compute='_compute_audio_player', sanitize=False)

    # Temp working file (always for video; transient for audio/image processing)
    tmp_path = fields.Char()
    source_filename = fields.Char()
    source_size = fields.Integer(help="Original uploaded size (bytes)")
    source_size_mb = fields.Float(compute='_compute_size_mb')
    compressed_size = fields.Integer(help="Size of what is actually stored (bytes)")
    compressed_size_mb = fields.Float(compute='_compute_size_mb')
    size_saving_display = fields.Char(compute='_compute_size_saving')
    source_hash = fields.Char(index=True)

    # ── Extraction output ────────────────────────────────────────────────────
    extracted_json = fields.Text(help="Full extracted_data envelope (any kind)")
    validation_status = fields.Char(help="Overall validation status from the engine")
    validation_json = fields.Text(
        help="Per-field match status against the reference data — the grounded, "
             "deterministic confidence (replaces LLM self-confidence)")

    # Work-order fields (audio/video schema v1; image reuses worker/company/location)
    worker_name = fields.Char()
    company = fields.Char()
    client = fields.Char(help="Client/account the work was done for")
    location = fields.Char()
    wo_date = fields.Char(help="Work-order date as written/spoken")
    vehicle_equipment = fields.Char()
    reported_problem = fields.Text()
    diagnosis_cause = fields.Text()
    work_performed = fields.Text()
    parts_used = fields.Text()
    start_time = fields.Char()
    end_time = fields.Char()
    total_time_spent = fields.Char()
    future_recommendations = fields.Text()
    remaining_tasks = fields.Text()

    # Image (schema v3) structured output
    task_ids = fields.One2many('unifix.workorder.task', 'job_id')
    extra_data_json = fields.Text(help="Image: travel / expenses / labour_summary JSON")

    # Video transcript/keyframes — populated only by the future keyframe feature
    transcript = fields.Text()
    segment_ids = fields.One2many('unifix.video.segment', 'job_id')
    keyframe_ids = fields.One2many('unifix.video.keyframe', 'job_id')
    keyframe_count = fields.Integer(compute='_compute_keyframe_count')
    video_keyframe_mode = fields.Selection([
        ('audio_cues', 'Audio cues → sharpest frame (cheapest, no extra AI)'),
        ('verified', 'Audio cues → AI verifies best frame (recommended)'),
        ('gemini_video', 'Full video → AI picks frames (best captions, most tokens)'),
    ], string="Keyframe mode",
        default=lambda self: self.env['ir.config_parameter'].sudo().get_param(
            'unifix.video_keyframe_mode', 'verified'),
        help="How video keyframes are chosen. 'verified' adds one cheap image "
             "call so the AI confirms each chosen frame actually shows what was "
             "described; 'gemini_video' sends the whole video (duration-priced).")

    error_message = fields.Text()
    retry_count = fields.Integer(default=0)
    processed_at = fields.Datetime()

    # Gemini token usage (summed across all calls for this job)
    input_tokens = fields.Integer(help="Gemini input (prompt) tokens")
    output_tokens = fields.Integer(help="Gemini output (response) tokens")
    token_cost_display = fields.Char(compute='_compute_token_cost')

    # Which editable AI prompt(s)/schema(s) the engine actually used this run.
    prompt_used = fields.Char(
        string="Prompt(s) used", readonly=True,
        help="Prompt template name(s) the engine requested for this job.")
    schema_used = fields.Char(
        string="Schema(s) used", readonly=True,
        help="JSON schema version(s) the engine requested for this job.")

    @api.depends('source_size', 'compressed_size')
    def _compute_size_mb(self):
        for rec in self:
            rec.source_size_mb = (rec.source_size or 0) / (1024.0 * 1024.0)
            rec.compressed_size_mb = (rec.compressed_size or 0) / (1024.0 * 1024.0)

    @api.depends('source_size', 'compressed_size')
    def _compute_size_saving(self):
        for rec in self:
            o, c = rec.source_size or 0, rec.compressed_size or 0
            if o and c:
                pct = (1 - c / float(o)) * 100
                rec.size_saving_display = (
                    f"{o / 1e6:.2f} MB → {c / 1e6:.2f} MB ({pct:.0f}% smaller)")
            elif o:
                rec.size_saving_display = f"{o / 1e6:.2f} MB"
            else:
                rec.size_saving_display = ''

    @api.depends('keyframe_ids')
    def _compute_keyframe_count(self):
        for rec in self:
            rec.keyframe_count = len(rec.keyframe_ids)

    @api.depends('input_tokens', 'output_tokens')
    def _compute_token_cost(self):
        rate = float(self.env['ir.config_parameter'].sudo()
                     .get_param('unifix.token_price_per_million', '0.30'))
        for r in self:
            tot = (r.input_tokens or 0) + (r.output_tokens or 0)
            r.token_cost_display = (
                f"{r.input_tokens:,} in + {r.output_tokens:,} out = {tot:,} tokens "
                f"(≈ ${tot / 1e6 * rate:.4f})") if tot else ''

    def _add_usage(self, prompt_tokens, response_tokens):
        self.input_tokens = (self.input_tokens or 0) + int(prompt_tokens or 0)
        self.output_tokens = (self.output_tokens or 0) + int(response_tokens or 0)

    @staticmethod
    def _usage_from_response(resp):
        u = getattr(resp, 'usage_metadata', None)
        if not u:
            return (0, 0)
        return (getattr(u, 'prompt_token_count', 0) or 0,
                getattr(u, 'candidates_token_count', 0) or 0)

    @api.depends('media_file', 'media_kind', 'media_filename')
    def _compute_audio_player(self):
        from urllib.parse import quote
        for rec in self:
            if rec.media_kind == 'audio' and rec.media_file and isinstance(rec.id, int):
                # URL-encode the user-supplied filename so it can't break out of
                # the src attribute (sanitize=False on this field).
                fname = quote(rec.media_filename or 'audio', safe='')
                url = (
                    "/web/content?model=unifix.workorder.job"
                    f"&id={rec.id}&field=media_file&filename={fname}"
                )
                rec.audio_player = (
                    '<audio controls preload="metadata" style="width:100%;max-width:520px" '
                    f'src="{url}"></audio>'
                )
            else:
                rec.audio_player = False

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_process(self):
        """Queue the job for the cron worker — never run the AI call inside the
        web request (avoids proxy/worker timeouts; the cron picks it up shortly)."""
        self.ensure_one()
        if self.state in ('processing', 'done', 'cancelled'):
            raise UserError("This job cannot be queued from its current state.")
        self.write({'state': 'received', 'error_message': False})

    def action_retry(self):
        """Re-queue a failed job. Audio/image re-run from stored media; video
        needs its temp file to still exist."""
        self.ensure_one()
        if self.state != 'failed':
            raise UserError("Only failed jobs can be retried.")
        if self.media_kind == 'video':
            has_tmp = bool(self.tmp_path and os.path.exists(self.tmp_path))
            if not has_tmp and not self.media_file:
                raise UserError("The original video has been cleaned up. Please upload again.")
        elif not self.media_file:
            raise UserError("No stored media to retry. Please upload again.")
        self.write({'state': 'received', 'error_message': False})

    def action_cancel(self):
        self.ensure_one()
        if self.state in ('done', 'cancelled'):
            raise UserError("Cannot cancel a completed or already-cancelled job.")
        self._cleanup_temp_files()
        self.write({'state': 'cancelled'})

    # ── Worker ───────────────────────────────────────────────────────────────

    def _process(self):
        """Process one job by dispatching to the vendored extraction engine."""
        self.ensure_one()
        tmp_path = None
        tmp_is_scratch = False  # True when we created a throwaway temp copy
        try:
            self.write({'state': 'processing', 'input_tokens': 0, 'output_tokens': 0})

            if self.media_kind == 'video' and self.tmp_path and os.path.exists(self.tmp_path):
                # Streamed to disk by the upload controller — never stored on the record.
                tmp_path = self.tmp_path
            else:
                # Audio/image, or a video created through the backend form: materialise
                # the stored media to a scratch temp file the engine can read.
                if not self.media_file:
                    if self.media_kind == 'video':
                        raise FileNotFoundError(
                            "No video to process — the temp file is gone and no media "
                            "is stored. Please upload again.")
                    raise UserError("No media stored on this job.")
                tmp_path = self._write_media_to_temp()
                tmp_is_scratch = True

            self.source_hash = self._compute_sha256(tmp_path)

            envelope = self._run_pipeline(tmp_path)
            self._apply_envelope(envelope)

            # Video: transcript/segments/keyframes BEFORE deleting the (never-stored) video.
            if self.media_kind == 'video':
                try:
                    self._process_video_extras(tmp_path)
                except Exception:
                    _logger.exception("Video extras (transcript/keyframes) failed for job %s", self.id)
                self._safe_unlink(self.tmp_path)
                self.tmp_path = False
                # Video is never persisted — drop any form-uploaded media.
                if self.media_file:
                    self.media_file = False
            elif self.media_kind == 'audio':
                try:
                    self._store_audio_transcript(tmp_path)
                except Exception:
                    _logger.exception("Audio transcript failed for job %s", self.id)
                self._compress_media_safe(tmp_path)
            elif self.media_kind == 'image':
                self._compress_media_safe(tmp_path)

            self.write({'state': 'done', 'processed_at': fields.Datetime.now()})
            _logger.info("Unifix job %s (%s) completed", self.id, self.media_kind)

        except Exception as e:
            _logger.exception("Unifix job %s failed", self.id)
            self.write({
                'state': 'failed',
                'error_message': str(e),
                'retry_count': self.retry_count + 1,
            })
        finally:
            if tmp_is_scratch and tmp_path:
                self._safe_unlink(tmp_path)

    def _run_pipeline(self, media_path):
        """Build a PipelineContext and run the right pipeline for this job."""
        from pipeline import get_pipeline
        from pipeline.base import PipelineContext
        from config import load_document_config

        ICP = self.env['ir.config_parameter'].sudo()
        model_id = ICP.get_param('unifix.gemini_model', _DEFAULT_MODEL)

        mode = {'image': 'image', 'video': 'video'}.get(self.media_kind, 'audio')
        cfg_name = 'image_v3' if mode == 'image' else 'audio_v1'
        cfg = load_document_config(cfg_name)

        client = self._gemini_client()
        provider = self._reference_provider()
        ctx = PipelineContext(
            source_path=Path(media_path),
            output_dir=None,
            client=client,
            model_id=model_id,
            config=cfg,
            serialize=False,                                   # Odoo keeps the envelope, not a file
            safe_root=os.path.dirname(os.path.abspath(media_path)),
        )
        # Bind this env so the engine's prompt/schema loader serves the editable
        # DB copies (unifix.ai.prompt / unifix.ai.schema) with file fallback.
        from .ai_prompt import bind_resolver_env, current_usage
        with bind_resolver_env(self.env):
            envelope = get_pipeline(mode, cfg, provider).run(ctx)
            used = current_usage() or {}
            self.prompt_used = ", ".join(used.get('prompt') or []) or False
            self.schema_used = ", ".join(used.get('schema') or []) or False
        return envelope

    def _apply_envelope(self, envelope):
        """Map the engine's returned envelope onto this record's fields."""
        ed = envelope.get('extracted_data', {}) or {}
        self.extracted_json = json.dumps(ed, ensure_ascii=False, indent=2)
        val = envelope.get('validation')
        if isinstance(val, dict):
            self.validation_status = val.get('overall_status')
            self.validation_json = json.dumps(
                val.get('fields') or {}, ensure_ascii=False, indent=2)
        else:
            self.validation_status = False
            self.validation_json = False
        u = envelope.get('usage') or {}
        self._add_usage(u.get('prompt_tokens'), u.get('response_tokens'))
        if self.media_kind == 'image':
            self._apply_image(ed)
        else:
            self._apply_workorder_flat(ed)

    def _apply_header(self, ed):
        """Shared header block — one mapping for every kind.

        worker / company / client / location / date + the follow-up notes are
        common to schema v1 (audio/video) and v3 (image), so both routes go
        through here instead of each mapping the overlap its own way.
        """
        self.write({
            'worker_name': ed.get('worker') or '',
            'company': ed.get('company') or '',
            'client': ed.get('client') or '',
            'location': ed.get('location') or '',
            'wo_date': ed.get('date') or '',
            'future_recommendations': ed.get('future_recommendations') or '',
            'remaining_tasks': ed.get('remaining_tasks') or '',
        })

    def _apply_workorder_flat(self, ed):
        """Audio/video — shared header + flat schema-v1 body."""
        self._apply_header(ed)
        self.write({
            'vehicle_equipment': ed.get('vehicle_equipment') or '',
            'reported_problem': ed.get('reported_problem') or '',
            'diagnosis_cause': ed.get('diagnosis_cause') or '',
            'work_performed': ed.get('work_performed') or '',
            'parts_used': ed.get('parts_used') or '',
            'start_time': ed.get('start_time') or '',
            'end_time': ed.get('end_time') or '',
            'total_time_spent': ed.get('total_time_spent') or '',
        })

    def _apply_image(self, ed):
        """Image — shared header + nested schema-v3 body (task rows + JSON extras)."""
        self._apply_header(ed)
        self.write({
            'extra_data_json': json.dumps({
                'travel': ed.get('travel'),
                'expenses': ed.get('expenses'),
                'labour_summary': ed.get('labour_summary'),
            }, ensure_ascii=False, indent=2),
        })
        # Task lines are backend-derived data; create them with elevated rights
        # so processing works regardless of the triggering user's group.
        self.task_ids.sudo().unlink()
        Task = self.env['unifix.workorder.task'].sudo()
        for i, t in enumerate(ed.get('tasks') or []):
            tm = t.get('time') or {}
            Task.create({
                'job_id': self.id,
                'sequence': (i + 1) * 10,
                'unit': t.get('unit') or '',
                'time_start': tm.get('start') or '',
                'time_end': tm.get('end') or '',
                'time_total': tm.get('total') or '',
                'description': t.get('description') or '',
                'work_order_ref': t.get('work_order_ref') or '',
            })

    # ── Gemini auth (Vertex/ADC by default — no API key on this host) ─────────

    def _gemini_client(self):
        """Build a genai.Client for the configured backend.

        Vertex AI (ADC) when ``unifix.use_vertexai`` / ``GOOGLE_GENAI_USE_VERTEXAI``
        is truthy (no API key — auth via Application Default Credentials); else
        the Gemini Developer API with ``unifix.gemini_api_key``.
        """
        from google import genai
        ICP = self.env['ir.config_parameter'].sudo()

        def _truthy(val):
            return str(val or '').strip().lower() in ('1', 'true', 'yes', 'on')

        use_vertex = _truthy(
            ICP.get_param('unifix.use_vertexai')
            or os.environ.get('GOOGLE_GENAI_USE_VERTEXAI')
        )
        if use_vertex:
            project = (ICP.get_param('unifix.gcp_project')
                       or os.environ.get('GOOGLE_CLOUD_PROJECT'))
            location = (ICP.get_param('unifix.gcp_location')
                        or os.environ.get('GOOGLE_CLOUD_LOCATION') or 'global')
            if not project:
                raise RuntimeError(
                    "Vertex AI is enabled but no project is configured "
                    "(set unifix.gcp_project or GOOGLE_CLOUD_PROJECT)."
                )
            return genai.Client(vertexai=True, project=project, location=location)

        api_key = ICP.get_param('unifix.gemini_api_key')
        if not api_key:
            raise RuntimeError(
                "No Gemini auth configured. Either enable Vertex AI "
                "(unifix.use_vertexai=True with ADC) or set unifix.gemini_api_key."
            )
        return genai.Client(api_key=api_key)

    def _reference_provider(self):
        """Select the field-validation reference source (config-driven)."""
        ICP = self.env['ir.config_parameter'].sudo()
        kind = (ICP.get_param('unifix.reference_provider') or 'memory').strip().lower()
        if kind == 'odoo':
            from .reference_provider import OdooReferenceDataProvider
            return OdooReferenceDataProvider(self.env)
        from db.memory import InMemoryProvider
        return InMemoryProvider()

    # ── Video extras: transcript + segments + keyframes ──────────────────────
    # OpenCV keyframe selection lives in processor.keyframes.KeyframeExtractor;
    # this model only calls Gemini for cues/transcript and persists the results.

    def _keyframes_enabled(self):
        val = self.env['ir.config_parameter'].sudo().get_param('unifix.enable_keyframes', 'True')
        return str(val).strip().lower() in ('1', 'true', 'yes', 'on')

    def _process_video_extras(self, video_path):
        """Per the selected keyframe mode, get transcript + cues, choose the
        frames, and store keyframe + matching segment 1:1 (same timestamp) so
        each segment is verifiable against its frame. Best-effort.

          * audio_cues   — cues from audio; sharpest/well-exposed frame per cue.
          * verified     — cues from audio; CV proposes candidates, one Gemini
                           image call picks the frame that shows the object.
          * gemini_video — Gemini watches the whole video and picks the moments
                           (duration-priced); a tight CV window grabs the frame.
        """
        if not self._keyframes_enabled():
            return
        from processor.keyframes import KeyframeExtractor

        ICP = self.env['ir.config_parameter'].sudo()
        max_kf = int(ICP.get_param('unifix.max_keyframes', '8'))
        mode = self._keyframe_mode()

        if mode == 'gemini_video':
            analysis = self._gemini_full_video_analysis(video_path)
            cues = analysis.get('cues') or []
            kept = KeyframeExtractor(window_before=0.2, window_after=0.5).extract(
                video_path, cues, max_kf)
        else:
            analysis = self._gemini_video_analysis(video_path)   # audio-only cues
            cues = analysis.get('cues') or []
            if mode == 'verified':
                kept = self._verified_keyframes(video_path, cues, max_kf)
            else:                                                # 'audio_cues'
                kept = KeyframeExtractor().extract(video_path, cues, max_kf)

        self.transcript = (analysis.get('transcript') or '').strip() or False
        self._store_keyframes_and_segments(kept)
        _logger.info("Job %s: %d keyframes/segments (mode=%s, %d cues)",
                     self.id, len(kept), mode, len(cues))

    def _keyframe_mode(self):
        """Per-job mode, falling back to the global config default."""
        return (self.video_keyframe_mode
                or self.env['ir.config_parameter'].sudo().get_param(
                    'unifix.video_keyframe_mode', 'verified'))

    def _gemini_video_analysis(self, video_path):
        """One Gemini audio pass → {transcript, cues:[(t, caption, text)]}.

        Each cue is a show-and-tell moment; its ``text`` is what the technician
        says there, so the matching keyframe + segment can be cross-checked.
        Operates on the AUDIO only — no video tokens.
        """
        from processor.video_extractor import extract_audio_from_video
        from processor.gemini_client import call_gemini_with_retry
        from processor.parser import parse_ai_json

        ICP = self.env['ir.config_parameter'].sudo()
        model_id = ICP.get_param('unifix.gemini_model', _DEFAULT_MODEL)
        prompt = (
            "You are reviewing the AUDIO of a field-service technician's walkthrough "
            "video. Return JSON with two keys:\n"
            "- \"transcript\": full verbatim transcript of everything spoken.\n"
            "- \"cues\": array, ordered by time, of the 4-12 moments where the "
            "technician points out / shows / describes something PHYSICAL they are "
            "looking at (deictic cues like 'here','this','look at','you can see', or "
            "naming a part/component/equipment/location). Each cue is {\"t\": <seconds "
            "from start>, \"caption\": <3-6 word label of the object shown>, \"text\": "
            "<the sentence(s) the technician says at that moment>}.\n"
            "All times are seconds from the start."
        )
        audio_tmp = None
        try:
            audio_bytes, audio_mime, audio_tmp = extract_audio_from_video(video_path)
            client = self._gemini_client()
            resp = call_gemini_with_retry(client, model_id, audio_bytes, audio_mime, prompt)
            self._add_usage(*self._usage_from_response(resp))
            data = parse_ai_json(resp.text)
        except Exception:
            _logger.exception("Job %s: video audio analysis failed", self.id)
            return {}
        finally:
            self._safe_unlink(audio_tmp)

        return {'transcript': data.get('transcript') or '', 'cues': self._parse_cues(data)}

    @staticmethod
    def _parse_cues(data):
        cues = []
        for c in (data.get('cues') or []):
            try:
                t = float(c.get('t'))
            except (TypeError, ValueError):
                continue
            cues.append((t, (c.get('caption') or '').strip(), (c.get('text') or '').strip()))
        return cues

    def _gemini_full_video_analysis(self, video_path):
        """Experiment: send the WHOLE video to Gemini (visual + audio) so it picks
        the keyframe moments itself. More tokens (video is duration-priced), but
        Gemini sees the actual frames. Returns {transcript, cues:[(t,caption,text)]}."""
        import mimetypes
        from processor.gemini_client import call_gemini_with_retry
        from processor.parser import parse_ai_json

        model_id = self.env['ir.config_parameter'].sudo().get_param(
            'unifix.gemini_model', _DEFAULT_MODEL)
        mime = mimetypes.guess_type(video_path)[0] or 'video/mp4'
        with open(video_path, 'rb') as f:
            video_bytes = f.read()
        prompt = (
            "You are analysing a field-service technician's walkthrough VIDEO (with "
            "audio). Return JSON with: \"transcript\" (full verbatim transcript) and "
            "\"cues\": 4-12 moments, ordered by time, that best show the equipment/work "
            "being discussed — choose timestamps where the object is clearly VISIBLE and "
            "in focus in the frame. Each cue is {\"t\": <seconds from start>, \"caption\": "
            "<3-6 word label of what's shown>, \"text\": <what the technician says then>}."
        )
        try:
            client = self._gemini_client()
            resp = call_gemini_with_retry(client, model_id, video_bytes, mime, prompt)
            self._add_usage(*self._usage_from_response(resp))
            data = parse_ai_json(resp.text)
        except Exception:
            _logger.exception("Job %s: full-video analysis failed", self.id)
            return {}
        return {'transcript': data.get('transcript') or '', 'cues': self._parse_cues(data)}

    def _verified_keyframes(self, video_path, cues, max_kf):
        """Option 2: CV proposes candidate frames per cue; one Gemini image call
        picks the one that actually shows the captioned object (or none). Falls
        back to the highest-quality CV frame if the verification call fails."""
        from processor.keyframes import KeyframeExtractor
        # Wider net than the single-best pick so the AI has real choices and
        # say↔show lag (either direction) is absorbed.
        ext = KeyframeExtractor(window_before=0.6, window_after=2.5)
        per_cue = ext.candidates_per_cue(video_path, cues, k_per_cue=3, max_cues=max_kf)
        if not per_cue:
            return []
        try:
            chosen = self._gemini_pick_frames(per_cue)   # {cue_index: cand_index | None}
        except Exception:
            _logger.exception("Job %s: frame verification failed; using CV best", self.id)
            chosen = None

        kept = []
        for i, cue in enumerate(per_cue):
            cands = cue['candidates']
            if chosen is None:
                idx = 0                       # verification failed → best CV frame
            else:
                idx = chosen.get(i)           # None/missing → AI saw nothing relevant → drop
            if idx is None or not (0 <= idx < len(cands)):
                continue
            _t_frame, jpg_full, _thumb = cands[idx]
            kept.append((cue['t'], cue['caption'], cue['text'], jpg_full))
        return kept[:max_kf]

    def _gemini_pick_frames(self, per_cue):
        """Send candidate thumbnails + captions; return {cue_index: candidate_index|None}."""
        from google.genai import types
        from processor.gemini_client import call_gemini_generic
        from processor.parser import parse_ai_json

        model_id = self.env['ir.config_parameter'].sudo().get_param(
            'unifix.gemini_model', _DEFAULT_MODEL)
        contents = [
            "You are choosing the best still frame for each labelled moment in a "
            "field-service walkthrough video. For every CUE below you are given "
            "candidate frames, indexed from 0. For each cue pick the ONE candidate "
            "that most clearly shows the object named in the caption AND is in sharp "
            "focus. If no candidate clearly shows it, use null for that cue.\n"
            'Return JSON only: {"choices": [{"cue": <int>, "candidate": <int or null>}]}'
        ]
        for i, cue in enumerate(per_cue):
            contents.append(
                f'\nCUE {i}: "{cue["caption"]}" — spoken: {(cue["text"] or "")[:160]}')
            for j, (_t, _full, thumb) in enumerate(cue['candidates']):
                contents.append(f"candidate {j}:")
                contents.append(types.Part.from_bytes(data=thumb, mime_type='image/jpeg'))

        client = self._gemini_client()
        resp = call_gemini_generic(
            client, model_id, contents,
            types.GenerateContentConfig(response_mime_type="application/json",
                                        temperature=0.1))
        self._add_usage(*self._usage_from_response(resp))
        data = parse_ai_json(resp.text)
        out = {}
        for ch in (data.get('choices') or []):
            try:
                cue_i = int(ch.get('cue'))
            except (TypeError, ValueError):
                continue
            cand = ch.get('candidate')
            out[cue_i] = (int(cand)
                          if isinstance(cand, (int, float)) and not isinstance(cand, bool)
                          else None)
        return out

    def _store_keyframes_and_segments(self, kept):
        """Create one segment + one keyframe per kept frame, linked 1:1 and sharing
        the same timestamp — so each segment is verifiable against its frame."""
        self.segment_ids.sudo().unlink()
        self.keyframe_ids.sudo().unlink()
        Segment = self.env['unifix.video.segment'].sudo()
        Keyframe = self.env['unifix.video.keyframe'].sudo()
        for i, (t, caption, text, jpg) in enumerate(kept):
            disp = self._fmt_ts(t)
            seg = Segment.create({
                'job_id': self.id,
                'sequence': (i + 1) * 10,
                'start_time': t,
                'end_time': t,
                'start_time_display': disp,
                'end_time_display': disp,
                'text': text or caption or '(no transcript at this moment)',
            })
            Keyframe.create({
                'job_id': self.id,
                'segment_id': seg.id,
                'sequence': (i + 1) * 10,
                'timestamp': t,
                'timestamp_display': disp,
                'image': base64.b64encode(jpg),
                'image_filename': f'keyframe_{int(t * 1000):08d}.jpg',
                'reason': caption or False,
            })
        # Video keeps no media; "stored" size = the derived keyframe bytes.
        self.compressed_size = sum(len(jpg) for (_t, _c, _x, jpg) in kept)

    def _store_audio_transcript(self, audio_path):
        """Transcribe a stored audio file and save it on the job (audio jobs)."""
        import mimetypes
        from processor.gemini_client import call_gemini_with_retry
        from processor.parser import parse_ai_json

        ICP = self.env['ir.config_parameter'].sudo()
        model_id = ICP.get_param('unifix.gemini_model', _DEFAULT_MODEL)
        mime = mimetypes.guess_type(self.media_filename or audio_path)[0] or 'audio/wav'
        with open(audio_path, 'rb') as f:
            audio_bytes = f.read()
        prompt = ('Return JSON {"transcript": "<full verbatim transcript of the spoken '
                  'audio, in the language spoken>"}. Transcribe everything said.')
        client = self._gemini_client()
        resp = call_gemini_with_retry(client, model_id, audio_bytes, mime, prompt)
        self._add_usage(*self._usage_from_response(resp))
        data = parse_ai_json(resp.text)
        self.transcript = (data.get('transcript') or '').strip() or False

    # ── Media compression (store the compressed derivative) ──────────────────

    def _compress_media_safe(self, original_path):
        """Best-effort compression — a storage optimisation must never fail an
        otherwise-successful job (P0.2)."""
        try:
            self._compress_and_store_media(original_path)
        except Exception:
            _logger.exception("Media compression failed for job %s (kept original)", self.id)
            if not self.compressed_size:
                self.compressed_size = self.source_size or 0

    def _compress_and_store_media(self, original_path):
        """Compress the stored media (audio→Opus, image→JPEG) and replace
        media_file with the smaller derivative; record the actual stored size."""
        from processor import compression
        ICP = self.env['ir.config_parameter'].sudo()
        if self.media_kind == 'audio':
            data = compression.compress_audio_to_opus(
                original_path, bitrate=ICP.get_param('unifix.audio_opus_bitrate', '24k'))
            ext = '.opus'
        elif self.media_kind == 'image':
            data, ext = compression.compress_image(
                original_path,
                max_dim=int(ICP.get_param('unifix.image_max_dim', '2048')),
                quality=int(ICP.get_param('unifix.image_jpeg_quality', '80')))
        else:
            return
        if not data:
            # Unsupported / failed → keep the original bytes; no reduction.
            self.compressed_size = self.source_size or os.path.getsize(original_path)
            return
        stem = os.path.splitext(self.media_filename or self.source_filename or 'media')[0]
        self.write({'media_file': base64.b64encode(data), 'media_filename': stem + ext})
        # Record the ACTUAL stored size — Odoo may further optimise images on store.
        self.flush_recordset(['media_file'])
        att = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'unifix.workorder.job'),
            ('res_field', '=', 'media_file'),
            ('res_id', '=', self.id)], limit=1, order='id desc')
        self.compressed_size = att.file_size if att else len(data)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _temp_dir(self):
        return self.env['ir.config_parameter'].sudo().get_param(
            'unifix.temp_dir', tempfile.gettempdir())

    def _write_media_to_temp(self):
        """Write the stored media to a temp file the pipeline can read."""
        data = base64.b64decode(self.media_file)
        ext = os.path.splitext(self.media_filename or '')[1].lower()
        if not ext:
            ext = {'audio': '.wav', 'image': '.jpg', 'video': '.mp4'}.get(self.media_kind, '')
        tmp_dir = self._temp_dir()
        os.makedirs(tmp_dir, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix='unifix_', suffix=ext, dir=tmp_dir)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.chmod(path, 0o600)
        return path

    def _compute_sha256(self, path):
        sha = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha.update(chunk)
        return sha.hexdigest()

    @staticmethod
    def _safe_unlink(path):
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                _logger.warning("Could not delete temp file: %s", path)

    def _cleanup_temp_files(self, extra_path=None):
        for path in [self.tmp_path, extra_path]:
            self._safe_unlink(path)
        if self.tmp_path and extra_path is None:
            self.tmp_path = False

    @staticmethod
    def _fmt_ts(seconds):
        m, s = divmod(int(seconds), 60)
        return f'{m:02d}:{s:02d}'

    @api.model
    def _cleanup_orphaned_files(self):
        """Cleanup orphaned temp files (called by cron)."""
        import glob
        import time

        ICP = self.env['ir.config_parameter'].sudo()
        tmp_dir = ICP.get_param('unifix.temp_dir', tempfile.gettempdir())
        age_h = int(ICP.get_param('unifix.cleanup_age_hours', '1'))
        cutoff = time.time() - age_h * 3600

        for pat in ['unifix_*.mp4', 'unifix_*.mkv', 'unifix_*.mov', 'unifix_*.avi',
                    'unifix_*.webm', 'unifix_*.wav', 'unifix_*.mp3', 'unifix_*.m4a',
                    'unifix_*.jpg', 'unifix_*.jpeg', 'unifix_*.png']:
            for path in glob.glob(os.path.join(tmp_dir, pat)):
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.unlink(path)
                except OSError:
                    pass

        stale = self.search([
            ('state', 'in', ['received', 'processing']),
            ('create_date', '<', fields.Datetime.subtract(fields.Datetime.now(), hours=age_h)),
        ])
        for job in stale:
            if job.media_kind == 'video' and (not job.tmp_path or not os.path.exists(job.tmp_path)):
                job.write({'state': 'failed',
                           'error_message': 'Temp file expired before processing completed.'})
