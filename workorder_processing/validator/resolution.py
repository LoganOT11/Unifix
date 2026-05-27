from .models import MatchStatus, OverallStatus, ValidationResult, FieldResult

REQUIRED_RESOLVED_FIELDS = {
    "worker", "company", "location", "vehicle_equipment",
    "start_time", "end_time",
}

REVIEW_TRIGGER_FIELDS = {"parts_used", "total_time_spent"}


def compute_overall_status(
    field_results: dict[str, FieldResult],
) -> tuple[OverallStatus, list[str], list[str]]:
    unresolved: list[str] = []
    review:     list[str] = []

    for fname, result in field_results.items():
        if result.status in (MatchStatus.NO_MATCH, MatchStatus.TIME_INVALID):
            if fname in REQUIRED_RESOLVED_FIELDS:
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
