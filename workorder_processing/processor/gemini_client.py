"""Gemini API client setup and resilient call wrapper."""

import os
import re
import time
import logging

from google import genai
from google.genai import types
from google.genai.types import HttpOptions

from .exceptions import APICallError
from .prompt_loader import load_prompt

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
# Prompts — loaded from prompts/ directory at module startup
# ---------------------------------------------------------------------------
SCHEMA_PROMPT_V2 = load_prompt("audio_extraction_v2", schema_version="v1", schema_format="simplified")

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
