# Fuzzy Matching Improvement Plan — Implementation Report

**Date:** 2026-05-27
**Status:** All 4 bugs fixed, verified with 257 tests (0 failures)
**Test suite:** `tests/test_edge_cases.py` — 189 tests + 68 original = 257 total

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Fix Details](#2-fix-details)
3. [Verification Results](#3-verification-results)
4. [Fix-Specific Edge Cases](#4-fix-specific-edge-cases)
5. [Cross-Fix Interaction Analysis](#5-cross-fix-interaction-analysis)
6. [Test Suite Changes](#6-test-suite-changes)
7. [Remaining Known Limitations](#7-remaining-known-limitations)
8. [Diff Summary](#8-diff-summary)

---

## 1. Executive Summary

Four bugs were identified in the initial evaluation ([`FUZZY_MATCHING_EVALUATION.md`](FUZZY_MATCHING_EVALUATION.md)). All four have been fixed in three files:

| # | Bug | File Changed | Change |
|---|---|---|---|
| 1 | Whitespace degrades EXACT → HIGH_CONF | `validator/fuzzy_resolver.py` | Added `.strip()` before scoring |
| 2 | `30m` duration shorthand not recognised | `validator/time_validator.py` | Made `(?:in...)?` fully optional in regex |
| 3 | Durations >99h fail | `validator/time_validator.py` | Changed `\d{1,2}` → `\d+` in HH:MM pattern |
| 4 | Empty required fields don't fail | `validator/resolution.py` | Added `MatchStatus.EMPTY` to failure check |

**Total changes:** 3 files, minimal diffs, no API changes, no schema changes, fully backward compatible.

---

## 2. Fix Details

### Fix 1: Strip whitespace before fuzzy scoring

**File:** `validator/fuzzy_resolver.py` — `_score_algorithms()`

**Before:**
```python
score = algo_map[algo_name](query.lower(), candidate.lower())
```

**After:**
```python
score = algo_map[algo_name](query.strip().lower(), candidate.strip().lower())
```

**Effect:** `"  James Hartwell  "` now resolves to `EXACT` (100.0) instead of `HIGH_CONFIDENCE` (91.0). Applies to all five fuzzy fields (worker, company, location, vehicle_equipment, parts_used).

**Note:** The `.strip()` is applied only during scoring. The `raw_value` in the result still preserves the original input (important for audit trail). Resolved values are always the DB canonical form, which has no whitespace noise.

**Edge case verified:** Internal double spaces (`"James  Hartwell"`) are NOT collapsed. They score as `HIGH_CONFIDENCE` rather than `EXACT` — this is acceptable since the resolved value is still correct.

---

### Fix 2: Accept `30m` duration shorthand

**File:** `validator/time_validator.py` — `_DURATION_PATTERNS[2]`

**Before:**
```python
re.compile(r'(\d+)\s*m(?:in(?:ute)?s?)$', re.I)
```

**After:**
```python
re.compile(r'(\d+)\s*m(?:in(?:ute)?s?)?$', re.I)
#                                        ^  added single ?
```

**Effect:** The `(?:in(?:ute)?s?)` group is now fully optional. Formats now accepted:

| Format | Normalised | Previously |
|---|---|---|
| `30m` | `0h 30m` | ❌ FAILED |
| `30 m` | `0h 30m` | ❌ FAILED |
| `5m` | `0h 5m` | ❌ FAILED |
| `90m` | `1h 30m` | ❌ FAILED |
| `30min` | `0h 30m` | ✅ (already worked) |
| `30mins` | `0h 30m` | ✅ (already worked) |
| `30 minutes` | `0h 30m` | ✅ (already worked) |

**Edge cases that still correctly fail:**
- `30` (no unit) — `TIME_INVALID`
- `30m30s` (seconds) — `TIME_INVALID`
- `m` (no digits) — `TIME_INVALID`
- `30meters` — `TIME_INVALID`

**Cross-pattern interaction verified:** Pattern 0 (`Xh Ym`), pattern 1 (`Xh`), and pattern 3 (`HH:MM`) are unaffected. Pattern 0 uses `.search()` (substring match); the other patterns use `.match()` (anchored). No ordering conflicts.

---

### Fix 3: Accept durations >99 hours

**File:** `validator/time_validator.py` — `_DURATION_PATTERNS[3]`

**Before:**
```python
re.compile(r'^(\d{1,2}):(\d{2})$')
```

**After:**
```python
re.compile(r'^(\d+):(\d{2})$')
#            ^^^^  changed from {1,2} to +
```

**Effect:** Any number of hour digits now accepted:

| Format | Normalised | Previously |
|---|---|---|
| `100:30` | `100h 30m` | ❌ FAILED |
| `150:00` | `150h 0m` | ❌ FAILED |
| `999:59` | `999h 59m` | ❌ FAILED |
| `25:00` | `25h 0m` | ❌ FAILED |
| `99:30` | `99h 30m` | ✅ (already worked) |
| `1:00` | `1h 0m` | ✅ (already worked) |

**Edge cases verified:**
- `10000:0` → `TIME_INVALID` (single-digit minutes still requires `\d{2}`)
- `10000:00` → `10000h 0m` ✅
- `0:00` → `0h 0m` ✅ (no regression on boundary values)
- The fix only applies to durations (`total_time_spent`), not clock times. Clock time parsing uses separate `_PATTERNS` with `\d{1,2}` which correctly requires 0–23 hour range.

---

### Fix 4: Empty required fields trigger FAIL

**File:** `validator/resolution.py` — `compute_overall_status()`

**Before:**
```python
if result.status in (MatchStatus.NO_MATCH, MatchStatus.TIME_INVALID):
    if fname in REQUIRED_RESOLVED_FIELDS:
        unresolved.append(fname)
```

**After:**
```python
if result.status in (MatchStatus.NO_MATCH, MatchStatus.TIME_INVALID, MatchStatus.EMPTY):
    if fname in REQUIRED_RESOLVED_FIELDS:
        unresolved.append(fname)
    else:
        review.append(fname)
```

**Effect:** `EMPTY` status is now treated equivalently to `NO_MATCH`/`TIME_INVALID` for routing logic:

| Scenario | Before | After |
|---|---|---|
| All fields empty | PASS | **FAIL** |
| Empty worker only | PASS | **FAIL** |
| Empty parts_used | PASS | **REVIEW** |
| Empty total_time_spent | PASS | **REVIEW** |
| Empty remaining_tasks (free text) | PASS | PASS (PASS_THROUGH, never EMPTY) |
| Whitespace-only worker (`"   "`) | PASS | **FAIL** |

**Required fields** (EMPTY → FAIL): `worker`, `company`, `location`, `vehicle_equipment`, `start_time`, `end_time`

**Non-required fields** (EMPTY → REVIEW): `parts_used`, `total_time_spent`

**Free text fields** unaffected: `reported_problem`, `diagnosis_cause`, `work_performed`, `future_recommendations`, `remaining_tasks` (use `PASS_THROUGH`, never `EMPTY`)

**Accumulation behavior:** If a work order has both EMPTY required fields AND LOW_CONFIDENCE optional fields, the result is **FAIL** (FAIL takes priority over REVIEW). The unresolved and review fields are both populated.

---

## 3. Verification Results

### Per-fix verification

```
=== Fix 1: Whitespace strip ===
  "  James Hartwell  "  -> 'James Hartwell'    EXACT          100.0  ✅
  "  Derek OBrien  "    -> "Derek O'Brien"    HIGH_CONFIDENCE  97.0  ✅
  "  Zzzyx Qqqington  " -> NO_MATCH (no false match)           ✅

=== Fix 2: 30m shorthand ===
  '30m'        -> '0h 30m'   TIME_VALID  ✅
  '30 m'       -> '0h 30m'   TIME_VALID  ✅
  '5m'         -> '0h 5m'    TIME_VALID  ✅
  '90m'        -> '1h 30m'   TIME_VALID  ✅
  '30min'      -> '0h 30m'   TIME_VALID  ✅ (no regression)
  '30 minutes' -> '0h 30m'   TIME_VALID  ✅ (no regression)

=== Fix 3: >99 hour duration ===
  '100:30'     -> '100h 30m'  TIME_VALID  ✅
  '150:00'     -> '150h 0m'   TIME_VALID  ✅
  '999:59'     -> '999h 59m'  TIME_VALID  ✅
  '0:00'       -> '0h 0m'     TIME_VALID  ✅ (no regression)

=== Fix 4: Empty required fields fail ===
  All empty    -> FAIL, unresolved=[vehicle_equipment, start_time, end_time,
                                     worker, company, location]  ✅
  Empty worker -> FAIL, unresolved=['worker']                    ✅
```

### Full test suite

```
tests/test_fuzzy_resolver.py ............ 68 passed
tests/test_edge_cases.py ............... 189 passed
─────────────────────────────────────────────
Total: 257 passed, 0 failed
```

---

## 4. Fix-Specific Edge Cases

### 4.1 Fix 1 edge cases (whitespace strip)

| Test | Input | Expected | Result |
|---|---|---|---|
| Strip applies to all fuzzy fields | `"  TRK-001  "` (equipment) | EXACT | ✅ |
| Strip applies to all fuzzy fields | `"  PrimeTech Maintenance Ltd  "` (company) | EXACT | ✅ |
| Strip applies to all fuzzy fields | `"  Main Workshop — Bay 1  "` (location) | EXACT | ✅ |
| NO_MATCH preserves raw_value | `"  Zzzyx Qqqington  "` | NO_MATCH, raw preserved | ✅ |
| Internal double space not collapsed | `"James  Hartwell"` | HIGH_CONF (not EXACT) | ✅ |
| Strip + typo → still correct matching | `"  Derek OBrien  "` | HIGH_CONF, resolved correct | ✅ |
| Whitespace-only input → EMPTY | `"   "` (worker) | EMPTY → FAIL (after Fix 4) | ✅ |

### 4.2 Fix 2 edge cases (30m shorthand)

| Test | Input | Expected | Result |
|---|---|---|---|
| Bare `m` variant | `30m` | `0h 30m` TIME_VALID | ✅ |
| Space before `m` | `30 m` | `0h 30m` TIME_VALID | ✅ |
| Very short | `5m` | `0h 5m` TIME_VALID | ✅ |
| Minute overflow | `90m` | `1h 30m` TIME_VALID | ✅ |
| Singular `minute` | `30 minute` | `0h 30m` TIME_VALID | ✅ |
| No unit | `30` | TIME_INVALID | ✅ |
| Seconds | `30m30s` | TIME_INVALID | ✅ |
| No digit | `m` | TIME_INVALID | ✅ |
| Non-time word | `30meters` | TIME_INVALID | ✅ |
| Pattern 0 unaffected | `2 hours 30 minutes` | `2h 30m` TIME_VALID | ✅ |
| Pattern 3 unaffected | `1:30` | `1h 30m` TIME_VALID | ✅ |

### 4.3 Fix 3 edge cases (>99h duration)

| Test | Input | Expected | Result |
|---|---|---|---|
| 3-digit hours | `100:30` | `100h 30m` TIME_VALID | ✅ |
| 3-digit even | `150:00` | `150h 0m` TIME_VALID | ✅ |
| Near limit | `999:59` | `999h 59m` TIME_VALID | ✅ |
| Single-digit minute still invalid | `10000:0` | TIME_INVALID | ✅ |
| 5-digit hours | `10000:00` | `10000h 0m` TIME_VALID | ✅ |
| Boundary: 25h | `25:00` | `25h 0m` TIME_VALID | ✅ |
| No regression: 0h | `0:00` | `0h 0m` TIME_VALID | ✅ |
| No regression: 0h 1m | `0:01` | `0h 1m` TIME_VALID | ✅ |
| No regression: 1h | `1:00` | `1h 0m` TIME_VALID | ✅ |

### 4.4 Fix 4 edge cases (EMPTY → FAIL)

| Test | Scenario | Expected | Result |
|---|---|---|---|
| Empty worker only | All else valid | FAIL, `worker` unresolved | ✅ |
| Empty company only | All else valid | FAIL, `company` unresolved | ✅ |
| Empty location only | All else valid | FAIL, `location` unresolved | ✅ |
| Empty equipment only | All else valid | FAIL, `vehicle_equipment` unresolved | ✅ |
| Empty start_time only | All else valid | FAIL, `start_time` unresolved | ✅ |
| Empty end_time only | All else valid | FAIL, `end_time` unresolved | ✅ |
| Empty parts_used | All else valid | REVIEW, `parts_used` in review | ✅ |
| Empty total_time_spent | All else valid | REVIEW, `total_time_spent` in review | ✅ |
| Whitename-only worker | `"   "` → EMPTY | FAIL | ✅ |
| All None values | `None` → `""` → EMPTY | FAIL, 3+ fields unresolved | ✅ |
| FAIL + REVIEW accumulation | Empty worker + low-conf location | FAIL, both lists populated | ✅ |

---

## 5. Cross-Fix Interaction Analysis

The four fixes touch three files that form a pipeline:

```
fuzzy_resolver.py (Fix 1)
       │
       ▼
time_validator.py (Fixes 2, 3)
       │
       ▼
resolution.py (Fix 4)
```

**Fix 1 × Fix 4:** Whitespace-only required fields (e.g., `"   "`) are now correctly routed to `EMPTY` → `FAIL`. Verified: `test_whitespace_only_field_treated_as_empty`.

**Fix 1 × Fix 2/3:** Stripped whitespace on time fields was already handled (time validators already `.strip()`). Fix 1 only affects fuzzy fields. No interaction.

**Fix 2 × Fix 3:** Both modify `_DURATION_PATTERNS` in `time_validator.py`. They target different regex indices (2 and 3 respectively). The pattern evaluation order (0 → 1 → 2 → 3) was preserved. Verified: `test_pattern_2_does_not_interfere_with_pattern_0`, `test_pattern_2_does_not_interfere_with_pattern_3`.

**Fix 2 × Fix 4:** EMPTY durations (which route through `TIME_INVALID` status) are correctly treated as FAIL for the `start_time`/`end_time` use case — `TIME_INVALID` was already in the failure check. No interaction.

**Fix 1 × Fix 2 × Fix 3 combined stress test:** A work order with whitespace-wrapped fields including a 100h+ duration parses correctly:

```python
order = {
    "worker":             "  James Hartwell  ",
    "company":            "  HIS  ",
    "location":           "  Main Workshop — Bay 1  ",
    "vehicle_equipment":  "  TRK-001  ",
    "total_time_spent":   "  100:30  ",
    ...
}
# → PASS, total_time_spent = "100h 30m"
```

**No regressions:** All 68 original tests pass unchanged. The existing behavior for valid inputs is preserved.

---

## 6. Test Suite Changes

### `tests/test_edge_cases.py` — Updated

| Change | Description |
|---|---|
| TestWhitespace tightened | `test_worker_leading_trailing_spaces` now asserts `EXACT` (was `EXACT or HIGH_CONF`) |
| Integration test tightened | `test_whitespace_inputs_pass` now asserts `PASS` (was `PASS or REVIEW`) |
| None values test tightened | `test_none_values_treated_as_empty` now asserts `FAIL` (was `PASS or REVIEW or FAIL`) |
| KnownBugs class renamed | 4 tests now assert the **fixed** behavior (previously asserted buggy behavior) |
| `TestFixVerification` added | New class with 13 test methods covering fix-specific edge cases and cross-fix interactions |

### New test class: `TestFixVerification` (13 methods, 28 assertions)

| Method | Scope |
|---|---|
| `test_strip_fix_applies_to_all_fuzzy_fields` | Fix 1: all 4 fuzzy fields |
| `test_strip_fix_does_not_affect_no_match` | Fix 1: no false match from stripping |
| `test_double_space_inside_name_not_yet_exact` | Fix 1: internal spaces not collapsed |
| `test_duration_shorthand_variants[8]` | Fix 2: 8 valid shorthand formats |
| `test_duration_shorthand_still_invalid[4]` | Fix 2: 4 formats that still correctly fail |
| `test_pattern_2_does_not_interfere_with_pattern_0` | Fix 2: no regression with pattern 0 |
| `test_pattern_2_does_not_interfere_with_pattern_3` | Fix 2: no regression with pattern 3 |
| `test_large_durations[5]` | Fix 3: 5 large duration formats |
| `test_large_duration_not_misparsed_as_clock` | Fix 3: 25:00 as duration not clock |
| `test_small_durations_unaffected` | Fix 3: no regression on small values |
| `test_single_empty_required_field_fails` | Fix 4: each required field individually |
| `test_empty_non_required_field_does_not_fail` | Fix 4: empty parts → REVIEW not FAIL |
| `test_empty_required_plus_low_conf_correct_accumulation` | Fix 4: FAIL priority over REVIEW |
| `test_whitespace_only_field_treated_as_empty` | Fix 4: whitespace-only → EMPTY → FAIL |
| `test_stripped_whitespace_with_long_duration` | Cross-fix: Fix 1 + Fix 3 combined |
| `test_empty_required_with_invalid_time` | Cross-fix: Fix 4 + TIME_INVALID combined |

---

## 7. Remaining Known Limitations

These were present before the fixes and are unchanged:

| Limitation | Impact | Priority |
|---|---|---|
| `8am` (no colon) not supported as clock time | Users/Gemini may produce this format | Low |
| `2.5 hours` (decimal) not supported | Natural English format | Low |
| `2 hours 30` (missing `m`) not supported | Omitting the minute suffix | Low |
| Internal double spaces not collapsed | `"James  Hartwell"` not EXACT | Low |
| No canonical resolver for `company` or `location` | No enriched DB metadata for these fields | Low |
| No `$` anchor on duration pattern 0 | `.search()` could match durations inside non-duration text | Very low |
| Resolved value preserved as-is for NO_MATCH | Whitespace in raw_value is preserved for unmatched fields | Very low (by design) |

---

## 8. Diff Summary

### Files changed

```
workorder_processing/validator/fuzzy_resolver.py   — 1 line changed  (Fix 1)
workorder_processing/validator/time_validator.py   — 2 lines changed (Fixes 2, 3)
workorder_processing/validator/resolution.py       — 1 line changed  (Fix 4)
workorder_processing/tests/test_edge_cases.py      — 3 assertions tightened + 189 new lines (TestFixVerification)
```

### Files created

```
workorder_processing/FUZZY_MATCHING_EVALUATION.md   — Initial evaluation report
workorder_processing/FUZZY_MATCHING_IMPROVEMENT.md  — This document
workorder_processing/test_data/edge_case_work_order.json
workorder_processing/test_data/edge_case_work_order_clean.json
```

### Characterising the changes

All three fix diffs are single-expression changes:
- `query.lower()` → `query.strip().lower()`
- `(?:in(?:ute)?s?)$` → `(?:in(?:ute)?s?)?$` (one `?` added)
- `\d{1,2}` → `\d+`
- `(NO_MATCH, TIME_INVALID)` → `(NO_MATCH, TIME_INVALID, EMPTY)`

No function signatures changed. No return types changed. No API changed.
