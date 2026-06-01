"""Processor package — public re-exports."""

from .exceptions import (
    WorkOrderProcessorError,
    AudioValidationError,
    APICallError,
    ResponseParseError,
    SchemaValidationError,
    CryptoError,
    VideoExtractionError,
)
from .validator import validate_audio_file, validate_video_file
from .video_extractor import extract_audio_from_video, is_video_extension
from .gemini_client import create_client, call_gemini_with_retry
from .parser import parse_ai_json, validate_extracted_data, build_response_envelope
from .crypto import write_encrypted_json, read_encrypted_json
from .logging_config import configure_logging, sanitize_log_message

__all__ = [
    # Exceptions
    "WorkOrderProcessorError",
    "AudioValidationError",
    "APICallError",
    "ResponseParseError",
    "SchemaValidationError",
    "CryptoError",
    "VideoExtractionError",
    # Validator
    "validate_audio_file",
    "validate_video_file",
    # Video extractor
    "extract_audio_from_video",
    "is_video_extension",
    # Gemini
    "create_client",
    "call_gemini_with_retry",
    # Parser
    "parse_ai_json",
    "validate_extracted_data",
    "build_response_envelope",
    # Crypto
    "write_encrypted_json",
    "read_encrypted_json",
    # Logging
    "configure_logging",
    "sanitize_log_message",
]
