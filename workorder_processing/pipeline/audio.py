"""Audio pipeline — validate → read → extract → validate → veracity → serialize."""

from __future__ import annotations

from pathlib import Path

from processor.validator import validate_audio_file
from processor.gemini_client import call_gemini_with_retry
from pipeline import register
from .base import BasePipeline, PipelineContext

import logging
logger = logging.getLogger("work_order_processor")


@register("audio")
class AudioPipeline(BasePipeline):

    def validate_input(self, ctx: PipelineContext) -> dict:
        safe_root = str(Path.cwd())
        meta = validate_audio_file(str(ctx.source_path), safe_root=safe_root)
        logger.info(
            "Validated audio file: %s  (%s, %s, %s bytes)",
            meta["path"], meta["detected_mime"], meta["extension"], f"{meta['size_bytes']:,}",
        )
        return meta

    def preprocess(self, ctx: PipelineContext, validated: dict) -> tuple[bytes, str]:
        audio_bytes = Path(validated["path"]).read_bytes()
        logger.info(
            "Sending to Gemini (model=%s, size=%d bytes)…", ctx.model_id, len(audio_bytes)
        )
        return audio_bytes, validated["detected_mime"]

    def extract(self, ctx: PipelineContext, media: tuple[bytes, str], prompt: str):
        audio_bytes, mime = media
        return call_gemini_with_retry(ctx.client, ctx.model_id, audio_bytes, mime, prompt)
