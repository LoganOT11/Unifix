"""Extract audio from video files using ffmpeg."""

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from .exceptions import VideoExtractionError

logger = logging.getLogger("work_order_processor")

ALLOWED_VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm",
    ".m4v", ".3gp", ".wmv", ".flv",
}

MAX_VIDEO_SIZE_BYTES = 2 * 1024 * 1024 * 1024   # 2 GB
MIN_VIDEO_SIZE_BYTES = 10 * 1024                 # 10 KB


def is_video_extension(path: str) -> bool:
    """Return True if the file extension is a known video format."""
    return Path(path).suffix.lower() in ALLOWED_VIDEO_EXTENSIONS


def extract_audio_from_video(video_path: str) -> tuple[bytes, str, str]:
    """
    Extract audio from a video file using ffmpeg.

    Returns (audio_bytes, mime_type, tmp_path).
    The caller must delete tmp_path when done.
    Raises VideoExtractionError on any failure.
    """
    _check_ffmpeg()

    path = Path(video_path).resolve()
    if not path.is_file():
        raise VideoExtractionError(f"File not found: {video_path}")

    ext = path.suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise VideoExtractionError(
            f"Extension '{ext}' is not a supported video format. "
            f"Supported: {sorted(ALLOWED_VIDEO_EXTENSIONS)}"
        )

    size = path.stat().st_size
    if size < MIN_VIDEO_SIZE_BYTES:
        raise VideoExtractionError(
            f"File too small ({size} bytes) — possibly corrupt or empty."
        )
    if size > MAX_VIDEO_SIZE_BYTES:
        raise VideoExtractionError(
            f"File too large ({size / 1e9:.2f} GB). Maximum is "
            f"{MAX_VIDEO_SIZE_BYTES / 1e9:.0f} GB."
        )

    fd, tmp_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(path),
        "-vn",                   # drop video stream
        "-acodec", "pcm_s16le",  # lossless PCM — accepted by Gemini as audio/wav
        "-ar", "16000",          # 16 kHz — standard for speech
        "-ac", "1",              # mono
        tmp_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        os.unlink(tmp_path)
        raise VideoExtractionError("ffmpeg timed out after 5 minutes.") from exc
    except FileNotFoundError as exc:
        os.unlink(tmp_path)
        raise VideoExtractionError(
            "ffmpeg not found. Install it: conda install -c conda-forge ffmpeg"
        ) from exc

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        os.unlink(tmp_path)
        raise VideoExtractionError(
            f"ffmpeg exited with code {result.returncode}.\n{stderr}"
        )

    try:
        audio_bytes = Path(tmp_path).read_bytes()
    except OSError as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise VideoExtractionError(
            f"Could not read extracted audio file: {exc}"
        ) from exc
    if not audio_bytes:
        os.unlink(tmp_path)
        raise VideoExtractionError(
            "ffmpeg produced an empty audio file — the video may have no audio track."
        )

    logger.info(
        "Extracted audio from video: %s → %s (%.1f MB → %.1f MB)",
        path.name,
        tmp_path,
        size / 1e6,
        len(audio_bytes) / 1e6,
    )
    return audio_bytes, "audio/wav", tmp_path


def _check_ffmpeg() -> None:
    """Raise VideoExtractionError with a helpful message if ffmpeg is missing."""
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise VideoExtractionError(
            "ffmpeg is not installed or not on PATH. "
            "Install it: conda install -c conda-forge ffmpeg"
        ) from exc
