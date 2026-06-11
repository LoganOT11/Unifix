from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MatchStatus(str, Enum):
    EXACT         = "EXACT"
    HIGH_CONF     = "HIGH_CONFIDENCE"
    LOW_CONF      = "LOW_CONFIDENCE"
    NO_MATCH      = "NO_MATCH"
    EMPTY         = "EMPTY"
    TIME_VALID    = "TIME_VALID"
    TIME_INVALID  = "TIME_INVALID"
    PASS_THROUGH  = "PASS_THROUGH"


class OverallStatus(str, Enum):
    PASS   = "PASS"
    REVIEW = "REVIEW"
    FAIL   = "FAIL"


@dataclass
class FieldResult:
    field_name:        str
    raw_value:         str
    resolved_value:    str
    status:            MatchStatus
    score:             float = 0.0
    matched_db_entry:  dict | None = None
    algorithm_scores:  dict[str, float] = field(default_factory=dict)
    notes:             str = ""
    top_candidates:    list[dict] = field(default_factory=list)


@dataclass
class ValidationResult:
    resolved_json:     dict[str, Any]
    field_results:     dict[str, FieldResult]
    overall_status:    OverallStatus
    unresolved_fields: list[str] = field(default_factory=list)
    review_fields:     list[str] = field(default_factory=list)
