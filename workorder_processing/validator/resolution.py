"""Aggregate per-field results into an overall validation status."""

from __future__ import annotations

from .models import MatchStatus, OverallStatus, FieldResult

# Default sets kept for backward compatibility; prefer passing them explicitly.
REQUIRED_RESOLVED_FIELDS = {
    "worker", "company", "location", "vehicle_equipment",
    "start_time", "end_time",
}
REVIEW_TRIGGER_FIELDS = {"parts_used", "total_time_spent"}


def compute_overall_status(
    field_results: dict[str, FieldResult],
    required_resolved_fields: set[str] | None = None,
    review_trigger_fields: set[str] | None = None,
) -> tuple[OverallStatus, list[str], list[str]]:
    required = required_resolved_fields if required_resolved_fields is not None else REQUIRED_RESOLVED_FIELDS
    # review_trigger_fields param is unused in logic (any LOW_CONF triggers review);
    # kept for future policy extension.

    unresolved: list[str] = []
    review: list[str] = []

    for fname, result in field_results.items():
        if result.status in (MatchStatus.NO_MATCH, MatchStatus.TIME_INVALID, MatchStatus.EMPTY):
            if fname in required:
                unresolved.append(fname)
            else:
                review.append(fname)
        elif result.status == MatchStatus.LOW_CONF:
            review.append(fname)

    if unresolved:
        return OverallStatus.FAIL, unresolved, review
    elif review:
        return OverallStatus.REVIEW, unresolved, review
    else:
        return OverallStatus.PASS, [], []
