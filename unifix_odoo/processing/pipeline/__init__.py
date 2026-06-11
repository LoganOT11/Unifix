"""Pipeline registry — maps document type names to pipeline classes."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import DocumentConfig
    from db.protocol import ReferenceDataProvider
    from .base import BasePipeline

_REGISTRY: dict[str, type] = {}


def register(name: str):
    """Class decorator that registers a pipeline under *name*."""
    def decorator(cls):
        _REGISTRY[name] = cls
        return cls
    return decorator


def get_pipeline(
    document_type: str,
    config: "DocumentConfig",
    provider: "ReferenceDataProvider",
) -> "BasePipeline":
    cls = _REGISTRY.get(document_type)
    if cls is None:
        raise ValueError(
            f"Unknown document type: {document_type!r}. "
            f"Registered types: {sorted(_REGISTRY)}"
        )
    return cls(config, provider)


# Import submodules so decorators fire and populate the registry.
from . import audio, video, image  # noqa: E402,F401
