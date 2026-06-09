"""Worker processing tests.

Tests the _process_video() method and helper functions with mocked
external dependencies (ffmpeg, Gemini).
"""

import os
import json
import tempfile
from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase
from odoo import fields


class TestWorkerProcessing(TransactionCase):

    def _make_job(self, **kwargs):
        """Create a minimal job record for testing."""
        vals = {
            'name': 'Test Job',
            'state': 'received',
            'tmp_path': '/tmp/unifix_test_fake.mp4',
        }
        vals.update(kwargs)
        return self.env['unifix.video.job'].create(vals)

    @patch('subprocess.run')
    def test_extract_audio_calls_ffmpeg(self, mock_run):
        """ffmpeg is invoked with correct arguments for audio extraction."""
        mock_run.return_value = MagicMock(returncode=0)
        job = self._make_job()

        # Create a temp file so the method can derive the audio path
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as f:
            tmp_path = f.name

        try:
            audio_path = job._extract_audio(tmp_path)
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            self.assertIn('ffmpeg', args)
            self.assertIn('-vn', args)
            self.assertIn('-acodec', args)
            self.assertIn('pcm_s16le', args)
            self.assertIn('-ar', args)
            self.assertIn('16000', args)
            self.assertIn('-ac', args)
            self.assertIn('1', args)
            self.assertTrue(audio_path.endswith('.wav'))
        finally:
            os.unlink(tmp_path)

    def test_compute_sha256(self):
        """SHA-256 is computed correctly and matches known hash."""
        import hashlib
        content = b'Hello, Unifix!'
        expected = hashlib.sha256(content).hexdigest()

        with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as f:
            f.write(content)
            tmp_path = f.name

        try:
            job = self._make_job()
            result = job._compute_sha256(tmp_path)
            self.assertEqual(result, expected)
        finally:
            os.unlink(tmp_path)

    def test_select_keyframe_timestamps_prefers_curated(self):
        """When Gemini returns curated keyframes, those are used."""
        job = self._make_job()
        result = {
            'keyframes': [
                {'t': 5.0, 'reason': 'valve'},
                {'t': 15.0, 'reason': 'panel'},
            ],
            'segments': [
                {'start': 0.0, 'end': 5.0, 'text': 'a'},
                {'start': 5.0, 'end': 15.0, 'text': 'b'},
            ],
        }
        ts = job._select_keyframe_timestamps(result)
        self.assertEqual(len(ts), 2)
        self.assertAlmostEqual(ts[0][0], 5.0)
        self.assertAlmostEqual(ts[1][0], 15.0)

    def test_select_keyframe_timestamps_falls_back_to_segments(self):
        """Without curated keyframes, segment start times are used."""
        job = self._make_job()
        result = {
            'keyframes': [],
            'segments': [
                {'start': 0.0, 'end': 5.0, 'text': 'a'},
                {'start': 5.0, 'end': 15.0, 'text': 'b'},
                {'start': 15.0, 'end': 20.0, 'text': 'c'},
            ],
        }
        ts = job._select_keyframe_timestamps(result)
        self.assertEqual(len(ts), 3)
        self.assertAlmostEqual(ts[0][0], 0.0)
        self.assertAlmostEqual(ts[1][0], 5.0)
        self.assertAlmostEqual(ts[2][0], 15.0)

    def test_select_keyframe_timestamps_deduplicates_nearby(self):
        """Timestamps within 0.5s are merged."""
        job = self._make_job()
        result = {
            'keyframes': [
                {'t': 5.0, 'reason': 'a'},
                {'t': 5.3, 'reason': 'b'},
                {'t': 6.0, 'reason': 'c'},
            ],
        }
        ts = job._select_keyframe_timestamps(result)
        self.assertEqual(len(ts), 2)
        self.assertAlmostEqual(ts[0][0], 5.0)
        self.assertAlmostEqual(ts[1][0], 6.0)

    def test_select_keyframe_timestamps_caps_at_max(self):
        """Timestamps are capped at the configured max_keyframes."""
        job = self._make_job()
        # Default max is 12, so 20 should be capped
        result = {
            'keyframes': [
                {'t': float(i), 'reason': f'frame {i}'}
                for i in range(20)
            ],
        }
        ts = job._select_keyframe_timestamps(result)
        self.assertEqual(len(ts), 12)

    def test_write_results_persists_fields(self):
        """Work-order fields are written to the job record."""
        job = self._make_job(state='processing')
        result = {
            'transcript': 'Full transcript here',
            'worker': 'John Doe',
            'company': 'Acme Corp',
            'location': 'Site A',
            'reported_problem': 'Broken pipe',
            'diagnosis_cause': 'Corrosion',
            'work_performed': 'Replaced pipe',
            'parts_used': 'Copper pipe 1/2in',
            'start_time': '8:00 AM',
            'end_time': '10:00 AM',
            'total_time_spent': '2h',
            'future_recommendations': 'Check again in 6 months',
            'remaining_tasks': 'None',
        }
        job._write_results(result)

        self.assertEqual(job.transcript, 'Full transcript here')
        self.assertEqual(job.worker_name, 'John Doe')
        self.assertEqual(job.company, 'Acme Corp')
        self.assertEqual(job.location, 'Site A')
        self.assertEqual(job.reported_problem, 'Broken pipe')
        self.assertEqual(job.parts_used, 'Copper pipe 1/2in')
        self.assertEqual(job.total_time_spent, '2h')

    def test_create_segments(self):
        """Segment records are created from Gemini result."""
        job = self._make_job(state='processing')
        result = {
            'segments': [
                {'start': 0.0, 'end': 5.0, 'text': 'Hello', 'confidence': 0.95},
                {'start': 5.0, 'end': 10.0, 'text': 'World', 'confidence': 0.90},
            ],
        }
        job._create_segments(result)

        self.assertEqual(len(job.segment_ids), 2)
        seg1 = job.segment_ids[0]
        self.assertAlmostEqual(seg1.start_time, 0.0)
        self.assertAlmostEqual(seg1.end_time, 5.0)
        self.assertEqual(seg1.text, 'Hello')
        self.assertAlmostEqual(seg1.confidence, 0.95)

    def test_fmt_ts(self):
        """Timestamp formatting produces mm:ss."""
        self.assertEqual(self.env['unifix.video.job']._fmt_ts(0), '00:00')
        self.assertEqual(self.env['unifix.video.job']._fmt_ts(65), '01:05')
        self.assertEqual(self.env['unifix.video.job']._fmt_ts(3661), '61:01')

    def test_action_retry_failed_job(self):
        """Retry resets a failed job to received state."""
        job = self._make_job(state='failed', error_message='test error',
                             tmp_path='/tmp/unifix_test_fake.mp4')
        # The file won't exist, so we need to patch exists check
        with patch('os.path.exists', return_value=True):
            job.action_retry()
        self.assertEqual(job.state, 'received')
        self.assertFalse(job.error_message)

    def test_action_retry_non_failed_raises(self):
        """Retry on a non-failed job raises UserError."""
        job = self._make_job(state='done')
        with self.assertRaises(Exception):
            job.action_retry()
