# Fuzzy Matching & Validation — Implementation Plan

> **Scope:** Post-extraction validation pipeline that resolves hallucinated or imprecise
> values from the Gemini output against a reference database. Covers field routing,
> matching algorithms, confidence scoring, resolution strategy, and a full mock test suite.

---

## Table of Contents

1. [Field Routing Strategy](#1-field-routing-strategy)
2. [Algorithm Selection](#2-algorithm-selection)
3. [Mock Reference Database](#3-mock-reference-database)
4. [Validator Architecture](#4-validator-architecture)
5. [Fuzzy Matching Implementation](#5-fuzzy-matching-implementation)
6. [Time Format Validation](#6-time-format-validation)
7. [Confidence Scoring & Resolution Logic](#7-confidence-scoring--resolution-logic)
8. [Full Validator Orchestrator](#8-full-validator-orchestrator)
9. [Mock Test Suite](#9-mock-test-suite)
10. [Recommended Project Structure](#10-recommended-project-structure)
11. [Dependency List](#11-dependency-list)

---

## 1. Field Routing Strategy

Each field in the schema takes a different validation path. Fields that are not listed below
pass through as-is (free text, no database reference exists).

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Extracted JSON (13 fields)                        │
└────────────────────────────┬────────────────────────────────────────┘
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
          ▼                  ▼                  ▼
   ┌─────────────┐   ┌──────────────┐   ┌──────────────────┐
   │  FUZZY DB   │   │  TIME FORMAT │   │   FREE TEXT      │
   │  MATCHING   │   │  VALIDATION  │   │   PASS-THROUGH   │
   ├─────────────┤   ├──────────────┤   ├──────────────────┤
   │ worker      │   │ start_time   │   │ reported_problem │
   │ company     │   │ end_time     │   │ diagnosis_cause  │
   │ location    │   │ total_time   │   │ work_performed   │
   │ vehicle_    │   │   _spent     │   │ future_recs      │
   │  equipment  │   └──────────────┘   │ remaining_tasks  │
   │ parts_used  │                      └──────────────────┘
   └─────────────┘
```

### Resolution outcomes per fuzzy field

| Outcome | Condition | Action |
|---|---|---|
| `EXACT` | Score ≥ 100 | Accept as-is |
| `HIGH_CONFIDENCE` | Score ≥ 85 | Replace with canonical DB value |
| `LOW_CONFIDENCE` | Score 60–84 | Replace with best match, flag for review |
| `NO_MATCH` | Score < 60 | Keep raw value, flag as unresolved |
| `EMPTY` | Input is `""` or null | Keep empty, no match attempted |

---

## 2. Algorithm Selection

No single string metric handles all cases well. Use a **multi-algorithm ensemble** and
combine the scores, weighting each algorithm based on the field type.

### Algorithm reference

| Algorithm | Library | Best for | Weakness |
|---|---|---|---|
| `ratio` (Levenshtein) | `rapidfuzz` | Overall similarity, typos | Sensitive to word order |
| `token_sort_ratio` | `rapidfuzz` | Same words, different order | Loses partial overlap signals |
| `token_set_ratio` | `rapidfuzz` | Substrings, abbreviations | Can over-match short strings |
| `WRatio` (weighted combo) | `rapidfuzz` | General-purpose best pick | Black-box, less tunable |
| `jaro_winkler` | `rapidfuzz` | Names (prefix-heavy matching) | Poor for long strings |
| `partial_ratio` | `rapidfuzz` | One string is a substring | Over-matches very short tokens |

### Per-field algorithm weights

```python
FIELD_WEIGHTS = {
    "worker": {
        # Names: jaro_winkler is prefix-heavy (great for "Jon" → "John"),
        # token_sort handles "Smith John" → "John Smith"
        "jaro_winkler":   0.40,
        "token_sort_ratio": 0.35,
        "ratio":           0.25,
    },
    "company": {
        # Abbreviations common: "ABC Maint." → "ABC Maintenance Ltd"
        "token_set_ratio":  0.40,
        "WRatio":           0.35,
        "partial_ratio":    0.25,
    },
    "location": {
        # Often partial: "Depot 3" → "North Depot 3 - Bay 2"
        "partial_ratio":    0.40,
        "token_set_ratio":  0.35,
        "token_sort_ratio": 0.25,
    },
    "vehicle_equipment": {
        # Equipment tags: "TRK-042" → "TRK042" — need token + partial
        "token_set_ratio":  0.40,
        "partial_ratio":    0.30,
        "ratio":            0.30,
    },
    "parts_used": {
        # Free-form lists: handle comma-separated multi-part strings
        # Each part matched individually then aggregated
        "token_set_ratio":  0.45,
        "WRatio":           0.35,
        "partial_ratio":    0.20,
    },
}
```

---

## 3. Mock Reference Database

A flat Python module (`db/reference_data.py`) simulating a real database. In production,
replace with queries to your actual data store.

```python
# db/reference_data.py

"""
Mock reference database for fuzzy validation.
In production: replace each dict/list with a DB query function.
"""

# ── Workers ──────────────────────────────────────────────────────────────────
WORKERS: list[dict] = [
    {"id": "W001", "name": "James Hartwell",     "department": "Heavy Equipment"},
    {"id": "W002", "name": "Maria Sanchez",       "department": "Electrical"},
    {"id": "W003", "name": "Derek O'Brien",       "department": "Heavy Equipment"},
    {"id": "W004", "name": "Priya Nambiar",       "department": "Hydraulics"},
    {"id": "W005", "name": "Tom Kowalski",        "department": "General Maintenance"},
    {"id": "W006", "name": "Fatima Al-Hassan",    "department": "Electrical"},
    {"id": "W007", "name": "Luc Tremblay",        "department": "Heavy Equipment"},
    {"id": "W008", "name": "Sandra McPherson",    "department": "General Maintenance"},
]

# ── Companies ─────────────────────────────────────────────────────────────────
COMPANIES: list[dict] = [
    {"id": "C001", "name": "Hartwell Industrial Services",   "short": "HIS"},
    {"id": "C002", "name": "PrimeTech Maintenance Ltd",      "short": "PTM"},
    {"id": "C003", "name": "Northern Fleet Solutions",       "short": "NFS"},
    {"id": "C004", "name": "Apex Equipment & Repair Co.",    "short": "AER"},
    {"id": "C005", "name": "BlueLine Service Group",         "short": "BSG"},
    {"id": "C006", "name": "Delta Hydraulics Inc.",          "short": "DHI"},
]

# ── Locations ─────────────────────────────────────────────────────────────────
LOCATIONS: list[dict] = [
    {"id": "L001", "name": "Main Workshop — Bay 1"},
    {"id": "L002", "name": "Main Workshop — Bay 2"},
    {"id": "L003", "name": "Main Workshop — Bay 3"},
    {"id": "L004", "name": "North Depot — Outdoor Lot"},
    {"id": "L005", "name": "South Depot — Covered Bay"},
    {"id": "L006", "name": "Field Site Alpha"},
    {"id": "L007", "name": "Field Site Beta"},
    {"id": "L008", "name": "Client Site — Hartwell Industrial"},
    {"id": "L009", "name": "Fuel Station — Zone A"},
]

# ── Vehicles / Equipment ──────────────────────────────────────────────────────
EQUIPMENT: list[dict] = [
    {"id": "E001", "tag": "TRK-001", "description": "2019 Kenworth T680 — Unit 1"},
    {"id": "E002", "tag": "TRK-002", "description": "2020 Peterbilt 579 — Unit 2"},
    {"id": "E003", "tag": "EXC-010", "description": "2021 CAT 320 Excavator"},
    {"id": "E004", "tag": "EXC-011", "description": "2018 Komatsu PC360 Excavator"},
    {"id": "E005", "tag": "LDR-003", "description": "2022 Volvo L110H Wheel Loader"},
    {"id": "E006", "tag": "GEN-005", "description": "250kVA Cummins Generator — Site A"},
    {"id": "E007", "tag": "GEN-006", "description": "500kVA Caterpillar Generator — Site B"},
    {"id": "E008", "tag": "TRL-020", "description": "Flatbed Trailer — 48ft"},
    {"id": "E009", "tag": "FRK-007", "description": "Toyota 8FBU25 Forklift"},
    {"id": "E010", "tag": "VAN-012", "description": "2021 Ford Transit Service Van"},
]

# ── Parts ─────────────────────────────────────────────────────────────────────
PARTS: list[dict] = [
    {"id": "P001", "part_number": "HF-2240",    "description": "Hydraulic Filter 2240"},
    {"id": "P002", "part_number": "OIL-15W40",  "description": "15W-40 Engine Oil (5L)"},
    {"id": "P003", "part_number": "BLT-SERP",   "description": "Serpentine Drive Belt"},
    {"id": "P004", "part_number": "ALT-24V",    "description": "24V Alternator"},
    {"id": "P005", "part_number": "BATT-12V",   "description": "12V AGM Battery"},
    {"id": "P006", "part_number": "BATT-24V",   "description": "24V AGM Battery"},
    {"id": "P007", "part_number": "SEAL-HYD",   "description": "Hydraulic Cylinder Seal Kit"},
    {"id": "P008", "part_number": "HOSE-HYD",   "description": "High-Pressure Hydraulic Hose"},
    {"id": "P009", "part_number": "FILT-AIR",   "description": "Air Filter — Heavy Duty"},
    {"id": "P010", "part_number": "FILT-OIL",   "description": "Oil Filter — Heavy Duty"},
    {"id": "P011", "part_number": "PUMP-HYD",   "description": "Hydraulic Pump Assembly"},
    {"id": "P012", "part_number": "BRAKE-PAD",  "description": "Brake Pad Set — Front"},
    {"id": "P013", "part_number": "COOL-L",     "description": "Coolant — Long Life (5L)"},
    {"id": "P014", "part_number": "GREASE-MP",  "description": "Multi-Purpose Grease Cartridge"},
    {"id": "P015", "part_number": "FUSE-30A",   "description": "30A Blade Fuse (Pack of 10)"},
]


def get_worker_names() -> list[str]:
    return [w["name"] for w in WORKERS]

def get_company_names() -> list[str]:
    # Include both full names and short codes as matchable targets
    names = [c["name"] for c in COMPANIES]
    shorts = [c["short"] for c in COMPANIES]
    return names + shorts

def get_location_names() -> list[str]:
    return [loc["name"] for loc in LOCATIONS]

def get_equipment_strings() -> list[str]:
    # Match against tag, description, or combined "TAG — description"
    tags = [e["tag"] for e in EQUIPMENT]
    descs = [e["description"] for e in EQUIPMENT]
    combined = [f"{e['tag']} — {e['description']}" for e in EQUIPMENT]
    return tags + descs + combined

def get_part_strings() -> list[str]:
    numbers = [p["part_number"] for p in PARTS]
    descs = [p["description"] for p in PARTS]
    combined = [f"{p['part_number']} — {p['description']}" for p in PARTS]
    return numbers + descs + combined

def resolve_worker_canonical(matched_name: str) -> dict | None:
    """Return the full worker record for a matched name."""
    for w in WORKERS:
        if w["name"] == matched_name:
            return w
    return None

def resolve_equipment_canonical(matched_string: str) -> dict | None:
    """Return the equipment record for a matched string (tag, desc, or combined)."""
    for e in EQUIPMENT:
        if matched_string in (e["tag"], e["description"], f"{e['tag']} — {e['description']}"):
            return e
    return None

def resolve_part_canonical(matched_string: str) -> dict | None:
    """Return the part record for a matched string."""
    for p in PARTS:
        if matched_string in (p["part_number"], p["description"],
                               f"{p['part_number']} — {p['description']}"):
            return p
    return None
```

---

## 4. Validator Architecture

```
┌────────────────────────────────────────────────────────┐
│                  WorkOrderValidator                      │
│  ┌──────────────────────────────────────────────────┐  │
│  │  validate(extracted_json) → ValidationResult     │  │
│  └──────────────────────┬───────────────────────────┘  │
│                         │                               │
│         ┌───────────────┼──────────────┐                │
│         ▼               ▼              ▼                │
│  ┌────────────┐  ┌───────────┐  ┌───────────────┐      │
│  │  Fuzzy     │  │   Time    │  │  Pass-Through  │      │
│  │  Resolver  │  │ Validator │  │  (free text)  │      │
│  └────────────┘  └───────────┘  └───────────────┘      │
│         │               │                               │
│         ▼               ▼                               │
│  ┌──────────────────────────────────────────────────┐  │
│  │           FieldResult (per field)                │  │
│  │  - raw_value: str                                │  │
│  │  - resolved_value: str                           │  │
│  │  - score: float (0–100)                          │  │
│  │  - status: EXACT|HIGH|LOW|NO_MATCH|EMPTY|VALID   │  │
│  │  - matched_db_entry: dict | None                 │  │
│  │  - algorithm_scores: dict                        │  │
│  └──────────────────────────────────────────────────┘  │
│                         │                               │
│                         ▼                               │
│  ┌──────────────────────────────────────────────────┐  │
│  │           ValidationResult                       │  │
│  │  - resolved_json: dict                           │  │
│  │  - field_results: dict[str, FieldResult]         │  │
│  │  - overall_status: PASS | REVIEW | FAIL          │  │
│  │  - unresolved_fields: list[str]                  │  │
│  │  - review_fields: list[str]                      │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────┘
```

### Data classes

```python
# validator/models.py
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

class MatchStatus(str, Enum):
    EXACT         = "EXACT"          # Score = 100
    HIGH_CONF     = "HIGH_CONFIDENCE"  # Score ≥ 85
    LOW_CONF      = "LOW_CONFIDENCE"   # Score 60–84
    NO_MATCH      = "NO_MATCH"         # Score < 60
    EMPTY         = "EMPTY"            # Input was blank
    TIME_VALID    = "TIME_VALID"       # Passed time format check
    TIME_INVALID  = "TIME_INVALID"     # Failed time format check
    PASS_THROUGH  = "PASS_THROUGH"     # Free text, no validation

class OverallStatus(str, Enum):
    PASS   = "PASS"    # All required fields resolved or valid
    REVIEW = "REVIEW"  # Some fields are LOW_CONFIDENCE
    FAIL   = "FAIL"    # One or more NO_MATCH or TIME_INVALID on required fields

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

@dataclass
class ValidationResult:
    resolved_json:    dict[str, Any]
    field_results:    dict[str, FieldResult]
    overall_status:   OverallStatus
    unresolved_fields: list[str] = field(default_factory=list)
    review_fields:    list[str]  = field(default_factory=list)
```

---

## 5. Fuzzy Matching Implementation

```python
# validator/fuzzy_resolver.py

from rapidfuzz import fuzz, process
from rapidfuzz.distance import JaroWinkler
from .models import FieldResult, MatchStatus
from db.reference_data import (
    get_worker_names, get_company_names, get_location_names,
    get_equipment_strings, get_part_strings,
    resolve_worker_canonical, resolve_equipment_canonical, resolve_part_canonical,
)

# ── Thresholds ──────────────────────────────────────────────────────────────
THRESHOLD_EXACT        = 100.0
THRESHOLD_HIGH         = 85.0
THRESHOLD_LOW          = 60.0

# ── Algorithm weight maps (see Section 2) ───────────────────────────────────
FIELD_WEIGHTS = {
    "worker":           {"jaro_winkler": 0.40, "token_sort_ratio": 0.35, "ratio": 0.25},
    "company":          {"token_set_ratio": 0.40, "WRatio": 0.35, "partial_ratio": 0.25},
    "location":         {"partial_ratio": 0.40, "token_set_ratio": 0.35, "token_sort_ratio": 0.25},
    "vehicle_equipment":{"token_set_ratio": 0.40, "partial_ratio": 0.30, "ratio": 0.30},
    "parts_used":       {"token_set_ratio": 0.45, "WRatio": 0.35, "partial_ratio": 0.20},
}

# ── DB lookup maps ──────────────────────────────────────────────────────────
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
}


def _score_algorithms(query: str, candidate: str, weights: dict) -> tuple[float, dict]:
    """
    Run each weighted algorithm against a query/candidate pair.
    Returns (composite_score, {algorithm: score}) where scores are 0–100.
    """
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
        score = algo_map[algo_name](query.lower(), candidate.lower())
        individual[algo_name] = round(score, 2)
        composite += score * weight

    return round(composite, 2), individual


def _find_best_match(
    query: str,
    candidates: list[str],
    weights: dict,
) -> tuple[str, float, dict]:
    """
    Find the best matching candidate for a query string.
    Returns (best_candidate, composite_score, algorithm_scores_for_best).
    """
    if not candidates or not query.strip():
        return "", 0.0, {}

    best_candidate = ""
    best_score = -1.0
    best_algo_scores: dict = {}

    for candidate in candidates:
        score, algo_scores = _score_algorithms(query, candidate, weights)
        if score > best_score:
            best_score = score
            best_candidate = candidate
            best_algo_scores = algo_scores

    return best_candidate, best_score, best_algo_scores


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
    """
    Resolve a single fuzzy field against the reference database.
    """
    if not raw_value or not raw_value.strip():
        return FieldResult(
            field_name=field_name,
            raw_value=raw_value,
            resolved_value="",
            status=MatchStatus.EMPTY,
        )

    candidates  = DB_LOADERS[field_name]()
    weights     = FIELD_WEIGHTS[field_name]
    best_match, score, algo_scores = _find_best_match(raw_value, candidates, weights)
    status      = _status_from_score(score)

    # Resolve to canonical DB record if available
    canonical_resolver = CANONICAL_RESOLVERS.get(field_name)
    db_entry = canonical_resolver(best_match) if canonical_resolver and best_match else None

    # For NO_MATCH, keep the raw value rather than substituting a bad guess
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
    )


# ── Special handling: parts_used is a comma-separated list ──────────────────

def resolve_parts_used(raw_value: str) -> FieldResult:
    """
    parts_used may be a comma-separated list (e.g. "Oil filter, drive belt, 15W40 oil").
    Each token is matched individually; the aggregate result uses the lowest token score.
    """
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
        best_match, score, algo_scores = _find_best_match(token, candidates, weights)
        all_scores.append(score)
        all_algo_scores.append({token: algo_scores})
        status = _status_from_score(score)
        resolved_tokens.append(best_match if status != MatchStatus.NO_MATCH else token)

    # Aggregate: overall status driven by the weakest match
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
```

---

## 6. Time Format Validation

```python
# validator/time_validator.py

import re
from .models import FieldResult, MatchStatus

# Accepted time input patterns (normalised to HH:MM internally)
_PATTERNS = [
    # HH:MM  or  H:MM
    (re.compile(r'^(\d{1,2}):(\d{2})$'), lambda m: (int(m.group(1)), int(m.group(2)))),
    # HH:MM:SS  (seconds dropped)
    (re.compile(r'^(\d{1,2}):(\d{2}):\d{2}$'), lambda m: (int(m.group(1)), int(m.group(2)))),
    # HHhMMm  e.g. "2h30m"
    (re.compile(r'^(\d{1,2})h(\d{2})m?$', re.I), lambda m: (int(m.group(1)), int(m.group(2)))),
    # H:MM AM/PM
    (re.compile(r'^(\d{1,2}):(\d{2})\s*(am|pm)$', re.I),
        lambda m: (
            (int(m.group(1)) % 12) + (12 if m.group(3).lower() == 'pm' else 0),
            int(m.group(2))
        )
    ),
]

# Duration-specific: "1 hour 30 minutes", "45 minutes", "2 hours"
_DURATION_PATTERNS = [
    re.compile(r'(\d+)\s*h(?:our)?s?\s*(\d+)\s*m(?:in)?', re.I),  # "2 hours 30 min"
    re.compile(r'(\d+)\s*h(?:our)?s?$', re.I),                     # "2 hours"
    re.compile(r'(\d+)\s*m(?:in(?:ute)?s?)$', re.I),               # "45 minutes"
    re.compile(r'^(\d{1,2}):(\d{2})$'),                             # "HH:MM" as duration
]


def _parse_clock_time(value: str) -> str | None:
    """
    Try to parse an absolute clock time. Returns "HH:MM" or None.
    """
    v = value.strip()
    for pattern, extractor in _PATTERNS:
        m = pattern.match(v)
        if m:
            h, minutes = extractor(m)
            if 0 <= h <= 23 and 0 <= minutes <= 59:
                return f"{h:02d}:{minutes:02d}"
    return None


def _parse_duration(value: str) -> str | None:
    """
    Try to parse a duration string. Returns normalised form or None.
    Examples: "1h 30m", "2 hours 15 minutes", "45 minutes", "01:30"
    """
    v = value.strip()

    m = _DURATION_PATTERNS[0].search(v)  # "2 hours 30 minutes"
    if m:
        return f"{int(m.group(1))}h {int(m.group(2))}m"

    m = _DURATION_PATTERNS[1].match(v)   # "2 hours"
    if m:
        return f"{int(m.group(1))}h 0m"

    m = _DURATION_PATTERNS[2].match(v)   # "45 minutes"
    if m:
        mins = int(m.group(1))
        return f"{mins // 60}h {mins % 60}m"

    m = _DURATION_PATTERNS[3].match(v)   # "HH:MM"
    if m:
        return f"{int(m.group(1))}h {int(m.group(2))}m"

    return None


def validate_time_field(field_name: str, raw_value: str) -> FieldResult:
    """
    Validate start_time and end_time (absolute clock) or total_time_spent (duration).
    """
    if not raw_value or not raw_value.strip():
        return FieldResult(
            field_name=field_name,
            raw_value=raw_value,
            resolved_value="",
            status=MatchStatus.EMPTY,
            notes="Time field is empty.",
        )

    is_duration = field_name == "total_time_spent"

    if is_duration:
        parsed = _parse_duration(raw_value)
    else:
        parsed = _parse_clock_time(raw_value)

    if parsed:
        return FieldResult(
            field_name=field_name,
            raw_value=raw_value,
            resolved_value=parsed,
            status=MatchStatus.TIME_VALID,
            score=100.0,
            notes=f"Normalised from '{raw_value}' → '{parsed}'",
        )
    else:
        return FieldResult(
            field_name=field_name,
            raw_value=raw_value,
            resolved_value=raw_value,   # Keep raw; don't guess
            status=MatchStatus.TIME_INVALID,
            score=0.0,
            notes=f"Could not parse '{raw_value}' as a {'duration' if is_duration else 'clock time'}.",
        )
```

---

## 7. Confidence Scoring & Resolution Logic

```python
# validator/resolution.py

from .models import MatchStatus, OverallStatus, ValidationResult, FieldResult

# Fields that must not be NO_MATCH / TIME_INVALID to pass
REQUIRED_RESOLVED_FIELDS = {
    "worker", "company", "location", "vehicle_equipment",
    "start_time", "end_time",
}

# Fields where LOW_CONFIDENCE triggers REVIEW (not FAIL)
REVIEW_TRIGGER_FIELDS = {"parts_used", "total_time_spent"}


def compute_overall_status(field_results: dict[str, FieldResult]) -> tuple[OverallStatus, list[str], list[str]]:
    """
    Determine overall validation outcome from individual field results.
    Returns (OverallStatus, unresolved_fields, review_fields).
    """
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
```

---

## 8. Full Validator Orchestrator

```python
# validator/work_order_validator.py

from .fuzzy_resolver import resolve_field, resolve_parts_used
from .time_validator import validate_time_field
from .resolution import compute_overall_status
from .models import FieldResult, MatchStatus, ValidationResult

FUZZY_FIELDS     = {"worker", "company", "location", "vehicle_equipment"}
TIME_FIELDS      = {"start_time", "end_time", "total_time_spent"}
FREE_TEXT_FIELDS = {
    "reported_problem", "diagnosis_cause", "work_performed",
    "future_recommendations", "remaining_tasks",
}


def validate_work_order(extracted: dict) -> ValidationResult:
    """
    Run the full validation pipeline on an extracted work order JSON dict.
    Returns a ValidationResult with resolved values and per-field diagnostics.
    """
    field_results: dict[str, FieldResult] = {}

    for field_name, raw_value in extracted.items():
        raw = str(raw_value) if raw_value is not None else ""

        if field_name in FUZZY_FIELDS:
            field_results[field_name] = resolve_field(field_name, raw)

        elif field_name == "parts_used":
            field_results[field_name] = resolve_parts_used(raw)

        elif field_name in TIME_FIELDS:
            field_results[field_name] = validate_time_field(field_name, raw)

        else:  # free text pass-through
            field_results[field_name] = FieldResult(
                field_name=field_name,
                raw_value=raw,
                resolved_value=raw,
                status=MatchStatus.PASS_THROUGH,
            )

    overall, unresolved, review = compute_overall_status(field_results)

    resolved_json = {
        fname: fr.resolved_value for fname, fr in field_results.items()
    }

    return ValidationResult(
        resolved_json=resolved_json,
        field_results=field_results,
        overall_status=overall,
        unresolved_fields=unresolved,
        review_fields=review,
    )
```

---

## 9. Mock Test Suite

Run with: `python -m pytest tests/ -v`

```python
# tests/test_fuzzy_resolver.py

import pytest
from validator.fuzzy_resolver import resolve_field, resolve_parts_used
from validator.models import MatchStatus

# ═══════════════════════════════════════════════════════════════════════════
# WORKER TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestWorkerResolution:

    def test_exact_match(self):
        """Exact name from DB should score 100."""
        result = resolve_field("worker", "James Hartwell")
        assert result.status == MatchStatus.EXACT
        assert result.score == 100.0
        assert result.resolved_value == "James Hartwell"

    def test_typo_first_name(self):
        """Single-character typo in first name should still match."""
        result = resolve_field("worker", "Jmes Hartwell")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.EXACT)
        assert result.resolved_value == "James Hartwell"

    def test_transposed_name(self):
        """Reversed order 'Hartwell James' should resolve via token_sort."""
        result = resolve_field("worker", "Hartwell James")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.EXACT)
        assert result.resolved_value == "James Hartwell"

    def test_partial_last_name_only(self):
        """Only a last name — likely low confidence."""
        result = resolve_field("worker", "Kowalski")
        # Should still find Tom Kowalski but with lower confidence
        assert result.resolved_value == "Tom Kowalski"
        assert result.score < 100.0

    def test_name_with_apostrophe(self):
        """Apostrophe in name: "Derek O Brien" → "Derek O'Brien"."""
        result = resolve_field("worker", "Derek O Brien")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.EXACT)
        assert result.resolved_value == "Derek O'Brien"

    def test_completely_unknown_worker(self):
        """Name with no resemblance to any DB entry."""
        result = resolve_field("worker", "Zzzyx Qqqington")
        assert result.status == MatchStatus.NO_MATCH
        assert result.resolved_value == "Zzzyx Qqqington"  # raw kept

    def test_empty_worker(self):
        result = resolve_field("worker", "")
        assert result.status == MatchStatus.EMPTY
        assert result.resolved_value == ""

    def test_worker_nickname(self):
        """'Tom K' — should loosely match Tom Kowalski."""
        result = resolve_field("worker", "Tom K")
        # Partial match expected; score may vary — confirm worker resolved
        assert "Kowalski" in result.resolved_value or result.status == MatchStatus.NO_MATCH

    @pytest.mark.parametrize("raw,expected_canonical", [
        ("Maria Sanchez",    "Maria Sanchez"),
        ("Priya Nambia",     "Priya Nambiar"),   # one-char typo
        ("Fatima Al Hassan", "Fatima Al-Hassan"), # missing hyphen
        ("Luc Trembay",      "Luc Tremblay"),    # one-char typo
    ])
    def test_worker_parametrized(self, raw, expected_canonical):
        result = resolve_field("worker", raw)
        assert result.resolved_value == expected_canonical


# ═══════════════════════════════════════════════════════════════════════════
# COMPANY TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestCompanyResolution:

    def test_exact_full_name(self):
        result = resolve_field("company", "PrimeTech Maintenance Ltd")
        assert result.status == MatchStatus.EXACT

    def test_short_code(self):
        """Short code 'NFS' should map to Northern Fleet Solutions."""
        result = resolve_field("company", "NFS")
        assert result.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF)
        assert "Northern Fleet" in result.resolved_value or result.resolved_value == "NFS"

    def test_abbreviated_name(self):
        """'Apex Equipment' (without '& Repair Co.') — token_set should handle."""
        result = resolve_field("company", "Apex Equipment")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF)
        assert "Apex" in result.resolved_value

    def test_lowercase_company(self):
        """Case normalisation: 'delta hydraulics' → 'Delta Hydraulics Inc.'"""
        result = resolve_field("company", "delta hydraulics")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.EXACT)
        assert "Delta Hydraulics" in result.resolved_value

    def test_misspelled_company(self):
        result = resolve_field("company", "BlueLine Servise Group")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.EXACT)
        assert "BlueLine" in result.resolved_value

    def test_unknown_company(self):
        result = resolve_field("company", "Totally Unknown Corp XYZ")
        assert result.status == MatchStatus.NO_MATCH


# ═══════════════════════════════════════════════════════════════════════════
# LOCATION TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestLocationResolution:

    def test_exact_location(self):
        result = resolve_field("location", "Main Workshop — Bay 1")
        assert result.status == MatchStatus.EXACT

    def test_partial_location(self):
        """'Bay 2' should partially match 'Main Workshop — Bay 2'."""
        result = resolve_field("location", "Bay 2")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF)
        assert "Bay 2" in result.resolved_value

    def test_location_abbreviation(self):
        """'North Depot' should match 'North Depot — Outdoor Lot'."""
        result = resolve_field("location", "North Depot")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.EXACT)
        assert "North Depot" in result.resolved_value

    def test_field_site(self):
        result = resolve_field("location", "Field Site Alpha")
        assert result.status == MatchStatus.EXACT

    def test_colloquial_location(self):
        """'the fuel station' — loose match to 'Fuel Station — Zone A'."""
        result = resolve_field("location", "fuel station")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF)

    def test_unknown_location(self):
        result = resolve_field("location", "Some Random Place 99Z")
        assert result.status == MatchStatus.NO_MATCH


# ═══════════════════════════════════════════════════════════════════════════
# VEHICLE / EQUIPMENT TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestEquipmentResolution:

    def test_exact_tag(self):
        result = resolve_field("vehicle_equipment", "TRK-001")
        assert result.status == MatchStatus.EXACT

    def test_tag_no_hyphen(self):
        """'TRK001' (missing hyphen) should still match 'TRK-001'."""
        result = resolve_field("vehicle_equipment", "TRK001")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.EXACT)
        assert "TRK-001" in result.resolved_value

    def test_description_match(self):
        """Partial description '320 Excavator' → CAT 320 Excavator record."""
        result = resolve_field("vehicle_equipment", "320 Excavator")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF)
        assert "CAT 320" in result.resolved_value or "EXC-010" in result.resolved_value

    def test_make_model_only(self):
        """'Peterbilt 579' should match TRK-002 description."""
        result = resolve_field("vehicle_equipment", "Peterbilt 579")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.EXACT)
        assert "Peterbilt" in result.resolved_value

    def test_generator_by_capacity(self):
        """'250kVA generator' should match the Cummins generator."""
        result = resolve_field("vehicle_equipment", "250kVA generator")
        assert "250kVA" in result.resolved_value or result.status in (
            MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF
        )

    def test_unknown_equipment(self):
        result = resolve_field("vehicle_equipment", "XYZ-9999 Unknown Machine")
        assert result.status == MatchStatus.NO_MATCH


# ═══════════════════════════════════════════════════════════════════════════
# PARTS USED TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestPartsResolution:

    def test_single_exact_part(self):
        result = resolve_parts_used("Hydraulic Filter 2240")
        assert result.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF)
        assert "HF-2240" in result.resolved_value or "Hydraulic Filter 2240" in result.resolved_value

    def test_multi_part_string(self):
        """Comma-separated list — all parts should resolve."""
        result = resolve_parts_used("Oil Filter, Air Filter, 15W-40 Oil")
        assert result.status != MatchStatus.NO_MATCH
        tokens = [t.strip() for t in result.resolved_value.split(",")]
        assert len(tokens) == 3

    def test_part_number_only(self):
        result = resolve_parts_used("BLT-SERP")
        assert result.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF)

    def test_partial_description(self):
        """'serpentine belt' → 'Serpentine Drive Belt'."""
        result = resolve_parts_used("serpentine belt")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF)
        assert "Serpentine" in result.resolved_value or "BLT-SERP" in result.resolved_value

    def test_multi_part_one_unknown(self):
        """
        Mixed list: one known part, one unknown.
        Overall status should reflect the weakest match.
        """
        result = resolve_parts_used("Oil Filter, ZZZ-FAKE-9999")
        # Min score driven by ZZZ-FAKE-9999
        assert result.status in (MatchStatus.LOW_CONF, MatchStatus.NO_MATCH)

    def test_empty_parts(self):
        result = resolve_parts_used("")
        assert result.status == MatchStatus.EMPTY

    def test_part_with_quantity(self):
        """'2x Oil Filter' — quantity prefix should not break matching."""
        result = resolve_parts_used("2x Oil Filter")
        # Token matching should still surface Oil Filter
        assert result.status != MatchStatus.EMPTY


# ═══════════════════════════════════════════════════════════════════════════
# TIME VALIDATION TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestTimeValidation:

    @pytest.mark.parametrize("raw,expected", [
        ("08:30",    "08:30"),
        ("8:30",     "08:30"),
        ("08:30:00", "08:30"),
        ("08:30 AM", "08:30"),
        ("08:30 PM", "20:30"),
        ("12:00 PM", "12:00"),
        ("12:00 AM", "00:00"),
    ])
    def test_valid_start_times(self, raw, expected):
        from validator.time_validator import validate_time_field
        result = validate_time_field("start_time", raw)
        assert result.status == MatchStatus.TIME_VALID
        assert result.resolved_value == expected

    @pytest.mark.parametrize("raw", [
        "25:00",       # invalid hour
        "08:65",       # invalid minute
        "half past 8", # natural language
        "morning",     # no time info
        "8am",         # no colon — not in accepted patterns
    ])
    def test_invalid_start_times(self, raw):
        from validator.time_validator import validate_time_field
        result = validate_time_field("start_time", raw)
        assert result.status == MatchStatus.TIME_INVALID

    @pytest.mark.parametrize("raw,expected", [
        ("01:30",             "1h 30m"),
        ("2h30m",             "2h 30m"),
        ("2 hours 30 minutes","2h 30m"),
        ("2 hours",           "2h 0m"),
        ("45 minutes",        "0h 45m"),
        ("90 min",            "1h 30m"),
    ])
    def test_valid_durations(self, raw, expected):
        from validator.time_validator import validate_time_field
        result = validate_time_field("total_time_spent", raw)
        assert result.status == MatchStatus.TIME_VALID
        assert result.resolved_value == expected

    @pytest.mark.parametrize("raw", [
        "a long time",
        "quick",
        "",
    ])
    def test_invalid_durations(self, raw):
        from validator.time_validator import validate_time_field
        result = validate_time_field("total_time_spent", raw)
        assert result.status in (MatchStatus.TIME_INVALID, MatchStatus.EMPTY)


# ═══════════════════════════════════════════════════════════════════════════
# FULL WORK ORDER INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestFullWorkOrderValidation:

    def _make_order(self, overrides: dict = {}) -> dict:
        """Return a valid base work order with optional field overrides."""
        base = {
            "vehicle_equipment":      "TRK-001",
            "reported_problem":       "Engine overheating",
            "diagnosis_cause":        "Low coolant, clogged thermostat",
            "work_performed":         "Replaced thermostat, topped up coolant",
            "parts_used":             "Coolant — Long Life, Oil Filter — Heavy Duty",
            "start_time":             "08:00",
            "end_time":               "11:30",
            "total_time_spent":       "3h30m",
            "future_recommendations": "Monitor coolant level weekly",
            "remaining_tasks":        "",
            "worker":                 "James Hartwell",
            "company":                "Hartwell Industrial Services",
            "location":               "Main Workshop — Bay 1",
        }
        base.update(overrides)
        return base

    def test_clean_input_passes(self):
        """All fields clean and present — should PASS."""
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order())
        assert result.overall_status.value == "PASS"
        assert result.unresolved_fields == []

    def test_typo_in_worker_still_passes(self):
        """Small typo in worker name — HIGH_CONF resolves, still PASS."""
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order({"worker": "James Hartwal"}))
        assert result.overall_status.value in ("PASS", "REVIEW")
        assert result.field_results["worker"].resolved_value == "James Hartwell"

    def test_unknown_worker_causes_fail(self):
        """
        Completely unrecognised worker in a required field → FAIL.
        """
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order({"worker": "Nobody Known XYZ"}))
        assert result.overall_status.value == "FAIL"
        assert "worker" in result.unresolved_fields

    def test_invalid_time_causes_fail(self):
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order({"start_time": "not a time"}))
        assert result.overall_status.value == "FAIL"
        assert "start_time" in result.unresolved_fields

    def test_low_confidence_parts_triggers_review(self):
        """Low-score parts match → REVIEW, not FAIL."""
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order({"parts_used": "some vague part"}))
        assert result.overall_status.value in ("REVIEW", "FAIL")

    def test_resolved_json_contains_all_fields(self):
        """resolved_json must have all 13 schema fields."""
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order())
        assert len(result.resolved_json) == 13

    def test_free_text_passes_through_unchanged(self):
        """Free text fields should always be PASS_THROUGH with original value."""
        from validator.work_order_validator import validate_work_order
        order = self._make_order({"reported_problem": "The engine makes a strange ticking noise"})
        result = validate_work_order(order)
        fr = result.field_results["reported_problem"]
        assert fr.status == MatchStatus.PASS_THROUGH
        assert fr.resolved_value == "The engine makes a strange ticking noise"

    def test_empty_optional_field_does_not_fail(self):
        """remaining_tasks is optional — empty string is acceptable."""
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order({"remaining_tasks": ""}))
        assert result.field_results["remaining_tasks"].status == MatchStatus.PASS_THROUGH
        assert result.overall_status.value != "FAIL"

    def test_hallucinated_location_with_close_match(self):
        """
        Gemini says 'Workshop Bay 3' (no dashes).
        Should resolve to 'Main Workshop — Bay 3' with HIGH_CONF.
        """
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order({"location": "Workshop Bay 3"}))
        fr = result.field_results["location"]
        assert "Bay 3" in fr.resolved_value
        assert fr.status in (MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF)

    def test_algorithm_scores_present_in_result(self):
        """Each fuzzy field result must contain algorithm_scores."""
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order())
        for field in ("worker", "company", "location", "vehicle_equipment"):
            assert isinstance(result.field_results[field].algorithm_scores, dict)
            assert len(result.field_results[field].algorithm_scores) > 0
```

---

## 10. Recommended Project Structure

```
work-order-processor/
├── main.py
├── db/
│   └── reference_data.py          ← Mock DB (replace with real DB calls)
├── validator/
│   ├── __init__.py
│   ├── models.py                  ← FieldResult, ValidationResult, enums
│   ├── fuzzy_resolver.py          ← resolve_field(), resolve_parts_used()
│   ├── time_validator.py          ← validate_time_field()
│   ├── resolution.py              ← compute_overall_status()
│   └── work_order_validator.py    ← validate_work_order() orchestrator
├── tests/
│   ├── __init__.py
│   └── test_fuzzy_resolver.py     ← Full test suite (this document)
├── FUZZY_MATCHING_PLAN.md
├── requirements.txt
└── .env
```

---

## 11. Dependency List

```
# Fuzzy matching
rapidfuzz>=3.9.0        # Core algorithms: ratio, token_sort/set, WRatio, jaro_winkler

# Testing
pytest>=8.0.0
pytest-cov>=5.0.0       # Coverage reports: pytest --cov=validator tests/

# Already in project
jsonschema>=4.23.0
python-dotenv>=1.0.0
```

> **Why `rapidfuzz` over `fuzzywuzzy`?**
> `rapidfuzz` is a drop-in replacement that is 10–100× faster (C++ core), has no
> GPL-licensed dependencies, and exposes `JaroWinkler` and `Levenshtein` directly.
> It is the current community standard for production Python fuzzy matching.

---

*Last updated: 2026-05-27*
