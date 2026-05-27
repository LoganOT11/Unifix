# Consolidated Improvement Plan — Implementation Evaluation

**Date:** 2026-05-27  
**Evaluator:** Automated analysis + edge-case probe  
**Scope:** `CONSOLIDATED_IMPROVEMENT_PLAN.md` vs actual codebase  
**Test suite:** 261 tests — all passing

---

## 1. Executive Summary

The `CONSOLIDATED_IMPROVEMENT_PLAN.md` describes a four-phase improvement roadmap for the
Unifix Work Order Processor. **All four phases have been largely implemented.** The codebase
is in excellent shape: all 4 bugs are fixed, prompt engineering v2 is deployed, the veracity
pass is wired into `main()`, and the image processing module is built.

However, the evaluation uncovered **16 issues** across three severity tiers:
- **3 HIGH** (missing tests for production modules, image path traversal vulnerability, missing retry logic)
- **8 MEDIUM** (plan/code discrepancies, missing CLI integration, duplicate code)
- **5 LOW** (edge cases, cosmetic issues, documentation drift)

---

## 2. Phase-by-Phase Implementation Status

### Phase 1 — Bug Fixes (✅ 100% implemented)

| # | Bug | Status | File |
|---|---|---|---|
| 1 | Whitespace degrades EXACT → HIGH_CONF | ✅ Fixed | `fuzzy_resolver.py:80` |
| 1b | Strip at `resolve_field()` entry | ✅ Fixed | `fuzzy_resolver.py:117` |
| 2 | `30m` duration shorthand | ✅ Fixed | `time_validator.py:27` |
| 2b | `8am`/`8pm` no-colon clock time | ✅ Fixed | `time_validator.py:18-23` |
| 2c | Decimal duration (`2.5 hours`) | ✅ Fixed | `time_validator.py:34-38` |
| 3 | >99 hour durations | ✅ Fixed | `time_validator.py:28` |
| 4 | EMPTY required fields → FAIL | ✅ Fixed | `resolution.py:13` |
| — | Location/company canonical resolvers | ✅ Fixed | `reference_data.py:114-128` |
| — | `CANONICAL_RESOLVERS` wiring | ✅ Fixed | `fuzzy_resolver.py:47-53` |
| — | `top_candidates` in FieldResult | ✅ Fixed | `models.py:41`, `fuzzy_resolver.py:72-74` |

**261 tests pass.** All 4 bug-regression tests in `TestKnownBugs` assert correct post-fix behaviour.

---

### Phase 2 — Prompt Engineering (✅ 95% implemented)

| Item | Status | Notes |
|---|---|---|
| `SCHEMA_PROMPT_V2` in `gemini_client.py` | ✅ Implemented | See issue #4 — missing few-shot examples |
| `extract_confidence_markers()` in `parser.py` | ✅ Implemented | |
| `GEMINI_CONFIDENCE_BOOST` in `fuzzy_resolver.py` | ✅ Implemented | Boosts: HIGH=+5, MEDIUM=0, LOW=−10 |
| `resolve_field_with_gemini_confidence()` | ✅ Implemented | Correctly skips boost for EMPTY/score=0 fields |
| Wiring into `validate_work_order()` | ✅ Implemented | `confidences` parameter flows through entire pipeline |
| Confidence in output envelope | ✅ Implemented | Confidence markers parsed but not stored verbatim in envelope |

---

### Phase 3 — Veracity Pass (✅ 95% implemented)

| Item | Status | Notes |
|---|---|---|
| `VERACITY_PROMPT` | ✅ Implemented | `veracity.py:13-62` |
| `should_run_veracity()` | ✅ Implemented | Triggers on REVIEW/FAIL + LOW confidence |
| `run_veracity_check()` | ✅ Implemented | Audio + text variant |
| `apply_veracity_corrections()` | ✅ Implemented | Handles INCORRECT + HIGH-conf UNCERTAIN |
| Wired into `main()` | ✅ Implemented | `main.py:93-113` |
| Audit trail in envelope | ✅ Implemented | `veracity_pass` key in envelope |
| Tests | ❌ Missing | **HIGH** — See issue #1 |

---

### Phase 4 — Image Processing (✅ 85% implemented)

| Item | Status | Notes |
|---|---|---|
| `assess_quality()` | ✅ Implemented | `image_preprocessor.py:33-40` |
| `deskew()` | ✅ Implemented | Hough transform with median angle |
| `enhance_contrast()` | ✅ Implemented | CLAHE, clipLimit=2.0 |
| `denoise()` | ✅ Implemented | Non-local means |
| `binarize()` | ✅ Implemented | Adaptive threshold |
| `upscale_if_small()` | ✅ Implemented | Lanczos4 interpolation |
| `preprocess_image()` | ✅ Implemented | Full pipeline orchestrator |
| `IMAGE_EXTRACTION_PROMPT` | ✅ Implemented | Field label mapping table |
| `process_image()` | ✅ Implemented | See issue #3 — missing retry |
| `validate_image_file()` | ✅ Implemented | See issue #2 — missing path traversal guard |
| `--mode` CLI flag | ❌ Missing | **MEDIUM** — See issue #6 |
| Tests | ❌ Missing | **HIGH** — See issue #1 |

---

## 3. Issues Found

### 🔴 HIGH Severity

---

#### Issue #1 — No tests for veracity, image preprocessor, or image processor

**Files:** `processor/veracity.py`, `processor/image_preprocessor.py`, `processor/image_processor.py`

**Impact:** Three production modules have zero test coverage. The veracity pass is wired into the
main pipeline (`main.py:93-113`) but has no unit tests for its trigger logic, correction logic,
or error handling. Image preprocessing functions have no tests for quality classification
accuracy or pipeline correctness. The image processor has no integration tests.

**Recommendation:**
- Add tests for `should_run_veracity()`: REVIEW triggers → `True`, PASS → `False`, LOW confidence → `True`
- Add tests for `apply_veracity_corrections()`: INCORRECT applied, CORRECT ignored, UNCERTAIN+HIGH applied
- Add tests for `run_veracity_check()`: mock Gemini response, error handling, empty response
- Add tests for `preprocess_image()`: mock image bytes, verify quality classification, verify ops list
- Add tests for `process_image()`: end-to-end with mock Gemini response
- Add tests for `validate_image_file()`: size bound, MIME validation, corrupt file detection

---

#### Issue #2 — Image file validation lacks path traversal guard

**File:** `processor/image_processor.py` → `validate_image_file()`

**Problem:** The audio validator (`processor/validator.py:41-47`) includes a `safe_root` parameter
that prevents path traversal (e.g., `../../../etc/passwd`). The image validator (`image_processor.py:74-83`)
does **not** have this guard.

```python
# image_processor.py — MISSING
def validate_image_file(file_path: str) -> dict:      # No safe_root parameter
    path = Path(file_path).resolve()
    if not path.exists():                              # Exists check only
        raise FileNotFoundError(f"Image file not found: {file_path}")
    size = path.stat().st_size
    if size > MAX_IMAGE_SIZE_BYTES:                    # Size check
        ...
    mime = detect_image_mime(file_path)
    if mime not in ALLOWED_IMAGE_MIMES:                # MIME check
        ...
    # NO path traversal guard!
```

**Recommendation:** Add `safe_root` parameter matching the audio validator's pattern:
```python
def validate_image_file(file_path: str, safe_root: str | None = None) -> dict:
    path = Path(file_path).resolve()
    root = Path(safe_root).resolve() if safe_root else Path.cwd()
    if not str(path).startswith(str(root)):
        raise ValueError(f"Path traversal detected: {file_path} resolves outside {root}")
    ...
```

---

#### Issue #3 — `process_image()` uses direct API call without retry logic

**File:** `processor/image_processor.py:87-107`

**Problem:** `process_image()` calls `client.models.generate_content()` directly instead of using
`call_gemini_with_retry()` from `gemini_client.py`. This means transient errors (429 rate limit,
5xx server errors) will cause immediate failure rather than retry with exponential backoff.

```python
# image_processor.py:98-103 — NO RETRY
response = client.models.generate_content(
    model=model_id,
    contents=[image_part, IMAGE_EXTRACTION_PROMPT],
    config=types.GenerateContentConfig(...),
)
```

Compare with the audio path which uses:
```python
# main.py:60-67 — WITH RETRY
response = call_gemini_with_retry(
    client=client, model_id=MODEL_ID,
    audio_bytes=audio_bytes, mime_type=meta["detected_mime"],
)
```

**Recommendation:** Replace with a call to `call_gemini_generic()` (which accepts arbitrary
contents lists and has retry logic):
```python
response = call_gemini_generic(
    client=client, model_id=model_id,
    contents=[image_part, IMAGE_EXTRACTION_PROMPT],
    config=types.GenerateContentConfig(
        response_mime_type="application/json", temperature=0.2,
    ),
)
```

---

### 🟡 MEDIUM Severity

---

#### Issue #4 — SCHEMA_PROMPT_V2 missing few-shot examples from plan

**File:** `processor/gemini_client.py` → `_build_schema_prompt_v2()`

**Problem:** The plan document includes a comprehensive `<few_shot_examples>` section in the v2
prompt (two worked examples — one clean, one ambiguous). The actual implementation omits this
section entirely.

The plan's version:
```
<few_shot_examples>
Example 1 — Clean input:
  Transcript: "This is Derek O'Brien from Northern Fleet Solutions..."
  Expected output: { ... full JSON ... }

Example 2 — Ambiguous input:
  Transcript: "Tom here, just finished up on the big excavator..."
  Expected output: { ... full JSON ... }
</few_shot_examples>
```

The actual implementation has no `few_shot_examples` section.

**Impact:** Few-shot examples are the most effective technique for guiding LLM output format.
Without them, Gemini may produce inconsistent JSON shapes run-to-run, especially for ambiguous
inputs. The plan explicitly identifies this as a problem with the original prompt (see table
row "No few-shot examples" → "No grounding for format; JSON shape may vary run-to-run").

**Recommendation:** Add the two few-shot examples from the plan into `_build_schema_prompt_v2()`.
The token cost is modest (~400 tokens) and the accuracy benefit is significant.

---

#### Issue #5 — Duplicate confidence marker extraction logic

**Files:** `processor/parser.py:94-113` and `processor/image_processor.py:117-129`

**Problem:** `parser.py` has a **public** `extract_confidence_markers()` function. `image_processor.py`
has a **private** `_split_confidence_markers()` function that does exactly the same thing. The
image processor doesn't import or reuse the parser's version.

```python
# parser.py — public, well-tested
FUZZY_FIELDS = {"worker", "company", "location", "vehicle_equipment", "parts_used"}

def extract_confidence_markers(raw_json: dict) -> tuple[dict, dict[str, str]]:
    ...

# image_processor.py — private, identical logic, NOT reusing parser's version
def _split_confidence_markers(raw_json: dict) -> tuple[dict, dict]:
    FUZZY_FIELDS = {"worker", "company", "location", "vehicle_equipment", "parts_used"}
    ...
```

Any fix to confidence marker extraction (e.g., handling `None` values, adding new fuzzy fields)
must be applied in two places.

**Recommendation:** Delete `_split_confidence_markers` from `image_processor.py` and import
`extract_confidence_markers` from `parser.py`.

---

#### Issue #6 — No CLI entry point for image processing

**File:** `workorder_processing/main.py`

**Problem:** The plan calls for a `--mode audio|image` CLI flag (Phase 4 checklist, last item).
The current CLI only supports audio:

```python
# main.py — audio only
parser.add_argument("audio_file", help="Path to the audio file to process.")
```

There is no way to run `process_image()` from the command line. The image processing module
exists but is API-access only.

**Recommendation:** Add `--mode` flag and route to `process_image()` vs `process_audio()`.
Image-specific output should also include preprocessing metadata in the envelope.

---

#### Issue #7 — Veracity check sends `"[See attached audio]"` as transcript

**File:** `processor/veracity.py:68`

**Problem:** The veracity prompt's `<source_material>` section is hardcoded to:
```python
prompt = VERACITY_PROMPT.format(
    source_transcript="[See attached audio]",
    first_pass_json=json.dumps(first_pass_json, indent=2),
)
```

The model receives the raw audio bytes AND the text `"[See attached audio]"`. This works
because Gemini can transcribe audio inline, but:
- The model may expend tokens re-transcribing audio that was already transcribed in Pass 1
- It can't cross-reference specific text against the first-pass JSON as easily
- The plan's original design says: "If audio_bytes is provided, sends the original audio
  alongside the transcript. This is more reliable than text-only for verifying spoken names and times."

**Impact:** Medium. Gemini Vision can process audio directly, but providing both the first-pass
transcript AND the audio would give more reliable verification. The `call_gemini_with_retry()`
function in `main.py` captures the raw response which could be used to extract the transcript.

**Recommendation:** After Pass 1, extract the raw transcription from the Gemini response (if
the model returns it) and include it in the veracity prompt alongside the audio bytes. This
gives the veracity model a text reference to check against.

---

#### Issue #8 — `build_response_envelope()` doesn't accept veracity/validation metadata

**File:** `processor/parser.py:60-89`

**Problem:** `build_response_envelope()` builds the audit envelope with source file, model info,
and extracted data, but doesn't have parameters for validation results or veracity audit trail.
These are manually patched in `main.py:115-129` after the envelope is built:

```python
envelope = build_response_envelope(abs_path, response, work_order, MODEL_ID)
envelope["validation"] = { ... }      # manually added
envelope["veracity_pass"] = { ... }   # manually added
```

**Impact:** Low — the current approach works. But if `build_response_envelope()` is called from
other contexts (e.g., image processing), it won't include validation data unless the caller
remembers to patch it.

**Recommendation:** Add optional `validation_result` and `veracity_info` parameters to
`build_response_envelope()` so the envelope is always complete.

---

#### Issue #9 — `process_image()` has no preprocessing metadata in output

**File:** `processor/image_processor.py:87-107`

**Problem:** `process_image()` preprocesses the image but doesn't return or log the preprocessing
results. The `PreprocessResult` object (quality assessment, operations applied, estimated
improvement) is computed but discarded:

```python
if preprocess and mime_type != "application/pdf":
    pre_result = preprocess_image(image_bytes, mime_type)
    image_bytes = pre_result.image_bytes
    mime_type = pre_result.mime_type
    # pre_result is discarded — no logging, no return to caller
```

**Recommendation:** Return preprocessing metadata as part of the tuple or log it. The plan shows
a `print()` call for this, but it was removed in the implementation.

---

#### Issue #10 — `upscale_if_small` not tracked in operations list

**File:** `processor/image_preprocessor.py:87-93`

**Problem:** The plan shows:
```python
img = upscale_if_small(img)
if img.shape[1] > 1200:
    ops.append("upscale_if_small (no-op: already large enough)")
```
But the actual implementation calls `upscale_if_small()` without tracking the result in `ops`:
```python
img = upscale_if_small(img)
# No ops tracking for upscale
```

**Recommendation:** Track whether upscaling occurred:
```python
img_before = img.shape[1]
img = upscale_if_small(img)
if img.shape[1] != img_before:
    ops.append(f"upscale_if_small ({img_before}→{img.shape[1]}px wide)")
```

---

#### Issue #11 — Veracity trigger uses hardcoded string instead of plan's constant

**File:** `processor/veracity.py:10`

**Problem:** The plan defines `VERACITY_TRIGGER_CONFIDENCE = {"LOW"}` as a named constant but the
implementation hardcodes `"LOW"` in the `any()` check:
```python
# Plan
VERACITY_TRIGGER_CONFIDENCE = {"LOW"}

# Actual
if any(v == "LOW" for v in gemini_confidences.values()):
```

**Recommendation:** Use the constant for consistency and future extensibility:
```python
if any(v in VERACITY_TRIGGER_CONFIDENCE for v in gemini_confidences.values()):
```

---

### 🟢 LOW Severity

---

#### Issue #12 — `None` confidence value becomes string `"NONE"`

**File:** `processor/parser.py:106`

**Problem:** When Gemini returns `null` for a confidence marker:
```json
{ "vehicle_equipment__confidence": null }
```
The code does `str(value).upper()` → `"NONE"`. This string is not in `GEMINI_CONFIDENCE_BOOST`
so it gets a 0.0 boost, effectively treated as MEDIUM. This is acceptable behavior but the
confidence dict will contain `"NONE"` which is misleading.

**Reproduction:**
```python
>>> raw = {"vehicle_equipment": "", "vehicle_equipment__confidence": None}
>>> _, confs = extract_confidence_markers(raw)
>>> confs["vehicle_equipment"]
'NONE'  # should be 'MEDIUM' or omitted
```

**Recommendation:** Handle `None` explicitly:
```python
if value is None:
    confidences[field_name] = "MEDIUM"
else:
    confidences[field_name] = str(value).upper()
```

---

#### Issue #13 — Double `__confidence` suffix edge case

**File:** `processor/parser.py:107`

**Problem:** If a field is literally named `foo__confidence` (with a double underscore in the
name itself), and Gemini also emits `foo__confidence__confidence`, the `replace("__confidence", "")`
operation produces a field named `foo__confidence` with confidence `"HIGH"`.

This is extremely unlikely in practice but the code has no guard against it:
```python
# Input: {"foo__confidence": "bar", "foo__confidence__confidence": "HIGH"}
# Output: work_order={}, confidences={"foo": "HIGH", ...}  # first key consumed as confidence!
```

**Recommendation:** Add a check — if the base field name after stripping `__confidence` still
ends with `__confidence`, it's a naming collision and should raise a warning.

---

#### Issue #14 — Test data has duplicate dict key

**File:** `tests/test_edge_cases.py:617-632` → `test_numeric_value_in_string_field()`

**Problem:** The test dict has `"start_time"` key twice:
```python
order = {
    "vehicle_equipment": "TRK-001",
    ...
    "start_time": "08:00",     # ← first occurrence (silently overridden)
    "end_time": "11:00",
    ...
    "start_time": 800,          # ← second occurrence (actual value used)
}
```

Python silently uses the last value (`800`). The test still passes because it's testing the
numeric-to-string conversion path, but the first `"start_time": "08:00"` is dead code and
misleading to readers.

**Recommendation:** Remove the first `"start_time"` entry.

---

#### Issue #15 — Validation not re-run after veracity corrections

**File:** `workorder_processing/main.py:93-113`

**Problem:** After the veracity pass applies corrections to the work order, the validation
is **not** re-run. The envelope's `validation` section reflects the **pre-correction** state.
If a veracity correction fixes a NO_MATCH field (e.g., correcting a garbled worker name to
"James Hartwell"), the envelope will still show the worker as unresolved.

**Recommendation:** Re-run `validate_work_order()` after corrections are applied if any
fields were changed, and update the envelope's `validation_post_veracity` section.

---

#### Issue #16 — `_find_best_match` score 100.0 for empty-vs-empty

**File:** `validator/fuzzy_resolver.py:60-74`

**Problem:** If both query and candidate are empty strings, rapidfuzz returns 100.0 similarity.
In practice this is unreachable because `_find_best_match()` short-circuits on empty query, but
the scoring function itself doesn't guard against this:

```python
>>> _score_algorithms("", "", {"ratio": 1.0})
(100.0, {"ratio": 100.0})  # empty strings match perfectly
```

**Recommendation:** Add an explicit guard in `_score_algorithms()`:
```python
if not query.strip() or not candidate.strip():
    return 0.0, {algo: 0.0 for algo in weights}
```

---

## 4. Additional Edge Cases Verified (All Pass)

The following edge cases were probed and found to be handled correctly:

| Edge Case | Result |
|---|---|
| Empty candidates list → `_find_best_match` | Sentinel `("", 0.0, {}, [])` returned ✅ |
| Whitespace-only input → EMPTY → FAIL (after Fix 4) | ✅ |
| `None` values in work order → `""` → EMPTY → FAIL | ✅ |
| Invalid confidence string → defaults to 0.0 boost | ✅ |
| `"HIGH"` confidence on empty field → boost skipped (score=0.0) | ✅ |
| `"0.0 hours"` decimal duration → `0h 0m` ✅ |
| `"0.25 hours"` fractional hour → `0h 15m` ✅ |
| Negative times → `TIME_INVALID` ✅ |
| Midnight `00:00` → `TIME_VALID` ✅ |
| SQL injection in worker name → `NO_MATCH` (non-destructive) ✅ |
| Very long strings (10,000 chars) → `NO_MATCH` (no crash) ✅ |
| Extra fields in work order → `PASS_THROUGH` ✅ |
| Numeric values in string fields → `str()` conversion ✅ |
| Em-dash vs hyphen in location → fuzzy-matched correctly ✅ |
| Short company codes → exact match via DB ✅ |

---

## 5. Test Coverage Summary

| Module | Tests | Coverage Assessment |
|---|---|---|
| `validator/fuzzy_resolver.py` | 68 (`test_fuzzy_resolver.py`) | ✅ Good |
| `validator/time_validator.py` | Via fuzzy resolver + edge cases | ✅ Good |
| `validator/resolution.py` | Via integration tests | ✅ Adequate |
| `validator/work_order_validator.py` | Via integration tests | ✅ Adequate |
| `processor/parser.py` | Implicitly via pipeline | ⚠️ No direct unit tests |
| `processor/veracity.py` | **None** | ❌ Missing |
| `processor/image_preprocessor.py` | **None** | ❌ Missing |
| `processor/image_processor.py` | **None** | ❌ Missing |
| `db/reference_data.py` | Implicitly via resolver tests | ✅ Adequate |
| `processor/gemini_client.py` | N/A (API-dependent) | N/A |
| **Total** | **261 tests, 0 failures** | |

---

## 6. Plan/Implementation Discrepancy Summary

| Plan Reference | What Plan Says | What's Implemented |
|---|---|---|
| Phase 1 checklist | 12 items | 12/12 implemented ✅ |
| Phase 2 checklist | 6 items | 5.5/6 (few-shot examples missing) ⚠️ |
| Phase 3 checklist | 8 items | 7/8 (no veracity tests) ⚠️ |
| Phase 4 checklist | 10 items | 7/10 (no tests, no CLI, no retry) ⚠️ |
| `SCHEMA_PROMPT_V2` | Includes `<few_shot_examples>` | Missing few-shot section |
| `preprocess_image()` | Tracks upscale in ops list | Upscale not tracked |
| `process_image()` | Uses retry wrapper | Direct API call |
| `main.py` | `--mode audio\|image` flag | Audio-only CLI |
| Image validation | Path traversal guard | Missing |
| Veracity trigger | Uses `VERACITY_TRIGGER_CONFIDENCE` constant | Hardcoded `"LOW"` |

---

## 7. Recommended Action Items (Priority Order)

### Immediate (this sprint)
1. **Add path traversal guard** to `validate_image_file()` (Issue #2)
2. **Add retry logic** to `process_image()` via `call_gemini_generic()` (Issue #3)
3. **Deduplicate** confidence marker extraction — reuse `parser.extract_confidence_markers()` in `image_processor.py` (Issue #5)

### Short-term (next sprint)
4. **Write tests** for veracity, image preprocessor, and image processor modules (Issue #1)
5. **Add few-shot examples** to `SCHEMA_PROMPT_V2` (Issue #4)
6. **Add `--mode` CLI flag** for image processing (Issue #6)
7. **Include transcript text** in veracity pass (Issue #7)
8. **Re-run validation** after veracity corrections (Issue #15)

### Nice-to-have
9. Fix `None` → `"NONE"` confidence handling (Issue #12)
10. Fix duplicate dict key in test (Issue #14)
11. Extend `build_response_envelope()` API (Issue #8)
12. Track upscale in ops list (Issue #10)
13. Use `VERACITY_TRIGGER_CONFIDENCE` constant (Issue #11)
14. Guard against double `__confidence` suffix (Issue #13)
15. Return preprocessing metadata from `process_image()` (Issue #9)
16. Guard empty-vs-empty score in `_score_algorithms()` (Issue #16)

---

## 8. Conclusion

The implementation is **substantially complete** and of high quality. All four confirmed bugs
are fixed and verified with a 261-test regression suite (0 failures). The veracity pass and
image processing modules are functional but need:
- Test coverage (critical gap)
- Security hardening (`validate_image_file` path traversal guard)
- Resilience hardening (`process_image` retry logic)
- CLI integration (image processing not accessible from command line)
- A few plan/code alignment fixes (few-shot examples, deduplication)

The overall architecture — shared validation layer, modality-agnostic pipeline, confidence
marker flow — is well-designed and correctly implemented. The issues found are fixable with
targeted, low-risk changes in a single sprint.
