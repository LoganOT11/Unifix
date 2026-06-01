"""Gemini API client setup and resilient call wrappers."""

import re
import time
import logging

from google import genai
from google.genai import types
from google.genai.types import HttpOptions

from .exceptions import APICallError

logger = logging.getLogger("work_order_processor")


def validate_api_key(key: str) -> None:
    """Basic format check — accepts legacy AIza... keys and new AQ.Ab8... keys."""
    legacy = re.match(r"^AIza[0-9A-Za-z_\-]{35}$", key)
    new_format = re.match(r"^AQ\.[0-9A-Za-z_\-]{2}[0-9A-Za-z_\-]+$", key)
    if not (legacy or new_format):
        raise RuntimeError(
            "GOOGLE_API_KEY format looks invalid. "
            "Expected legacy format (AIza + 35 chars) or new format (AQ. prefix)."
        )


def create_client(api_key: str, timeout_ms: int = 600_000) -> genai.Client:
    """Build a genai.Client with pinned timeout (ms) and validated key."""
    validate_api_key(api_key)
    return genai.Client(
        api_key=api_key,
        http_options=HttpOptions(api_version="v1", timeout=timeout_ms),
    )


def call_gemini_with_retry(
    client: genai.Client,
    model_id: str,
    media_bytes: bytes,
    mime_type: str,
    prompt: str,
    max_retries: int = 3,
) -> types.GenerateContentResponse:
    """
    Send *media_bytes* + *prompt* to Gemini with exponential backoff.

    Retryable: 429, 500, 502, 503, 504.
    Non-retryable: 400, 401, 403, 404 (fail immediately).
    """
    media_part = types.Part.from_bytes(data=media_bytes, mime_type=mime_type)
    return call_gemini_generic(
        client=client,
        model_id=model_id,
        contents=[media_part, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
        ),
        max_retries=max_retries,
    )


def call_gemini_generic(
    client: genai.Client,
    model_id: str,
    contents: list,
    config: types.GenerateContentConfig,
    max_retries: int = 3,
) -> types.GenerateContentResponse:
    """Generic Gemini call with exponential-backoff retry on transient errors."""
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
