"""Unit tests for the unifix.workorder.job model.

These exercise the Odoo-side orchestration (envelope mapping, media-storage
semantics, provider selection, helpers) WITHOUT calling Gemini — the pipeline
is mocked. Live extraction is verified separately through odoo shell.
"""

import base64
from unittest.mock import patch

from odoo.tests.common import TransactionCase
from odoo import fields

from odoo.addons.unifix_odoo.controllers.upload import _detect_kind


_AUDIO_ENVELOPE = {
    "extracted_data": {
        "worker": "Jane Doe", "company": "ACME", "location": "Milton",
        "vehicle_equipment": "water filter", "reported_problem": "leak",
        "diagnosis_cause": "corroded valve", "work_performed": "replaced valve",
        "parts_used": "57821", "start_time": "09:00", "end_time": "12:00",
        "total_time_spent": "3 hours", "future_recommendations": "replace unit",
        "remaining_tasks": "none",
    },
    "validation": {"overall_status": "PASS"},
}

_IMAGE_ENVELOPE = {
    "extracted_data": {
        "company": "Uni-Fix", "client": "OREZONE", "location": "La Sarre",
        "date": "2016/04/14", "worker": "Francis Blais",
        "travel": {"legs": [], "total_km": 10},
        "expenses": {"total": "5"},
        "labour_summary": {"start": "5h", "end": "7h", "total_hours": "14h"},
        "tasks": [
            {"unit": "533", "time": {"start": "8", "end": "18", "total": "10:00"},
             "description": "Maintenance", "work_order_ref": "BT-1"},
            {"unit": "Travel", "time": {"start": "", "end": "", "total": "1:00"},
             "description": "Travel", "work_order_ref": ""},
        ],
        "future_recommendations": "n/a", "remaining_tasks": "n/a",
    },
    "validation": None,
}


class TestWorkorderJob(TransactionCase):

    def _job(self, **vals):
        base = {'name': 'T', 'state': 'received'}
        base.update(vals)
        return self.env['unifix.workorder.job'].create(base)

    # ── Envelope mapping ─────────────────────────────────────────────────────

    def test_audio_envelope_maps_flat_fields(self):
        job = self._job(media_kind='audio')
        with patch.object(type(job), '_run_pipeline', return_value=_AUDIO_ENVELOPE), \
             patch.object(type(job), '_write_media_to_temp', return_value='/tmp/x.wav'), \
             patch.object(type(job), '_compute_sha256', return_value='deadbeef'), \
             patch('os.path.exists', return_value=True):
            job.media_file = base64.b64encode(b'x' * 2048)
            job._process()
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.location, 'Milton')
        self.assertEqual(job.vehicle_equipment, 'water filter')
        self.assertEqual(job.total_time_spent, '3 hours')
        self.assertEqual(job.validation_status, 'PASS')

    def test_image_envelope_maps_tasks_and_header(self):
        job = self._job(media_kind='image')
        with patch.object(type(job), '_run_pipeline', return_value=_IMAGE_ENVELOPE), \
             patch.object(type(job), '_write_media_to_temp', return_value='/tmp/x.jpg'), \
             patch.object(type(job), '_compute_sha256', return_value='deadbeef'), \
             patch('os.path.exists', return_value=True):
            job.media_file = base64.b64encode(b'x' * 2048)
            job._process()
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.worker_name, 'Francis Blais')
        self.assertEqual(job.client, 'OREZONE')
        self.assertEqual(job.wo_date, '2016/04/14')
        self.assertEqual(len(job.task_ids), 2)
        self.assertEqual(job.task_ids[0].unit, '533')
        self.assertEqual(job.task_ids[0].time_total, '10:00')
        self.assertIn('labour_summary', job.extra_data_json)

    # ── Media-storage semantics ──────────────────────────────────────────────

    def test_audio_and_image_can_store_media(self):
        for kind, ext in (('audio', '.wav'), ('image', '.jpg')):
            job = self._job(media_kind=kind,
                            media_file=base64.b64encode(b'x' * 2048),
                            media_filename=f'f{ext}')
            self.assertTrue(job.media_file)

    def test_media_file_is_filestore_attachment(self):
        """media_file must be an attachment Binary (filestore, not the DB row)."""
        field = self.env['unifix.workorder.job']._fields['media_file']
        self.assertIsInstance(field, fields.Binary)
        self.assertTrue(field.attachment, "media_file must use attachment storage")

    def test_video_does_not_store_media(self):
        """Video jobs carry only a temp path; media_file stays empty."""
        job = self._job(media_kind='video', tmp_path='/tmp/unifix_fake.mp4')
        self.assertFalse(job.media_file)

    # ── Provider selection ───────────────────────────────────────────────────

    def test_provider_defaults_to_memory(self):
        self.env['ir.config_parameter'].sudo().set_param('unifix.reference_provider', 'memory')
        job = self._job(media_kind='audio')
        self.assertEqual(job._reference_provider().__class__.__name__, 'InMemoryProvider')

    def test_provider_odoo_when_configured(self):
        self.env['ir.config_parameter'].sudo().set_param('unifix.reference_provider', 'odoo')
        job = self._job(media_kind='audio')
        self.assertEqual(job._reference_provider().__class__.__name__, 'OdooReferenceDataProvider')

    # ── Helpers / controller ─────────────────────────────────────────────────

    def test_fmt_ts(self):
        Job = self.env['unifix.workorder.job']
        self.assertEqual(Job._fmt_ts(0), '00:00')
        self.assertEqual(Job._fmt_ts(65), '01:05')

    def test_detect_kind(self):
        self.assertEqual(_detect_kind('.wav'), 'audio')
        self.assertEqual(_detect_kind('.jpg'), 'image')
        self.assertEqual(_detect_kind('.mp4'), 'video')
        self.assertIsNone(_detect_kind('.exe'))
