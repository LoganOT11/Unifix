"""Video pipeline — extract audio from video then run the audio pipeline steps."""

from __future__ import annotations

import os
from pathlib import Path

from processor.validator import validate_video_file
from processor.video_extractor import extract_audio_from_video
from pipeline import register
from .audio import AudioPipeline
from .base import PipelineContext

import logging
logger = logging.getLogger("work_order_processor")


@register("video")
class VideoPipeline(AudioPipeline):
    """Inherits all audio pipeline steps; overrides input validation and preprocessing."""

    def validate_input(self, ctx: PipelineContext) -> dict:
        safe_root = str(Path.cwd())
        meta = validate_video_file(str(ctx.source_path), safe_root=safe_root)
        logger.info(
            "Validated video file: %s  (%s, %s bytes)",
            meta["path"], meta["detected_mime"], f"{meta['size_bytes']:,}",
        )
        return meta

    def preprocess(self, ctx: PipelineContext, validated: dict) -> tuple[bytes, str]:
        audio_bytes, audio_mime, tmp_path = extract_audio_from_video(validated["path"])
        logger.info(
            "Audio extracted: %s  (mime=%s, %s bytes)",
            tmp_path, audio_mime, f"{len(audio_bytes):,}",
        )
        ctx.extra["_tmp_audio_path"] = tmp_path
        logger.info("Sending to Gemini (model=%s, size=%d bytes)…", ctx.model_id, len(audio_bytes))
        return audio_bytes, audio_mime

    def run(self, ctx: PipelineContext) -> dict:
        try:
            return super().run(ctx)
        finally:
            tmp = ctx.extra.pop("_tmp_audio_path", None)
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _build_envelope(self, ctx, work_order, val_result, ver_result, response) -> dict:
        envelope = super()._build_envelope(ctx, work_order, val_result, ver_result, response)
        envelope["source_type"] = "video"
        return envelope
