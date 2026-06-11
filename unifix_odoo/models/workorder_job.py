"""Work-order processing job model.

Handles three media kinds through one lifecycle
(draft → received → processing → done | failed | cancelled):

  * audio  — stored; sent to the audio pipeline (schema v1).
  * image  — stored; sent to the image pipeline (schema v3, nested tasks).
  * video  — NOT stored (too large); audio is extracted and run through the
             audio pipeline, then the video temp file is deleted. Keyframe
             extraction/storage from video is a planned future feature — the
             segment/keyframe models and helpers below are kept dormant for it.

Extraction is delegated to the vendored engine under ``unifix_odoo/processing``
(``get_pipeline(mode, cfg, provider).run(ctx)``); this model only orchestrates
I/O, auth, provider selection, and maps the returned envelope onto fields.
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

    # Temp working file (always for video; transient for audio/image processing)
    tmp_path = fields.Char()
    source_filename = fields.Char()
    source_size = fields.Integer()
    source_size_mb = fields.Float(compute='_compute_size_mb')
    source_hash = fields.Char(index=True)

    # ── Extraction output ────────────────────────────────────────────────────
    extracted_json = fields.Text(help="Full extracted_data envelope (any kind)")
    validation_status = fields.Char(help="Overall validation status from the engine")

    # Work-order fields (audio/video schema v1; image reuses worker/company/location)
    worker_name = fields.Char()
    company = fields.Char()
    client = fields.Char(help="Image work orders: client/account name")
    location = fields.Char()
    wo_date = fields.Char(help="Image work orders: date as written")
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

    def action_process(self):
        """Manually run processing now (synchronously)."""
        self.ensure_one()
        self._process()

    def action_retry(self):
        """Re-queue a failed job. Audio/image re-run from stored media; video
        needs its temp file to still exist."""
        self.ensure_one()
        if self.state != 'failed':
            raise UserError("Only failed jobs can be retried.")
        if self.media_kind == 'video' and (not self.tmp_path or not os.path.exists(self.tmp_path)):
            raise UserError("The original video has been cleaned up. Please upload again.")
        if self.media_kind != 'video' and not self.media_file:
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
            self.write({'state': 'processing'})

            if self.media_kind == 'video':
                tmp_path = self.tmp_path
                if not tmp_path or not os.path.exists(tmp_path):
                    raise FileNotFoundError(f"Temp video file not found: {tmp_path}")
            else:
                if not self.media_file:
                    raise UserError("No media stored on this job.")
                tmp_path = self._write_media_to_temp()
                tmp_is_scratch = True

            self.source_hash = self._compute_sha256(tmp_path)

            envelope = self._run_pipeline(tmp_path)
            self._apply_envelope(envelope)

            # Video is never stored: delete its temp file now that we're done.
            if self.media_kind == 'video':
                self._safe_unlink(self.tmp_path)
                self.tmp_path = False

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
        model_id = ICP.get_param('unifix.gemini_model', 'gemini-2.5-pro')

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
        return get_pipeline(mode, cfg, provider).run(ctx)

    def _apply_envelope(self, envelope):
        """Map the engine's returned envelope onto this record's fields."""
        ed = envelope.get('extracted_data', {}) or {}
        self.extracted_json = json.dumps(ed, ensure_ascii=False, indent=2)
        val = envelope.get('validation')
        self.validation_status = (val or {}).get('overall_status') if isinstance(val, dict) else False
        if self.media_kind == 'image':
            self._apply_image(ed)
        else:
            self._apply_workorder_flat(ed)

    def _apply_workorder_flat(self, ed):
        """Audio/video — flat schema-v1 fields."""
        self.write({
            'worker_name': ed.get('worker', ''),
            'company': ed.get('company', ''),
            'location': ed.get('location', ''),
            'vehicle_equipment': ed.get('vehicle_equipment', ''),
            'reported_problem': ed.get('reported_problem', ''),
            'diagnosis_cause': ed.get('diagnosis_cause', ''),
            'work_performed': ed.get('work_performed', ''),
            'parts_used': ed.get('parts_used', ''),
            'start_time': ed.get('start_time', ''),
            'end_time': ed.get('end_time', ''),
            'total_time_spent': ed.get('total_time_spent', ''),
            'future_recommendations': ed.get('future_recommendations', ''),
            'remaining_tasks': ed.get('remaining_tasks', ''),
        })

    def _apply_image(self, ed):
        """Image — nested schema-v3: header + tasks child rows + JSON extras."""
        self.write({
            'worker_name': ed.get('worker') or '',
            'company': ed.get('company') or '',
            'client': ed.get('client') or '',
            'location': ed.get('location') or '',
            'wo_date': ed.get('date') or '',
            'future_recommendations': ed.get('future_recommendations') or '',
            'remaining_tasks': ed.get('remaining_tasks') or '',
            'extra_data_json': json.dumps({
                'travel': ed.get('travel'),
                'expenses': ed.get('expenses'),
                'labour_summary': ed.get('labour_summary'),
            }, ensure_ascii=False, indent=2),
        })
        self.task_ids.unlink()
        Task = self.env['unifix.workorder.task']
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

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _temp_dir(self):
        return self.env['ir.config_parameter'].sudo().get_param(
            'unifix.temp_dir', tempfile.gettempdir())

    def _write_media_to_temp(self):
        """Write the stored media to a temp file the pipeline can read."""
        data = base64.b64decode(self.media_file)
        ext = os.path.splitext(self.media_filename or '')[1].lower()
        if not ext:
            ext = {'audio': '.wav', 'image': '.jpg'}.get(self.media_kind, '')
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
