"""Structured logging with PII sanitization."""

import logging
import re
import sys
from pathlib import Path

# Patterns that must never appear in logs
_SENSITIVE_PATTERNS = [
    (re.compile(r'(api[_-]?key["\s:=]+)[^\s&"]+', re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(AIza[0-9A-Za-z_\-]{35})"), "[REDACTED_API_KEY]"),
    (re.compile(r'(x-goog-api-key["\s:=]+)[^\s&"]+', re.IGNORECASE), r"\1[REDACTED]"),
]


def sanitize_log_message(msg: str) -> str:
    """Strip API keys and tokens from *msg* before it hits a log sink."""
    for pattern, replacement in _SENSITIVE_PATTERNS:
        msg = pattern.sub(replacement, msg)
    return msg


class _SanitizingFormatter(logging.Formatter):
    """Formatter that runs every log message through the sanitizer."""

    def format(self, record: logging.LogRecord) -> str:
        record.msg = sanitize_log_message(str(record.msg))
        return super().format(record)


def configure_logging(
    log_file: str | Path | None = None,
    level: int = logging.DEBUG,
) -> logging.Logger:
    """
    Set up the application logger with console (INFO+) and optional file (DEBUG+).

    Returns the root application logger so callers can do
    ``logger = logging.getLogger("work_order_processor")``.
    """
    logger = logging.getLogger("work_order_processor")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()  # idempotent

    fmt = _SanitizingFormatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    # Console: INFO and above
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # File: DEBUG and above (full audit trail)
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger
