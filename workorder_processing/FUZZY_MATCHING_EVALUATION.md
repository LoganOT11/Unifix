# Fuzzy Matching Evaluation Report

**Date:** 2026-05-27
**Scope:** `validator/` package — fuzzy resolver, time validator, work order validator, resolution logic
**Test suite:** `tests/test_edge_cases.py` — 159 tests, all passing

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Strengths](#2-strengths)
3. [Bugs Found](#3-bugs-found)
4. [Edge Cases by Category](#4-edge-cases-by-category)
5. [Test Data Files](#5-test-data-files)
6. [Full Pipeline Demo](#6-full-pipeline-demo)
7. [Recommendations](#7-recommendations)

---

## 1. Architecture Overview

The validation pipeline runs after Gemini extracts structured JSON from audio:

```
Gemini output dict
    │
    ├─ worker, company, location, vehicle_equipment  →  fuzzy_resolver.resolve_field()
    ├─ parts_used                                     →  fuzzy_resolver.resolve_parts_used()
    ├─ start_time, end_time, total_time_spent         →  time_validator.validate_time_field()
    └─ reported_problem, diagnosis_cause, …            →  PASS_THROUGH (free text)
    │
    ▼
resolution.compute_overall_status()  →  PASS / REVIEW / FAIL
```

### Scoring model

Each fuzzy field uses a **weighted ensemble** of rapidfuzz algorithms:

| Field | Algorithms (weights) |
|---|---|
| `worker` | Jaro-Winkler (0.40), token_sort_ratio (0.35), ratio (0.25) |
| `company` | token_set_ratio (0.40), WRatio (0.35), partial_ratio (0.25) |
| `location` | partial_ratio (0.40), token_set_ratio (0.35), token_sort_ratio (0.25) |
| `vehicle_equipment` | token_set_ratio (0.40), partial_ratio (0.30), ratio (0.30) |
| `parts_used` | token_set_ratio (0.45), WRatio (0.35), partial_ratio (0.20) |

### Thresholds

| Score range | Status | Resolution action |
|---|---|---|
| ≥ 100 | `EXACT` | Use matched DB value |
| ≥ 85 | `HIGH_CONFIDENCE` | Use matched DB value |
| 60–84 | `LOW_CONFIDENCE` | Use matched DB value, flag for review |
| < 60 | `NO_MATCH` | Use raw value, flag as unresolved |

### Overall status logic

- **FAIL** — Any required field has `NO_MATCH` or `TIME_INVALID`
- **REVIEW** — Any `LOW_CONFIDENCE` field, or `NO_MATCH` on optional fields
- **PASS** — Everything resolved at `HIGH_CONFIDENCE` or better

---

## 2. Strengths

### 2.1 Algorithm ensemble is well-tuned

The multi-algorithm approach handles a wide variety of input distortions. No single algorithm dominates — each contributes to robustness:

```
"Derek O'Brien" vs "Derek OBrien"
  jaro_winkler:      97.0
  token_sort_ratio: 100.0
  ratio:             94.7
  composite:         97.0  →  HIGH_CONFIDENCE
```

### 2.2 Case normalization

All comparisons use `.lower()`, so case never affects matching:

| Input | Status | Score |
|---|---|---|
| `JAMES HARTWELL` | EXACT | 100.0 |
| `james hartwell` | EXACT | 100.0 |
| `jAmEs HaRtWeLl` | EXACT | 100.0 |

### 2.3 Typo tolerance

Handles 1–2 character errors, transposed letters, missing punctuation:

| Input | Resolved | Status |
|---|---|---|
| `James Hartweel` | James Hartwell | HIGH_CONF |
| `Jmes Hartwell` | James Hartwell | HIGH_CONF |
| `Priya Nambia` | Priya Nambiar | HIGH_CONF |
| `Luc Trembay` | Luc Tremblay | HIGH_CONF |
| `Sandra Mcperson` | Sandra McPherson | HIGH_CONF |
| `Blueline Service Grup` | BlueLine Service Group | HIGH_CONF |

### 2.4 Partial input resolution

Short codes, last-name-only, and abbreviated names resolve correctly:

| Input | Resolved | Score |
|---|---|---|
| `HIS` | HIS (Hartwell Industrial Services) | 100.0 |
| `Kowalski` | Tom Kowalski | 72.9 |
| `Bay 2` | Main Workshop — Bay 2 | 84.6 |
| `TRK001` | TRK-001 | 89.6 |
| `Peterbilt` | 2020 Peterbilt 579 — Unit 2 | 85.0 |
| `forklift` | Toyota 8FBU25 Forklift | 86.0 |

### 2.5 Special character handling

Apostrophes, hyphens, ampersands, and em-dashes are handled gracefully:

| Input | Resolved | Note |
|---|---|---|
| `Derek OBrien` | Derek O'Brien | Missing apostrophe |
| `Derek O' Brien` | Derek O'Brien | Extra space after apostrophe |
| `Apex Equipment and Repair` | Apex Equipment & Repair Co. | `and` vs `&` |
| `South Depot - Covered Bay` | South Depot — Covered Bay | ASCII hyphen vs em-dash |

### 2.6 Parts multi-token resolution

Comma-separated parts are split, resolved independently, and aggregated by minimum score:

```
"Oil Filter, Air Filter, Coolant"
  → Oil Filter — Heavy Duty     (96.5)
  → Air Filter — Heavy Duty     (96.5)
  → Coolant — Long Life (5L)    (96.5)
  → Overall: HIGH_CONFIDENCE
```

### 2.7 Time format flexibility

Clock times and durations accept a wide range of formats:

| Input | Normalised | Valid? |
|---|---|---|
| `8:30` | 08:30 | ✅ |
| `08:30 AM` | 08:30 | ✅ |
| `08:30 PM` | 20:30 | ✅ |
| `12:00 AM` | 00:00 | ✅ |
| `08:30:00` | 08:30 | ✅ |
| `2h30m` | 2h 30m | ✅ |
| `1 hour 30 minutes` | 1h 30m | ✅ |
| `90 min` | 1h 30m | ✅ |

### 2.8 Security posture

Injection attempts are safely rejected:

| Input | Result |
|---|---|
| `'; DROP TABLE workers; --` | NO_MATCH |
| `<script>alert('xss')</script>` | NO_MATCH |

---

## 3. Bugs Found

### Bug 1: Whitespace degrades EXACT → HIGH_CONFIDENCE

**Location:** `fuzzy_resolver.py` → `_score_algorithms()`

**Problem:** The function applies `.lower()` to both query and candidate but does **not** `.strip()` whitespace. This causes EXACT matches with leading/trailing spaces to score 91 instead of 100.

**Reproduction:**
```python
resolve_field("worker", "  James Hartwell  ")
# → resolved_value = "James Hartwell"  ✅ correct
# → status = HIGH_CONFIDENCE           ❌ should be EXACT
# → score = 91.0                       ❌ should be 100.0
```

**Root cause:** In `_score_algorithms()`:
```python
score = algo_map[algo_name](query.lower(), candidate.lower())
# Should be:
score = algo_map[algo_name](query.strip().lower(), candidate.strip().lower())
```

**Impact:** Low. The resolved value is still correct, and HIGH_CONFIDENCE is still accepted. But the status and score are misleading. If thresholds change, this could cause incorrect REVIEW flags.

**Fix:**
```python
def _score_algorithms(query: str, candidate: str, weights: dict) -> tuple[float, dict]:
    q = query.strip().lower()
    c = candidate.strip().lower()
    # ... use q, c instead of query.lower(), candidate.lower()
```

---

### Bug 2: Duration shorthand `30m` not recognized

**Location:** `time_validator.py` → `_DURATION_PATTERNS[2]`

**Problem:** The regex `r'(\d+)\s*m(?:in(?:ute)?s?)$'` requires `m` followed by `in`. The common shorthand `30m` (without `in`) fails to match.

**Reproduction:**
```python
validate_time_field("total_time_spent", "30m")
# → status = TIME_INVALID    ❌ should be TIME_VALID
# → resolved = "30m"         ❌ should be "0h 30m"
```

**Root cause:** The regex group `(?:in(?:ute)?s?)` is NOT optional — only the `s?` inside it is optional. The `in` prefix is required.

```
Pattern:  (\d+)\s*m(?:in(?:ute)?s?)$
                            ^^
                            This ? makes 's' optional, not the whole group
```

**Impact:** Medium. `30m`, `90m`, `5m` are extremely common duration shorthands that users (and Gemini) would naturally produce.

**Fix:** Make the `(?:in...)` group fully optional:
```python
re.compile(r'(\d+)\s*m(?:in(?:ute)?s?)?$', re.I)
#                                        ^ add this ?
```

With this fix:
- `30m` → match → `0h 30m` ✅
- `30 min` → still matches ✅
- `30 minutes` → still matches ✅

---

### Bug 3: Durations >99 hours fail

**Location:** `time_validator.py` → `_DURATION_PATTERNS[3]`

**Problem:** The HH:MM duration pattern `r'^(\d{1,2}):(\d{2})$'` only accepts 1–2 digit hours. `100:30` (100 hours 30 minutes) fails.

**Reproduction:**
```python
validate_time_field("total_time_spent", "100:30")
# → status = TIME_INVALID    ❌ should be TIME_VALID
```

**Root cause:** `\d{1,2}` limits hours to 00–99.

**Impact:** Low. Multi-day durations are uncommon in work orders, but legitimate for equipment uptime logs or multi-day field work.

**Fix:**
```python
re.compile(r'^(\d+):(\d{2})$')
#            ^^^^  change from {1,2} to +
```

---

### Bug 4: Empty required fields don't trigger FAIL

**Location:** `resolution.py` → `compute_overall_status()`

**Problem:** `EMPTY` status is not in the failure check. If Gemini returns `""` for required fields (worker, company, location, etc.), the work order passes validation.

**Reproduction:**
```python
order = {k: "" for k in all_13_fields}
result = validate_work_order(order)
# → overall_status = PASS    ❌ should be FAIL
```

**Root cause:** The status check only catches `NO_MATCH` and `TIME_INVALID`:
```python
if result.status in (MatchStatus.NO_MATCH, MatchStatus.TIME_INVALID):
    if fname in REQUIRED_RESOLVED_FIELDS:
        unresolved.append(fname)
```

`EMPTY` is not in that tuple.

**Impact:** High. A hallucinated or truncated Gemini response with empty required fields would pass validation silently. This is the most critical bug.

**Fix (option A — strict):**
```python
if result.status in (MatchStatus.NO_MATCH, MatchStatus.TIME_INVALID, MatchStatus.EMPTY):
```

**Fix (option B — conservative, only fail required fields):**
```python
EMPTY_REQUIRED_FIELDS = {"worker", "company", "location", "vehicle_equipment"}

if result.status in (MatchStatus.NO_MATCH, MatchStatus.TIME_INVALID):
    if fname in REQUIRED_RESOLVED_FIELDS:
        unresolved.append(fname)
elif result.status == MatchStatus.EMPTY and fname in EMPTY_REQUIRED_FIELDS:
    unresolved.append(fname)
```

---

## 4. Edge Cases by Category

### 4.1 Whitespace Handling

| Input | Field | Result | Notes |
|---|---|---|---|
| `"  James Hartwell  "` | worker | HIGH_CONF (91.0) | Bug: should be EXACT |
| `"James  Hartwell"` | worker | HIGH_CONF | Double space |
| `"\tField Site Alpha\n"` | location | Matches | Tabs/newlines |
| `"  08:00  "` | start_time | TIME_VALID | Time validator strips |
| `"  2h30m  "` | duration | TIME_VALID | Duration validator strips |
| `"Oil Filter  ,  Air Filter"` | parts | 2 tokens | Extra spaces around commas |

**Observation:** Time validators strip correctly. Fuzzy resolver does not.

### 4.2 Case Normalization

All 8 case variation tests pass. The `.lower()` in `_score_algorithms()` handles this correctly.

### 4.3 Typos and Misspellings

**Workers (10 test cases):** All resolve to correct canonical name. Strongest matches: `Derek OBrien` (97.0), `Sandra Mcperson` (97.7). Weakest: `Hartwell, James` (75.1 — comma disrupts token matching).

**Companies (6 test cases):** All resolve correctly. `Apex Equipment and Repair` → `Apex Equipment & Repair Co.` handles the `&`/`and` substitution well (89.8).

**Locations (4 test cases):** All resolve correctly. `Field Ste Alpha` → `Field Site Alpha` handles single-char deletion.

### 4.4 Partial / Abbreviated Input

| Input | Field | Resolved | Status | Notes |
|---|---|---|---|---|
| `Kowalski` | worker | Tom Kowalski | LOW_CONF (72.9) | Last name only |
| `J. Hartwell` | worker | James Hartwell | LOW_CONF (79.9) | Initial + last |
| `HIS` | company | HIS | EXACT (100.0) | Short code |
| `Bay 2` | location | Main Workshop — Bay 2 | LOW_CONF (84.6) | Partial, near threshold |
| `TRK001` | vehicle_equipment | TRK-001 | HIGH_CONF (89.6) | Missing hyphen |
| `Peterbilt` | vehicle_equipment | 2020 Peterbilt 579 — Unit 2 | HIGH_CONF (85.0) | Make only |
| `forklift` | vehicle_equipment | Toyota 8FBU25 Forklift | HIGH_CONF (86.0) | Type only |
| `2021` | vehicle_equipment | 2021 CAT 320 Excavator | LOW_CONF (79.2) | Year only |

### 4.5 Special Characters

| Input | Field | Resolved | Notes |
|---|---|---|---|
| `Derek OBrien` | worker | Derek O'Brien | Missing apostrophe → HIGH_CONF (97.0) |
| `Derek O'Brian` | worker | Derek O'Brien | Alternate spelling |
| `Derek O' Brien` | worker | Derek O'Brien | Extra space after apostrophe |
| `Apex Equipment and Repair` | company | Apex Equipment & Repair Co. | `and` ↔ `&` |
| `South Depot - Covered Bay` | location | South Depot — Covered Bay | ASCII hyphen vs em-dash → HIGH_CONF (95.0) |

### 4.6 Ambiguity

| Input | Field | Resolved | Notes |
|---|---|---|---|
| `Main Workshop` | location | Main Workshop — Bay 1 | 3 bays share prefix; picks highest scorer |
| `Generator` | vehicle_equipment | 250kVA Cummins Generator — Site A | 2 generators in DB |
| `Excavator` | vehicle_equipment | 2021 CAT 320 Excavator | 2 excavators; CAT wins |
| `Hartwell` | worker | James Hartwell | Also matches company name, but field routing is correct |

### 4.7 Time Edge Cases

**Valid clock times (13 tests):** All pass. Handles midnight (`00:00`), end-of-day (`23:59`), single-digit hours, no-space AM/PM (`08:30am`), with-seconds (`08:30:00`).

**Invalid clock times (11 tests):** All correctly rejected. Includes `8:3` (single-digit minute), `8.30` (dot separator), `830` (no separator), `8am` (no colon), `24:00` (boundary).

**Valid durations (9 tests):** All pass. Handles `0:30`, `2h30m`, `1 hour 30 minutes`, `90 min`.

**Invalid durations (6 tests):** All correctly rejected. Includes `2.5 hours` (decimal), `90` (bare number), `2h 30` (missing minute unit).

### 4.8 Parts Edge Cases

| Input | Status | Notes |
|---|---|---|
| `BLT-SERP` | EXACT | Part number exact match |
| `Oil Filter, Air Filter, Coolant` | HIGH_CONF | 3 tokens, all match |
| `Oil Filter,Air Filter` | HIGH_CONF | No space after comma — still works |
| `Oil Filter, ZZZ-FAKE-9999` | NO_MATCH | One unknown pulls min score down |
| `2x Oil Filter` | HIGH_CONF | Quantity prefix doesn't break matching |
| `Oil Filter (used)` | LOW_CONF (77.4) | Parenthetical note hurts score |
| `,,,` | NO_MATCH | Empty tokens filtered |
| `   ` | EMPTY | Whitespace-only |

### 4.9 Boundary / Defensive Inputs

| Input | Field | Result | Notes |
|---|---|---|---|
| `a` | worker | NO_MATCH (36.3) | Single character |
| `"A" * 1000` | worker | NO_MATCH | Very long string |
| `12345` | worker | NO_MATCH | Numeric |
| `!@#$%^&*()` | worker | NO_MATCH | Special chars only |
| `Derek O'Brien` | worker | Matches | Unicode curly apostrophe |
| `'; DROP TABLE workers; --` | worker | NO_MATCH | SQL injection |
| `<script>alert('xss')</script>` | worker | NO_MATCH | HTML injection |
| Extra field in order | — | PASS_THROUGH | Unknown fields ignored |
| Numeric `800` for start_time | start_time | TIME_INVALID | `str(800)` → `"800"` |

---

## 5. Test Data Files

### `test_data/edge_case_work_order.json`

Intentionally "noisy" work order that simulates realistic Gemini output with imprecise but close-to-correct values:

```json
{
  "vehicle_equipment":      "TRK001",              // missing hyphen
  "reported_problem":       "Engine overheating and making strange hissing noise...",
  "diagnosis_cause":        "Radiator cap seal degraded, coolant leak at upper hose clamp...",
  "work_performed":         "Replaced radiator cap, re-clamped upper hose...",
  "parts_used":             "Oil Filter, Air Filter, Coolant",  // short descriptions
  "start_time":             "8:30",                 // single digit hour
  "end_time":               "11:30 AM",             // AM/PM format
  "total_time_spent":       "3h30m",                // no spaces
  "future_recommendations": "Monitor coolant level weekly for 1 month...",
  "remaining_tasks":        "Need to order replacement upper radiator hose...",
  "worker":                 "James Hartwell",       // exact
  "company":                "Hartwell Industrial",  // partial
  "location":               "Bay 1"                 // partial
}
```

**Pipeline result:** REVIEW (location `Bay 1` scores 84.6 — just below HIGH_CONF threshold of 85.0)

### `test_data/edge_case_work_order_clean.json`

Canonical reference with all exact matches and proper formatting. Pipeline result: PASS.

---

## 6. Full Pipeline Demo

### Noisy work order → REVIEW

```
Field                     Raw                              Resolved                           Status               Score
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
vehicle_equipment         TRK001                           TRK-001                            HIGH_CONFIDENCE      89.6
reported_problem          Engine overheating and making…   Engine overheating and making…     PASS_THROUGH          0.0
diagnosis_cause           Radiator cap seal degraded,…     Radiator cap seal degraded,…       PASS_THROUGH          0.0
work_performed            Replaced radiator cap, re-cl…    Replaced radiator cap, re-cl…      PASS_THROUGH          0.0
parts_used                Oil Filter, Air Filter, Coolant  Oil Filter — Heavy Duty, Air…     HIGH_CONFIDENCE      96.5
start_time                8:30                             08:30                              TIME_VALID          100.0
end_time                  11:30 AM                         11:30                              TIME_VALID          100.0
total_time_spent          3h30m                            3h 30m                             TIME_VALID          100.0
future_recommendations    Monitor coolant level weekly…    Monitor coolant level weekly…      PASS_THROUGH          0.0
remaining_tasks           Need to order replacement…       Need to order replacement…         PASS_THROUGH          0.0
worker                    James Hartwell                   James Hartwell                     EXACT               100.0
company                   Hartwell Industrial              Hartwell Industrial Services       HIGH_CONFIDENCE      98.2
location                  Bay 1                            Main Workshop — Bay 1              LOW_CONFIDENCE       84.6

Overall: REVIEW
Reason: location "Bay 1" → LOW_CONFIDENCE (84.6, threshold is 85.0)
```

This is a **good result** — the system correctly identifies that `Bay 1` is ambiguous (matches Bay 1, 2, and 3) and flags it for human review.

---

## 7. Recommendations

### Priority 1 — Fix empty required field validation (Bug 4)

Empty strings for `worker`, `company`, `location`, `vehicle_equipment`, `start_time`, `end_time` should trigger FAIL, not PASS. This is a data integrity risk.

### Priority 2 — Fix `30m` duration shorthand (Bug 2)

This is a very common format that Gemini is likely to produce. One-character regex fix.

### Priority 3 — Strip whitespace before scoring (Bug 1)

Add `.strip()` to `_score_algorithms()` so whitespace doesn't degrade scores. Low risk, high correctness improvement.

### Priority 4 — Support >99 hour durations (Bug 3)

Change `\d{1,2}` to `\d+` in the HH:MM duration pattern. Low frequency but easy fix.

### Future considerations

- **Fuzzy field stripping:** Apply `.strip()` in `resolve_field()` and `resolve_parts_used()` before scoring, not just in the time validators.
- **`8am` support:** The pattern `8am` (no colon) is a natural format. Could add a regex: `r'^(\d{1,2})\s*(am|pm)$'`.
- **Decimal durations:** `2.5 hours` is natural English. Could parse `float(group1) * 60 + int(group2)`.
- **Location ambiguity:** When `Bay` matches 3 candidates at similar scores, consider returning all candidates with scores so the caller can disambiguate.
- **Canonical resolver for locations and companies:** `CANONICAL_RESOLVERS` currently covers `worker`, `vehicle_equipment`, and `parts_used` but not `company` or `location`. Adding these would enable richer DB metadata in results.

---

## Test Suite

```bash
cd workorder_processing
python -m pytest tests/test_edge_cases.py -v    # 159 tests
python -m pytest tests/ -v                       # 227 total (159 new + 68 existing)
```

**159 edge case tests across 12 categories:**

| Category | Count | Description |
|---|---|---|
| Whitespace | 8 | Leading/trailing/double spaces, tabs, newlines |
| Case normalization | 8 | ALL CAPS, lowercase, mixed case, AM/PM |
| Typos | 16 | Misspellings across workers, companies, locations |
| Partial input | 12 | Last-name-only, short codes, year-only, type-only |
| Special characters | 8 | Apostrophes, hyphens, ampersands, em-dashes, periods |
| Ambiguity | 5 | Multi-candidate disambiguation, cross-entity names |
| Time edge cases | 31 | Boundary times, AM/PM, invalid formats, duration shorthands |
| Parts edge cases | 12 | Comma handling, quantities, parentheticals, empty tokens |
| Full integration | 18 | PASS/REVIEW/FAIL scenarios, JSON file validation, None handling |
| Boundary conditions | 12 | Empty, None, Unicode, injection, extra fields, numeric types |
| Score thresholds | 5 | Threshold boundary verification |
| Known bugs | 4 | Documented buggy behavior with assertion of current (wrong) behavior |
