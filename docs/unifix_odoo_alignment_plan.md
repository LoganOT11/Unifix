# Unifix Odoo Module — Alignment Plan

**Date:** 2026-06-10  
**Status:** Draft — pending review  
**Scope:** Align `unifix_odoo` module with `workorder_processing` prototype + Odoo v19 best practices

---

## 1. Executive Summary

The `unifix_odoo` module (formerly `unifix_video`) currently implements a **video-only** processing pipeline with several critical gaps compared to the `workorder_processing` Python prototype. This plan addresses:

1. **Missing processing modes** — no audio-only or image support
2. **Prompt/schema misalignment** — generic transcript prompt vs structured extraction prompts
3. **Missing fields** — `vehicle_equipment` not extracted
4. **Wrong Gemini model** — `gemini-2.5-pro` vs `gemini-3.1-flash-lite`
5. **Missing validation/fuzzy matching** — no DB-backed field verification
6. **Missing confidence tracking** — no `__confidence` markers
7. **Missing veracity checking** — no second-pass quality gate
8. **Odoo v19 conformance** — minor view syntax issues

---

## 2. Current State Analysis

### 2.1 What `unifix_odoo` Does Today

| Component | Status | Notes |
|-----------|--------|-------|
| Video upload | ✅ Working | Streams to temp file, creates job record |
| Audio extraction | ✅ Working | ffmpeg mono 16kHz WAV |
| Gemini transcription | ✅ Working | Returns transcript + segments + keyframes + fields |
| Frame extraction | ✅ Working | JPEG frames at Gemini-specified timestamps |
| Job state machine | ✅ Working | `received → processing → done \| failed` |
| Cleanup cron | ✅ Working | Hourly orphan cleanup |
| Views | ✅ Working | List, form (with Frames tab), search |
| Settings | ✅ Working | API key, model, keyframe config |

### 2.2 Critical Gaps vs Prototype

| Feature | `workorder_processing` | `unifix_odoo` | Gap |
|---------|------------------------|---------------|-----|
| Audio-only processing | ✅ `AudioPipeline` | ❌ Not supported | **Missing mode** |
| Image processing | ✅ `ImagePipeline` + preprocessing | ❌ Not supported | **Missing mode** |
| `vehicle_equipment` field | ✅ Required in schema | ❌ Missing | **Missing field** |
| Structured extraction prompt | ✅ `audio_extraction_v2.txt` with field defs, rules, examples | ❌ Generic `transcript_v1.txt` | **Prompt mismatch** |
| Confidence markers | ✅ 5 fields with `__confidence` | ❌ None | **Missing feature** |
| Fuzzy DB validation | ✅ `FuzzyResolver` with weighted ensemble | ❌ None | **Missing validation** |
| Time normalization | ✅ `normalise_clock_time()`, `normalise_duration()` | ❌ None | **Missing validation** |
| Veracity checking | ✅ Second Gemini pass on REVIEW/FAIL/LOW | ❌ None | **Missing QA gate** |
| Schema validation | ✅ `jsonschema.validate()` | ❌ None | **Missing validation** |
| Output encryption | ✅ Fernet `.json.enc` | ❌ None | **Missing security** |
| Gemini model | `gemini-3.1-flash-lite` | `gemini-2.5-pro` | **Wrong model** |

---

## 3. Odoo v19 Architecture Requirements

Based on the design docs (`odoo_v19_implementation_plan.md`, `architecture.md`):

### 3.1 Hard Constraints (from `odoo_plan.md`)

| # | Constraint | Current Status |
|---|------------|----------------|
| 1 | No video in Odoo storage | ✅ Compliant — video never becomes `ir.attachment` |
| 2 | Heavy compute outside HTTP workers | ✅ Compliant — cron worker runs ffmpeg + Gemini |
| 3 | Async, not synchronous | ✅ Compliant — cron-based processing |
| 4 | Odoo = system-of-record for derived data | ✅ Compliant — transcript, segments, keyframes stored |
| 5 | v19 conformance | ⚠️ Minor issues (see §3.2) |

### 3.2 v19 Conformance Issues

| Issue | Location | Fix |
|-------|----------|-----|
| `<tree>` tag (deprecated since v18) | `views/video_job_views.xml` | Already uses `<list>` ✅ |
| `attrs="{}"` syntax (deprecated since v17) | `views/video_job_views.xml` | Already uses inline `invisible=` ✅ |
| `type='json'` route (should be `type='http'`) | `controllers/upload.py` | Already uses `type='http'` ✅ |
| CSS prefix `o_unifix` | N/A | Not applicable (no custom CSS) |
| Model naming `unifix.*` | All models | ✅ Compliant |

**Verdict:** The module is already v19-compliant. No changes needed.

### 3.3 Recommended Odoo Dependencies

The current manifest depends only on `base`. For a production work-order system, consider adding:

| Module | Purpose | Priority |
|--------|---------|----------|
| `maintenance` | Equipment/asset tracking, link `vehicle_equipment` to `maintenance.equipment` | **High** |
| `hr` | Technician/employee records, link `worker_name` to `hr.employee` | **High** |
| `helpdesk` | Work order ticket integration | Medium |
| `stock` | Parts inventory, link `parts_used` to `stock.product` | Medium |
| `account` | Expense tracking (image schema has `expenses`) | Low |

**Decision required:** Whether to add these dependencies now or defer to Phase 2.

---

## 4. Alignment Plan

### Phase 1: Critical Fixes (Must-Have)

#### 4.1 Add `vehicle_equipment` Field

**Files to modify:**
- `models/video_job.py` — add field
- `views/video_job_views.xml` — add to form/list
- `prompts/transcript_v1.txt` — add to extraction instructions

```python
# models/video_job.py — add after 'location'
vehicle_equipment = fields.Char(
    help="Asset tag, equipment ID, or name of vehicle/machine serviced"
)
```

#### 4.2 Update Prompt to Match Prototype

**File to modify:** `prompts/transcript_v1.txt`

Replace the generic transcript prompt with the structured extraction prompt from `audio_extraction_v2.txt`. The prompt should:

1. Include detailed field definitions for all 14 fields (including `vehicle_equipment`)
2. Add extraction rules (5 rules from prototype)
3. Add confidence markers for 5 key fields (`worker`, `company`, `location`, `vehicle_equipment`, `parts_used`)
4. Include 2 few-shot examples (from prototype)
5. Request `transcript`, `segments`, `keyframes` (video-specific additions)

**New prompt structure:**
```
<system_role>
You are a precise data extraction engine for mechanical and maintenance work order logs.
You receive audio transcripts or documents from field technicians and extract structured
records. Accuracy is critical — every extracted value will be validated against a master
database. Do not infer, guess, or embellish values that are not clearly stated.
</system_role>

<field_definitions>
Extract ONLY the following fields:
- vehicle_equipment : The asset tag, equipment ID, or full name of the vehicle or machine
  being serviced. Examples: "TRK-001", "CAT 320 Excavator", "GEN-005". If multiple assets
  are mentioned, capture the primary one being worked on.
[... all 14 fields ...]
</field_definitions>

<extraction_rules>
1. ONLY extract values explicitly stated or clearly implied. Never invent or estimate.
2. If a field is ambiguous or unclear, use "" rather than guessing.
3. Preserve the speaker's terminology — the downstream system will normalise values.
4. For times: extract exactly what is stated. Do not convert unless unambiguous.
5. Return ONLY the JSON object — no preamble, no markdown fences, no explanation.
</extraction_rules>

<confidence_markers>
For each of these five fields, also emit a confidence suffix:
  vehicle_equipment, worker, company, location, parts_used
Format: "field_name__confidence": "HIGH" | "MEDIUM" | "LOW"
</confidence_markers>

<transcript_format>
Additionally, return:
- "transcript": Full verbatim transcript of the audio
- "segments": Array of {start, end, text, confidence} (5-30 second segments)
- "keyframes": Array of {t, reason} (5-15 important visual moments)
</transcript_format>

<output_schema>
{schema_block}
</output_schema>

<few_shot_examples>
[... 2 examples from audio_extraction_v2.txt ...]
</few_shot_examples>
```

#### 4.3 Change Default Gemini Model

**Files to modify:**
- `data/default_params.xml` — change `gemini-2.5-pro` → `gemini-3.1-flash-lite`
- `models/res_config_settings.py` — update default

```xml
<!-- data/default_params.xml -->
<record id="default_gemini_model" model="ir.config_parameter">
    <field name="key">unifix.gemini_model</field>
    <field name="value">gemini-3.1-flash-lite</field>
</record>
```

#### 4.4 Add Confidence Parsing

**File to modify:** `models/video_job.py` — `_call_gemini()` and `_write_results()`

Parse `__confidence` keys from Gemini response and store them:

```python
# Add fields to store confidence
worker_name_confidence = fields.Selection([
    ('HIGH', 'High'), ('MEDIUM', 'Medium'), ('LOW', 'Low')
])
company_confidence = fields.Selection([...])
location_confidence = fields.Selection([...])
vehicle_equipment_confidence = fields.Selection([...])
parts_used_confidence = fields.Selection([...])

def _write_results(self, result):
    """Persist transcript, segments, work-order fields, and confidences."""
    # Extract confidence markers
    confidences = {}
    for key in list(result.keys()):
        if key.endswith('__confidence'):
            field_name = key.removesuffix('__confidence')
            confidences[field_name] = result.pop(key).upper()
    
    self.write({
        'transcript': result.get('transcript', ''),
        'worker_name': result.get('worker', ''),
        'worker_name_confidence': confidences.get('worker', 'MEDIUM'),
        'company': result.get('company', ''),
        'company_confidence': confidences.get('company', 'MEDIUM'),
        'location': result.get('location', ''),
        'location_confidence': confidences.get('location', 'MEDIUM'),
        'vehicle_equipment': result.get('vehicle_equipment', ''),
        'vehicle_equipment_confidence': confidences.get('vehicle_equipment', 'MEDIUM'),
        # ... other fields ...
        'parts_used': result.get('parts_used', ''),
        'parts_used_confidence': confidences.get('parts_used', 'MEDIUM'),
    })
```

#### 4.5 Add Schema Validation

**File to add:** `models/video_job.py` — `_validate_schema()`

Validate Gemini response against JSON schema before persisting:

```python
import jsonschema

WORK_ORDER_SCHEMA = {
    "type": "object",
    "required": ["worker", "company", "location", "vehicle_equipment",
                  "reported_problem", "diagnosis_cause", "work_performed",
                  "parts_used", "start_time", "end_time", "total_time_spent",
                  "future_recommendations", "remaining_tasks"],
    "properties": {
        "vehicle_equipment": {"type": "string"},
        # ... all fields ...
    }
}

def _validate_schema(self, result):
    """Validate extracted data against JSON schema."""
    try:
        jsonschema.validate(result, WORK_ORDER_SCHEMA)
    except jsonschema.ValidationError as e:
        raise ValueError(f"Schema validation failed: {e.message}")
```

---

### Phase 2: Validation Pipeline (Should-Have)

#### 4.6 Add Fuzzy DB Validation

**Files to add:**
- `models/fuzzy_resolver.py` — port from `workorder_processing/validator/fuzzy_resolver.py`
- `models/reference_data.py` — port from `workorder_processing/db/reference_data.py`
- `models/work_order_validator.py` — port from `workorder_processing/validator/work_order_validator.py`

**Files to modify:**
- `models/video_job.py` — add `_validate_fields()` step after schema validation
- `data/default_params.xml` — add threshold config

**Dependencies to add:**
- `rapidfuzz` — add to `requirements.txt` or Odoo external dependencies

**Decision required:** Whether to:
1. Port the Python validator code into the Odoo module (self-contained)
2. Import from `workorder_processing` package (requires package installation)
3. Create a shared library (cleanest but more complex)

**Recommendation:** Port the code into the module for self-containment. The validator is ~500 lines and has no external dependencies beyond `rapidfuzz`.

#### 4.7 Add Time Normalization

**File to add:** `models/time_validator.py` — port from `workorder_processing/validator/time_validator.py`

Normalize `start_time`, `end_time`, `total_time_spent` to standard formats:
- Clock times → `HH:MM` (24h)
- Durations → `Xh Ym`

#### 4.8 Add Veracity Checking (Optional)

**Files to add:**
- `models/veracity.py` — port from `workorder_processing/processor/veracity.py`
- `prompts/veracity_v1.txt` — port from `workorder_processing/prompts/veracity_v1.txt`

**Files to modify:**
- `models/video_job.py` — add `_run_veracity()` step after field validation

**Decision required:** Whether to implement veracity checking now or defer. The prototype shows it can catch hallucinated values, but doubles API cost.

---

### Phase 3: Audio + Image Support (Nice-to-Have)

#### 4.9 Add Audio-Only Processing

**Files to add:**
- `models/audio_job.py` — new model `unifix.audio.job`
- `controllers/upload.py` — add `/unifix/upload/audio` route
- `views/audio_job_views.xml` — new views
- `prompts/audio_extraction_v2.txt` — port from prototype

**Files to modify:**
- `__manifest__.py` — add new views/security data
- `security/ir.model.access.csv` — add audio job access rules

**Architecture decision:** Create a separate `unifix.audio.job` model or extend `unifix.video.job` to handle both?

**Recommendation:** Extend `unifix.video.job` with a `source_type` field (`video`/`audio`/`image`). This:
- Shares the state machine, cron worker, and views
- Reduces code duplication
- Allows unified job listing

#### 4.10 Add Image Processing

**Files to add:**
- `models/image_job.py` — or extend unified job model
- `controllers/upload.py` — add `/unifix/upload/image` route
- `prompts/image_extraction_v3.txt` — port from prototype
- `schemas/work_order_v3.json` — port from prototype (nested schema)

**Dependencies to add:**
- `opencv-python-headless` — for image preprocessing
- `Pillow` — for image format support
- `numpy` — for OpenCV operations

**Decision required:** Image processing requires:
1. OpenCV for preprocessing (quality assessment, deskew, CLAHE)
2. A different schema (`work_order_v3.json` with nested `tasks[]`, `travel`, `expenses`)
3. A different prompt (`image_extraction_v3.txt`)
4. Different validation logic (image fields don't map to the same fuzzy DB)

**Recommendation:** Defer image support to Phase 3 unless there's an immediate business need. The prototype shows it's a separate pipeline with its own schema and validation rules.

---

### Phase 4: Security + Polish (Optional)

#### 4.11 Add Output Encryption

**Files to add:**
- `models/crypto.py` — port from `workorder_processing/processor/crypto.py`

**Decision required:** The prototype uses Fernet encryption for output files. In Odoo, the database provides encryption at rest. Is additional encryption needed?

**Recommendation:** Defer unless there's a compliance requirement. Odoo's database encryption + access controls provide equivalent protection.

#### 4.12 Add PII Sanitization to Logs

**Files to modify:**
- `models/video_job.py` — sanitize API keys in log messages

The prototype has a `_SanitizingFormatter` that redacts API keys from logs. This is a good practice but low priority.

#### 4.13 Improve Error Handling

**Files to modify:**
- `models/video_job.py` — add exponential backoff for Gemini API calls

The prototype has sophisticated retry logic with exponential backoff (3 attempts, 429/5xx). The Odoo module has basic retry but no backoff.

---

## 5. Implementation Order

### Priority 1 — Do First (1-2 days)
1. ✅ Add `vehicle_equipment` field to model + views
2. ✅ Update prompt to match prototype structure
3. ✅ Change default model to `gemini-3.1-flash-lite`
4. ✅ Add confidence parsing and storage

### Priority 2 — Do Next (2-3 days)
5. Add schema validation
6. Port fuzzy resolver + time validator
7. Wire validation into processing pipeline

### Priority 3 — Do Later (3-5 days)
8. Add audio-only support (extend unified job model)
9. Add veracity checking
10. Add image processing support

### Priority 4 — Optional
11. Output encryption
12. PII log sanitization
13. Exponential backoff for API calls

---

## 6. Testing Strategy

### 6.1 Guard Tests (Already Exist)

The module has tests for:
- `test_no_video_storage.py` — ensures video never persists
- `test_upload.py` — controller tests
- `test_worker.py` — worker processing tests
- `test_keyframes.py` — frame extraction tests

### 6.2 New Tests Needed

| Test | Purpose |
|------|---------|
| `test_vehicle_equipment_extraction` | Verify field is extracted and stored |
| `test_confidence_parsing` | Verify `__confidence` keys are parsed correctly |
| `test_schema_validation` | Verify invalid responses are rejected |
| `test_fuzzy_matching` | Port tests from `workorder_processing/tests/test_fuzzy_resolver.py` |
| `test_time_normalization` | Port tests from prototype |
| `test_audio_mode` | Verify audio-only uploads work (if implemented) |

### 6.3 Porting Existing Tests

The prototype has 233+ tests. Key test files to port:
- `test_fuzzy_resolver.py` (49 tests) — fuzzy matching logic
- `test_edge_cases.py` (124 tests) — pipeline edge cases
- `test_veracity.py` (24 tests) — veracity checking
- `test_image_processing.py` (36 tests) — image preprocessing

---

## 7. Dependencies

### 7.1 Python Packages

| Package | Current | Needed For | Priority |
|---------|---------|------------|----------|
| `google-genai` | ✅ Used | Gemini API | Required |
| `rapidfuzz` | ❌ Missing | Fuzzy validation | Priority 2 |
| `jsonschema` | ❌ Missing | Schema validation | Priority 2 |
| `opencv-python-headless` | ❌ Missing | Image preprocessing | Priority 3 |
| `Pillow` | ❌ Missing | Image format support | Priority 3 |
| `numpy` | ❌ Missing | OpenCV operations | Priority 3 |
| `cryptography` | ❌ Missing | Output encryption | Priority 4 |

### 7.2 System Dependencies

| Dependency | Status | Notes |
|------------|--------|-------|
| `ffmpeg` | ✅ Required | Audio/frame extraction (must be on PATH) |
| Python ≥ 3.11 | ✅ Required | Odoo 19 requirement |

### 7.3 Odoo Module Dependencies

| Module | Current | Recommended | Notes |
|--------|---------|-------------|-------|
| `base` | ✅ | ✅ | Minimum |
| `maintenance` | ❌ | Optional | Equipment tracking |
| `hr` | ❌ | Optional | Employee records |

---

## 8. Open Decisions

| # | Decision | Options | Recommendation |
|---|----------|---------|----------------|
| 1 | **Validation approach** | Port code vs import package | Port code (self-contained) |
| 2 | **Audio/image model design** | Separate models vs unified | Unified with `source_type` field |
| 3 | **Odoo dependencies** | Add `maintenance`, `hr` now vs later | Defer to Phase 2 |
| 4 | **Veracity checking** | Implement now vs later | Defer (doubles API cost) |
| 5 | **Image processing** | Implement now vs later | Defer (separate pipeline) |
| 6 | **Output encryption** | Implement now vs later | Defer (Odoo DB encryption sufficient) |
| 7 | **Reference data source** | Static fixtures vs real DB | Start with fixtures, migrate later |

---

## 9. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Prompt changes break extraction quality | Medium | High | Test with real audio samples before deploying |
| Fuzzy matching adds latency | Low | Medium | Profile with realistic data; tune candidate limits |
| OpenCV not available on Odoo.sh | High | High | Defer image processing or use Pillow-only preprocessing |
| Schema validation rejects valid responses | Medium | Medium | Use lenient validation; log warnings instead of failing |
| Breaking existing video processing | Low | High | Run full test suite after each change |

---

## 10. Success Criteria

### Phase 1 Complete When:
- [ ] `vehicle_equipment` field extracted and displayed
- [ ] Prompt matches prototype structure with field definitions, rules, examples
- [ ] Default model is `gemini-3.1-flash-lite`
- [ ] Confidence markers parsed and stored for 5 fields
- [ ] All existing tests pass
- [ ] Manual test with `test_audio.wav` produces correct extraction

### Phase 2 Complete When:
- [ ] Fuzzy resolver validates extracted fields against reference data
- [ ] Time fields normalized to standard formats
- [ ] Overall validation status computed (PASS/REVIEW/FAIL)
- [ ] Validation results displayed in UI

### Phase 3 Complete When:
- [ ] Audio-only uploads processed correctly
- [ ] Image uploads processed correctly (if implemented)
- [ ] Nested schema (`work_order_v3`) supported for images

---

## Appendix A: File Change Summary

### Files to Modify

| File | Changes |
|------|---------|
| `models/video_job.py` | Add `vehicle_equipment` field, confidence fields, `_validate_schema()`, `_validate_fields()` |
| `models/res_config_settings.py` | Update default model |
| `data/default_params.xml` | Change model to `gemini-3.1-flash-lite` |
| `prompts/transcript_v1.txt` | Complete rewrite to match prototype |
| `views/video_job_views.xml` | Add `vehicle_equipment` + confidence fields |
| `security/ir.model.access.csv` | Update access rules if new models added |

### Files to Add

| File | Purpose |
|------|---------|
| `models/fuzzy_resolver.py` | Port fuzzy matching logic |
| `models/time_validator.py` | Port time normalization |
| `models/work_order_validator.py` | Port validation orchestration |
| `models/reference_data.py` | Static reference data (or DB integration) |
| `models/veracity.py` | Port veracity checking (optional) |
| `prompts/veracity_v1.txt` | Port veracity prompt (optional) |
| `tests/test_validation.py` | New validation tests |

### Files Unchanged

| File | Reason |
|------|--------|
| `__manifest__.py` | Already correct |
| `__init__.py` | Already correct |
| `controllers/upload.py` | Already correct |
| `models/video_segment.py` | Already correct |
| `models/video_keyframe.py` | Already correct |
| `data/cron.xml` | Already correct |
| `security/security.xml` | Already correct |

---

## Appendix B: Prompt Comparison

### Current (`transcript_v1.txt`)
```
You are a professional field-service audio transcriptionist and work-order analyst.
Listen to the provided audio recording of a technician's field visit and return a JSON
object with the following structure:
{
  "transcript": "...",
  "segments": [...],
  "keyframes": [...],
  "worker": "...",
  "company": "...",
  ...
}
```

**Issues:**
- No field definitions
- No extraction rules
- No confidence markers
- No few-shot examples
- Missing `vehicle_equipment`
- Generic instructions

### Proposed (aligned with `audio_extraction_v2.txt`)
```
<system_role>
You are a precise data extraction engine for mechanical and maintenance work order logs.
You receive audio transcripts or documents from field technicians and extract structured
records. Accuracy is critical — every extracted value will be validated against a master
database. Do not infer, guess, or embellish values that are not clearly stated.
</system_role>

<field_definitions>
Extract ONLY the following fields:
- vehicle_equipment : The asset tag, equipment ID, or full name of the vehicle or machine
  being serviced. Examples: "TRK-001", "CAT 320 Excavator", "GEN-005".
- worker : The full name of the technician.
- company : The name of the maintenance company.
[... all 14 fields with detailed definitions ...]
</field_definitions>

<extraction_rules>
1. ONLY extract values explicitly stated or clearly implied. Never invent or estimate.
2. If a field is ambiguous or unclear, use "" rather than guessing.
3. Preserve the speaker's terminology — the downstream system will normalise values.
4. For times: extract exactly what is stated. Do not convert unless unambiguous.
5. Return ONLY the JSON object — no preamble, no markdown fences, no explanation.
</extraction_rules>

<confidence_markers>
For each of these five fields, also emit a confidence suffix:
  vehicle_equipment, worker, company, location, parts_used
Format: "field_name__confidence": "HIGH" | "MEDIUM" | "LOW"
</confidence_markers>

<transcript_format>
Additionally, return:
- "transcript": Full verbatim transcript of the audio
- "segments": Array of {start, end, text, confidence} (5-30 second segments)
- "keyframes": Array of {t, reason} (5-15 important visual moments)
</transcript_format>

<output_schema>
{schema_block}
</output_schema>

<few_shot_examples>
[... 2 detailed examples ...]
</few_shot_examples>
```

---

## Appendix C: Reference Data

The prototype uses static fixture data in `db/reference_data.py`:

```python
WORKERS = [
    "James Hartwell", "Tom Kowalski", "Derek O'Brien",
    "Maria Garcia", "Chen Wei", "Aisha Patel",
    "Robert Johnson", "Sarah Williams"
]

COMPANIES = [
    "Hartwell Industrial Services", "TechMech Solutions",
    "FieldPro Maintenance", "QuickFix Services",
    "Industrial Care Inc", "Maintenance Masters"
]

LOCATIONS = [
    "Main Workshop — Bay 1", "North Plant — Sector 4",
    "Central Warehouse", "East Facility — Unit 7",
    "South Depot — Yard B", "West Complex — Floor 3"
]

VEHICLE_EQUIPMENT = [
    "TRK-001", "TRK-002", "CAT 320 Excavator",
    "GEN-005", "COMP-012", "PUMP-003"
]

PARTS = [
    "OIL-FLT-HVY", "BLT-SERP", "HYD-HOSE-3/4",
    "BRG-6205", "SEAL-O-RING-50", "COMP-FILTER-3/4"
]
```

For production, this should be replaced with queries to Odoo's `maintenance.equipment`, `hr.employee`, etc.

---

**End of document**
