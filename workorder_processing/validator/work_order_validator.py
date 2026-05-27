from .fuzzy_resolver import resolve_field, resolve_parts_used, resolve_field_with_gemini_confidence
from .time_validator import validate_time_field
from .resolution import compute_overall_status
from .models import FieldResult, MatchStatus, ValidationResult

FUZZY_FIELDS = {"worker", "company", "location", "vehicle_equipment"}
TIME_FIELDS  = {"start_time", "end_time", "total_time_spent"}
FREE_TEXT_FIELDS = {
    "reported_problem", "diagnosis_cause", "work_performed",
    "future_recommendations", "remaining_tasks",
}


def validate_work_order(
    extracted: dict,
    confidences: dict[str, str] | None = None,
) -> ValidationResult:
    field_results: dict[str, FieldResult] = {}
    conf = confidences or {}

    for field_name, raw_value in extracted.items():
        raw = str(raw_value) if raw_value is not None else ""

        if field_name in FUZZY_FIELDS or field_name == "parts_used":
            gemini_conf = conf.get(field_name, "MEDIUM")
            field_results[field_name] = resolve_field_with_gemini_confidence(
                field_name, raw, gemini_conf
            )
        elif field_name in TIME_FIELDS:
            field_results[field_name] = validate_time_field(field_name, raw)
        else:
            field_results[field_name] = FieldResult(
                field_name=field_name,
                raw_value=raw,
                resolved_value=raw,
                status=MatchStatus.PASS_THROUGH,
            )

    overall, unresolved, review = compute_overall_status(field_results)
    resolved_json = {fname: fr.resolved_value for fname, fr in field_results.items()}

    return ValidationResult(
        resolved_json=resolved_json,
        field_results=field_results,
        overall_status=overall,
        unresolved_fields=unresolved,
        review_fields=review,
    )
