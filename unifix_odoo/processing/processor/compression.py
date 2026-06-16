"""Framework-free media compression (audio→Opus, image→JPEG).

No Odoo dependency — pure functions, unit-testable. The Odoo worker calls these
to produce the small derivative it stores (originals are not kept in the cloud).
"""

import logging
import os
import subprocess

logger = logging.getLogger("work_order_processor")


def ffmpeg_exe():
    """Resolve an ffmpeg binary: bundled imageio-ffmpeg (Odoo.sh) or system."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def compress_audio_to_opus(input_path, bitrate="24k"):
    """Transcode to mono Opus. Returns bytes, or None on failure."""
    out_path = input_path + ".opus"
    try:
        proc = subprocess.run(
            [ffmpeg_exe(), "-i", input_path, "-vn", "-ac", "1",
             "-c:a", "libopus", "-b:a", str(bitrate), "-y", out_path],
            capture_output=True)
        if proc.returncode != 0:
            logger.warning("Opus compression failed: %s", (proc.stderr or b"")[:300])
            return None
        with open(out_path, "rb") as f:
            return f.read()
    except Exception:
        logger.exception("Opus compression error")
        return None
    finally:
        if os.path.exists(out_path):
            try:
                os.unlink(out_path)
            except OSError:
                pass


def compress_image(input_path, max_dim=2048, quality=80):
    """Resize (cap longest edge) + re-encode JPEG, stripping metadata.

    Returns (jpeg_bytes, '.jpg'), or (None, None) if the input can't be decoded
    (e.g. PDF/HEIC) — caller should then keep the original.
    """
    import cv2
    img = cv2.imread(input_path)
    if img is None:
        return None, None
    h, w = img.shape[:2]
    scale = min(1.0, max_dim / float(max(h, w) or 1))
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)),
                         interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    return (buf.tobytes(), ".jpg") if ok else (None, None)
