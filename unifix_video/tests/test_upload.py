"""Upload controller tests.

Tests the /unifix/upload endpoint for correct behavior:
- Returns job_id on valid upload
- Rejects invalid extensions
- Rejects files that are too small or too large
"""

import os
import tempfile

from odoo.tests.common import HttpCase, tagged


@tagged('-at_install', 'post_install')
class TestUploadController(HttpCase):

    def test_upload_returns_job_id(self):
        """Valid video upload returns a job ID and state."""
        # Create a dummy MP4 file header + padding
        video_content = b'\x00' * 0x00 + b'\x00' * 10240  # 10KB
        response = self.url_open(
            '/unifix/upload',
            files={'video_file': ('test.mp4', video_content, 'video/mp4')},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('job_id', data)
        self.assertEqual(data['state'], 'received')

    def test_upload_no_file_returns_400(self):
        """Missing file returns 400."""
        response = self.url_open('/unifix/upload')
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn('error', data)

    def test_upload_rejects_invalid_extension(self):
        """Non-video extensions are rejected."""
        content = b'\x00' * 10240
        response = self.url_open(
            '/unifix/upload',
            files={'video_file': ('test.exe', content, 'application/octet-stream')},
        )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn('Unsupported video format', data.get('error', ''))

    def test_upload_rejects_too_small(self):
        """Files under 10KB are rejected."""
        content = b'\x00' * 100  # 100 bytes
        response = self.url_open(
            '/unifix/upload',
            files={'video_file': ('tiny.mp4', content, 'video/mp4')},
        )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn('too small', data.get('error', '').lower())

    def test_upload_creates_job_record(self):
        """Job record is created in 'received' state with tmp_path set."""
        video_content = b'\x00' * 10240
        response = self.url_open(
            '/unifix/upload',
            files={'video_file': ('record_test.mp4', video_content, 'video/mp4')},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()

        job = self.env['unifix.video.job'].browse(data['job_id'])
        self.assertTrue(job.exists())
        self.assertEqual(job.state, 'received')
        self.assertTrue(job.tmp_path)
        self.assertEqual(job.source_filename, 'record_test.mp4')

        # Cleanup temp file
        if job.tmp_path and os.path.exists(job.tmp_path):
            os.unlink(job.tmp_path)

    def test_upload_sets_source_size(self):
        """source_size matches the uploaded file size."""
        content = b'\x00' * 20480  # 20KB
        response = self.url_open(
            '/unifix/upload',
            files={'video_file': ('sized.mp4', content, 'video/mp4')},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()

        job = self.env['unifix.video.job'].browse(data['job_id'])
        self.assertEqual(job.source_size, 20480)
        self.assertAlmostEqual(job.source_size_mb, 20480 / (1024 * 1024), places=4)

        # Cleanup
        if job.tmp_path and os.path.exists(job.tmp_path):
            os.unlink(job.tmp_path)
