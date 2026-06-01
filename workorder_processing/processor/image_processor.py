"""Process handwritten work order images through Gemini Vision."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from google.genai import types
from .exceptions import InputValidationError
from .gemini_client import call_gemini_generic
from .image_preprocessor import preprocess_image
from .parser import parse_ai_json, extract_confidence_markers

ALLOWED_IMAGE_MIMES = {
    "image/jpeg", "image/jpg", "image/png",
    "image/webp", "image/heic", "image/heif",
    "application/pdf",
}

MAX_IMAGE_SIZE_BYTES = 20 * 1024 * 1024

_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif",
    ".pdf": "application/pdf",
}


def detect_image_mime(file_path: str) -> str | None:
    mime, _ = mimetypes.guess_type(file_path)
    if mime in ALLOWED_IMAGE_MIMES:
        return mime
    return _EXT_MIME.get(Path(file_path).suffix.lower())


def validate_image_file(file_path: str, safe_root: str | None = None) -> dict:
    path = Path(file_path).resolve()
    root = Path(safe_root).resolve() if safe_root else Path.cwd()
    if not path.is_relative_to(root):
        raise InputValidationError(
            f"Path traversal detected: {file_path!r} resolves outside {root}"
        )
    if not path.is_file():
        raise InputValidationError(f"Image file not found: {file_path}")
    size = path.stat().st_size
    if size > MAX_IMAGE_SIZE_BYTES:
        raise InputValidationError(
            f"Image too large: {size:,} bytes (max {MAX_IMAGE_SIZE_BYTES:,})"
        )
    mime = detect_image_mime(file_path)
    if mime is None:
        raise InputValidationError(
            f"Unsupported image type: '{Path(file_path).suffix or 'unknown'}'. "
            f"Supported: {sorted(ALLOWED_IMAGE_MIMES)}"
        )
    return {"path": str(path), "mime_type": mime, "size_bytes": size}


def process_image(
    file_path: str,
    client,
    model_id: str,
    prompt: str,
    preprocess: bool = True,
    fuzzy_fields: frozenset[str] | None = None,
) -> tuple[dict, dict, object, object]:
    """
    Process a handwritten work order image through Gemini Vision.

    *prompt* is the fully-rendered extraction prompt string.
    *fuzzy_fields* are passed through to extract_confidence_markers.

    Returns:
        (work_order_dict, confidence_markers, raw_response, preprocess_result)
        preprocess_result is None if preprocessing was skipped.
    """
    meta = validate_image_file(file_path)
    image_bytes = Path(file_path).read_bytes()
    mime_type = meta["mime_type"]

    preprocess_result = None
    if preprocess and mime_type != "application/pdf":
        preprocess_result = preprocess_image(image_bytes, mime_type)
        image_bytes = preprocess_result.image_bytes
        mime_type = preprocess_result.mime_type

    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    response = call_gemini_generic(
        client=client,
        model_id=model_id,
        contents=[image_part, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )

    raw_json = parse_ai_json(response.text)
    work_order, confidences = extract_confidence_markers(raw_json, fuzzy_fields)
    return work_order, confidences, response, preprocess_result
