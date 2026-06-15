"""Orchestrate post-extraction field validation for a work order."""

from __future__ import annotations

from .fuzzy_resolver import FuzzyResolver
from .time_validator import validate_time_field
from .resolution import compute_overall_status
from .models import FieldResult, MatchStatus, ValidationResult


def validate_work_order(
    extracted: dict,
    config,          # ValidationConfig from config module
    provider,        # ReferenceDataProvider from db.protocol
    confidences: dict[str, str] | None = None,
) -> ValidationResult:
    """
    Validate each field in *extracted* according to *config*.

    Fuzzy fields are resolved against *provider*; time fields are normalised;
    everything else passes through as-is.
    """
    resolver = FuzzyResolver(
        config.fuzzy_fields,
        provider,
        thresholds=config.thresholds,
    )
    fuzzy_field_names = {fc.name for fc in config.fuzzy_fields}
    time_fields = set(config.time_fields)
    # NOTE: *confidences* (Gemini self-reported HIGH/MEDIUM/LOW) is accepted for
    # backward compatibility but intentionally no longer influences the result.
    # LLM self-confidence is poorly calibrated; field confidence is now derived
    # purely from the deterministic match against the reference data.

    field_results: dict[str, FieldResult] = {}

    for field_name, raw_value in extracted.items():
        raw = str(raw_value) if raw_value is not None else ""

        if field_name in fuzzy_field_names:
            if field_name == "parts_used":
                field_results[field_name] = resolver.resolve_parts_used(raw)
            else:
                field_results[field_name] = resolver.resolve_field(field_name, raw)
        elif field_name in time_fields:
            field_results[field_name] = validate_time_field(field_name, raw)
        else:
            field_results[field_name] = FieldResult(
                field_name=field_name,
                raw_value=raw,
                resolved_value=raw,
                status=MatchStatus.PASS_THROUGH,
            )

    overall, unresolved, review = compute_overall_status(
        field_results,
        required_resolved_fields=config.required_resolved_fields,
        review_trigger_fields=config.review_trigger_fields,
    )
    resolved_json = {fname: fr.resolved_value for fname, fr in field_results.items()}

    return ValidationResult(
        resolved_json=resolved_json,
        field_results=field_results,
        overall_status=overall,
        unresolved_fields=unresolved,
        review_fields=review,
    )
