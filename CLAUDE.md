# CLAUDE.md — Unifix Work Order Processor

## Conda Environment

```bash
conda activate unifix
conda env create -f environment.yml   # full rebuild
pip install -r requirements.txt       # pip deps only
```

## API Keys

Copy `.env` and fill in your keys (gitignored, never committed).

Required: `GOOGLE_API_KEY` (legacy `AIza…` or new `AQ.…`). For `AQ.` keys also set: `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GOOGLE_GENAI_USE_VERTEXAI`.

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_API_KEY` | *(required)* | Gemini API key |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Model ID |
| `PROCESSOR_LOG` | `processor.log` | Audit log path |
| `ENCRYPTION_KEY_PATH` | `~/.workorder_processor.key` | Fernet key location |

## Project Structure

```
workorder_processing/
├── main.py                        ← Thin CLI shim (~157 lines)
├── audio/
│   ├── main.py                    ← Legacy standalone audio script (predates pipeline; kept for reference)
│   └── test_audio.wav             ← Sample audio file for testing
├── video/
│   └── 8224.mp4                   ← Sample video file for testing
├── images/                        ← Sample image files and their outputs
├── outputs/                       ← Default output directory for encrypted JSON
├── config/
│   ├── __init__.py                ← DocumentConfig dataclasses + load_document_config()
│   ├── settings.py                ← App-level constants (DEFAULT_MODEL, IMAGE_EXTENSIONS)
│   └── document_types/
│       ├── audio_v1.yaml          ← Field weights, thresholds, fuzzy/time/free-text routing
│       ├── audio_v1_fr.yaml       ← French-language audio config variant
│       └── image_v3.yaml          ← Image document type config
├── pipeline/
│   ├── __init__.py                ← @register() decorator + get_pipeline() factory
│   ├── base.py                    ← BasePipeline ABC + PipelineContext dataclass
│   ├── audio.py                   ← AudioPipeline
│   ├── video.py                   ← VideoPipeline (extends AudioPipeline)
│   └── image.py                   ← ImagePipeline
├── db/
│   ├── protocol.py                ← ReferenceDataProvider Protocol
│   ├── memory.py                  ← InMemoryProvider (wraps reference_data.py lists)
│   └── reference_data.py          ← Static fixture data (replace with real DB in prod)
├── processor/
│   ├── gemini_client.py           ← create_client(), call_gemini_with_retry(…, prompt), call_gemini_generic()
│   ├── prompt_loader.py           ← load_prompt(name, schema_version, schema_format)
│   ├── parser.py                  ← parse_ai_json(), validate_extracted_data(), build_response_envelope()
│   ├── veracity.py                ← should_run_veracity(), run_veracity_check(), apply_veracity_corrections()
│   ├── image_preprocessor.py      ← OpenCV quality assessment, deskew, CLAHE, binarize
│   ├── image_processor.py         ← validate_image_file(), process_image(…, prompt)
│   ├── validator.py               ← validate_audio_file(), validate_video_file()
│   ├── video_extractor.py         ← extract_audio_from_video(), is_video_extension()
│   ├── exceptions.py              ← InputValidationError, APICallError, etc.
│   ├── logging_config.py          ← Structured logging + PII sanitizer
│   └── crypto.py                  ← Fernet encrypt/decrypt
├── validator/
│   ├── models.py                  ← MatchStatus, OverallStatus, FieldResult, ValidationResult
│   ├── fuzzy_resolver.py          ← FuzzyResolver class (config-driven rapidfuzz ensemble)
│   ├── time_validator.py          ← Clock time + duration normalisation
│   ├── resolution.py              ← compute_overall_status() → PASS / REVIEW / FAIL
│   └── work_order_validator.py    ← validate_work_order(extracted, config, provider, confidences)
├── prompts/                       ← Prompt templates loaded at runtime
│   ├── audio_extraction_v1.txt    ← Audio extraction prompt (v1)
│   ├── audio_extraction_v2.txt    ← Audio extraction prompt (v2)
│   ├── audio_extraction_v2_fr.txt ← French audio extraction prompt
│   ├── image_extraction_v1.txt    ← Image extraction prompt (v1)
│   ├── image_extraction_v3.txt    ← Image extraction prompt (v3)
│   └── veracity_v1.txt            ← Veracity check prompt
├── schemas/
│   ├── work_order_v1.json         ← Audio schema (13 flat string fields)
│   ├── work_order_v1_fr.json      ← French audio schema variant
│   └── work_order_v3.json         ← Image schema (nested: tasks[], travel, expenses)
├── test_data/                     ← Edge-case fixtures for testing
├── test_database/                 ← SQLite test database
└── tests/                         ← 233 tests across 4 files
```

## Quick Start

```bash
cd workorder_processing
python main.py audio/test_audio.wav                    # audio (auto-detected)
python main.py form.jpg --mode image                   # image (Uni-Fix form)
python main.py video.mp4                               # video (auto-detected)
python main.py audio/test_audio.wav -o outputs/ -p    # output dir + plaintext sidecar
GEMINI_MODEL=gemini-2.5-pro python main.py audio/test_audio.wav
```

## Pipeline Architecture

Each mode is a `BasePipeline` subclass registered via `@register("audio"|"video"|"image")`. The shared `run()` template executes these steps in order:

1. `validate_input()` — path traversal guard, extension allowlist, size bounds, MIME sniff
2. `preprocess()` — read bytes (audio/video: ffmpeg extract; image: OpenCV enhance)
3. `_load_prompt()` — loads from `config.prompt` + injects schema block
4. `extract()` — `call_gemini_with_retry()` with exponential backoff (3 attempts, 429/5xx)
5. `_parse()` — strip markdown fences → JSON → split `field__confidence` keys
6. `_validate_schema()` — `jsonschema.validate()` against `work_order_{version}.json`
7. `_validate_fields()` — `FuzzyResolver` + time normalisation (audio only; image skips)
8. `_run_veracity()` — second Gemini pass if REVIEW/FAIL or any LOW confidence (audio only)
9. `_build_envelope()` — wraps extracted data with SHA-256, token usage, validation trail
10. `_serialize()` — Fernet-encrypted `.json.enc`; optional plaintext `.json` sidecar

## Adding a New Document Type

1. Create `config/document_types/{type}_v1.yaml` (copy `audio_v1.yaml` as template)
2. Create `pipeline/{type}.py` with `@register("{type}")` on the subclass
3. No other files need to change

## Key APIs

```python
# Config loading
from config import load_document_config
cfg = load_document_config("audio_v1")   # DocumentConfig dataclass

# Pipeline dispatch
from pipeline import get_pipeline
from db.memory import InMemoryProvider
pipeline = get_pipeline("audio", cfg, InMemoryProvider())
envelope = pipeline.run(ctx)             # PipelineContext

# Field validation (audio v1)
from validator.work_order_validator import validate_work_order
result = validate_work_order(extracted, cfg.validation, provider, confidences)
print(result.overall_status, result.unresolved_fields)

# FuzzyResolver directly
from validator.fuzzy_resolver import FuzzyResolver
resolver = FuzzyResolver(cfg.validation.fuzzy_fields, provider, cfg.validation.thresholds)
fr = resolver.resolve_field("worker", "James Hartwal")

# Decrypt output
from processor.crypto import read_encrypted_json
import json
print(json.dumps(read_encrypted_json("outputs/test_audio.json.enc"), indent=2))
```

## Validation Details (audio v1)

**Field routing** (defined in `config/document_types/audio_v1.yaml`):
- **Fuzzy DB match**: `worker`, `company`, `location`, `vehicle_equipment`, `parts_used`
- **Time normalise**: `start_time`, `end_time`, `total_time_spent`
- **Pass-through**: `reported_problem`, `diagnosis_cause`, `work_performed`, `future_recommendations`, `remaining_tasks`

**Scores:** EXACT ≥ 100 · HIGH_CONF ≥ 85 · LOW_CONF 60–84 · NO_MATCH < 60.
Gemini confidence boosts: HIGH +5, LOW −10. To change weights/thresholds, edit the YAML.

**Time normalisation:** `start_time`/`end_time` → `HH:MM` 24h; `total_time_spent` → `Xh Ym`.
Accepts: `8am`, `08:30 PM`, `08:30:00`, `30m`, `2.5 hours`, `90 min`, `1:30`, `100:30`.

## Common Tasks

```bash
# Tests
cd workorder_processing && python -m pytest tests/ -v
python -m pytest tests/test_edge_cases.py -v        # 124 edge-case tests
python -m pytest tests/test_fuzzy_resolver.py -v    # 49 fuzzy-matching tests
python -m pytest tests/test_veracity.py -v          # 24 veracity tests
python -m pytest tests/test_image_processing.py -v  # 36 image-processing tests

# Validate a file without calling Gemini
python -c "from processor.validator import validate_audio_file; \
  print(validate_audio_file('audio/test_audio.wav'))"
```
