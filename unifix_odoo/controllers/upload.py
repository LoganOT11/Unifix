"""HTTP controller for video uploads.

Streams video to a temp file on disk, creates a job record, and returns immediately.
The video never becomes an ir.attachment or binary field — it lives only as a temp file
until the cron worker processes and deletes it.
"""

import os
import uuid
import tempfile
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

ALLOWED_VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.mov', '.avi', '.webm'}
_CHUNK = 8192


class UnifixUploadController(http.Controller):

    @http.route('/unifix/upload', type='http', auth='user',
                methods=['POST'], csrf=True)
    def upload_video(self, video_file=None, **kwargs):
        if not video_file:
            return self._err(400, 'No file provided')

        ext = os.path.splitext(video_file.filename or '')[1].lower()
        if ext not in ALLOWED_VIDEO_EXTENSIONS:
            return self._err(400, f'Unsupported video format: {ext}')

        ICP = request.env['ir.config_parameter'].sudo()
        temp_dir = ICP.get_param('unifix.temp_dir', tempfile.gettempdir())
        max_mb = int(ICP.get_param('unifix.max_video_size_mb', '1024'))

        os.makedirs(temp_dir, exist_ok=True)
        tmp_path = os.path.join(temp_dir, f'unifix_{uuid.uuid4().hex}{ext}')

        # Stream to disk — never buffer the whole file in memory.
        size = 0
        with open(tmp_path, 'wb') as f:
            os.chmod(tmp_path, 0o600)
            while True:
                chunk = video_file.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)

        if size < 10240:
            os.unlink(tmp_path)
            return self._err(400, 'File too small')
        if size > max_mb * 1024 * 1024:
            os.unlink(tmp_path)
            return self._err(400, f'File too large (max {max_mb} MB)')

        job = request.env['unifix.video.job'].create({
            'name': f'Video Job {uuid.uuid4().hex[:8]}',
            'state': 'received',
            'tmp_path': tmp_path,
            'source_size': size,
            'source_filename': video_file.filename,
        })

        _logger.info(
            "Video uploaded: job=%s file=%s size=%d",
            job.id, video_file.filename, size,
        )

        return request.make_json_response({
            'job_id': job.id,
            'state': 'received',
            'message': 'Video uploaded. Processing will begin shortly.',
        })

    def _err(self, code, msg):
        return request.make_json_response({'error': msg}, status=code)
