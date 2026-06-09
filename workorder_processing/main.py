#!/usr/bin/env python3
"""Work Order Processor — CLI entry point."""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from config import load_document_config
from config.settings import DEFAULT_MODEL, DEFAULT_LOG_PATH, IMAGE_EXTENSIONS, MAX_ENV_WALK_DEPTH
from db.memory import InMemoryProvider
from pipeline import get_pipeline
from pipeline.base import PipelineContext
from processor import (
    InputValidationError,
    APICallError,
    ResponseParseError,
    SchemaValidationError,
    CryptoError,
    VideoExtractionError,
    WorkOrderProcessorError,
    create_client,
    configure_logging,
)
from processor.video_extractor import is_video_extension

# ---------------------------------------------------------------------------
# Bootstrap: load .env by walking up from this file's directory
# ---------------------------------------------------------------------------
_script_dir = Path(__file__).resolve().parent
for _ in range(MAX_ENV_WALK_DEPTH):
    _candidate = _script_dir / ".env"
    if _candidate.is_file():
        load_dotenv(_candidate)
        break
    _script_dir = _script_dir.parent


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Either:\n"
            "  - Place a .env file in the project root, or\n"
            f"  - Export it:  export {name}=your-value"
        )
    return value


def _auto_detect_mode(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    if is_video_extension(file_path):
        print("Auto-detected video file — using video mode.")
        return "video"
    if ext in IMAGE_EXTENSIONS:
        print("Auto-detected image file — using image mode.")
        return "image"
    return "audio"


def _config_name(mode: str) -> str:
    return "image_v3" if mode == "image" else "audio_v1"


def _print_summary(envelope: dict, mode: str) -> None:
    ed = envelope.get("extracted_data", {})
    if mode == "image":
        ls = ed.get("labour_summary") or {}
        tasks = ed.get("tasks") or []
        print(f"\nSummary  —  {ed.get('worker', 'N/A')} / {ed.get('client', 'N/A')}")
        print(f"  Location : {ed.get('location', 'N/A')}  |  Date: {ed.get('date', 'N/A')}")
        print(f"  Time     : {ls.get('start', '?')} -> {ls.get('end', '?')} ({ls.get('total_hours', '?')})")
        print(f"  Tasks    : {len(tasks)}")
    else:
        label = "Video" if mode == "video" else "Audio"
        print(f"\nSummary ({label})  —  {ed.get('vehicle_equipment', 'N/A')}")
        print(f"  Problem  : {ed.get('reported_problem', 'N/A')[:80]}")
        print(f"  Time     : {ed.get('start_time', '?')} -> {ed.get('end_time', '?')} "
              f"({ed.get('total_time_spent', '?')})")
    print(f"  Finish   : {envelope.get('finish_reason', 'UNKNOWN')}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract structured work-order JSON from audio/video/image using Gemini.",
    )
    parser.add_argument("input_file", help="Path to the file to process.")
    parser.add_argument(
        "--mode", "-m",
        choices=["audio", "image", "video"],
        default=None,
        help="Processing mode. Auto-detected from file extension when omitted.",
    )
    parser.add_argument("--output-dir", "-o", default=None,
                        help="Directory for output files (default: input file directory).")
    parser.add_argument("--plaintext", "-p", action="store_true",
                        help="Also write a plaintext .json sidecar alongside the encrypted output.")
    parser.add_argument("--config", "-c", default=None,
                        help="Document config name to use (e.g. audio_v1_fr). Overrides auto-detected default.")
    args = parser.parse_args()

    log_file = os.environ.get("PROCESSOR_LOG", str(Path(__file__).resolve().parent / DEFAULT_LOG_PATH))
    logger = configure_logging(log_file=log_file)

    mode = args.mode or _auto_detect_mode(args.input_file)
    config = load_document_config(args.config if args.config else _config_name(mode))
    provider = InMemoryProvider()
    pipeline = get_pipeline(mode, config, provider)

    api_key = _require_env("GOOGLE_API_KEY")
    client = create_client(api_key)
    model_id = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)

    ctx = PipelineContext(
        source_path=Path(args.input_file).resolve(),
        output_dir=Path(args.output_dir).resolve() if args.output_dir else None,
        client=client,
        model_id=model_id,
        config=config,
        plaintext=args.plaintext,
    )

    try:
        envelope = pipeline.run(ctx)
        _print_summary(envelope, mode)
    except (InputValidationError, VideoExtractionError) as exc:
        logger.error("Validation failed: %s", exc)
        print(f"Validation error: {exc}", file=sys.stderr)
        sys.exit(2)
    except APICallError as exc:
        logger.error("API call failed: %s", exc)
        print(f"API error: {exc}", file=sys.stderr)
        sys.exit(3)
    except ResponseParseError as exc:
        logger.error("Response parsing failed: %s", exc)
        print(f"Parse error: {exc}", file=sys.stderr)
        sys.exit(4)
    except SchemaValidationError as exc:
        logger.warning("Schema validation failed: %s", exc)
        print(f"Schema validation failed (output was NOT saved): {exc}", file=sys.stderr)
        sys.exit(6)
    except (CryptoError, WorkOrderProcessorError) as exc:
        logger.error("Processing failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(5)
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        print(f"Unexpected error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
