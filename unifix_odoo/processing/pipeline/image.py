"""Image pipeline — preprocess → extract → schema validate → serialize."""

from __future__ import annotations

from pathlib import Path

from processor.image_processor import validate_image_file, process_image
from processor.gemini_client import call_gemini_generic
from google.genai import types
from pipeline import register
from .base import BasePipeline, PipelineContext

import logging
logger = logging.getLogger("work_order_processor")


@register("image")
class ImagePipeline(BasePipeline):

    def validate_input(self, ctx: PipelineContext) -> dict:
        safe_root = ctx.safe_root or str(Path.cwd())
        meta = validate_image_file(str(ctx.source_path), safe_root=safe_root)
        logger.info(
            "Validated image file: %s  (%s, %s bytes)",
            meta["path"], meta["mime_type"], f"{meta['size_bytes']:,}",
        )
        return meta

    def preprocess(self, ctx: PipelineContext, validated: dict) -> tuple[bytes, str]:
        from processor.image_preprocessor import preprocess_image

        image_bytes = Path(validated["path"]).read_bytes()
        mime_type = validated["mime_type"]

        if self.config.preprocessing_enabled and mime_type != "application/pdf":
            result = preprocess_image(image_bytes, mime_type)
            ctx.extra["preprocess_result"] = result
            logger.info(
                "Image preprocessed: quality=%s, ops=%s",
                result.quality_before.value,
                result.operations_applied,
            )
            return result.image_bytes, result.mime_type

        return image_bytes, mime_type

    def extract(self, ctx: PipelineContext, media: tuple[bytes, str], prompt: str):
        image_bytes, mime = media
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime)
        return call_gemini_generic(
            client=ctx.client,
            model_id=ctx.model_id,
            contents=[image_part, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )

    def _build_envelope(self, ctx, work_order, val_result, ver_result, response) -> dict:
        envelope = super()._build_envelope(ctx, work_order, val_result, ver_result, response)
        preprocess_result = ctx.extra.get("preprocess_result")
        if preprocess_result:
            envelope["preprocessing"] = {
                "quality_before": preprocess_result.quality_before.value,
                "operations_applied": preprocess_result.operations_applied,
                "estimated_improvement": preprocess_result.estimated_improvement,
            }
        return envelope
