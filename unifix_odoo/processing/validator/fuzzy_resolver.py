"""Config-driven fuzzy resolver for work-order fields."""

from __future__ import annotations

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from .models import FieldResult, MatchStatus

# Default thresholds kept as constants so tests can reference them directly.
THRESHOLD_EXACT = 100.0
THRESHOLD_HIGH  = 85.0
THRESHOLD_LOW   = 60.0

GEMINI_CONFIDENCE_BOOST: dict[str, float] = {
    "HIGH":   5.0,
    "MEDIUM": 0.0,
    "LOW":   -10.0,
}

_ALGO_MAP = {
    "ratio":            lambda q, c: fuzz.ratio(q, c),
    "partial_ratio":    lambda q, c: fuzz.partial_ratio(q, c),
    "token_sort_ratio": lambda q, c: fuzz.token_sort_ratio(q, c),
    "token_set_ratio":  lambda q, c: fuzz.token_set_ratio(q, c),
    "WRatio":           lambda q, c: fuzz.WRatio(q, c),
    "jaro_winkler":     lambda q, c: JaroWinkler.similarity(q, c) * 100,
}


def _score_algorithms(query: str, candidate: str, weights: dict) -> tuple[float, dict]:
    if not query.strip() or not candidate.strip():
        return 0.0, {algo: 0.0 for algo in weights}

    individual: dict[str, float] = {}
    composite = 0.0
    for algo_name, weight in weights.items():
        score = _ALGO_MAP[algo_name](query.strip().lower(), candidate.strip().lower())
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


class FuzzyResolver:
    """
    Resolves work-order fields against a reference data provider.

    Field configurations (weights, scoring strategy) and score thresholds are
    supplied at construction time, typically loaded from a DocumentConfig YAML.
    """

    def __init__(
        self,
        field_configs,  # list[FuzzyFieldConfig] from config
        provider,       # ReferenceDataProvider
        thresholds: dict[str, float] | None = None,
        confidence_boosts: dict[str, float] | None = None,
    ) -> None:
        self._field_map = {fc.name: fc for fc in field_configs}
        self._provider = provider
        th = thresholds or {}
        self._threshold_exact = th.get("exact", THRESHOLD_EXACT)
        self._threshold_high  = th.get("high_conf", THRESHOLD_HIGH)
        self._threshold_low   = th.get("low_conf", THRESHOLD_LOW)
        self._confidence_boosts = confidence_boosts or GEMINI_CONFIDENCE_BOOST

    def _status_from_score(self, score: float) -> MatchStatus:
        if score >= self._threshold_exact:
            return MatchStatus.EXACT
        elif score >= self._threshold_high:
            return MatchStatus.HIGH_CONF
        elif score >= self._threshold_low:
            return MatchStatus.LOW_CONF
        else:
            return MatchStatus.NO_MATCH

    def resolve_field(self, field_name: str, raw_value: str) -> FieldResult:
        if not raw_value or not raw_value.strip():
            return FieldResult(
                field_name=field_name,
                raw_value=raw_value,
                resolved_value="",
                status=MatchStatus.EMPTY,
            )

        fc = self._field_map.get(field_name)
        if fc is None:
            return FieldResult(
                field_name=field_name,
                raw_value=raw_value,
                resolved_value=raw_value,
                status=MatchStatus.PASS_THROUGH,
            )

        raw_value = raw_value.strip()
        candidates = self._provider.get_names(field_name)
        best_match, score, algo_scores, top_candidates = _find_best_match(
            raw_value, candidates, fc.weights
        )
        status = self._status_from_score(score)
        db_entry = self._provider.resolve_canonical(field_name, best_match) if best_match else None
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

    def resolve_parts_used(self, raw_value: str) -> FieldResult:
        if not raw_value or not raw_value.strip():
            return FieldResult(
                field_name="parts_used",
                raw_value=raw_value,
                resolved_value="",
                status=MatchStatus.EMPTY,
            )

        fc = self._field_map.get("parts_used")
        weights = fc.weights if fc else {}

        tokens = [t.strip() for t in raw_value.split(",") if t.strip()]
        candidates = self._provider.get_names("parts_used")

        resolved_tokens: list[str] = []
        all_scores: list[float] = []
        all_algo_scores: list[dict] = []

        for token in tokens:
            best_match, score, algo_scores, _ = _find_best_match(token, candidates, weights)
            all_scores.append(score)
            all_algo_scores.append({token: algo_scores})
            status = self._status_from_score(score)
            resolved_tokens.append(best_match if status != MatchStatus.NO_MATCH else token)

        min_score = min(all_scores) if all_scores else 0.0
        overall_status = self._status_from_score(min_score)
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

    def resolve_field_with_gemini_confidence(
        self,
        field_name: str,
        raw_value: str,
        gemini_confidence: str = "MEDIUM",
    ) -> FieldResult:
        if field_name == "parts_used":
            result = self.resolve_parts_used(raw_value)
        else:
            result = self.resolve_field(field_name, raw_value)

        boost = self._confidence_boosts.get(gemini_confidence.upper(), 0.0)
        if boost != 0.0 and result.score > 0.0:
            adjusted = max(0.0, min(100.0, result.score + boost))
            result.score = round(adjusted, 2)
            result.status = self._status_from_score(adjusted)
            result.notes += f" | Gemini confidence: {gemini_confidence} (boost: {boost:+.1f})"

        return result
