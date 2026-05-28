# CLAUDE.md — Unifix Work Order Processor

## Conda Environment

This project uses the `unifix` conda environment (Python 3.12).

```bash
conda activate unifix              # activate
conda deactivate                   # deactivate
conda env create -f environment.yml   # full rebuild
pip install -r requirements.txt       # pip deps only
```

## API Keys

Copy `.env` and fill in your keys. The `.env` file is gitignored and never committed.

Required: `GOOGLE_API_KEY` (legacy `AIza…` or new `AQ.…`). Required for `AQ.` keys: `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GOOGLE_GENAI_USE_VERTEXAI`. Optional: `GEMINI_MODEL`, `PROCESSOR_LOG`, `ENCRYPTION_KEY_PATH`.

## Project Structure

```
Unifix/
├── .env                              ← API keys (gitignored)
├── workorder_processing/
│   ├── main.py                       ← CLI entry point
│   ├── prompts/                      ← Prompt templates (loaded at runtime)
│   │   ├── audio_extraction_v1.txt   ← Legacy audio prompt
│   │   ├── audio_extraction_v2.txt   ← Current audio prompt (active)
│   │   ├── image_extraction_v1.txt   ← Legacy generic image prompt
│   │   ├── image_extraction_v3.txt   ← Current Uni-Fix form prompt (active)
│   │   └── veracity_v1.txt           ← Veracity QA prompt
│   ├── schemas/
│   │   ├── work_order_v1.json        ← Audio schema (13 flat string fields)
│   │   └── work_order_v3.json        ← Image schema (nested: tasks[], travel, expenses)
│   ├── processor/
│   │   ├── prompt_loader.py          ← load_prompt(name, schema_version, schema_format)
│   │   ├── gemini_client.py          ← Client factory, SCHEMA_PROMPT_V2, retry wrappers
│   │   ├── parser.py                 ← JSON parse, schema validate, confidence markers
│   │   ├── veracity.py               ← Second-pass Gemini verification
│   │   ├── image_preprocessor.py     ← OpenCV quality assessment, deskew, CLAHE, binarize
│   │   ├── image_processor.py        ← Gemini Vision ingest (prompt_version="v3" default)
│   │   ├── validator.py              ← Audio file pre-flight validation
│   │   ├── exceptions.py             ← Typed exception hierarchy
│   │   ├── logging_config.py         ← Structured logging + PII sanitizer
│   │   └── crypto.py                 ← Fernet encrypt/decrypt for output files
│   ├── db/
│   │   └── reference_data.py         ← Mock reference data (replace with real DB in prod)
│   ├── validator/
│   │   ├── models.py                 ← MatchStatus, OverallStatus, FieldResult, ValidationResult
│   │   ├── fuzzy_resolver.py         ← rapidfuzz multi-algorithm ensemble per field type
│   │   ├── time_validator.py         ← Clock time + duration normalisation
│   │   ├── resolution.py             ← PASS / REVIEW / FAIL aggregation
│   │   └── work_order_validator.py   ← Orchestrator: validate_work_order()
│   ├── tests/                        ← 321 tests across 4 files
│   ├── audio/test_audio.wav          ← Sample audio
│   ├── images/                       ← Sample images
│   ├── test_database/                ← Legacy SQLite FTS5 lookup (superseded by validator/)
│   └── outputs/                      ← Encrypted .json.enc output files (gitignored)
```

## Quick Start

```bash
cd workorder_processing
python main.py audio/test_audio.wav                    # audio (default, v1 schema)
python main.py form.jpg --mode image                   # image (v3 schema, Uni-Fix form)
python main.py audio/test_audio.wav -o outputs/ -p    # with output dir + plaintext sidecar
GEMINI_MODEL=gemini-2.5-pro python main.py audio/test_audio.wav
```

## Pipeline

**Audio mode** (`--mode audio`, default) — v1 schema, `audio_extraction_v2.txt` prompt:
1. **Validate** — Path traversal guard, extension allowlist, size bounds, magic-byte MIME sniff.
2. **Gemini Pass 1** — `audio_extraction_v2.txt` prompt (with v1 schema block injected). 3-attempt exponential backoff on 429/5xx.
3. **Parse** — Markdown fence strip → `json.loads()` → `extract_confidence_markers()` splits 13 fields from `field__confidence` keys.
4. **Schema validate** — `jsonschema.validate()` against `work_order_v1.json`.
5. **Fuzzy validate** — `validate_work_order()`: rapidfuzz ensemble + time normalisation. Gemini confidence adjusts scores (HIGH +5, LOW −10).
6. **Veracity pass** — Triggered on REVIEW/FAIL status or any LOW confidence. Second Gemini call patches INCORRECT fields; validation re-runs and result stored in `veracity_info["validation_post_veracity"]`.
7. **Encrypt & Write** — Fernet-encrypted `.json.enc` with SHA-256, tokens, validation status, veracity trail.

**Image mode** (`--mode image`) — v3 schema, `image_extraction_v3.txt` prompt:
- Steps 1–3 same; uses `image_extraction_v3.txt` (full v3 schema injected).
- Schema validate against `work_order_v3.json` (nested: `tasks[]`, `travel`, `labour_summary`, `expenses`).
- Fuzzy validate skipped (v3 fields differ); veracity skipped.
- Envelope emits `schema_version: "3.0"`.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_API_KEY` | *(required)* | Legacy `AIza…` or new `AQ.…` format |
| `GOOGLE_CLOUD_PROJECT` | *(required for AQ)* | Vertex AI project ID |
| `GOOGLE_CLOUD_LOCATION` | `global` | Vertex AI region |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Model ID |
| `PROCESSOR_LOG` | `processor.log` | Audit log path |
| `ENCRYPTION_KEY_PATH` | `~/.workorder_processor.key` | Fernet key location |

## Prompts & Schemas

Prompts live in `prompts/` and are loaded at runtime via `processor/prompt_loader.py`:

```python
from processor.prompt_loader import load_prompt
prompt = load_prompt("audio_extraction_v2", schema_version="v1", schema_format="simplified")
prompt = load_prompt("image_extraction_v3", schema_version="v3", schema_format="full")
```

Templates with `{schema_block}` have the schema injected on load. `schema_format="simplified"` renders `{field: type}` dict; `"full"` renders the complete JSON schema document.

**v1 fields** (audio): `vehicle_equipment`, `reported_problem`, `diagnosis_cause`, `work_performed`, `parts_used`, `start_time`, `end_time`, `total_time_spent`, `future_recommendations`, `remaining_tasks`, `worker`, `company`, `location`.

**v3 fields** (image): `company`, `client`, `location`, `date`, `worker`, `travel{legs[], total_km}`, `labour_summary{start, end, total_hours}`, `expenses{repas, logement, avion, autres, total}`, `tasks[]{unit, time{start,end,total}, description, work_order_ref}`, `future_recommendations`, `remaining_tasks`.

## Post-Extraction Validator (v1 / audio only)

**Field routing:**
- **Fuzzy DB match** (`rapidfuzz` ensemble): `worker`, `company`, `location`, `vehicle_equipment`, `parts_used`
- **Time normalise** (`HH:MM` / `Xh Ym`): `start_time`, `end_time`, `total_time_spent`
- **Pass-through**: remaining free-text fields

**Scores:** EXACT ≥ 100 · HIGH_CONF ≥ 85 · LOW_CONF 60–84 · NO_MATCH < 60.
Overall: `PASS` / `REVIEW` (any LOW_CONF) / `FAIL` (NO_MATCH or EMPTY).

Per-field algorithm weights:
- `worker`: Jaro-Winkler (0.40), token_sort_ratio (0.35), ratio (0.25)
- `company`: token_set_ratio (0.40), WRatio (0.35), partial_ratio (0.25)
- `location`: partial_ratio (0.40), token_set_ratio (0.35), token_sort_ratio (0.25)
- `vehicle_equipment`: token_set_ratio (0.40), partial_ratio (0.30), ratio (0.30)
- `parts_used`: token_set_ratio (0.45), WRatio (0.35), partial_ratio (0.20) — uses **minimum** per-token score

`FieldResult` includes `top_candidates: list[dict]` (top-3 scored DB matches with scores). `db/reference_data.py` is the mock store — replace its getter functions with real DB calls in production.

```python
from validator.work_order_validator import validate_work_order
result = validate_work_order(extracted_json, confidences={"worker": "HIGH", "company": "LOW"})
print(result.overall_status, result.unresolved_fields)
```

**Time normalisation:** `start_time`/`end_time` → `HH:MM` 24h; `total_time_spent` → `Xh Ym`.
Accepts: `8am`, `08:30 PM`, `08:30:00`, `30m`, `2.5 hours`, `90 min`, `1:30`, `100:30`.

## Image Processing

Requires `opencv-python-headless Pillow numpy`.

`image_preprocessor.py` — quality assessment (GOOD/FAIR/POOR), deskew (Hough), CLAHE, denoise, binarize, upscale.

```python
from processor.image_processor import process_image
work_order, confidences, response, preprocess_result = process_image(
    "form.jpg", client, model_id, prompt_version="v3"  # default
)
```

Supported formats: JPEG, PNG, WebP, HEIC/HEIF, PDF (scanned). Max 20 MB.

## Development Proxy

`proxy.py` at the repo root injects the real API key on port 8787 for local testing without exposing keys in requests: `python proxy.py`

## Common Tasks

```bash
# Run the full test suite (321 tests)
cd workorder_processing && python -m pytest tests/ -v

# Run a specific test file
python -m pytest tests/test_edge_cases.py -v        # 159 edge-case tests
python -m pytest tests/test_fuzzy_resolver.py -v    # 68 fuzzy-matching tests
python -m pytest tests/test_veracity.py -v          # 24 veracity tests
python -m pytest tests/test_image_processing.py -v  # 36 image-processing tests

# Validate an audio file without calling Gemini
python -c "from processor.validator import validate_audio_file; \
  print(validate_audio_file('audio/test_audio.wav'))"

# Decrypt an output file
python -c "from processor.crypto import read_encrypted_json; import json; \
  print(json.dumps(read_encrypted_json('outputs/test_audio.json.enc'), indent=2))"

# Seed legacy SQLite DB
python test_database/database.py --seed
```
