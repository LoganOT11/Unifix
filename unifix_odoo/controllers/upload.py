"""HTTP controller for work-order uploads.

Accepts audio, image, or video. The kind is auto-detected from the extension.

  * audio / image — stored on the job (media_file, filestore attachment).
  * video         — streamed to a temp file and NEVER persisted; the worker
                    extracts audio, processes it, then deletes the temp file.

The job is created in state 'received'; the cron worker picks it up.
"""

import os
import uuid
import base64
import tempfile
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {
    'video': {'.mp4', '.mkv', '.mov', '.avi', '.webm'},
    'audio': {'.mp3', '.wav', '.m4a', '.flac', '.ogg', '.opus', '.aac'},
    'image': {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif', '.pdf'},
}
_MIN_SIZE = 1024            # 1 KB
_CHUNK = 8192


def _detect_kind(ext):
    for kind, exts in ALLOWED_EXTENSIONS.items():
        if ext in exts:
            return kind
    return None


class UnifixUploadController(http.Controller):

    @http.route('/unifix/upload', type='http', auth='user',
                methods=['POST'], csrf=True)
    def upload(self, upload_file=None, video_file=None, **kwargs):
        # Accept 'upload_file' (new) or 'video_file' (legacy).
        upload_file = upload_file or video_file
        if not upload_file:
            return self._err(400, 'No file provided')

        ext = os.path.splitext(upload_file.filename or '')[1].lower()
        kind = _detect_kind(ext)
        if not kind:
            return self._err(400, f'Unsupported file format: {ext}')

        ICP = request.env['ir.config_parameter'].sudo()
        temp_dir = ICP.get_param('unifix.temp_dir', tempfile.gettempdir())
        max_mb = int(ICP.get_param({
            'video': 'unifix.max_video_size_mb',
            'audio': 'unifix.max_audio_size_mb',
            'image': 'unifix.max_image_size_mb',
        }[kind], {'video': '1024', 'audio': '200', 'image': '25'}[kind]))

        os.makedirs(temp_dir, exist_ok=True)
        tmp_path = os.path.join(temp_dir, f'unifix_{uuid.uuid4().hex}{ext}')

        # Stream to disk — never buffer the whole upload in memory.
        size = 0
        with open(tmp_path, 'wb') as f:
            os.chmod(tmp_path, 0o600)
            while True:
                chunk = upload_file.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)

        if size < _MIN_SIZE:
            os.unlink(tmp_path)
            return self._err(400, 'File too small')
        if size > max_mb * 1024 * 1024:
            os.unlink(tmp_path)
            return self._err(400, f'File too large (max {max_mb} MB)')

        vals = {
            'name': f'{kind.capitalize()} Job {uuid.uuid4().hex[:8]}',
            'media_kind': kind,
            'state': 'received',
            'source_size': size,
            'source_filename': upload_file.filename,
        }

        if kind == 'video':
            # Ephemeral: keep only the temp file path; never store the video.
            vals['tmp_path'] = tmp_path
        else:
            # Persist audio/image media on the job, then drop the temp file.
            with open(tmp_path, 'rb') as f:
                vals['media_file'] = base64.b64encode(f.read())
            vals['media_filename'] = upload_file.filename
            os.unlink(tmp_path)

        job = request.env['unifix.workorder.job'].create(vals)

        _logger.info("Upload: job=%s kind=%s file=%s size=%d",
                     job.id, kind, upload_file.filename, size)

        return request.make_json_response({
            'job_id': job.id,
            'media_kind': kind,
            'state': 'received',
            'message': 'Uploaded. Processing will begin shortly.',
        })

    def _err(self, code, msg):
        return request.make_json_response({'error': msg}, status=code)
