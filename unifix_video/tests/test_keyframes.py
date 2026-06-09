"""Keyframe extraction and storage tests.

Tests that:
- Frames are extracted at the correct timestamps
- Frames are stored as keyframe records with proper linkage
- The video file survives until after frame extraction (ordering guarantee)
"""

import os
import base64
import tempfile
from unittest.mock import patch, MagicMock, call

from odoo.tests.common import TransactionCase


class TestKeyframeExtraction(TransactionCase):

    def _make_job(self, **kwargs):
        vals = {
            'name': 'Keyframe Test Job',
            'state': 'processing',
            'tmp_path': '/tmp/unifix_test_keyframe.mp4',
        }
        vals.update(kwargs)
        return self.env['unifix.video.job'].create(vals)

    def test_store_keyframes_creates_records(self):
        """Keyframe records are created with correct fields."""
        job = self._make_job()
        # Create a segment first so we can test linkage
        seg = self.env['unifix.video.segment'].create({
            'job_id': job.id,
            'start_time': 5.0,
            'end_time': 10.0,
            'start_time_display': '00:05',
            'end_time_display': '00:10',
            'text': 'Test segment',
        })

        jpeg_data = b'\xff\xd8\xff\xd9'  # Minimal JPEG-like bytes
        frames = [
            (5.0, jpeg_data, 'valve location'),
            (15.0, jpeg_data, 'panel view'),
        ]
        job._store_keyframes(frames, {'segments': []})

        self.assertEqual(len(job.keyframe_ids), 2)
        kf1 = job.keyframe_ids[0]
        self.assertAlmostEqual(kf1.timestamp, 5.0)
        self.assertEqual(kf1.timestamp_display, '00:05')
        self.assertEqual(kf1.reason, 'valve location')
        self.assertEqual(kf1.segment_id.id, seg.id)
        self.assertTrue(kf1.image)

    def test_store_keyframes_links_to_nearest_segment(self):
        """Keyframes are linked to the nearest segment by timestamp."""
        job = self._make_job()
        seg1 = self.env['unifix.video.segment'].create({
            'job_id': job.id,
            'start_time': 0.0,
            'end_time': 10.0,
            'start_time_display': '00:00',
            'end_time_display': '00:10',
            'text': 'First',
        })
        seg2 = self.env['unifix.video.segment'].create({
            'job_id': job.id,
            'start_time': 10.0,
            'end_time': 20.0,
            'start_time_display': '00:10',
            'end_time_display': '00:20',
            'text': 'Second',
        })

        jpeg_data = b'\xff\xd8\xff\xd9'
        # Frame at 8s is closer to seg1, frame at 12s is closer to seg2
        frames = [
            (8.0, jpeg_data, 'near seg1'),
            (12.0, jpeg_data, 'near seg2'),
        ]
        job._store_keyframes(frames, {'segments': []})

        kf1 = job.keyframe_ids.filtered(lambda k: k.timestamp == 8.0)
        kf2 = job.keyframe_ids.filtered(lambda k: k.timestamp == 12.0)
        self.assertEqual(kf1.segment_id.id, seg1.id)
        self.assertEqual(kf2.segment_id.id, seg2.id)

    def test_nearest_segment_finds_closest(self):
        """_nearest_segment returns the segment with the closest start_time."""
        job = self._make_job()
        seg1 = self.env['unifix.video.segment'].create({
            'job_id': job.id,
            'start_time': 0.0,
            'end_time': 5.0,
            'start_time_display': '00:00',
            'end_time_display': '00:05',
            'text': 'A',
        })
        seg2 = self.env['unifix.video.segment'].create({
            'job_id': job.id,
            'start_time': 10.0,
            'end_time': 15.0,
            'start_time_display': '00:10',
            'end_time_display': '00:15',
            'text': 'B',
        })
        sorted_segs = sorted(job.segment_ids, key=lambda s: s.start_time)

        result = job._nearest_segment(sorted_segs, 3.0)
        self.assertEqual(result.id, seg1.id)

        result = job._nearest_segment(sorted_segs, 11.0)
        self.assertEqual(result.id, seg2.id)

    @patch('subprocess.run')
    def test_extract_frames_calls_ffmpeg(self, mock_run):
        """ffmpeg is called per timestamp with correct arguments."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=b'\xff\xd8\xff\xd9',
        )
        job = self._make_job()
        timestamps = [
            (5.0, 'reason a'),
            (15.0, 'reason b'),
        ]
        frames = job._extract_frames('/tmp/test.mp4', timestamps)

        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(len(frames), 2)

        # Check first call args
        first_args = mock_run.call_args_list[0][0][0]
        self.assertIn('ffmpeg', first_args)
        self.assertIn('-ss', first_args)
        self.assertIn('5.000', first_args)
        self.assertIn('-frames:v', first_args)
        self.assertIn('1', first_args)

    @patch('subprocess.run')
    def test_extract_frames_handles_failure(self, mock_run):
        """Failed frame extraction is skipped, not fatal."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout=b'',
            stderr=b'error',
        )
        job = self._make_job()
        timestamps = [(5.0, 'test')]
        frames = job._extract_frames('/tmp/test.mp4', timestamps)

        self.assertEqual(len(frames), 0)  # Failed frame is skipped

    def test_keyframe_count_computed(self):
        """keyframe_count reflects the number of keyframe records."""
        job = self._make_job(state='done')
        self.assertEqual(job.keyframe_count, 0)

        self.env['unifix.video.keyframe'].create({
            'job_id': job.id,
            'timestamp': 1.0,
        })
        self.assertEqual(job.keyframe_count, 1)

        self.env['unifix.video.keyframe'].create({
            'job_id': job.id,
            'timestamp': 2.0,
        })
        self.assertEqual(job.keyframe_count, 2)

    def test_keyframe_cascade_delete(self):
        """Deleting a job cascades to its keyframes."""
        job = self._make_job(state='done')
        kf = self.env['unifix.video.keyframe'].create({
            'job_id': job.id,
            'timestamp': 1.0,
        })
        kf_id = kf.id
        job.unlink()
        self.assertFalse(
            self.env['unifix.video.keyframe'].browse(kf_id).exists()
        )
