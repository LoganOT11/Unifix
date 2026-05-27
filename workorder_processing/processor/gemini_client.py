"""Gemini API client setup and resilient call wrapper."""

import json
import os
import re
import time
import logging
from pathlib import Path

from google import genai
from google.genai import types

from .exceptions import APICallError

logger = logging.getLogger("work_order_processor")

# ---------------------------------------------------------------------------
# API key helpers
# ---------------------------------------------------------------------------
def validate_api_key(key: str) -> None:
    """Basic format check — Google AI keys start with 'AIza' and are 39 chars."""
    if not re.match(r"^AIza[0-9A-Za-z_\-]{35}$", key):
        raise RuntimeError(
            "GOOGLE_API_KEY format looks invalid. "
            "Expected format: AIza followed by 35 alphanumeric characters."
        )


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------
def create_client(api_key: str, timeout_ms: int = 600_000) -> genai.Client:
    """Build a genai.Client with pinned timeout (ms) and validated key."""
    validate_api_key(api_key)
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=timeout_ms),
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
                contents=[audio_part, SCHEMA_PROMPT],
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
