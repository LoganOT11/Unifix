"""Gemini API client setup and resilient call wrapper."""

import json
import os
import re
import time
import logging
from pathlib import Path

from google import genai
from google.genai import types
from google.genai.types import HttpOptions

from .exceptions import APICallError

logger = logging.getLogger("work_order_processor")

# ---------------------------------------------------------------------------
# API key helpers
# ---------------------------------------------------------------------------
def validate_api_key(key: str) -> None:
    """Basic format check — accepts legacy AIza... keys and new AQ.Ab8... keys."""
    legacy = re.match(r"^AIza[0-9A-Za-z_\-]{35}$", key)
    new_format = re.match(r"^AQ\.[0-9A-Za-z_\-]{2}[0-9A-Za-z_\-]+$", key)
    if not (legacy or new_format):
        raise RuntimeError(
            "GOOGLE_API_KEY format looks invalid. "
            "Expected legacy format (AIza + 35 chars) or new format (AQ. prefix)."
        )


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------
def create_client(api_key: str, timeout_ms: int = 600_000) -> genai.Client:
    """Build a genai.Client with pinned timeout (ms) and validated key."""
    validate_api_key(api_key)
    return genai.Client(
        api_key=api_key,
        http_options=HttpOptions(api_version="v1", timeout=timeout_ms),
    )


# ---------------------------------------------------------------------------
# Prompt — built from the canonical JSON schema file
# ---------------------------------------------------------------------------
_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "work_order_v1.json"


def _build_schema_prompt() -> str:
    """Read the JSON schema file and wrap it in the extraction instructions."""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        schema = json.load(fh)

    # Render a clean { field: "type" } block from the schema properties
    schema_block = json.dumps(
        {name: prop.get("type", "string") for name, prop in schema["properties"].items()},
        indent=2,
    )

    return f"""System Role: You are a specialized data extraction assistant for mechanical and maintenance logs. 
Your task is to analyze the provided transcript and extract specific technical details into a structured JSON format.

Instructions:
1. Extract the specific values listed in the schema below.
2. Calculate the total_time_spent by finding the difference between the start_time and end_time.
3. Handle Nulls: If a piece of information is not mentioned, use an empty string "".
4. Formatting: Ensure the output is valid JSON.

JSON Schema:
{schema_block}"""


SCHEMA_PROMPT = _build_schema_prompt()


def _build_schema_prompt_v2() -> str:
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        schema = json.load(fh)
    schema_block = json.dumps(
        {name: prop.get("type", "string") for name, prop in schema["properties"].items()},
        indent=2,
    )
    return f"""<system_role>
You are a precise data extraction engine for mechanical and maintenance work order logs.
You receive audio transcripts or documents from field technicians and extract structured
records. Accuracy is critical — every extracted value will be validated against a master
database. Do not infer, guess, or embellish values that are not clearly stated.
</system_role>

<field_definitions>
Extract ONLY the following fields:

- vehicle_equipment : The asset tag, equipment ID, or full name of the vehicle or machine
  being serviced. Examples: "TRK-001", "CAT 320 Excavator", "GEN-005". If multiple assets
  are mentioned, capture the primary one being worked on.
- reported_problem : The fault or complaint as described by the operator or requester.
  Use their exact words where possible. Do not diagnose — only report.
- diagnosis_cause : The root cause identified by the technician after inspection.
  If no diagnosis is mentioned, use "".
- work_performed : A concise description of the repair or maintenance actions taken.
  Use active voice: "Replaced X", "Adjusted Y", "Flushed Z".
- parts_used : Comma-separated list of parts, consumables, or materials used.
  Include part numbers if stated. If no parts were used, use "".
- start_time : The time the technician began work. If AM/PM is stated, convert to 24-hour.
  Format: HH:MM. If unknown, use "".
- end_time : The time work was completed. Same format as start_time.
- total_time_spent : Do NOT calculate this. Extract it verbatim if stated. If not, use "".
- future_recommendations : Any follow-up actions or warnings the technician recommends.
  If none, use "".
- remaining_tasks : Work deferred to a future visit. If none, use "".
- worker : The full name of the technician. If only partial name given, capture what is
  available. If unknown, use "".
- company : The name of the maintenance company. Capture abbreviations if that is all
  stated. If unknown, use "".
- location : Where the work was performed. As specific as stated. If unknown, use "".
</field_definitions>

<extraction_rules>
1. ONLY extract values explicitly stated or clearly implied. Never invent or estimate.
2. If a field is ambiguous or unclear, use "" rather than guessing.
3. Preserve the speaker's terminology — the downstream system will normalise values.
4. For times: extract exactly what is stated. Do not convert unless unambiguous.
5. Return ONLY the JSON object — no preamble, no markdown fences, no explanation.
</extraction_rules>

<confidence_markers>
For each of these five fields, also emit a confidence suffix:
  vehicle_equipment, worker, company, location, parts_used
Format: "field_name__confidence": "HIGH" | "MEDIUM" | "LOW"
- HIGH   = value was stated explicitly and unambiguously
- MEDIUM = value was implied, partially stated, or required minor inference
- LOW    = best guess; not confident it is correct
</confidence_markers>

<output_schema>
{schema_block}
</output_schema>

<few_shot_examples>
Example 1 — Clean input (all fields explicit):
  Transcript: "This is James Hartwell from Hartwell Industrial Services. Date is the 14th.
  I'm out at Main Workshop Bay 1 working on Truck TRK-001. The operator reported the engine
  warning light was on and the oil pressure was low. Diagnosis: clogged oil filter and worn
  serpentine belt. I replaced the heavy-duty oil filter and serpentine belt. Parts used:
  OIL-FLT-HVY and BLT-SERP. Started at 8am, finished at 10:30. Total time two and a half
  hours. No future recommendations. No remaining tasks."
  Expected output: {{
    "vehicle_equipment": "TRK-001",
    "reported_problem": "Engine warning light on, low oil pressure",
    "diagnosis_cause": "Clogged oil filter and worn serpentine belt",
    "work_performed": "Replaced heavy-duty oil filter and serpentine belt",
    "parts_used": "OIL-FLT-HVY, BLT-SERP",
    "start_time": "08:00",
    "end_time": "10:30",
    "total_time_spent": "2h 30m",
    "future_recommendations": "",
    "remaining_tasks": "",
    "worker": "James Hartwell",
    "company": "Hartwell Industrial Services",
    "location": "Main Workshop — Bay 1",
    "worker__confidence": "HIGH",
    "company__confidence": "HIGH",
    "location__confidence": "HIGH",
    "vehicle_equipment__confidence": "HIGH",
    "parts_used__confidence": "HIGH"
  }}

Example 2 — Ambiguous input (partial names, inferred values):
  Transcript: "Tom here, just finished up on the big excavator up at the northern yard.
  Had a hydraulic leak — turned out to be a busted hose fitting. Swapped it out.
  Can't remember the exact part number off the top of my head. Started around nine,
  done by half eleven. Uh, no other issues."
  Expected output: {{
    "vehicle_equipment": "CAT 320 Excavator",
    "reported_problem": "Hydraulic leak",
    "diagnosis_cause": "Burst hose fitting",
    "work_performed": "Replaced hydraulic hose fitting",
    "parts_used": "",
    "start_time": "09:00",
    "end_time": "11:30",
    "total_time_spent": "",
    "future_recommendations": "",
    "remaining_tasks": "",
    "worker": "Tom",
    "company": "",
    "location": "Northern Depot — Yard B",
    "worker__confidence": "LOW",
    "company__confidence": "LOW",
    "location__confidence": "MEDIUM",
    "vehicle_equipment__confidence": "MEDIUM",
    "parts_used__confidence": "HIGH"
  }}
</few_shot_examples>

Analyze the provided audio and return the JSON object now."""


SCHEMA_PROMPT_V2 = _build_schema_prompt_v2()

# ---------------------------------------------------------------------------
# Resilient API call
# ---------------------------------------------------------------------------
def call_gemini_with_retry(
    client: genai.Client,
    model_id: str,
    audio_bytes: bytes,
    mime_type: str,
    max_retries: int = 3,
) -> types.GenerateContentResponse:
    """
    Call Gemini with exponential backoff on transient errors.

    Retryable: 429 (rate limit), 500, 502, 503, 504.
    Non-retryable: 400, 401, 403, 404 (fail immediately).
    """
    retryable_codes = {429, 500, 502, 503, 504}
    audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)

    for attempt in range(1, max_retries + 1):
        try:
            return client.models.generate_content(
                model=model_id,
                contents=[audio_part, SCHEMA_PROMPT_V2],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )
        except Exception as exc:
            status = _extract_status(exc)
            if status in retryable_codes and attempt < max_retries:
                wait = 2**attempt  # 2, 4, 8 seconds
                logger.warning(
                    "API error %s (attempt %d/%d). Retrying in %ds…",
                    status or type(exc).__name__, attempt, max_retries, wait,
                )
                time.sleep(wait)
            else:
                raise APICallError(
                    f"Gemini API call failed after {attempt} attempt(s): {exc}"
                ) from exc

    # Should be unreachable
    raise APICallError(f"Gemini API call exhausted all {max_retries} retries.")


def call_gemini_generic(
    client: genai.Client,
    model_id: str,
    contents: list,
    config: types.GenerateContentConfig,
    max_retries: int = 3,
) -> types.GenerateContentResponse:
    """Generic Gemini call with retry — accepts arbitrary contents list."""
    retryable_codes = {429, 500, 502, 503, 504}
    for attempt in range(1, max_retries + 1):
        try:
            return client.models.generate_content(
                model=model_id,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            status = _extract_status(exc)
            if status in retryable_codes and attempt < max_retries:
                wait = 2 ** attempt
                logger.warning(
                    "API error %s (attempt %d/%d). Retrying in %ds…",
                    status or type(exc).__name__, attempt, max_retries, wait,
                )
                time.sleep(wait)
            else:
                raise APICallError(
                    f"Gemini API call failed after {attempt} attempt(s): {exc}"
                ) from exc
    raise APICallError(f"Gemini API call exhausted all {max_retries} retries.")


def _extract_status(exc: Exception) -> int | None:
    """Try to pull an HTTP status code from the exception."""
    for attr in ("status_code", "code", "grpc_status_code"):
        val = getattr(exc, attr, None)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    return None
