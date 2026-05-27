"""Parse, validate, and wrap Gemini API responses."""

import datetime
import hashlib
import json
import logging
import os
import re
from pathlib import Path

import jsonschema
from google.genai import types as genai_types

from .exceptions import ResponseParseError, SchemaValidationError

logger = logging.getLogger("work_order_processor")

# ---------------------------------------------------------------------------
# JSON Schema for extracted work-order data
# ---------------------------------------------------------------------------
_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "work_order_v1.json"
with open(_SCHEMA_PATH, "r", encoding="utf-8") as _fh:
    WORK_ORDER_SCHEMA = json.load(_fh)


# ---------------------------------------------------------------------------
# Parse AI response
# ---------------------------------------------------------------------------
def parse_ai_json(raw_text: str) -> dict:
    """
    Strip markdown fences and parse JSON from an AI response.

    Raises ResponseParseError if the text is not valid JSON.
    """
    cleaned = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        raw_text.strip(),
        flags=re.MULTILINE,
    )
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ResponseParseError(
            f"AI returned invalid JSON: {exc}\nRaw text: {raw_text[:500]}"
        ) from exc


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------
def validate_extracted_data(data: dict) -> None:
    """
    Validate *data* against the work-order JSON schema.

    Raises SchemaValidationError with a human-readable message on mismatch.
    """
    try:
        jsonschema.validate(instance=data, schema=WORK_ORDER_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise SchemaValidationError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Response envelope
# ---------------------------------------------------------------------------
def build_response_envelope(
    audio_path: str,
    raw_response: genai_types.GenerateContentResponse,
    extracted: dict,
    model_id: str,
    validation_result=None,
    veracity_info: dict | None = None,
) -> dict:
    """
    Wrap the extracted data with audit metadata:
      - source file hash
      - model & token usage
      - finish reason
    """
    audio_bytes = open(audio_path, "rb").read()

    usage = {}
    if hasattr(raw_response, "usage_metadata") and raw_response.usage_metadata:
        u = raw_response.usage_metadata
        usage["prompt_tokens"] = getattr(u, "prompt_token_count", None)
        usage["response_tokens"] = getattr(u, "candidates_token_count", None)

    finish_reason = "UNKNOWN"
    if raw_response.candidates:
        finish_reason = str(
            getattr(raw_response.candidates[0], "finish_reason", "UNKNOWN")
        )

    envelope = {
        "schema_version": "1.0",
        "processed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_file": {
            "name": os.path.basename(audio_path),
            "sha256": hashlib.sha256(audio_bytes).hexdigest(),
            "size_bytes": len(audio_bytes),
        },
        "model": model_id,
        "usage": usage,
        "finish_reason": finish_reason,
        "extracted_data": extracted,
    }
    if validation_result is not None:
        envelope["validation"] = {
            "overall_status": validation_result.overall_status.value,
            "unresolved_fields": validation_result.unresolved_fields,
            "review_fields": validation_result.review_fields,
        }
    if veracity_info is not None:
        envelope["veracity_pass"] = veracity_info
    return envelope


FUZZY_FIELDS = {"worker", "company", "location", "vehicle_equipment", "parts_used"}


def extract_confidence_markers(raw_json: dict) -> tuple[dict, dict[str, str]]:
    """
    Split Gemini response into clean work order fields and confidence markers.

    Returns:
        work_order: dict with only the 13 schema fields (no __confidence keys)
        confidences: {"worker": "HIGH", "company": "MEDIUM", ...}
    """
    work_order: dict = {}
    confidences: dict[str, str] = {}

    for key, value in raw_json.items():
        if key.endswith("__confidence"):
            field_name = key.replace("__confidence", "")
            if field_name.endswith("__confidence"):
                logger.warning("Skipping malformed confidence key: %s", key)
                continue
            if value is None:
                confidences[field_name] = "MEDIUM"
            else:
                confidences[field_name] = str(value).upper()
        else:
            work_order[key] = value

    for f in FUZZY_FIELDS:
        confidences.setdefault(f, "MEDIUM")

    return work_order, confidences
