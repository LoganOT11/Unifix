# CLAUDE.md — Unifix Work Order Processor

## Conda Environment

This project uses the `unifix` conda environment (Python 3.12).

```bash
conda activate unifix       # activate
conda deactivate            # deactivate
```

Dependencies are tracked in two files:

- **`environment.yml`** — Conda environment spec. Rebuild with:
  ```bash
  conda env create -f environment.yml
  ```

- **`requirements.txt`** — Pip dependencies. Install into active env:
  ```bash
  pip install -r requirements.txt
  ```

### Adding New Dependencies

```bash
conda env export --no-builds | grep -v "^prefix:" > environment.yml  # after conda install
pip freeze > requirements.txt                                         # after pip install
```

## API Keys

Copy `.env` and fill in your keys. The `.env` file is gitignored and never committed.

Required: `GOOGLE_API_KEY` (starts with `AIza…`). Optional overrides: `GEMINI_MODEL`, `PROCESSOR_LOG`, `ENCRYPTION_KEY_PATH`.

## Project Structure

```
Unifix/
├── .env                          ← API keys (gitignored)
├── .gitignore
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
│   ├── audio/
│   │   ├── test_audio.wav        ← Sample audio for testing
│   │   └── docs/
│   │       └── IMPLEMENTATION_PLAN.md
│   ├── test_database/
│   │   ├── database.py           ← SQLite FTS5 reference data + fuzzy matching
│   │   └── test_data.db          ← Seeded DB (equipment, companies, locations, parts)
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

## Test Database (Fuzzy Matching)

SQLite with FTS5 full-text search. 81 seeded records across four tables:

| Table | Records | Example |
|---|---|---|
| `equipment` | 26 | Caterpillar D6T Dozer, Komatsu PC200 Excavator |
| `companies` | 20 | Caterpillar Inc., United Rentals, Summit Mining Corp |
| `locations` | 15 | Denver Maintenance Yard, Houston Heavy Equipment Depot |
| `parts` | 20 | CAT-6T-7489 (track chain), KOM-PC200-3310 (seal kit) |

### Usage

```bash
# Seed the database (idempotent)
python test_database/database.py --seed

# Test fuzzy matching
python test_database/database.py --query equipment "cat dozer"
```

```python
from test_database.database import get_db, fuzzy_match

conn = get_db()
matches = fuzzy_match(conn, "equipment", "Cat D6T", threshold=0.4)
# → [{"name": "Caterpillar D6T Dozer", "score": 0.588, ...}, ...]
conn.close()
```

Fuzzy matching uses FTS5 prefix queries with a `difflib.SequenceMatcher` fallback (pure stdlib,
no extra deps). The `threshold` parameter controls minimum similarity (0.0–1.0).

## Proxy (Development)

`proxy.py` runs a local API key injector on port 8787. It sits between the app and
`generativelanguage.googleapis.com`, swapping a dummy key for the real one so the key never
enters the codebase. Run with:

```bash
python proxy.py
```

Configure the Gemini client to point at `http://localhost:8787` with a dummy key. The current
`main.py` uses the key directly — the proxy is optional and kept for future Vertex AI use.

## Common Tasks

### Process an audio file
```bash
cd workorder_processing && python main.py audio/test_audio.wav -p
```

### Change the model
```bash
GEMINI_MODEL=gemini-2.5-pro python main.py audio/test_audio.wav
```

### Add a reference record to the test database
```bash
sqlite3 test_database/test_data.db "INSERT INTO equipment (name, category, manufacturer) VALUES ('New Dozer', 'dozer', 'BrandX');"
sqlite3 test_database/test_data.db "INSERT INTO equipment_fts(equipment_fts) VALUES ('rebuild');"
```

### Validate an audio file without sending to Gemini
```python
from processor.validator import validate_audio_file
meta = validate_audio_file("audio/test_audio.wav")
print(meta["detected_mime"], meta["size_bytes"])
```

### Decrypt an output file
```python
from processor.crypto import read_encrypted_json
data = read_encrypted_json("outputs/test_audio.json.enc")
print(data["extracted_data"])
```
