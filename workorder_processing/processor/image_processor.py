"""Process handwritten work order images through Gemini Vision."""

import json
import mimetypes
import os
from pathlib import Path

from google.genai import types
from .gemini_client import call_gemini_generic, create_client
from .image_preprocessor import preprocess_image
from .parser import parse_ai_json, extract_confidence_markers
from .prompt_loader import load_prompt

ALLOWED_IMAGE_MIMES = {
    "image/jpeg", "image/jpg", "image/png",
    "image/webp", "image/heic", "image/heif",
    "application/pdf",
}

MAX_IMAGE_SIZE_BYTES = 20 * 1024 * 1024

_PROMPT_REGISTRY = {
    "v1": ("image_extraction_v1", None, "simplified"),
    "v3": ("image_extraction_v3", "v3", "full"),
}

_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif",
    ".pdf": "application/pdf",
}


def detect_image_mime(file_path: str) -> str:
    mime, _ = mimetypes.guess_type(file_path)
    if mime in ALLOWED_IMAGE_MIMES:
        return mime
    ext = Path(file_path).suffix.lower()
    return _EXT_MIME.get(ext, "image/jpeg")


def validate_image_file(file_path: str, safe_root: str | None = None) -> dict:
    path = Path(file_path).resolve()
    root = Path(safe_root).resolve() if safe_root else Path.cwd()
    if not str(path).startswith(str(root)):
        raise ValueError(f"Path traversal detected: {file_path!r} resolves outside {root}")
    if not path.exists():
        raise FileNotFoundError(f"Image file not found: {file_path}")
    size = path.stat().st_size
    if size > MAX_IMAGE_SIZE_BYTES:
        raise ValueError(f"Image too large: {size:,} bytes (max {MAX_IMAGE_SIZE_BYTES:,})")
    mime = detect_image_mime(file_path)
    if mime not in ALLOWED_IMAGE_MIMES:
        raise ValueError(f"Unsupported image type: {mime}")
    return {"path": str(path), "mime_type": mime, "size_bytes": size}


def process_image(
    file_path: str,
    client,
    model_id: str,
    preprocess: bool = True,
    prompt_version: str = "v3",
) -> tuple[dict, dict, object, object]:
    """
    Process a handwritten work order image through Gemini Vision.

    prompt_version selects the prompt+schema pair: "v3" (default, Uni-Fix form)
    or "v1" (legacy generic form).

    Returns:
        (work_order_dict, confidence_markers, raw_response, preprocess_result)
        preprocess_result is None if preprocessing was skipped (PDF or preprocess=False).
    """
    meta = validate_image_file(file_path)
    image_bytes = Path(file_path).read_bytes()
    mime_type = meta["mime_type"]

    preprocess_result = None
    if preprocess and mime_type != "application/pdf":
        preprocess_result = preprocess_image(image_bytes, mime_type)
        image_bytes = preprocess_result.image_bytes
        mime_type = preprocess_result.mime_type

    prompt_name, schema_version, schema_format = _PROMPT_REGISTRY[prompt_version]
    prompt = load_prompt(prompt_name, schema_version=schema_version, schema_format=schema_format)

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
    work_order, confidences = extract_confidence_markers(raw_json)
    return work_order, confidences, response, preprocess_result
