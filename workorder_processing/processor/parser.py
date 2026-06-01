"""Parse, validate, and wrap Gemini API responses."""

from __future__ import annotations

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

_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"

# Default fuzzy fields used when no config is supplied (audio v1 behaviour).
_DEFAULT_FUZZY_FIELDS = frozenset({
    "worker", "company", "location", "vehicle_equipment", "parts_used"
})


def _load_schema(version: str) -> dict:
    with open(_SCHEMAS_DIR / f"work_order_{version}.json", "r", encoding="utf-8") as fh:
        return json.load(fh)


def parse_ai_json(raw_text: str | None) -> dict:
    """Strip markdown fences and parse JSON from an AI response."""
    if not raw_text:
        raise ResponseParseError(
            "AI returned an empty or null response — the request may have been "
            "blocked by safety filters or the model produced no output."
        )
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


def validate_extracted_data(data: dict, schema_version: str = "v1") -> None:
    """Validate *data* against the named work-order JSON schema."""
    schema = _load_schema(schema_version)
    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as exc:
        raise SchemaValidationError(str(exc)) from exc


def build_response_envelope(
    source_path: str,
    raw_response: genai_types.GenerateContentResponse,
    extracted: dict,
    model_id: str,
    validation_result=None,
    veracity_info: dict | None = None,
    schema_version: str = "1.0",
) -> dict:
    """
    Wrap extracted data with audit metadata (source hash, token usage, etc.).
    """
    source_bytes = Path(source_path).read_bytes()

    usage: dict = {}
    if hasattr(raw_response, "usage_metadata") and raw_response.usage_metadata:
        u = raw_response.usage_metadata
        usage["prompt_tokens"] = getattr(u, "prompt_token_count", None)
        usage["response_tokens"] = getattr(u, "candidates_token_count", None)

    finish_reason = "UNKNOWN"
    if raw_response.candidates:
        finish_reason = str(
            getattr(raw_response.candidates[0], "finish_reason", "UNKNOWN")
        )

    envelope: dict = {
        "schema_version": schema_version,
        "processed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_file": {
            "name": os.path.basename(source_path),
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
            "size_bytes": len(source_bytes),
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


def extract_confidence_markers(
    raw_json: dict,
    fuzzy_fields: frozenset[str] | None = None,
) -> tuple[dict, dict[str, str]]:
    """
    Split Gemini response into clean work-order fields and confidence markers.

    Returns:
        work_order: dict with only the schema fields (no __confidence keys)
        confidences: {"worker": "HIGH", "company": "MEDIUM", ...}
    """
    fields = fuzzy_fields if fuzzy_fields is not None else _DEFAULT_FUZZY_FIELDS
    work_order: dict = {}
    confidences: dict[str, str] = {}

    for key, value in raw_json.items():
        if key.endswith("__confidence"):
            field_name = key.removesuffix("__confidence")
            if not field_name or field_name.endswith("__confidence"):
                logger.warning("Skipping malformed confidence key: %s", key)
                continue
            confidences[field_name] = "MEDIUM" if value is None else str(value).upper()
        else:
            work_order[key] = value

    for f in fields:
        confidences.setdefault(f, "MEDIUM")

    return work_order, confidences
