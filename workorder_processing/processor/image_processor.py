"""Process handwritten work order images through Gemini Vision."""

import json
import mimetypes
import os
from pathlib import Path

from google.genai import types
from .gemini_client import call_gemini_generic, create_client
from .image_preprocessor import preprocess_image
from .parser import parse_ai_json, extract_confidence_markers

ALLOWED_IMAGE_MIMES = {
    "image/jpeg", "image/jpg", "image/png",
    "image/webp", "image/heic", "image/heif",
    "application/pdf",
}

MAX_IMAGE_SIZE_BYTES = 20 * 1024 * 1024

IMAGE_EXTRACTION_PROMPT = """
<system_role>
You are a precise data extraction engine for handwritten mechanical and maintenance
work order forms. Extract structured data from the image with high accuracy.
</system_role>

<image_reading_guidance>
Read ALL text visible in the image, including headers, labels, and pre-printed fields.
If handwriting is unclear, make your best attempt and mark confidence as LOW.
Common form sections and their schema mappings:
  "Technician" / "Mechanic" / "Performed by"  → worker
  "Company" / "Contractor" / "Employer"        → company
  "Location" / "Site" / "Workshop"             → location
  "Unit #" / "Asset" / "Vehicle" / "Equipment" → vehicle_equipment
  "Fault" / "Complaint" / "Problem reported"   → reported_problem
  "Cause" / "Diagnosis" / "Root cause"         → diagnosis_cause
  "Work done" / "Actions taken" / "Repair"     → work_performed
  "Parts" / "Materials used"                   → parts_used
  "Start" / "Time in" / "Begin"                → start_time
  "Finish" / "Time out" / "End"                → end_time
  "Total time" / "Hours"                       → total_time_spent
  "Recommendations" / "Notes" / "Follow-up"    → future_recommendations
  "Deferred" / "Outstanding" / "Remaining"     → remaining_tasks
</image_reading_guidance>

<extraction_rules>
1. Extract only what is legibly written. Use "" for blank or illegible fields.
2. Preserve original spelling for names, part numbers, and equipment tags.
3. Return ONLY the JSON object — no preamble, no markdown fences, no explanation.
</extraction_rules>

<confidence_markers>
For worker, company, location, vehicle_equipment, and parts_used, append:
  "field_name__confidence": "HIGH" | "MEDIUM" | "LOW"
HIGH = clearly printed or legibly handwritten
MEDIUM = mostly legible but some characters uncertain
LOW = significant portions illegible or inferred
</confidence_markers>
"""

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
) -> tuple[dict, dict, object, object]:
    """
    Process a handwritten work order image through Gemini Vision.

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

    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

    response = call_gemini_generic(
        client=client,
        model_id=model_id,
        contents=[image_part, IMAGE_EXTRACTION_PROMPT],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )

    raw_json = parse_ai_json(response.text)
    work_order, confidences = extract_confidence_markers(raw_json)
    return work_order, confidences, response, preprocess_result
