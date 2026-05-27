"""Pre-flight audio file validation."""

import os
import magic
from pathlib import Path

from .exceptions import AudioValidationError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_FILE_SIZE_BYTES = 200 * 1024 * 1024    # 200 MB (Gemini File API limit)
MIN_FILE_SIZE_BYTES = 1 * 1024             # 1 KB  — reject empty / stub files
MAX_FILENAME_LENGTH = 255

ALLOWED_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".flac", ".ogg",
    ".opus", ".webm", ".aac", ".pcm", ".mpga",
}

ALLOWED_AUDIO_MIMES = {
    "audio/mpeg",        # .mp3
    "audio/wav",         # .wav
    "audio/x-wav",       # .wav (alternate)
    "audio/mp4",         # .m4a
    "audio/x-m4a",       # .m4a (alternate)
    "audio/flac",        # .flac
    "audio/ogg",         # .ogg / .opus
    "audio/opus",        # .opus
    "audio/webm",        # .webm
    "audio/aac",         # .aac
    "audio/x-aac",       # .aac (alternate)
    "audio/pcm",         # .pcm / raw
    "audio/mpga",        # .mpga
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def validate_audio_file(file_path: str, safe_root: str | None = None) -> dict:
    """
    Run all pre-flight checks on an audio file.

    Returns a dict with: {path, size_bytes, extension, detected_mime}
    Raises AudioValidationError on any failure.
    """
    path = Path(file_path).resolve()

    # 1. Path traversal guard
    root = Path(safe_root).resolve() if safe_root else Path.cwd()
    if not str(path).startswith(str(root)):
        raise AudioValidationError(
            f"Path traversal detected: {file_path} resolves outside {root}"
        )

    # 2. File existence
    if not path.is_file():
        raise AudioValidationError(f"File not found: {file_path}")

    # 3. Filename safety
    if len(path.name) > MAX_FILENAME_LENGTH:
        raise AudioValidationError(
            f"Filename exceeds maximum length ({MAX_FILENAME_LENGTH} chars)."
        )
    dangerous = {"..", "\x00", "/", "\\"}
    if any(c in path.name for c in dangerous):
        raise AudioValidationError("Filename contains unsafe characters.")

    # 4. Extension allowlist
    ext = path.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise AudioValidationError(
            f"Extension '{ext}' is not allowed. "
            f"Allowed: {sorted(ALLOWED_EXTENSIONS)}"
        )

    # 5. File size bounds
    size = path.stat().st_size
    if size < MIN_FILE_SIZE_BYTES:
        raise AudioValidationError(
            f"File too small ({size} bytes). Possibly corrupt or empty."
        )
    if size > MAX_FILE_SIZE_BYTES:
        raise AudioValidationError(
            f"File too large ({size / 1e6:.1f} MB). "
            f"Maximum is {MAX_FILE_SIZE_BYTES / 1e6:.0f} MB."
        )

    # 6. Magic-byte MIME detection
    mime_detector = magic.Magic(mime=True)
    detected_mime = mime_detector.from_file(str(path))
    if detected_mime not in ALLOWED_AUDIO_MIMES:
        raise AudioValidationError(
            f"File content identified as '{detected_mime}' — not a supported "
            f"audio type. The file extension may have been spoofed. "
            f"Allowed types: {sorted(ALLOWED_AUDIO_MIMES)}"
        )

    return {
        "path": str(path),
        "size_bytes": size,
        "extension": ext,
        "detected_mime": detected_mime,
    }
