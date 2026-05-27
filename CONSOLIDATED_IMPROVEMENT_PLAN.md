# Consolidated Improvement Plan
## Prompt Engineering · Bug Fixes · Veracity Pass · Handwritten Image Processing

> **Basis:** Fuzzy Matching Evaluation Report (2026-05-27) — 159 tests, 4 confirmed bugs,
> 7 future recommendations. This document covers all four improvement tracks in priority order.

---

## Table of Contents

1. [Bug Fixes (from Evaluation Report)](#1-bug-fixes-from-evaluation-report)
2. [Prompt Engineering Improvements](#2-prompt-engineering-improvements)
3. [Second-Pass Veracity Check](#3-second-pass-veracity-check)
4. [Handwritten Work Order Image Processing](#4-handwritten-work-order-image-processing)
5. [Unified Pipeline Architecture](#5-unified-pipeline-architecture)
6. [Implementation Checklist](#6-implementation-checklist)

---

## 1. Bug Fixes (from Evaluation Report)

Four confirmed bugs — all targeted fixes, no architectural changes required.

---

### Bug 1 — Whitespace degrades EXACT → HIGH_CONFIDENCE

**File:** `validator/fuzzy_resolver.py` → `_score_algorithms()`

**Root cause:** `.lower()` applied but `.strip()` is not. Leading/trailing spaces deflate
scores from 100.0 to ~91.0.

```python
# BEFORE (buggy)
score = algo_map[algo_name](query.lower(), candidate.lower())

# AFTER
score = algo_map[algo_name](query.strip().lower(), candidate.strip().lower())
```

**Also apply strip() at entry points** to catch whitespace before it reaches scoring:

```python
# In resolve_field()
if not raw_value or not raw_value.strip():   # already present ✅

# Add at top of resolve_field(), after the empty check:
raw_value = raw_value.strip()

# In resolve_parts_used(), after tokenising:
tokens = [t.strip() for t in raw_value.split(",") if t.strip()]   # already present ✅
```

---

### Bug 2 — `30m` duration shorthand not parsed

**File:** `validator/time_validator.py` → `_DURATION_PATTERNS`

**Root cause:** The minute-only regex requires `m` at the end but the pattern
`(\d+)\s*m(?:in(?:ute)?s?)$` does not match bare `30m` because the non-capturing
group `(?:in(?:ute)?s?)` is not optional in all paths.

```python
# BEFORE
re.compile(r'(\d+)\s*m(?:in(?:ute)?s?)$', re.I)

# AFTER — make the full 'in...' suffix optional
re.compile(r'(\d+)\s*m(?:in(?:ute)?s?)?$', re.I)
```

**Also add:** `8am` / `8pm` (no colon) clock time pattern to `_PATTERNS`:

```python
# Add to _PATTERNS list in time_validator.py
(
    re.compile(r'^(\d{1,2})\s*(am|pm)$', re.I),
    lambda m: (
        (int(m.group(1)) % 12) + (12 if m.group(2).lower() == 'pm' else 0),
        0
    )
),
```

**And** decimal duration support — `"2.5 hours"` is natural English and Gemini will produce it:

```python
# Add to _parse_duration(), before existing patterns
m = re.match(r'^(\d+(?:\.\d+)?)\s*h(?:our)?s?$', v, re.I)
if m:
    total_minutes = float(m.group(1)) * 60
    h, mins = divmod(int(total_minutes), 60)
    return f"{h}h {mins}m"
```

---

### Bug 3 — `>99` hour durations rejected

**File:** `validator/time_validator.py` → `_DURATION_PATTERNS[-1]`

**Root cause:** The HH:MM-as-duration pattern uses `\d{1,2}` which rejects `100:00`.

```python
# BEFORE
re.compile(r'^(\d{1,2}):(\d{2})$')

# AFTER
re.compile(r'^(\d+):(\d{2})$')
```

---

### Bug 4 — Empty required fields do not trigger FAIL (data integrity risk)

**File:** `validator/resolution.py` → `compute_overall_status()`

**Root cause:** `MatchStatus.EMPTY` is not included in the FAIL trigger condition.
An empty `worker` or `start_time` silently passes.

```python
# BEFORE
if result.status in (MatchStatus.NO_MATCH, MatchStatus.TIME_INVALID):
    if fname in REQUIRED_RESOLVED_FIELDS:
        unresolved.append(fname)

# AFTER
FAIL_STATUSES = {MatchStatus.NO_MATCH, MatchStatus.TIME_INVALID, MatchStatus.EMPTY}

if result.status in FAIL_STATUSES:
    if fname in REQUIRED_RESOLVED_FIELDS:
        unresolved.append(fname)
    else:
        review.append(fname)   # optional empty field → REVIEW, not FAIL
```

---

### Additional Improvements from Future Considerations

**Location & company canonical resolvers** — currently missing from `CANONICAL_RESOLVERS`:

```python
# db/reference_data.py — add these two functions

def resolve_location_canonical(matched_name: str) -> dict | None:
    for loc in LOCATIONS:
        if loc["name"] == matched_name:
            return loc
    return None

def resolve_company_canonical(matched_name: str) -> dict | None:
    for c in COMPANIES:
        if c["name"] == matched_name or c["short"] == matched_name:
            return c
    return None
```

```python
# validator/fuzzy_resolver.py — add to CANONICAL_RESOLVERS dict
CANONICAL_RESOLVERS = {
    "worker":            resolve_worker_canonical,
    "vehicle_equipment": resolve_equipment_canonical,
    "parts_used":        resolve_part_canonical,
    "location":          resolve_location_canonical,   # NEW
    "company":           resolve_company_canonical,    # NEW
}
```

**Ambiguous location — return top-N candidates:**

When `Bay` matches three bays at similar scores (84.x each), the current system silently
picks the highest. Expose the runner-up candidates so callers can surface them in a UI:

```python
# Add to FieldResult datamodel
@dataclass
class FieldResult:
    ...
    top_candidates: list[dict] = field(default_factory=list)
    # e.g. [{"value": "Main Workshop — Bay 1", "score": 84.6},
    #        {"value": "Main Workshop — Bay 2", "score": 84.1}]
```

```python
# In _find_best_match(), also return top-3
def _find_best_match(query, candidates, weights, top_n=3):
    scored = []
    for candidate in candidates:
        score, algo_scores = _score_algorithms(query, candidate, weights)
        scored.append((candidate, score, algo_scores))
    scored.sort(key=lambda x: x[1], reverse=True)
    best = scored[0] if scored else ("", 0.0, {})
    top = [{"value": c, "score": s} for c, s, _ in scored[:top_n]]
    return best[0], best[1], best[2], top
```

---

## 2. Prompt Engineering Improvements

### Current prompt audit

```
System Role: You are a specialized data extraction assistant for mechanical and maintenance logs.
Your task is to analyze the provided transcript and extract specific technical details into a structured JSON format.
Instructions:
1. Extract the specific values listed in the schema below.
2. Calculate the total_time_spent by finding the difference between the start_time and end_time.
3. Handle Nulls: If a piece of information is not mentioned, use an empty string "".
4. Formatting: Ensure the output is valid JSON.
JSON Schema:
{schema_block}
```

### Problems with the current prompt

| Issue | Impact |
|---|---|
| No field-level definitions | Gemini guesses what "vehicle_equipment" means — could return a description instead of an asset tag |
| No few-shot examples | No grounding for format; JSON shape may vary run-to-run |
| No extraction confidence signal | No way to distinguish "I found this clearly" from "I inferred this" |
| `worker`, `company`, `location` added to schema but not mentioned in instructions | These three fields were not in the original schema; Gemini has no guidance on what level of detail to extract |
| Time calculation instruction is fragile | "Calculate the difference" can produce wrong values if AM/PM is inconsistent; better to extract raw times and let the validator calculate |
| No explicit output instruction | `response_mime_type="application/json"` handles format but the prompt should reinforce it |
| No hallucination guard | Nothing telling Gemini to use `""` rather than inventing a value it can't find |

---

### Improved prompt (v2)

```python
SCHEMA_PROMPT_V2 = """
<system_role>
You are a precise data extraction engine for mechanical and maintenance work order logs.
You receive audio transcripts or documents from field technicians and extract structured
records. Accuracy is critical — every extracted value will be validated against a master
database. Do not infer, guess, or embellish values that are not clearly stated.
</system_role>

<field_definitions>
Extract ONLY the following fields. Each definition explains exactly what to capture:

- vehicle_equipment : The asset tag, equipment ID, or full name of the vehicle or machine
  being serviced. Examples: "TRK-001", "CAT 320 Excavator", "GEN-005". If multiple assets
  are mentioned, capture the primary one being worked on.

- reported_problem : The fault or complaint as described by the operator or requester.
  Use their exact words where possible. Do not diagnose — only report.

- diagnosis_cause : The root cause identified by the technician after inspection.
  If no diagnosis is mentioned, use "".

- work_performed : A concise description of the repair or maintenance actions taken.
  Use active voice: "Replaced X", "Adjusted Y", "Flushed Z".

- parts_used : Comma-separated list of parts, consumables, or materials used.
  Include part numbers if stated. Example: "HF-2240, 15W-40 oil (5L), serpentine belt".
  If no parts were used, use "".

- start_time : The time the technician began work. Use HH:MM (24-hour) if determinable.
  If AM/PM is stated, convert to 24-hour. If unknown, use "".

- end_time : The time work was completed. Same format as start_time.

- total_time_spent : Do NOT calculate this. Extract it verbatim if the technician states
  it (e.g. "took about 3 hours", "we were done in 90 minutes"). If not stated, use "".

- future_recommendations : Any follow-up actions, scheduled maintenance, or warnings
  the technician recommends for the future. If none, use "".

- remaining_tasks : Work that was started but not completed, or items deferred to a
  future visit. If none, use "".

- worker : The full name of the technician performing the work. If only a first name or
  last name is given, capture what is available. If unknown, use "".

- company : The name of the maintenance company or employer performing the work.
  Capture abbreviations or short codes if that is all that is stated. If unknown, use "".

- location : Where the work was performed. Capture as specifically as stated — bay number,
  site name, depot, or address. If unknown, use "".
</field_definitions>

<extraction_rules>
1. ONLY extract values that are explicitly stated or clearly implied in the source material.
   Never invent or estimate a value.
2. If a field is ambiguous or unclear, use "" rather than guessing.
3. Preserve the speaker's terminology for equipment and parts — do not standardise.
   The downstream system will normalise values.
4. For times: extract exactly what is stated. Include AM/PM if present. Do not convert.
5. For parts: preserve the speaker's exact descriptions, including quantities if mentioned.
6. Return ONLY the JSON object — no preamble, no markdown fences, no explanation.
</extraction_rules>

<confidence_markers>
For each of the five database-matched fields (worker, company, location,
vehicle_equipment, parts_used), append a confidence suffix using this format:
  "field_name__confidence": "HIGH" | "MEDIUM" | "LOW"

Use:
- HIGH   — value was stated explicitly and unambiguously
- MEDIUM — value was implied, partially stated, or required minor inference
- LOW    — value is a best guess; you are not confident it is correct

Example output fragment:
  "worker": "James Hartwell",
  "worker__confidence": "HIGH",
  "company": "Hartwell",
  "company__confidence": "MEDIUM"
</confidence_markers>

<few_shot_examples>
Example 1 — Clean input:
  Transcript: "This is Derek O'Brien from Northern Fleet Solutions. Today's date is the
  14th, I'm working on TRK-002, the Peterbilt, out at the North Depot. Started at 8am,
  finished up around half eleven. Reported fault was the engine warning light. Found a
  loose alternator connector — cleaned and reseated it. No parts needed. Job took
  about 3 and a half hours. Recommend full electrical check at next service."

  Expected output:
  {
    "vehicle_equipment":      "TRK-002",
    "vehicle_equipment__confidence": "HIGH",
    "reported_problem":       "Engine warning light on",
    "diagnosis_cause":        "Loose alternator connector",
    "work_performed":         "Cleaned and reseated alternator connector",
    "parts_used":             "",
    "parts_used__confidence": "HIGH",
    "start_time":             "08:00",
    "end_time":               "11:30",
    "total_time_spent":       "3 and a half hours",
    "future_recommendations": "Full electrical check at next service",
    "remaining_tasks":        "",
    "worker":                 "Derek O'Brien",
    "worker__confidence":     "HIGH",
    "company":                "Northern Fleet Solutions",
    "company__confidence":    "HIGH",
    "location":               "North Depot",
    "location__confidence":   "HIGH"
  }

Example 2 — Ambiguous input:
  Transcript: "Tom here, just finished up on the big excavator over at the field site.
  Replaced the hydraulic filter and topped up the fluid. Started around nine, done by
  noon. No other issues."

  Expected output:
  {
    "vehicle_equipment":      "excavator",
    "vehicle_equipment__confidence": "LOW",
    "reported_problem":       "",
    "diagnosis_cause":        "",
    "work_performed":         "Replaced hydraulic filter, topped up hydraulic fluid",
    "parts_used":             "hydraulic filter",
    "parts_used__confidence": "MEDIUM",
    "start_time":             "09:00",
    "end_time":               "12:00",
    "total_time_spent":       "",
    "future_recommendations": "",
    "remaining_tasks":        "",
    "worker":                 "Tom",
    "worker__confidence":     "MEDIUM",
    "company":                "",
    "company__confidence":    "LOW",
    "location":               "field site",
    "location__confidence":   "LOW"
  }
</few_shot_examples>

<output_schema>
{schema_block}
</output_schema>

Analyze the provided source material and return the JSON object now.
"""
```

### How to consume the confidence markers in the validator

```python
# validator/fuzzy_resolver.py — incorporate Gemini confidence into scoring

GEMINI_CONFIDENCE_BOOST = {
    "HIGH":   5.0,   # Add 5 points to composite score
    "MEDIUM": 0.0,   # No adjustment
    "LOW":   -10.0,  # Penalise — treat with more suspicion
}

def resolve_field_with_gemini_confidence(
    field_name: str,
    raw_value: str,
    gemini_confidence: str = "MEDIUM",
) -> FieldResult:
    result = resolve_field(field_name, raw_value)
    boost = GEMINI_CONFIDENCE_BOOST.get(gemini_confidence.upper(), 0.0)
    adjusted_score = max(0.0, min(100.0, result.score + boost))

    if adjusted_score != result.score:
        result.score = adjusted_score
        result.status = _status_from_score(adjusted_score)
        result.notes += f" | Gemini confidence: {gemini_confidence} (boost: {boost:+.1f})"

    return result
```

### Parse the confidence markers out of the response

```python
# processor/parser.py

FUZZY_FIELDS = {"worker", "company", "location", "vehicle_equipment", "parts_used"}

def extract_confidence_markers(raw_json: dict) -> tuple[dict, dict[str, str]]:
    """
    Split Gemini response into clean work order fields and confidence markers.

    Returns:
        work_order: dict with only the 13 schema fields
        confidences: {"worker": "HIGH", "company": "MEDIUM", ...}
    """
    work_order  = {}
    confidences = {}

    for key, value in raw_json.items():
        if key.endswith("__confidence"):
            field_name = key.replace("__confidence", "")
            confidences[field_name] = str(value).upper()
        else:
            work_order[key] = value

    # Default any missing confidence to MEDIUM
    for field in FUZZY_FIELDS:
        confidences.setdefault(field, "MEDIUM")

    return work_order, confidences
```

---

## 3. Second-Pass Veracity Check

### Should you do it? Evaluation

| Factor | Assessment |
|---|---|
| **Cost** | ~2× token cost per work order (one extraction pass + one verification pass). At Gemini Flash pricing this is negligible for low-volume use. For batch processing 1000s of orders, consider only triggering on REVIEW/FAIL. |
| **Latency** | Adds 2–5 seconds per order (sequential). Run in parallel with validation if acceptable. Or run only when first-pass status is REVIEW or FAIL. |
| **Accuracy gain** | High. The model catches its own errors when shown the transcript alongside its output — especially for named entities, times, and ambiguous equipment descriptions. Estimated false positive reduction: 30–50% in practice. |
| **Verdict** | ✅ **Worthwhile**, but gate it: only trigger on REVIEW or FAIL status, or when any fuzzy field has LOW confidence. Don't run on clean PASS orders — it adds cost for no benefit. |

---

### Veracity prompt (v1)

```python
VERACITY_PROMPT = """
<system_role>
You are a quality-assurance reviewer for mechanical work order data extraction.
You will be given the original source material alongside a first-pass JSON extraction.
Your job is to verify each extracted value against the source, identify errors, and
return a corrected JSON with an audit trail.
</system_role>

<task>
For each field in the extracted JSON:
1. Find the evidence in the source material that supports or contradicts the value.
2. Assess whether the value is CORRECT, INCORRECT, or UNCERTAIN.
3. If INCORRECT or UNCERTAIN, provide the corrected or best-available value.
4. Return the fields that require attention — do not return fields that are correct.
</task>

<focus_fields>
Focus your review on these fields only (others are free text and do not need verification):
- worker         (is this the correct person's name as stated in the audio?)
- company        (is this the correct company name as stated?)
- location       (is the location accurately captured?)
- vehicle_equipment (is the correct asset tag or equipment name captured?)
- parts_used     (are all mentioned parts captured? any hallucinated?)
- start_time     (does this time appear in the source?)
- end_time       (does this time appear in the source?)
</focus_fields>

<output_format>
Return a JSON object with this structure:
{
  "verified_fields": {
    "<field_name>": {
      "original_value": "<value from first pass>",
      "verdict":        "CORRECT" | "INCORRECT" | "UNCERTAIN",
      "corrected_value": "<new value, or same as original if CORRECT>",
      "evidence":       "<verbatim quote or description from source supporting your verdict>",
      "confidence":     "HIGH" | "MEDIUM" | "LOW"
    }
  },
  "overall_verdict": "PASS" | "CORRECTIONS_MADE" | "UNCERTAIN",
  "corrections_count": <integer>
}

Only include fields in "verified_fields" where the verdict is INCORRECT or UNCERTAIN,
plus any field where you found evidence worth noting. Omit fields you have no comment on.
</output_format>

<source_material>
{source_transcript_or_description}
</source_material>

<first_pass_extraction>
{first_pass_json}
</first_pass_extraction>

Review the extraction now and return the verification JSON.
"""
```

### Veracity check implementation

```python
# processor/veracity.py

import json
from google.genai import types
from .gemini_client import call_gemini_with_retry, client, MODEL_ID

VERACITY_TRIGGER_STATUSES = {"REVIEW", "FAIL"}
VERACITY_TRIGGER_CONFIDENCE = {"LOW"}


def should_run_veracity(
    validation_result,          # ValidationResult from validator
    gemini_confidences: dict,   # {"worker": "HIGH", ...}
) -> bool:
    """
    Decide whether to run the second-pass veracity check.
    Trigger on: REVIEW/FAIL overall status, OR any LOW Gemini confidence field.
    """
    if validation_result.overall_status.value in VERACITY_TRIGGER_STATUSES:
        return True

    if any(v == "LOW" for v in gemini_confidences.values()):
        return True

    return False


def run_veracity_check(
    source_material: str,        # transcript text or image description
    first_pass_json: dict,
    audio_bytes: bytes | None = None,
    mime_type: str | None = None,
) -> dict:
    """
    Send source material + first-pass JSON back to Gemini for verification.
    Returns the parsed veracity result dict.

    If audio_bytes is provided, sends the original audio alongside the transcript.
    This is more reliable than text-only for verifying spoken names and times.
    """
    prompt = VERACITY_PROMPT.format(
        source_transcript_or_description=source_material,
        first_pass_json=json.dumps(first_pass_json, indent=2),
    )

    contents = []

    # Include original audio if available (most accurate for name/time verification)
    if audio_bytes and mime_type:
        contents.append(
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
        )

    contents.append(prompt)

    response = call_gemini_with_retry(
        client=client,
        model_id=MODEL_ID,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,    # Lower than extraction — we want deterministic corrections
        ),
    )

    return json.loads(response.text)


def apply_veracity_corrections(
    work_order: dict,
    veracity_result: dict,
) -> tuple[dict, list[str]]:
    """
    Apply corrections from the veracity check to the work order dict.
    Returns (corrected_work_order, list_of_corrected_fields).
    """
    corrected = work_order.copy()
    corrected_fields: list[str] = []

    verified = veracity_result.get("verified_fields", {})

    for field_name, verification in verified.items():
        verdict    = verification.get("verdict", "CORRECT")
        corrected_value = verification.get("corrected_value", work_order.get(field_name, ""))

        if verdict == "INCORRECT" and field_name in corrected:
            corrected[field_name] = corrected_value
            corrected_fields.append(field_name)

        # For UNCERTAIN — update if confidence is HIGH in the correction
        elif verdict == "UNCERTAIN" and verification.get("confidence") == "HIGH":
            corrected[field_name] = corrected_value
            corrected_fields.append(field_name)

    return corrected, corrected_fields
```

### Updated main pipeline flow with veracity pass

```
┌─────────────────────────────────────────────────────────────┐
│  1. validate_audio_file()                                   │
│     Input validation (MIME, size, path safety)              │
└─────────────────────────┬───────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────┐
│  2. process_audio()  [Pass 1 — Extraction]                  │
│     Gemini: audio → structured JSON + confidence markers    │
└─────────────────────────┬───────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────┐
│  3. extract_confidence_markers()                            │
│     Split work order fields from __confidence fields        │
└─────────────────────────┬───────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────┐
│  4. validate_work_order()                                   │
│     Fuzzy matching + time validation + resolution status    │
└─────────────────────────┬───────────────────────────────────┘
                          │
                ┌─────────▼──────────┐
                │ should_run_veracity │
                └──────┬──────┬──────┘
               YES     │      │ NO
                       │      └──────────────────────┐
┌──────────────▼──────────────────────┐              │
│  5. run_veracity_check()  [Pass 2]  │              │
│     Gemini: audio + first-pass →    │              │
│     verification JSON               │              │
└──────────────┬──────────────────────┘              │
               │                                     │
┌──────────────▼──────────────────────┐              │
│  6. apply_veracity_corrections()    │              │
│     Patch work order with verdicts  │              │
└──────────────┬──────────────────────┘              │
               │                                     │
               └──────────────┬──────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────┐
│  7. build_response_envelope()                               │
│     SHA-256, token usage, status, audit trail               │
└─────────────────────────────┬───────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────┐
│  8. write_encrypted_json()                                  │
│     AES-encrypted output to disk                            │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. Handwritten Work Order Image Processing

### Can the same pipeline be used? Verdict: ✅ Yes, with a modified input stage

The validation pipeline (fuzzy matching, time validation, resolution logic) is entirely
agnostic to input modality. The only changes needed are:

- Replace audio ingest with image ingest
- Add an optional image preprocessing stage
- Adapt the extraction prompt for visual/document context

---

### 4.1 Is preprocessing necessary?

| Image condition | Direct Gemini Vision accuracy | After preprocessing | Recommendation |
|---|---|---|---|
| Clean scan, good contrast, typed form | ~95% | ~95% | Preprocessing optional |
| Photo on phone, good light | ~85% | ~90% | Preprocessing recommended |
| Faded, pencil, low contrast | ~55% | ~75% | Preprocessing required |
| Skewed / rotated scan | ~60% | ~88% | Deskew required |
| Crumpled, shadow-heavy | ~45% | ~65% | Preprocessing required, results uncertain |
| Very small handwriting | ~50% | ~70% | Preprocessing required |

**Rule of thumb:** Gemini Vision is strong enough to skip preprocessing for clean,
high-contrast images. For field-shot photos or aged documents, preprocessing is
meaningfully worth doing.

---

### 4.2 Preprocessing pipeline

```python
# processor/image_preprocessor.py
"""
Image preprocessing for handwritten work order photos before Gemini Vision ingestion.
Requires: pip install opencv-python-headless Pillow numpy
"""

import cv2
import numpy as np
from PIL import Image
import io
from enum import Enum
from dataclasses import dataclass


class ImageQuality(str, Enum):
    GOOD    = "GOOD"     # No preprocessing needed
    FAIR    = "FAIR"     # Light enhancement recommended
    POOR    = "POOR"     # Full preprocessing pipeline required


@dataclass
class PreprocessResult:
    image_bytes: bytes
    mime_type:   str          # Always "image/jpeg" after processing
    quality_before: ImageQuality
    operations_applied: list[str]
    estimated_improvement: str    # Human-readable note


def assess_quality(img: np.ndarray) -> ImageQuality:
    """
    Heuristic quality assessment based on contrast, brightness variance,
    and edge density (proxy for sharpness).
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img

    # Contrast: standard deviation of pixel values
    std = float(np.std(gray))

    # Brightness: mean pixel value (very dark or very bright = worse)
    mean = float(np.mean(gray))

    # Sharpness: Laplacian variance (higher = sharper)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    if std > 60 and 80 < mean < 200 and sharpness > 100:
        return ImageQuality.GOOD
    elif std > 35 and sharpness > 40:
        return ImageQuality.FAIR
    else:
        return ImageQuality.POOR


def deskew(img: np.ndarray) -> tuple[np.ndarray, float]:
    """
    Detect and correct document skew using Hough line transform.
    Returns (corrected_image, angle_degrees).
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=100,
                            minLineLength=100, maxLineGap=10)

    if lines is None:
        return img, 0.0

    angles = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        if x2 != x1:
            angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
            if abs(angle) < 45:  # Ignore near-vertical lines
                angles.append(angle)

    if not angles:
        return img, 0.0

    median_angle = float(np.median(angles))

    if abs(median_angle) < 0.5:   # Skip trivial corrections
        return img, median_angle

    h, w = img.shape[:2]
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, median_angle, 1.0)
    corrected = cv2.warpAffine(img, M, (w, h),
                               flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)
    return corrected, median_angle


def enhance_contrast(img: np.ndarray) -> np.ndarray:
    """
    Adaptive histogram equalisation (CLAHE) for locally even contrast.
    Works well on faded ink and uneven lighting.
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)


def denoise(img: np.ndarray) -> np.ndarray:
    """
    Non-local means denoising — effective for scanned documents.
    """
    return cv2.fastNlMeansDenoisingColored(img, None, h=10, hColor=10,
                                           templateWindowSize=7,
                                           searchWindowSize=21)


def binarize(img: np.ndarray) -> np.ndarray:
    """
    Adaptive thresholding — converts to black-and-white optimised for text.
    Better than global threshold for uneven illumination.
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=11,
        C=2
    )
    return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)


def upscale_if_small(img: np.ndarray, min_width: int = 1200) -> np.ndarray:
    """
    Upscale images that are too small for reliable OCR/vision.
    Gemini Vision works best with images ≥ 1024px wide.
    """
    h, w = img.shape[:2]
    if w < min_width:
        scale = min_width / w
        new_w, new_h = int(w * scale), int(h * scale)
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
    return img


def preprocess_image(image_bytes: bytes, mime_type: str) -> PreprocessResult:
    """
    Full preprocessing pipeline. Assessment → selective enhancement → encode.
    """
    # Decode
    nparr = np.frombuffer(image_bytes, np.uint8)
    img   = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if img is None:
        raise ValueError("Could not decode image bytes. File may be corrupt.")

    quality = assess_quality(img)
    ops: list[str] = []

    img = upscale_if_small(img)
    if img.shape[1] > 1200:
        ops.append("upscale_if_small (no-op: already large enough)")

    if quality in (ImageQuality.FAIR, ImageQuality.POOR):
        img, angle = deskew(img)
        if abs(angle) > 0.5:
            ops.append(f"deskew ({angle:+.1f}°)")

        img = denoise(img)
        ops.append("denoise")

        img = enhance_contrast(img)
        ops.append("enhance_contrast (CLAHE)")

    if quality == ImageQuality.POOR:
        img = binarize(img)
        ops.append("binarize (adaptive threshold)")

    # Encode to JPEG for Gemini
    _, buffer = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    processed_bytes = buffer.tobytes()

    return PreprocessResult(
        image_bytes=processed_bytes,
        mime_type="image/jpeg",
        quality_before=quality,
        operations_applied=ops if ops else ["none (quality assessed as GOOD)"],
        estimated_improvement={
            ImageQuality.GOOD: "Minimal — image was already clean",
            ImageQuality.FAIR: "Moderate — deskew and denoising applied",
            ImageQuality.POOR: "Significant — full enhancement pipeline applied; "
                               "manual review recommended",
        }[quality],
    )
```

---

### 4.3 Image-specific extraction prompt

The audio prompt needs targeted changes for handwritten documents:

```python
IMAGE_EXTRACTION_PROMPT = """
<system_role>
You are a precise data extraction engine for handwritten mechanical and maintenance
work order forms. You will be given an image of a handwritten or partially printed
work order. Extract the structured data with high accuracy.
</system_role>

<image_reading_guidance>
- Read ALL text visible in the image, including headers, labels, and pre-printed fields.
- If handwriting is unclear, make your best attempt and mark confidence as LOW.
- Printed field labels (e.g. "Technician:", "Unit #:", "Date:") are your field anchors —
  match their values to the schema fields below.
- Common form sections and their schema mappings:
    "Technician" / "Mechanic" / "Performed by"  → worker
    "Company" / "Contractor" / "Employer"        → company
    "Location" / "Site" / "Workshop"             → location
    "Unit #" / "Asset" / "Vehicle" / "Equipment" → vehicle_equipment
    "Fault" / "Complaint" / "Problem reported"   → reported_problem
    "Cause" / "Diagnosis" / "Root cause"         → diagnosis_cause
    "Work done" / "Actions taken" / "Repair"     → work_performed
    "Parts" / "Materials used"                   → parts_used
    "Start" / "Time in" / "Begin"                → start_time
    "Finish" / "Time out" / "End"                → end_time
    "Total time" / "Hours"                       → total_time_spent
    "Recommendations" / "Notes" / "Follow-up"    → future_recommendations
    "Deferred" / "Outstanding" / "Remaining"     → remaining_tasks
- If a field label is not present but the value can be reasonably inferred from context,
  extract it with confidence MEDIUM.
- Do NOT extract values from checked checkboxes unless the value is also written in text.
</image_reading_guidance>

<extraction_rules>
1. Extract only what is legibly written. Use "" for fields that are blank or illegible.
2. Preserve original spelling for names, part numbers, and equipment tags — do not correct.
   The downstream validator will normalise values against a database.
3. For illegible but partially visible text, transcribe what you can see and mark LOW confidence.
4. Return ONLY the JSON object — no preamble, no markdown fences, no explanation.
</extraction_rules>

<confidence_markers>
For worker, company, location, vehicle_equipment, and parts_used, append a confidence suffix:
  "field_name__confidence": "HIGH" | "MEDIUM" | "LOW"

HIGH   = clearly printed or legibly handwritten
MEDIUM = mostly legible but some characters uncertain
LOW    = significant portions illegible or inferred
</confidence_markers>

{schema_block}

Extract the work order data from the provided image now.
"""
```

### 4.4 Image ingest function

```python
# processor/image_processor.py

import os
from google.genai import types
from .gemini_client import call_gemini_with_retry, client, MODEL_ID
from .image_preprocessor import preprocess_image
from .parser import parse_ai_json, extract_confidence_markers

ALLOWED_IMAGE_MIMES = {
    "image/jpeg", "image/jpg", "image/png",
    "image/webp", "image/heic", "image/heif",
    "application/pdf",   # Scanned PDF — Gemini handles natively
}

MAX_IMAGE_SIZE_BYTES = 20 * 1024 * 1024   # 20 MB (Gemini inline limit)


def process_image(
    file_path: str,
    preprocess: bool = True,
) -> tuple[dict, dict, object]:
    """
    Process a handwritten work order image through Gemini Vision.

    Args:
        file_path:   Path to image file.
        preprocess:  Whether to run the image enhancement pipeline.

    Returns:
        (work_order_dict, confidence_markers, raw_response)
    """
    from .validator_bridge import validate_audio_file   # reuse file validation
    # Note: for images, use a separate image validator with image MIME allowlist

    with open(file_path, "rb") as f:
        image_bytes = f.read()

    mime_type = _detect_mime(file_path)
    pre_result = None

    if preprocess and mime_type != "application/pdf":
        pre_result = preprocess_image(image_bytes, mime_type)
        image_bytes = pre_result.image_bytes
        mime_type   = pre_result.mime_type
        print(f"🖼️  Preprocessing: {pre_result.operations_applied}")
        print(f"   Quality: {pre_result.quality_before.value} → {pre_result.estimated_improvement}")

    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

    schema_block = _build_schema_block()
    prompt = IMAGE_EXTRACTION_PROMPT.replace("{schema_block}", schema_block)

    response = call_gemini_with_retry(
        client=client,
        model_id=MODEL_ID,
        contents=[image_part, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )

    raw_json = parse_ai_json(response.text)
    work_order, confidences = extract_confidence_markers(raw_json)

    return work_order, confidences, response


def _detect_mime(file_path: str) -> str:
    import mimetypes
    mime, _ = mimetypes.guess_type(file_path)
    ext = os.path.splitext(file_path)[1].lower()
    fallback = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif",
        ".pdf": "application/pdf",
    }
    return mime if mime in ALLOWED_IMAGE_MIMES else fallback.get(ext, "image/jpeg")
```

---

### 4.5 What preprocessing cannot fix

Be honest with end-users about these limits:

| Condition | Preprocessing helps? | Recommendation |
|---|---|---|
| Fully illegible section | ❌ No | Manual data entry required |
| Two columns of handwriting overlapping | ❌ No | Re-photograph |
| Image is < 200px in smallest dimension | ❌ No | Re-photograph at higher resolution |
| Water damage, torn page | ⚠️ Partial | Extract what's visible; flag for review |
| Non-English handwriting | ✅ Yes (Gemini multilingual) | Specify language in prompt if known |
| Filled-in checkboxes only (no text) | ❌ No | Extend schema with checkbox fields |

---

## 5. Unified Pipeline Architecture

All three input modalities (audio, image, veracity) now share the same validation core:

```
┌──────────────────────────────────────────────────────────────────────┐
│                        INPUT LAYER                                   │
│                                                                      │
│   Audio (.mp3/.wav/…)          Image (.jpg/.png/…/PDF)              │
│        │                               │                             │
│   validate_audio_file()       validate_image_file()                 │
│        │                               │                             │
│   process_audio()    ←——————→  preprocess_image()                   │
│   [Pass 1 Gemini]              process_image()                       │
│        │                       [Pass 1 Gemini Vision]               │
│        └────────────────┬───────────────┘                           │
└─────────────────────────┼────────────────────────────────────────────┘
                          │
               extract_confidence_markers()
                          │
┌─────────────────────────▼────────────────────────────────────────────┐
│                     VALIDATION LAYER (shared)                        │
│                                                                      │
│         validate_work_order()                                        │
│         ├── fuzzy_resolver    (worker/company/location/equip/parts)  │
│         ├── time_validator    (start/end/duration)                   │
│         └── resolution        (PASS / REVIEW / FAIL)                │
└─────────────────────────┬────────────────────────────────────────────┘
                          │
             should_run_veracity()?
                   │         │
                  YES        NO
                   │         │
┌──────────────────▼──────┐  │
│   run_veracity_check()  │  │
│   [Pass 2 Gemini]       │  │
│   apply_corrections()   │  │
└──────────────┬──────────┘  │
               └─────────────┘
                          │
┌─────────────────────────▼────────────────────────────────────────────┐
│                      OUTPUT LAYER (shared)                           │
│                                                                      │
│         build_response_envelope()                                    │
│         write_encrypted_json()                                       │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 6. Implementation Checklist

Work through in order. Each phase is independently deployable.

### Phase 1 — Bug Fixes (1–2 hours)
- [ ] Bug 1: Add `.strip()` in `_score_algorithms()` for both query and candidate
- [ ] Bug 1b: Add `.strip()` at entry of `resolve_field()` after empty check
- [ ] Bug 2: Fix `30m` regex — make `(?:in...)?` optional
- [ ] Bug 2b: Add `8am/8pm` (no-colon) clock time pattern to `_PATTERNS`
- [ ] Bug 2c: Add decimal duration support (`2.5 hours`)
- [ ] Bug 3: Change `\d{1,2}` to `\d+` in HH:MM duration pattern
- [ ] Bug 4: Add `MatchStatus.EMPTY` to `FAIL_STATUSES` for required fields
- [ ] Add `resolve_location_canonical()` and `resolve_company_canonical()` to `reference_data.py`
- [ ] Add both to `CANONICAL_RESOLVERS` in `fuzzy_resolver.py`
- [ ] Add `top_candidates: list[dict]` to `FieldResult` dataclass
- [ ] Update `_find_best_match()` to return top-N candidates
- [ ] Re-run all 227 tests; verify 4 known-bug tests now flip from wrong → correct

### Phase 2 — Prompt Engineering (2–4 hours)
- [ ] Replace `SCHEMA_PROMPT` with `SCHEMA_PROMPT_V2` in `main.py`
- [ ] Implement `extract_confidence_markers()` in `processor/parser.py`
- [ ] Implement `resolve_field_with_gemini_confidence()` in `fuzzy_resolver.py`
- [ ] Wire confidence extraction and boost into `validate_work_order()` orchestrator
- [ ] Update `build_response_envelope()` to include Gemini confidence markers in output
- [ ] Manual test: run 3–5 real audio recordings through the new prompt; compare output quality

### Phase 3 — Veracity Pass (3–5 hours)
- [ ] Implement `VERACITY_PROMPT` in `processor/veracity.py`
- [ ] Implement `should_run_veracity()` with trigger logic
- [ ] Implement `run_veracity_check()` (text-only and audio+text variants)
- [ ] Implement `apply_veracity_corrections()` with INCORRECT + UNCERTAIN handling
- [ ] Wire veracity pass into `main()` between validation and output
- [ ] Add veracity audit trail to response envelope: `"veracity_pass": {"ran": true, "corrections": [...]}`
- [ ] Write tests: verify veracity triggers on REVIEW/FAIL, skips on PASS
- [ ] Write tests: verify corrections are applied correctly; CORRECT verdicts don't change values

### Phase 4 — Image Processing (4–8 hours)
- [ ] Install dependencies: `opencv-python-headless Pillow numpy`
- [ ] Implement `assess_quality()`, `deskew()`, `enhance_contrast()`, `denoise()`, `binarize()`, `upscale_if_small()` in `image_preprocessor.py`
- [ ] Implement `preprocess_image()` orchestrator
- [ ] Implement `IMAGE_EXTRACTION_PROMPT` with field label mappings
- [ ] Implement `process_image()` in `image_processor.py`
- [ ] Add image MIME allowlist and image-specific file validation
- [ ] Add `--mode audio|image` CLI flag to `main.py`
- [ ] Test preprocessing on: clean scan, phone photo, faded photocopy, skewed scan
- [ ] Write preprocessing unit tests: quality assessment accuracy, deskew angle, binarize output
- [ ] Write integration tests: full image → JSON pipeline with mock Gemini responses

### Phase 5 — New Tests for All Changes
- [ ] 4 bug-fix regression tests (one per bug, asserting corrected behaviour)
- [ ] Confidence marker parsing tests (strip, parse, default to MEDIUM)
- [ ] Confidence boost tests (HIGH/MEDIUM/LOW boosts change score correctly)
- [ ] Veracity trigger tests (REVIEW → runs, PASS → skips, LOW confidence → runs)
- [ ] Veracity correction tests (INCORRECT applied, CORRECT ignored)
- [ ] Image preprocessing tests (quality classification, ops applied per quality tier)
- [ ] Image integration test (end-to-end with sample test image)

---

*Last updated: 2026-05-27 | Extends: FUZZY_MATCHING_PLAN.md + IMPLEMENTATION_PLAN.md*
