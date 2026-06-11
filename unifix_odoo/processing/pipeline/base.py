"""Base pipeline — shared steps with overridable hooks for each document type."""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import DocumentConfig
from db.protocol import ReferenceDataProvider
from processor.gemini_client import call_gemini_with_retry
from processor.parser import (
    parse_ai_json,
    validate_extracted_data,
    build_response_envelope,
    extract_confidence_markers,
)
from processor.crypto import write_encrypted_json
from processor.prompt_loader import load_prompt
from processor.veracity import (
    should_run_veracity,
    run_veracity_check,
    apply_veracity_corrections,
)
from validator.work_order_validator import validate_work_order

logger = logging.getLogger("work_order_processor")


@dataclass
class PipelineContext:
    source_path: Path
    output_dir: Path | None
    client: Any
    model_id: str
    config: DocumentConfig
    plaintext: bool = False
    extra: dict = field(default_factory=dict)
    # Root the path-traversal guard against (defaults to cwd for CLI use).
    # Odoo passes the temp-upload directory so /tmp files validate.
    safe_root: str | None = None
    # When False, skip writing the encrypted .json.enc to disk — callers that
    # consume the returned envelope directly (e.g. Odoo) don't need it.
    serialize: bool = True


class BasePipeline(ABC):
    """
    Template-method pipeline.  Subclasses implement validate_input, preprocess,
    and extract; shared steps (parse, schema validate, field validate, veracity,
    envelope, serialize) live here with default implementations.
    """

    def __init__(self, config: DocumentConfig, provider: ReferenceDataProvider) -> None:
        self.config = config
        self.provider = provider

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, ctx: PipelineContext) -> dict:
        validated = self.validate_input(ctx)
        media = self.preprocess(ctx, validated)

        prompt = self._load_prompt()
        response = self.extract(ctx, media, prompt)
        logger.info("Gemini response received. %s", self._usage_str(response))

        fuzzy_names = frozenset(fc.name for fc in (self.config.validation.fuzzy_fields if self.config.validation else []))
        work_order, confidences = self._parse(response, fuzzy_names)
        self._validate_schema(work_order)

        val_result = self._validate_fields(work_order, confidences)
        ver_result = self._run_veracity(ctx, media, work_order, confidences, val_result)

        envelope = self._build_envelope(ctx, work_order, val_result, ver_result, response)
        self._serialize(ctx, envelope)
        return envelope

    # ------------------------------------------------------------------
    # Abstract hooks — must be implemented by each pipeline subclass
    # ------------------------------------------------------------------

    @abstractmethod
    def validate_input(self, ctx: PipelineContext) -> dict:
        """Validate the source file and return metadata dict."""

    @abstractmethod
    def preprocess(self, ctx: PipelineContext, validated: dict) -> tuple[bytes, str]:
        """Return (media_bytes, mime_type) ready for Gemini."""

    @abstractmethod
    def extract(self, ctx: PipelineContext, media: tuple[bytes, str], prompt: str) -> Any:
        """Send media + prompt to Gemini and return the raw response."""

    # ------------------------------------------------------------------
    # Shared default implementations
    # ------------------------------------------------------------------

    def _load_prompt(self) -> str:
        return load_prompt(
            self.config.prompt,
            schema_version=self.config.schema_version,
            schema_format=self.config.schema_format,
        )

    def _parse(self, response, fuzzy_names: frozenset) -> tuple[dict, dict]:
        extracted = parse_ai_json(response.text)
        return extract_confidence_markers(extracted, fuzzy_names)

    def _validate_schema(self, work_order: dict) -> None:
        validate_extracted_data(work_order, schema_version=self.config.schema_version)
        logger.info("Extracted data passed schema validation.")

    def _validate_fields(self, work_order: dict, confidences: dict):
        if self.config.validation is None:
            return None
        result = validate_work_order(work_order, self.config.validation, self.provider, confidences)
        logger.info(
            "Validation: %s  (unresolved=%s, review=%s)",
            result.overall_status.value,
            result.unresolved_fields,
            result.review_fields,
        )
        return result

    def _run_veracity(self, ctx, media, work_order, confidences, val_result) -> dict:
        if not self.config.veracity.enabled or val_result is None:
            return {"ran": False}
        if not should_run_veracity(val_result, confidences):
            return {"ran": False}

        audio_bytes, mime_type = media
        logger.info("Running veracity pass (status=%s)…", val_result.overall_status.value)
        veracity_result = run_veracity_check(
            client=ctx.client,
            model_id=ctx.model_id,
            audio_bytes=audio_bytes,
            mime_type=mime_type,
            first_pass_json=work_order,
        )
        if not veracity_result:
            return {"ran": True, "overall_verdict": "ERROR", "corrections_count": 0}

        work_order_out, corrected_fields = apply_veracity_corrections(work_order, veracity_result)
        # Mutate work_order in place so envelope gets corrected values.
        work_order.clear()
        work_order.update(work_order_out)

        ver_info: dict = {
            "ran": True,
            "overall_verdict": veracity_result.get("overall_verdict"),
            "corrections_count": veracity_result.get("corrections_count", 0),
            "corrected_fields": corrected_fields,
        }
        if corrected_fields:
            logger.info("Veracity corrections applied to: %s", corrected_fields)
            post_val = validate_work_order(
                work_order, self.config.validation, self.provider, confidences
            )
            logger.info(
                "Post-veracity validation: %s  (unresolved=%s)",
                post_val.overall_status.value,
                post_val.unresolved_fields,
            )
            ver_info["validation_post_veracity"] = {
                "overall_status": post_val.overall_status.value,
                "unresolved_fields": post_val.unresolved_fields,
                "review_fields": post_val.review_fields,
            }
        return ver_info

    def _build_envelope(self, ctx, work_order, val_result, ver_result, response) -> dict:
        envelope = build_response_envelope(
            source_path=str(ctx.source_path),
            raw_response=response,
            extracted=work_order,
            model_id=ctx.model_id,
            validation_result=val_result,
            veracity_info=ver_result,
            schema_version=self.config.envelope_version,
        )
        return envelope

    def _serialize(self, ctx: PipelineContext, envelope: dict) -> None:
        if not ctx.serialize:
            return
        out_dir = ctx.output_dir or ctx.source_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / ctx.source_path.stem)
        enc_path = write_encrypted_json(envelope, out_path)
        logger.info("Output written: %s", enc_path)

        if ctx.plaintext:
            plain_path = out_dir / (ctx.source_path.stem + ".json")
            plain_path.write_text(
                json.dumps(envelope, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            logger.info("Plaintext sidecar: %s", plain_path)

    @staticmethod
    def _usage_str(response) -> str:
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            u = response.usage_metadata
            pt = getattr(u, "prompt_token_count", "?")
            rt = getattr(u, "candidates_token_count", "?")
            return f"prompt_tokens={pt}, response_tokens={rt}"
        return ""
