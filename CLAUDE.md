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

Required: `GOOGLE_API_KEY` (starts with `AIza…`). Optional overrides: `GEMINI_MODEL`, `PROCESSOR_LOG`, `ENCRYPTION_KEY_PATH`.

## Project Structure

```
Unifix/
├── .env                          ← API keys (gitignored)
├── proxy.py                      ← Dev proxy for API key injection (port 8787)
├── workorder_processing/
│   ├── main.py                   ← CLI entry point
│   ├── processor/
│   │   ├── exceptions.py         ← Typed exception hierarchy
│   │   ├── logging_config.py     ← Structured logging + PII sanitizer
│   │   ├── validator.py          ← Audio file pre-flight validation (6 checks)
│   │   ├── gemini_client.py      ← Client factory, prompt, retry wrapper
│   │   ├── parser.py             ← JSON parse, jsonschema validate, audit envelope
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
│   │   ├── conftest.py           ← sys.path fix for pytest
│   │   ├── test_fuzzy_resolver.py ← 68 tests (workers, companies, locations, equipment, parts, time)
│   │   └── test_edge_cases.py    ← 159 tests (12 categories: whitespace, typos, ambiguity, injection, etc.)
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
python main.py audio/test_audio.wav
python main.py audio/test_audio.wav -o outputs/ -p   # with output dir + plaintext sidecar
```

### Pipeline

1. **Validate** — Path traversal guard, file existence, filename safety, extension allowlist,
   size bounds (1 KB–200 MB), magic-byte MIME sniffing.
2. **Call Gemini** — Audio sent inline via `types.Part.from_bytes()` with the schema prompt
   (built from `schemas/work_order_v1.json`). 3-attempt exponential backoff on 429/5xx.
3. **Parse & Validate** — Markdown fence stripping, `json.loads()`, `jsonschema.validate()`
   against `schemas/work_order_v1.json`.
4. **Encrypt & Write** — Full audit envelope (SHA-256, token counts, finish reason) written
   as Fernet-encrypted `.json.enc`. Key stored at `~/.workorder_processor.key` (chmod 600).

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_API_KEY` | *(required)* | Google AI API key |
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
Overall status: `PASS` / `REVIEW` (any LOW_CONF) / `FAIL` (NO_MATCH on required fields).

```python
from validator.work_order_validator import validate_work_order
result = validate_work_order(extracted_json)
print(result.overall_status, result.unresolved_fields)
```

`db/reference_data.py` is the mock store — replace its getter functions with real DB queries in production.

## Fuzzy Matching Evaluation

Full evaluation report: `FUZZY_MATCHING_EVALUATION.md`

### Known Bugs

| # | Severity | Bug | Location |
|---|---|---|---|
| 1 | Low | Whitespace degrades EXACT → HIGH_CONF (no `.strip()` before scoring) | `validator/fuzzy_resolver.py:_score_algorithms()` |
| 2 | Medium | `30m` duration shorthand not recognised (regex requires `in` after `m`) | `validator/time_validator.py:_DURATION_PATTERNS[2]` |
| 3 | Low | Durations >99h fail (`\d{1,2}` limits hours) | `validator/time_validator.py:_DURATION_PATTERNS[3]` |
| 4 | **High** | Empty required fields don't fail validation (`EMPTY` not in failure check) | `validator/resolution.py:compute_overall_status()` |

### Scoring Model

Each fuzzy field uses a weighted multi-algorithm ensemble from rapidfuzz:
- `worker`: Jaro-Winkler (0.40), token_sort_ratio (0.35), ratio (0.25)
- `company`: token_set_ratio (0.40), WRatio (0.35), partial_ratio (0.25)
- `location`: partial_ratio (0.40), token_set_ratio (0.35), token_sort_ratio (0.25)
- `vehicle_equipment`: token_set_ratio (0.40), partial_ratio (0.30), ratio (0.30)
- `parts_used`: token_set_ratio (0.45), WRatio (0.35), partial_ratio (0.20)

Thresholds: EXACT ≥ 100, HIGH_CONF ≥ 85, LOW_CONF ≥ 60, NO_MATCH < 60.
Parts use **minimum** per-token score for overall status.

## Test Database (Legacy SQLite)

SQLite with FTS5 full-text search. 81 seeded records across `equipment`, `companies`, `locations`, `parts`.

```bash
python test_database/database.py --seed                      # seed (idempotent)
python test_database/database.py --query equipment "cat dozer"
```

Uses `difflib.SequenceMatcher` (stdlib only). The `validator/` package supersedes this for
post-extraction validation; `test_database/` remains as a standalone reference lookup tool.

## Proxy (Development)

`proxy.py` injects the real API key on port 8787, keeping it out of the codebase. Run with:
```bash
python proxy.py
```

## Common Tasks

```bash
# Process an audio file
cd workorder_processing && python main.py audio/test_audio.wav -p

# Change the model
GEMINI_MODEL=gemini-2.5-pro python main.py audio/test_audio.wav

# Run the validator test suite (68 original + 159 edge case = 227 total)
cd workorder_processing && python -m pytest tests/ -v

# Run only edge case tests
cd workorder_processing && python -m pytest tests/test_edge_cases.py -v

# Run the noisy test work order through the full validator
cd workorder_processing && python -c "
import json
from validator.work_order_validator import validate_work_order
with open('test_data/edge_case_work_order.json') as f:
    order = json.load(f)
result = validate_work_order(order)
print(f'Status: {result.overall_status.value}')
print(f'Unresolved: {result.unresolved_fields}')
print(f'Review: {result.review_fields}')
"

# Validate an audio file without sending to Gemini
python -c "from processor.validator import validate_audio_file; print(validate_audio_file('audio/test_audio.wav'))"

# Decrypt an output file
python -c "from processor.crypto import read_encrypted_json; import json; print(json.dumps(read_encrypted_json('outputs/test_audio.json.enc'), indent=2))"
```
