from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler
from .models import FieldResult, MatchStatus
from db.reference_data import (
    get_worker_names, get_company_names, get_location_names,
    get_equipment_strings, get_part_strings,
    resolve_worker_canonical, resolve_equipment_canonical, resolve_part_canonical,
    resolve_location_canonical, resolve_company_canonical,
)

THRESHOLD_EXACT = 100.0
THRESHOLD_HIGH  = 85.0
THRESHOLD_LOW   = 60.0

FIELD_WEIGHTS = {
    "worker":            {"jaro_winkler": 0.40, "token_sort_ratio": 0.35, "ratio": 0.25},
    "company":           {"token_set_ratio": 0.40, "WRatio": 0.35, "partial_ratio": 0.25},
    "location":          {"partial_ratio": 0.40, "token_set_ratio": 0.35, "token_sort_ratio": 0.25},
    "vehicle_equipment": {"token_set_ratio": 0.40, "partial_ratio": 0.30, "ratio": 0.30},
    "parts_used":        {"token_set_ratio": 0.45, "WRatio": 0.35, "partial_ratio": 0.20},
}

DB_LOADERS = {
    "worker":            get_worker_names,
    "company":           get_company_names,
    "location":          get_location_names,
    "vehicle_equipment": get_equipment_strings,
    "parts_used":        get_part_strings,
}

CANONICAL_RESOLVERS = {
    "worker":            resolve_worker_canonical,
    "vehicle_equipment": resolve_equipment_canonical,
    "parts_used":        resolve_part_canonical,
    "location":          resolve_location_canonical,
    "company":           resolve_company_canonical,
}


def _score_algorithms(query: str, candidate: str, weights: dict) -> tuple[float, dict]:
    algo_map = {
        "ratio":            lambda q, c: fuzz.ratio(q, c),
        "partial_ratio":    lambda q, c: fuzz.partial_ratio(q, c),
        "token_sort_ratio": lambda q, c: fuzz.token_sort_ratio(q, c),
        "token_set_ratio":  lambda q, c: fuzz.token_set_ratio(q, c),
        "WRatio":           lambda q, c: fuzz.WRatio(q, c),
        "jaro_winkler":     lambda q, c: JaroWinkler.similarity(q, c) * 100,
    }

    individual: dict[str, float] = {}
    composite = 0.0

    for algo_name, weight in weights.items():
        score = algo_map[algo_name](query.strip().lower(), candidate.strip().lower())
        individual[algo_name] = round(score, 2)
        composite += score * weight

    return round(composite, 2), individual


def _find_best_match(
    query: str,
    candidates: list[str],
    weights: dict,
    top_n: int = 3,
) -> tuple[str, float, dict, list[dict]]:
    if not candidates or not query.strip():
        return "", 0.0, {}, []

    scored: list[tuple[str, float, dict]] = []
    for candidate in candidates:
        score, algo_scores = _score_algorithms(query, candidate, weights)
        scored.append((candidate, score, algo_scores))

    scored.sort(key=lambda x: x[1], reverse=True)
    best_candidate, best_score, best_algo_scores = scored[0]
    top_candidates = [{"value": c, "score": round(s, 2)} for c, s, _ in scored[:top_n]]

    return best_candidate, best_score, best_algo_scores, top_candidates


def _status_from_score(score: float) -> MatchStatus:
    if score >= THRESHOLD_EXACT:
        return MatchStatus.EXACT
    elif score >= THRESHOLD_HIGH:
        return MatchStatus.HIGH_CONF
    elif score >= THRESHOLD_LOW:
        return MatchStatus.LOW_CONF
    else:
        return MatchStatus.NO_MATCH


def resolve_field(field_name: str, raw_value: str) -> FieldResult:
    if not raw_value or not raw_value.strip():
        return FieldResult(
            field_name=field_name,
            raw_value=raw_value,
            resolved_value="",
            status=MatchStatus.EMPTY,
        )

    raw_value = raw_value.strip()
    candidates = DB_LOADERS[field_name]()
    weights    = FIELD_WEIGHTS[field_name]
    best_match, score, algo_scores, top_candidates = _find_best_match(raw_value, candidates, weights)
    status = _status_from_score(score)

    canonical_resolver = CANONICAL_RESOLVERS.get(field_name)
    db_entry = canonical_resolver(best_match) if canonical_resolver and best_match else None

    resolved = best_match if status != MatchStatus.NO_MATCH else raw_value

    return FieldResult(
        field_name=field_name,
        raw_value=raw_value,
        resolved_value=resolved,
        status=status,
        score=score,
        matched_db_entry=db_entry,
        algorithm_scores=algo_scores,
        notes=f"Best DB candidate: '{best_match}'" if status == MatchStatus.NO_MATCH else "",
        top_candidates=top_candidates,
    )


GEMINI_CONFIDENCE_BOOST: dict[str, float] = {
    "HIGH":   5.0,
    "MEDIUM": 0.0,
    "LOW":   -10.0,
}


def resolve_field_with_gemini_confidence(
    field_name: str,
    raw_value: str,
    gemini_confidence: str = "MEDIUM",
) -> FieldResult:
    """Resolve a field then apply a score boost based on Gemini's confidence."""
    if field_name == "parts_used":
        result = resolve_parts_used(raw_value)
    else:
        result = resolve_field(field_name, raw_value)

    boost = GEMINI_CONFIDENCE_BOOST.get(gemini_confidence.upper(), 0.0)
    if boost != 0.0 and result.score > 0.0:
        adjusted = max(0.0, min(100.0, result.score + boost))
        result.score = round(adjusted, 2)
        result.status = _status_from_score(adjusted)
        result.notes += f" | Gemini confidence: {gemini_confidence} (boost: {boost:+.1f})"

    return result


def resolve_parts_used(raw_value: str) -> FieldResult:
    if not raw_value or not raw_value.strip():
        return FieldResult(
            field_name="parts_used",
            raw_value=raw_value,
            resolved_value="",
            status=MatchStatus.EMPTY,
        )

    tokens = [t.strip() for t in raw_value.split(",") if t.strip()]
    candidates = get_part_strings()
    weights = FIELD_WEIGHTS["parts_used"]

    resolved_tokens: list[str] = []
    all_scores: list[float] = []
    all_algo_scores: list[dict] = []

    for token in tokens:
        best_match, score, algo_scores, _ = _find_best_match(token, candidates, weights)
        all_scores.append(score)
        all_algo_scores.append({token: algo_scores})
        status = _status_from_score(score)
        resolved_tokens.append(best_match if status != MatchStatus.NO_MATCH else token)

    min_score = min(all_scores) if all_scores else 0.0
    overall_status = _status_from_score(min_score)
    avg_score = round(sum(all_scores) / len(all_scores), 2) if all_scores else 0.0

    return FieldResult(
        field_name="parts_used",
        raw_value=raw_value,
        resolved_value=", ".join(resolved_tokens),
        status=overall_status,
        score=avg_score,
        algorithm_scores={"per_token": all_algo_scores},
        notes=f"Matched {len(tokens)} part token(s). Min score: {min_score}",
    )
