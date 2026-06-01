"""Document type configuration — dataclasses and YAML loader."""

from __future__ import annotations

import yaml
from dataclasses import dataclass, field
from pathlib import Path

_DOC_TYPES_DIR = Path(__file__).resolve().parent / "document_types"


@dataclass
class FuzzyFieldConfig:
    name: str
    weights: dict[str, float]
    scoring: str  # "maximum" | "minimum"


@dataclass
class ValidationConfig:
    fuzzy_fields: list[FuzzyFieldConfig]
    time_fields: list[str]
    free_text_fields: list[str]
    thresholds: dict[str, float]
    required_resolved_fields: set[str]
    review_trigger_fields: set[str]


@dataclass
class VeracityConfig:
    enabled: bool
    trigger_statuses: list[str] = field(default_factory=list)
    trigger_confidences: list[str] = field(default_factory=list)


@dataclass
class DocumentConfig:
    document_type: str
    schema_version: str      # "v1", "v3"
    envelope_version: str    # "1.0", "3.0" — written into output envelope
    prompt: str              # prompt file name without .txt extension
    schema_format: str       # "simplified" | "full"
    preprocessing_enabled: bool
    validation: ValidationConfig | None
    veracity: VeracityConfig


def load_document_config(name: str) -> DocumentConfig:
    """Load config/document_types/{name}.yaml and return a DocumentConfig."""
    yaml_path = _DOC_TYPES_DIR / f"{name}.yaml"
    if not yaml_path.is_file():
        available = [p.stem for p in _DOC_TYPES_DIR.glob("*.yaml")]
        raise FileNotFoundError(
            f"No document config found for {name!r}. "
            f"Available: {sorted(available)}"
        )
    with open(yaml_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    validation: ValidationConfig | None = None
    if raw.get("validation") is not None:
        v = raw["validation"]
        fuzzy_fields = [
            FuzzyFieldConfig(
                name=fc["name"],
                weights=dict(fc["weights"]),
                scoring=fc["scoring"],
            )
            for fc in v.get("fuzzy_fields", [])
        ]
        validation = ValidationConfig(
            fuzzy_fields=fuzzy_fields,
            time_fields=list(v.get("time_fields", [])),
            free_text_fields=list(v.get("free_text_fields", [])),
            thresholds={k: float(val) for k, val in v.get("thresholds", {}).items()},
            required_resolved_fields=set(v.get("required_resolved_fields", [])),
            review_trigger_fields=set(v.get("review_trigger_fields", [])),
        )

    ver_raw = raw.get("veracity", {})
    veracity = VeracityConfig(
        enabled=bool(ver_raw.get("enabled", False)),
        trigger_statuses=list(ver_raw.get("trigger_statuses", [])),
        trigger_confidences=list(ver_raw.get("trigger_confidences", [])),
    )

    return DocumentConfig(
        document_type=raw["document_type"],
        schema_version=raw["schema_version"],
        envelope_version=raw.get("envelope_version", "1.0"),
        prompt=raw["prompt"],
        schema_format=raw["schema_format"],
        preprocessing_enabled=bool(raw.get("preprocessing_enabled", False)),
        validation=validation,
        veracity=veracity,
    )
