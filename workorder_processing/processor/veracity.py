"""Second-pass veracity check — re-verify Gemini extraction against source audio."""

import json
import logging

from google.genai import types
from .gemini_client import call_gemini_generic
from .exceptions import APICallError

logger = logging.getLogger("work_order_processor")

VERACITY_TRIGGER_STATUSES = {"REVIEW", "FAIL"}
VERACITY_TRIGGER_CONFIDENCE = {"LOW"}

VERACITY_PROMPT = """<system_role>
You are a quality-assurance reviewer for mechanical work order data extraction.
You will be given the original source material alongside a first-pass JSON extraction.
Verify each extracted value against the source, identify errors, and return a corrected
JSON with an audit trail.
</system_role>

<task>
For each field in the extracted JSON:
1. Find evidence in the source material that supports or contradicts the value.
2. Assess whether the value is CORRECT, INCORRECT, or UNCERTAIN.
3. If INCORRECT or UNCERTAIN, provide the corrected or best-available value.
4. Return only fields that require attention.
</task>

<focus_fields>
Focus ONLY on these fields (others are free text and do not need verification):
- worker, company, location, vehicle_equipment, parts_used, start_time, end_time
</focus_fields>

<output_format>
Return a JSON object:
{{
  "verified_fields": {{
    "<field_name>": {{
      "original_value": "<value from first pass>",
      "verdict": "CORRECT" | "INCORRECT" | "UNCERTAIN",
      "corrected_value": "<new value, or same as original if CORRECT>",
      "evidence": "<verbatim quote or description from source>",
      "confidence": "HIGH" | "MEDIUM" | "LOW"
    }}
  }},
  "overall_verdict": "PASS" | "CORRECTIONS_MADE" | "UNCERTAIN",
  "corrections_count": <integer>
}}
Omit fields you have no comment on.
</output_format>

<source_material>
{source_transcript}
</source_material>

<first_pass_extraction>
{first_pass_json}
</first_pass_extraction>

Review the extraction now and return the verification JSON."""


def should_run_veracity(
    validation_result,
    gemini_confidences: dict[str, str],
) -> bool:
    if validation_result.overall_status.value in VERACITY_TRIGGER_STATUSES:
        return True
    if any(v in VERACITY_TRIGGER_CONFIDENCE for v in gemini_confidences.values()):
        return True
    return False


def run_veracity_check(
    client,
    model_id: str,
    audio_bytes: bytes,
    mime_type: str,
    first_pass_json: dict,
    first_pass_transcript: str | None = None,
) -> dict:
    """
    Send original audio + first-pass JSON back to Gemini for verification.
    Returns the parsed veracity result dict, or empty dict on parse failure.
    """
    transcript_text = first_pass_transcript if first_pass_transcript else "[See attached audio]"
    prompt = VERACITY_PROMPT.format(
        source_transcript=transcript_text,
        first_pass_json=json.dumps(first_pass_json, indent=2),
    )

    audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)

    try:
        response = call_gemini_generic(
            client=client,
            model_id=model_id,
            contents=[audio_part, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        return json.loads(response.text)
    except (APICallError, json.JSONDecodeError, Exception) as exc:
        logger.warning("Veracity check failed (non-fatal): %s", exc)
        return {}


def apply_veracity_corrections(
    work_order: dict,
    veracity_result: dict,
) -> tuple[dict, list[str]]:
    """Apply INCORRECT verdicts (and HIGH-confidence UNCERTAIN) to the work order."""
    corrected = work_order.copy()
    corrected_fields: list[str] = []

    for field_name, verification in veracity_result.get("verified_fields", {}).items():
        verdict = verification.get("verdict", "CORRECT")
        corrected_value = verification.get("corrected_value", work_order.get(field_name, ""))

        if verdict == "INCORRECT" and field_name in corrected:
            corrected[field_name] = corrected_value
            corrected_fields.append(field_name)
        elif (verdict == "UNCERTAIN"
              and verification.get("confidence") == "HIGH"
              and field_name in corrected):
            corrected[field_name] = corrected_value
            corrected_fields.append(field_name)

    return corrected, corrected_fields
