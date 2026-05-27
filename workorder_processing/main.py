#!/usr/bin/env python3
"""
Work Order Audio Processor

Processes audio recordings of mechanical/maintenance work and extracts
structured JSON data using Gemini.

Sends a local audio file to Gemini for transcription and schema extraction,
validates the result against a JSON schema, wraps it in an audit envelope,
and writes an encrypted .json.enc output file.

Usage:
    python main.py <audio_file_path>
    python main.py examples/recording.mp3
    python main.py examples/recording.mp3 --output-dir outputs/
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from processor import (
    AudioValidationError,
    APICallError,
    ResponseParseError,
    SchemaValidationError,
    CryptoError,
    WorkOrderProcessorError,
    validate_audio_file,
    create_client,
    call_gemini_with_retry,
    parse_ai_json,
    validate_extracted_data,
    build_response_envelope,
    write_encrypted_json,
    configure_logging,
)
from processor.parser import extract_confidence_markers
from processor.veracity import should_run_veracity, run_veracity_check, apply_veracity_corrections
try:
    from processor.image_processor import process_image as _process_image_gemini, validate_image_file as _validate_image_file
    from processor.image_preprocessor import PreprocessResult
    _IMAGE_PROCESSING_AVAILABLE = True
except ImportError:
    _IMAGE_PROCESSING_AVAILABLE = False
from validator.work_order_validator import validate_work_order

# ---------------------------------------------------------------------------
# Bootstrap — env, logging, client
# ---------------------------------------------------------------------------
# Walk up from this file's directory to find the project-root .env
_script_dir = Path(__file__).resolve().parent
for _ in range(3):
    _candidate = _script_dir / ".env"
    if _candidate.is_file():
        load_dotenv(_candidate)
        break
    _script_dir = _script_dir.parent

API_KEY = os.environ.get("GOOGLE_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "GOOGLE_API_KEY is not set. Either:\n"
        "  - Place a .env file in the project root, or\n"
        "  - Export it:  export GOOGLE_API_KEY=your-key"
    )

log_file = os.environ.get("PROCESSOR_LOG", str(Path(__file__).resolve().parent / "processor.log"))
logger = configure_logging(log_file=log_file)

MODEL_ID = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
client = create_client(API_KEY)

# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------
def process_audio(file_path: str, output_dir: str | None = None) -> dict:
    """
    Full pipeline: validate → send to Gemini → parse → validate schema →
    build envelope → encrypt and write.

    Returns the full response envelope dict.
    Raises WorkOrderProcessorError subclasses on failure.
    """
    abs_path = str(Path(file_path).resolve())

    # ── Phase 1: Input validation ──────────────────────────────────────
    safe_root = str(Path.cwd())
    meta = validate_audio_file(abs_path, safe_root=safe_root)
    logger.info(
        "Validated audio file: %s  (%s, %s, %s)",
        meta["path"], meta["detected_mime"], meta["extension"],
        f"{meta['size_bytes']:,} bytes",
    )

    # ── Phase 2: Read + send to Gemini ─────────────────────────────────
    audio_bytes = Path(abs_path).read_bytes()
    logger.info("Sending to Gemini (model=%s, size=%d bytes)…", MODEL_ID, len(audio_bytes))

    response = call_gemini_with_retry(
        client=client,
        model_id=MODEL_ID,
        audio_bytes=audio_bytes,
        mime_type=meta["detected_mime"],
    )

    usage_info = ""
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        u = response.usage_metadata
        usage_info = (
            f"prompt_tokens={getattr(u, 'prompt_token_count', '?')}, "
            f"response_tokens={getattr(u, 'candidates_token_count', '?')}"
        )
    logger.info("Gemini response received. %s", usage_info)

    # ── Phase 3: Parse + validate ──────────────────────────────────────
    extracted = parse_ai_json(response.text)
    logger.debug("Parsed extracted data: %s", extracted)

    work_order, confidences = extract_confidence_markers(extracted)

    validate_extracted_data(work_order)
    logger.info("Extracted data passed schema validation.")

    # ── Phase 3b: Post-extraction validation ───────────────────────────
    validation_result = validate_work_order(work_order, confidences)
    logger.info(
        "Validation: %s  (unresolved=%s, review=%s)",
        validation_result.overall_status.value,
        validation_result.unresolved_fields,
        validation_result.review_fields,
    )

    # ── Phase 3c: Veracity pass ────────────────────────────────────────
    veracity_info: dict = {}
    if should_run_veracity(validation_result, confidences):
        logger.info("Running veracity pass (status=%s)…", validation_result.overall_status.value)
        veracity_result = run_veracity_check(
            client=client,
            model_id=MODEL_ID,
            audio_bytes=audio_bytes,
            mime_type=meta["detected_mime"],
            first_pass_json=work_order,
        )
        if veracity_result:
            work_order, corrected_fields = apply_veracity_corrections(work_order, veracity_result)
            veracity_info = {
                "ran": True,
                "overall_verdict": veracity_result.get("overall_verdict"),
                "corrections_count": veracity_result.get("corrections_count", 0),
                "corrected_fields": corrected_fields,
            }
            if corrected_fields:
                logger.info("Veracity corrections applied to: %s", corrected_fields)
                post_veracity_validation = validate_work_order(work_order, confidences)
                logger.info(
                    "Post-veracity validation: %s  (unresolved=%s)",
                    post_veracity_validation.overall_status.value,
                    post_veracity_validation.unresolved_fields,
                )
                veracity_info["validation_post_veracity"] = {
                    "overall_status": post_veracity_validation.overall_status.value,
                    "unresolved_fields": post_veracity_validation.unresolved_fields,
                    "review_fields": post_veracity_validation.review_fields,
                }
        else:
            veracity_info = {"ran": True, "overall_verdict": "ERROR", "corrections_count": 0}
    else:
        veracity_info = {"ran": False}

    # ── Phase 4: Build envelope + write encrypted output ────────────────
    envelope = build_response_envelope(abs_path, response, work_order, MODEL_ID)
    envelope["validation"] = {
        "overall_status": validation_result.overall_status.value,
        "unresolved_fields": validation_result.unresolved_fields,
        "review_fields": validation_result.review_fields,
    }
    envelope["veracity_pass"] = veracity_info

    out_dir = Path(output_dir).resolve() if output_dir else Path(abs_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    base = Path(abs_path).stem
    out_path = str(out_dir / base)

    enc_path = write_encrypted_json(envelope, out_path)
    logger.info("✅  Output written: %s", enc_path)

    return envelope


def process_image_file(file_path: str, output_dir: str | None = None) -> dict:
    """
    Full pipeline for image work orders: validate → preprocess → send to Gemini →
    parse → post-extraction validate → optional veracity pass → encrypt and write.
    """
    if not _IMAGE_PROCESSING_AVAILABLE:
        raise WorkOrderProcessorError(
            "Image processing dependencies are not installed. "
            "Run: pip install opencv-python-headless Pillow numpy"
        )
    abs_path = str(Path(file_path).resolve())
    safe_root = str(Path.cwd())
    meta = _validate_image_file(abs_path, safe_root=safe_root)
    logger.info(
        "Validated image file: %s  (%s, %s bytes)",
        meta["path"], meta["mime_type"], f"{meta['size_bytes']:,}",
    )

    work_order, confidences, response, preprocess_result = _process_image_gemini(
        abs_path, client, MODEL_ID, preprocess=True,
    )
    if preprocess_result:
        logger.info(
            "Image preprocessed: quality=%s, ops=%s",
            preprocess_result.quality_before.value,
            preprocess_result.operations_applied,
        )

    validate_extracted_data(work_order)
    logger.info("Extracted data passed schema validation.")

    validation_result = validate_work_order(work_order, confidences)
    logger.info(
        "Validation: %s  (unresolved=%s, review=%s)",
        validation_result.overall_status.value,
        validation_result.unresolved_fields,
        validation_result.review_fields,
    )

    veracity_info: dict = {"ran": False}

    envelope = build_response_envelope(abs_path, response, work_order, MODEL_ID,
                                       validation_result=validation_result,
                                       veracity_info=veracity_info)
    if preprocess_result:
        envelope["preprocessing"] = {
            "quality_before": preprocess_result.quality_before.value,
            "operations_applied": preprocess_result.operations_applied,
            "estimated_improvement": preprocess_result.estimated_improvement,
        }

    out_dir = Path(output_dir).resolve() if output_dir else Path(abs_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    base = Path(abs_path).stem
    out_path = str(out_dir / base)

    enc_path = write_encrypted_json(envelope, out_path)
    logger.info("✅  Output written: %s", enc_path)
    return envelope


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Extract structured work-order JSON from audio using Gemini.",
    )
    parser.add_argument(
        "input_file",
        help="Path to the audio or image file to process.",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["audio", "image"],
        default="audio",
        help="Processing mode: 'audio' (default) or 'image'.",
    )
    parser.add_argument(
        "--output-dir", "-o",
        default=None,
        help="Directory for output files (default: same directory as audio file).",
    )
    parser.add_argument(
        "--plaintext", "-p",
        action="store_true",
        help="Also write a plaintext .json file alongside the encrypted one.",
    )
    args = parser.parse_args()

    try:
        if args.mode == "image":
            envelope = process_image_file(args.input_file, output_dir=args.output_dir)
        else:
            envelope = process_audio(args.input_file, output_dir=args.output_dir)

        # Print a summary to stdout
        ed = envelope.get("extracted_data", {})
        print(f"\n📋  Summary  —  {ed.get('vehicle_equipment', 'N/A')}")
        print(f"   Problem   : {ed.get('reported_problem', 'N/A')[:80]}")
        print(f"   Time      : {ed.get('start_time', '?')} → {ed.get('end_time', '?')} "
              f"({ed.get('total_time_spent', '?')})")
        print(f"   Finish    : {envelope.get('finish_reason', 'UNKNOWN')}")

        # Optional plaintext sidecar
        if args.plaintext:
            import json
            out_dir = Path(args.output_dir).resolve() if args.output_dir else Path(args.input_file).resolve().parent
            out_dir.mkdir(parents=True, exist_ok=True)
            plain_path = out_dir / (Path(args.input_file).stem + ".json")
            plain_path.write_text(json.dumps(envelope, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"📄  Plaintext sidecar: {plain_path}")

    except AudioValidationError as exc:
        logger.error("Validation failed: %s", exc)
        print(f"❌  Validation error: {exc}", file=sys.stderr)
        sys.exit(2)
    except APICallError as exc:
        logger.error("API call failed: %s", exc)
        print(f"❌  API error: {exc}", file=sys.stderr)
        sys.exit(3)
    except ResponseParseError as exc:
        logger.error("Response parsing failed: %s", exc)
        print(f"❌  Parse error: {exc}", file=sys.stderr)
        sys.exit(4)
    except SchemaValidationError as exc:
        logger.warning("Schema validation failed: %s", exc)
        print(f"⚠️  Schema validation warning (output still saved): {exc}", file=sys.stderr)
        # Continue — the output was already written before the exit
    except (CryptoError, WorkOrderProcessorError) as exc:
        logger.error("Processing failed: %s", exc)
        print(f"❌  Error: {exc}", file=sys.stderr)
        sys.exit(5)
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        print(f"❌  Unexpected error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
