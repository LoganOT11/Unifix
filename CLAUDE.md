# CLAUDE.md — Unifix Work Order Processor

## Conda Environment

This project uses the `unifix` conda environment (Python 3.12).

```bash
conda activate unifix       # activate
conda deactivate            # deactivate
```

Dependencies: `environment.yml` (conda) and `requirements.txt` (pip). Rebuild with:
```bash
conda env create -f environment.yml   # full rebuild
pip install -r requirements.txt       # pip deps only
```

## API Keys

Copy `.env` and fill in your keys. The `.env` file is gitignored and never committed.

Required: `GOOGLE_API_KEY` (legacy `AIza…` or new `AQ.…` format). Required for `AQ.`-format keys: `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GOOGLE_GENAI_USE_VERTEXAI`. Optional overrides: `GEMINI_MODEL`, `PROCESSOR_LOG`, `ENCRYPTION_KEY_PATH`.

## Project Structure

```
Unifix/
├── .env                          ← API keys (gitignored)
├── workorder_processing/
│   ├── main.py                   ← CLI entry point
│   ├── processor/
│   │   ├── exceptions.py         ← Typed exception hierarchy
│   │   ├── logging_config.py     ← Structured logging + PII sanitizer
│   │   ├── validator.py          ← Audio file pre-flight validation (6 checks)
│   │   ├── gemini_client.py      ← Client factory, SCHEMA_PROMPT_V2, retry wrappers
│   │   ├── parser.py             ← JSON parse, schema validate, confidence marker extraction
│   │   ├── veracity.py           ← Second-pass Gemini verification (REVIEW/FAIL trigger)
│   │   ├── image_preprocessor.py ← OpenCV quality assessment, deskew, CLAHE, binarize
│   │   ├── image_processor.py    ← Gemini Vision ingest for handwritten work order images
│   │   └── crypto.py             ← Fernet encrypt/decrypt for output files
│   ├── schemas/
│   │   └── work_order_v1.json    ← Canonical JSON schema (13 required string fields)
│   ├── db/
│   │   └── reference_data.py     ← Mock reference data (workers, companies, locations, equipment, parts)
│   ├── validator/
│   │   ├── models.py             ← MatchStatus, OverallStatus, FieldResult, ValidationResult
│   │   ├── fuzzy_resolver.py     ← rapidfuzz multi-algorithm ensemble per field type
│   │   ├── time_validator.py     ← Clock time + duration normalisation
│   │   ├── resolution.py         ← PASS / REVIEW / FAIL aggregation
│   │   └── work_order_validator.py ← Orchestrator: validate_work_order()
│   ├── tests/
│   │   ├── conftest.py               ← sys.path fix for pytest
│   │   ├── test_fuzzy_resolver.py    ← 68 tests (workers, companies, locations, equipment, parts, time)
│   │   ├── test_edge_cases.py        ← 159 tests (12 categories: whitespace, typos, ambiguity, injection, etc.)
│   │   ├── test_veracity.py          ← 24 tests (should_run_veracity, apply_corrections, run_check)
│   │   └── test_image_processing.py  ← 36 tests (assess_quality, preprocess, validate_image_file, process_image)
│   ├── audio/
│   │   └── test_audio.wav        ← Sample audio for testing
│   ├── test_database/
│   │   ├── database.py           ← SQLite FTS5 reference data + fuzzy matching
│   │   └── test_data.db          ← Seeded DB (equipment, companies, locations, parts)
│   ├── test_data/
│   │   ├── edge_case_work_order.json        ← Noisy work order (partial names, typos, short forms)
│   │   └── edge_case_work_order_clean.json  ← Clean reference work order (all exact matches)
│   └── outputs/                  ← Encrypted .json.enc output files (gitignored)
```

## Work Order Processor

### Quick Start

```bash
cd workorder_processing
python main.py audio/test_audio.wav                         # audio (default)
python main.py form.jpg --mode image                        # image
python main.py audio/test_audio.wav -o outputs/ -p         # with output dir + plaintext sidecar
```

### Pipeline

1. **Validate** — Path traversal guard (`safe_root` check), file existence, filename safety,
   extension allowlist, size bounds, magic-byte MIME sniffing. Both audio and image paths
   enforce path traversal protection.
2. **Call Gemini (Pass 1)** — File sent inline with `SCHEMA_PROMPT_V2` (audio) or
   `IMAGE_EXTRACTION_PROMPT` (image). Both prompts include field definitions, extraction rules,
   few-shot examples, and `__confidence` markers for the 5 fuzzy fields. 3-attempt exponential
   backoff on 429/5xx via `call_gemini_generic()`.
3. **Parse** — Markdown fence stripping, `json.loads()`, then `extract_confidence_markers()`
   splits the 13 work-order fields from the `field__confidence` keys. `None` confidence → `"MEDIUM"`.
4. **Schema validate** — `jsonschema.validate()` on the clean 13-field dict.
5. **Post-extraction validate** — Fuzzy matching + time normalisation via `validate_work_order()`.
   Gemini confidence markers boost/penalise scores (HIGH +5, LOW −10).
6. **Veracity pass (conditional)** — If overall status is REVIEW/FAIL *or* any fuzzy field has
   LOW Gemini confidence, a second Gemini call re-verifies and patches INCORRECT fields.
   Validation is **re-run** after corrections; `veracity_info["validation_post_veracity"]`
   captures the updated status.
7. **Encrypt & Write** — Full audit envelope (SHA-256, token counts, validation status,
   veracity audit trail, optional preprocessing metadata) written as Fernet-encrypted `.json.enc`.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_API_KEY` | *(required)* | Google AI API key (legacy `AIza…` or new `AQ.…`) |
| `GOOGLE_CLOUD_PROJECT` | *(required for AQ)* | Vertex AI project ID |
| `GOOGLE_CLOUD_LOCATION` | `global` | Vertex AI region |
| `GOOGLE_GENAI_USE_VERTEXAI` | `True` | Route through Vertex AI endpoint |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Model ID to use |
| `PROCESSOR_LOG` | `processor.log` | Audit log file path |
| `ENCRYPTION_KEY_PATH` | `~/.workorder_processor.key` | Fernet key location |

### JSON Schema Fields

`vehicle_equipment`, `reported_problem`, `diagnosis_cause`, `work_performed`, `parts_used`,
`start_time`, `end_time`, `total_time_spent`, `future_recommendations`, `remaining_tasks`,
`worker`, `company`, `location` — all required strings.

## Post-Extraction Validator

Resolves Gemini output against a reference database after extraction. Run from `workorder_processing/`.

**Field routing:**
- **Fuzzy DB match** (`rapidfuzz` ensemble): `worker`, `company`, `location`, `vehicle_equipment`, `parts_used`
- **Time format** (normalised to `HH:MM` / `Xh Ym`): `start_time`, `end_time`, `total_time_spent`
- **Pass-through** (free text): `reported_problem`, `diagnosis_cause`, `work_performed`, `future_recommendations`, `remaining_tasks`

**Resolution outcomes:** `EXACT` (≥100) → `HIGH_CONFIDENCE` (≥85) → `LOW_CONFIDENCE` (60–84) → `NO_MATCH` (<60).
Overall status: `PASS` / `REVIEW` (any LOW_CONF) / `FAIL` (NO_MATCH or EMPTY on required fields).

`validate_work_order()` accepts an optional `confidences` dict from Gemini's confidence markers:
```python
from validator.work_order_validator import validate_work_order
result = validate_work_order(extracted_json, confidences={"worker": "HIGH", "company": "LOW"})
print(result.overall_status, result.unresolved_fields)
```

`FieldResult` now includes `top_candidates: list[dict]` (top-3 scored DB matches with scores).
`db/reference_data.py` is the mock store — replace its getter functions with real DB queries in production.

## Fuzzy Matching & Time Validation

### Scoring Model

Each fuzzy field uses a weighted multi-algorithm ensemble from rapidfuzz:
- `worker`: Jaro-Winkler (0.40), token_sort_ratio (0.35), ratio (0.25)
- `company`: token_set_ratio (0.40), WRatio (0.35), partial_ratio (0.25)
- `location`: partial_ratio (0.40), token_set_ratio (0.35), token_sort_ratio (0.25)
- `vehicle_equipment`: token_set_ratio (0.40), partial_ratio (0.30), ratio (0.30)
- `parts_used`: token_set_ratio (0.45), WRatio (0.35), partial_ratio (0.20)

Thresholds: EXACT ≥ 100, HIGH_CONF ≥ 85, LOW_CONF ≥ 60, NO_MATCH < 60.
Parts use **minimum** per-token score for overall status.

**Time normalisation:** `start_time`/`end_time` → `HH:MM` 24h; `total_time_spent` → `Xh Ym`.
Accepts: `8am`, `08:30 PM`, `08:30:00`, `30m`, `2.5 hours`, `90 min`, `1:30`, `100:30`.

## Image Processing (requires `opencv-python-headless`)

```bash
pip install opencv-python-headless Pillow numpy
```

`processor/image_preprocessor.py` — quality assessment (GOOD/FAIR/POOR), deskew (Hough
transform), CLAHE contrast enhancement, denoising, binarization, upscaling.

`processor/image_processor.py` — Gemini Vision ingest. Validates file (with path traversal
guard), optionally preprocesses, sends to Gemini with `IMAGE_EXTRACTION_PROMPT` via retry
wrapper, reuses `extract_confidence_markers()` from `parser.py`.

```python
from processor.image_processor import process_image
work_order, confidences, response, preprocess_result = process_image("form.jpg", client, model_id)
# preprocess_result is PreprocessResult (quality, ops applied) or None for PDFs
```

Supported: JPEG, PNG, WebP, HEIC/HEIF, PDF (scanned). Max 20 MB.

## Test Database (Legacy SQLite)

SQLite + FTS5, 81 seeded records. The `validator/` package supersedes this; `test_database/` remains as a standalone lookup tool. Seed: `python test_database/database.py --seed`

## Proxy (Development)

`proxy.py` injects the real API key on port 8787: `python proxy.py`

## Common Tasks

```bash
# Process an audio file
cd workorder_processing && python main.py audio/test_audio.wav -p

# Change the model
GEMINI_MODEL=gemini-2.5-pro python main.py audio/test_audio.wav

# Run the full test suite (321 tests)
cd workorder_processing && python -m pytest tests/ -v

# Run only edge case tests
cd workorder_processing && python -m pytest tests/test_edge_cases.py -v

# Run only veracity tests
cd workorder_processing && python -m pytest tests/test_veracity.py -v

# Run only image processing tests
cd workorder_processing && python -m pytest tests/test_image_processing.py -v

# Validate an audio file without sending to Gemini
python -c "from processor.validator import validate_audio_file; print(validate_audio_file('audio/test_audio.wav'))"

# Decrypt an output file
python -c "from processor.crypto import read_encrypted_json; import json; print(json.dumps(read_encrypted_json('outputs/test_audio.json.enc'), indent=2))"
```
