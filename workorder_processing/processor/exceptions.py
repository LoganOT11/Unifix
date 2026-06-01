"""Typed exception hierarchy for the work order audio processor."""


class WorkOrderProcessorError(Exception):
    """Base exception for this application."""


class AudioValidationError(WorkOrderProcessorError):
    """File failed pre-flight validation."""


class APICallError(WorkOrderProcessorError):
    """Gemini API call failed after all retries."""


class ResponseParseError(WorkOrderProcessorError):
    """Could not parse the model's response as valid JSON."""


class SchemaValidationError(WorkOrderProcessorError):
    """Extracted JSON does not conform to the work order schema."""


class CryptoError(WorkOrderProcessorError):
    """Encryption or decryption failed."""


class VideoExtractionError(WorkOrderProcessorError):
    """Audio extraction from a video file failed."""
