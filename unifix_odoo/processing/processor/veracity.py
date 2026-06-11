"""Second-pass veracity check — re-verify Gemini extraction against source audio."""

import json
import logging

from google.genai import types
from .gemini_client import call_gemini_generic
from .exceptions import APICallError
from .prompt_loader import load_prompt

logger = logging.getLogger("work_order_processor")

VERACITY_TRIGGER_STATUSES = {"REVIEW", "FAIL"}
VERACITY_TRIGGER_CONFIDENCE = {"LOW"}

_VERACITY_PROMPT_TEMPLATE = load_prompt("veracity_v1")


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
    prompt = _VERACITY_PROMPT_TEMPLATE.format(
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
