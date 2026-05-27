# Work Order Audio Processor — Implementation Plan

> **Scope:** Security hardening, input validation, data-at-rest/in-transit protection,
> audio format handling, error resilience, and structured output management for `main.py`.

---

## Table of Contents

1. [Current State Analysis](#1-current-state-analysis)
2. [Audio Format Strategy](#2-audio-format-strategy)
3. [Input Validation Layer](#3-input-validation-layer)
4. [Security: Data in Transit](#4-security-data-in-transit)
5. [Security: Data at Rest](#5-security-data-at-rest)
6. [API Key & Secrets Management](#6-api-key--secrets-management)
7. [Storing & Structuring API Responses](#7-storing--structuring-api-responses)
8. [Error Handling & Resilience](#8-error-handling--resilience)
9. [Logging & Audit Trail](#9-logging--audit-trail)
10. [Recommended Project Structure](#10-recommended-project-structure)
11. [Dependency List](#11-dependency-list)
12. [Implementation Checklist](#12-implementation-checklist)

---

## 1. Current State Analysis

### What the current code does well
- Uses `mimetypes` for basic MIME detection with a fallback extension map
- Reads the API key from environment variables (not hardcoded)
- Walks up the directory tree to locate `.env` (avoids path-coupling)
- Sets `temperature=0.2` to reduce hallucinations
- Forces `response_mime_type="application/json"` for structured output

### Gaps to address

| Area | Issue |
|---|---|
| Input validation | Only checks `os.path.isfile()` — no size, MIME magic-byte, or duration checks |
| MIME detection | Relies on file extension, which can be spoofed |
| Secrets management | Custom `.env` parser is fragile; no protection if `.env` is committed to git |
| Data at rest | Output `.json` and source audio written to disk in plaintext |
| Error handling | Single broad `except Exception` — no retry, no partial-failure recovery |
| Logging | Uses `print()` — no structured log, no audit trail |
| API response storage | No raw response envelope stored; only the parsed JSON is saved |
| Model string | `"gemini-3.1-flash-lite"` — verify this is the current stable model ID |

---

## 2. Audio Format Strategy

### Gemini-supported audio MIME types

Based on the official Gemini API documentation, the following types are supported natively. Use these as your allowlist:

```python
ALLOWED_AUDIO_MIMES = {
    "audio/mpeg",       # .mp3
    "audio/wav",        # .wav
    "audio/x-wav",      # .wav (alternate)
    "audio/mp4",        # .m4a
    "audio/x-m4a",      # .m4a (alternate)
    "audio/flac",       # .flac
    "audio/ogg",        # .ogg / .opus
    "audio/opus",       # .opus
    "audio/webm",       # .webm
    "audio/aac",        # .aac
    "audio/x-aac",      # .aac (alternate)
    "audio/pcm",        # .pcm / raw
    "audio/mpga",       # .mpga
}
```

### Key Gemini audio constraints to enforce before sending

| Constraint | Limit | Action if exceeded |
|---|---|---|
| Max combined audio length | 9.5 hours per prompt | Reject with clear error |
| File size (inline upload) | 20 MB inline / 100 MB via File API | Auto-route to File API above 20 MB |
| Sample rate (downsampled by Gemini) | 16 Kbps | Inform user; no action needed |
| Multi-channel audio | Combined to mono by Gemini | Log a notice |

### Recommended: Magic-byte MIME sniffing

Never trust the file extension alone — it can be renamed. Use `python-magic` (libmagic bindings)
to read the actual file header bytes:

```python
import magic

def validate_mime_magic(file_path: str) -> str:
    """Detect MIME type from file magic bytes, not extension."""
    mime_detector = magic.Magic(mime=True)
    detected = mime_detector.from_file(file_path)
    if detected not in ALLOWED_AUDIO_MIMES:
        raise ValueError(
            f"File content is '{detected}', which is not a supported audio type. "
            f"Allowed: {sorted(ALLOWED_AUDIO_MIMES)}"
        )
    return detected
```

**Why this matters:** An attacker (or accidental mismatch) could rename `malware.exe` to
`recording.mp3`. Magic-byte checking catches this because the first bytes of an executable
do not match any audio format signature.

---

## 3. Input Validation Layer

Create a dedicated `validate_audio_file()` function that runs **before** any file is read into
memory or sent to the API. All checks must pass or the function raises a typed exception.

```python
import os
import magic
from pathlib import Path

# Limits
MAX_FILE_SIZE_BYTES = 200 * 1024 * 1024   # 200 MB hard cap (Gemini File API limit)
MIN_FILE_SIZE_BYTES = 1 * 1024            # 1 KB — reject empty/stub files
MAX_FILENAME_LENGTH = 255
ALLOWED_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".flac", ".ogg",
    ".opus", ".webm", ".aac", ".pcm", ".mpga"
}

class AudioValidationError(ValueError):
    """Raised when an audio file fails pre-flight validation."""

def validate_audio_file(file_path: str) -> dict:
    """
    Run all pre-flight checks on an audio file.

    Returns a dict with: {path, size_bytes, extension, detected_mime}
    Raises AudioValidationError on any failure.
    """
    path = Path(file_path).resolve()

    # 1. Path traversal guard — ensure the resolved path stays within a safe root
    # (adjust SAFE_ROOT to your project directory)
    SAFE_ROOT = Path.cwd()
    if not str(path).startswith(str(SAFE_ROOT)):
        raise AudioValidationError(f"Path traversal detected: {file_path}")

    # 2. File existence
    if not path.is_file():
        raise AudioValidationError(f"File not found: {file_path}")

    # 3. Filename length and character safety
    if len(path.name) > MAX_FILENAME_LENGTH:
        raise AudioValidationError("Filename exceeds maximum length.")
    if any(c in path.name for c in ['..', '\x00', '/', '\\']):
        raise AudioValidationError("Filename contains unsafe characters.")

    # 4. Extension allowlist
    ext = path.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise AudioValidationError(
            f"Extension '{ext}' not in allowlist: {sorted(ALLOWED_EXTENSIONS)}"
        )

    # 5. File size bounds
    size = path.stat().st_size
    if size < MIN_FILE_SIZE_BYTES:
        raise AudioValidationError(f"File too small ({size} bytes). Possibly corrupt or empty.")
    if size > MAX_FILE_SIZE_BYTES:
        raise AudioValidationError(
            f"File too large ({size / 1e6:.1f} MB). Maximum is "
            f"{MAX_FILE_SIZE_BYTES / 1e6:.0f} MB."
        )

    # 6. Magic-byte MIME detection (requires: pip install python-magic)
    mime_detector = magic.Magic(mime=True)
    detected_mime = mime_detector.from_file(str(path))
    if detected_mime not in ALLOWED_AUDIO_MIMES:
        raise AudioValidationError(
            f"File content identified as '{detected_mime}' — not an audio type. "
            "File extension may have been spoofed."
        )

    return {
        "path": str(path),
        "size_bytes": size,
        "extension": ext,
        "detected_mime": detected_mime,
    }
```

### Validation flow diagram

```
 CLI argument (file path)
         │
         ▼
 ┌───────────────────┐
 │ Path traversal    │──FAIL──► AudioValidationError + exit(1)
 │ guard             │
 └────────┬──────────┘
          │ PASS
          ▼
 ┌───────────────────┐
 │ File existence    │──FAIL──► FileNotFoundError + exit(1)
 └────────┬──────────┘
          │ PASS
          ▼
 ┌───────────────────┐
 │ Filename safety   │──FAIL──► AudioValidationError + exit(1)
 └────────┬──────────┘
          │ PASS
          ▼
 ┌───────────────────┐
 │ Extension         │──FAIL──► AudioValidationError + exit(1)
 │ allowlist         │
 └────────┬──────────┘
          │ PASS
          ▼
 ┌───────────────────┐
 │ File size bounds  │──FAIL──► AudioValidationError + exit(1)
 └────────┬──────────┘
          │ PASS
          ▼
 ┌───────────────────┐
 │ Magic-byte MIME   │──FAIL──► AudioValidationError + exit(1)
 │ sniffing          │
 └────────┬──────────┘
          │ PASS
          ▼
   process_audio()
```

---

## 4. Security: Data in Transit

### Why the current code is already partially protected

The `google-genai` Python SDK sends all requests over HTTPS using TLS 1.2+. Google enforces
this on all AI API endpoints. You do not need to implement transport encryption yourself.

### What you should verify and enforce

**4.1 Pin TLS / certificate verification**

Never disable certificate verification. Ensure no code in your project sets:
```python
# ❌ NEVER do this
import ssl; ssl._create_default_https_context = ssl._create_unverified_context
```

**4.2 Sanitize what you log**

Audio bytes and API keys must never appear in logs or stack traces. Add a log sanitizer:

```python
import re

SENSITIVE_PATTERNS = [
    (re.compile(r'(api[_-]?key["\s:=]+)[^\s&"]+', re.I), r'\1[REDACTED]'),
    (re.compile(r'(AIza[0-9A-Za-z_\-]{35})'), '[REDACTED_API_KEY]'),
]

def sanitize_log_message(msg: str) -> str:
    for pattern, replacement in SENSITIVE_PATTERNS:
        msg = pattern.sub(replacement, msg)
    return msg
```

**4.3 Network timeout**

Add a timeout so the process doesn't hang indefinitely if the API is unreachable:

```python
# When constructing the client
import httpx

client = genai.Client(
    api_key=API_KEY,
    http_options={"timeout": 120}   # 2-minute ceiling per request
)
```

**4.4 Google's data handling policy**

> ⚠️ Be aware: Audio submitted to the Gemini API may be subject to Google's abuse-review
> policy. For recordings containing PII (names, addresses, health info), review Google's
> Data Processing Addendum and consider whether a data processing agreement is needed for
> your compliance obligations (GDPR, HIPAA, etc.).

---

## 5. Security: Data at Rest

### 5.1 Encrypt output JSON files

Sensitive work-order data (vehicle IDs, diagnoses, parts) should not sit on disk as
plaintext. Use **Fernet** (AES-128-CBC + HMAC-SHA256) from the `cryptography` library for
simple file-level encryption:

```python
from cryptography.fernet import Fernet
import os

def get_fernet() -> Fernet:
    """Load or generate the symmetric encryption key."""
    key_path = os.environ.get("ENCRYPTION_KEY_PATH", ".secret.key")
    if os.path.isfile(key_path):
        with open(key_path, "rb") as f:
            key = f.read()
    else:
        key = Fernet.generate_key()
        with open(key_path, "wb") as f:
            f.write(key)
        os.chmod(key_path, 0o600)   # owner read/write only
    return Fernet(key)

def write_encrypted_json(data: dict, out_path: str) -> None:
    """Serialize data to JSON and write it as an encrypted .json.enc file."""
    fernet = get_fernet()
    plaintext = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    ciphertext = fernet.encrypt(plaintext)
    enc_path = out_path + ".enc"
    with open(enc_path, "wb") as f:
        f.write(ciphertext)
    os.chmod(enc_path, 0o640)   # owner rw, group r, others none
    print(f"🔒 Encrypted output: {enc_path}")

def read_encrypted_json(enc_path: str) -> dict:
    """Read and decrypt an encrypted .json.enc file."""
    fernet = get_fernet()
    with open(enc_path, "rb") as f:
        ciphertext = f.read()
    plaintext = fernet.decrypt(ciphertext)
    return json.loads(plaintext.decode("utf-8"))
```

> For production workloads with multiple machines, replace the local `.secret.key` file
> with a cloud KMS: **Google Cloud KMS**, **AWS KMS**, or **HashiCorp Vault**.

### 5.2 Key storage rules

- Store `.secret.key` **outside** the project root and **outside** version control.
- Add to `.gitignore`:
  ```
  .env
  *.key
  *.enc
  ```
- Set file permissions to `600` (owner read/write only) on Unix systems.
- Rotate the key periodically and re-encrypt archived outputs.

### 5.3 Secure file deletion of temp files

If you ever write audio bytes to a temp file (e.g., for conversion), wipe it securely:

```python
import tempfile, os

def secure_delete(file_path: str) -> None:
    """Overwrite file with zeros before deletion to prevent recovery."""
    size = os.path.getsize(file_path)
    with open(file_path, "r+b") as f:
        f.write(b'\x00' * size)
        f.flush()
        os.fsync(f.fileno())
    os.remove(file_path)
```

---

## 6. API Key & Secrets Management

### Replace the custom `.env` parser

The current inline `.env` parser is fragile (no multi-line values, no quote handling edge
cases). Replace it with `python-dotenv`:

```python
# In requirements.txt:  python-dotenv>=1.0.0
from dotenv import load_dotenv
load_dotenv()   # Reads .env in CWD or any parent
```

### Environment variable loading order (recommended)

```
1. System environment (set by CI/CD, Docker, or shell export)    ← Highest priority
2. .env file in project root                                      ← Local dev only
3. Default fallback value in code (non-secret defaults only)     ← Lowest priority
```

### Key rules

- **Never** commit `.env` to version control. Use `.env.example` with placeholder values.
- Use a secrets manager for production:
  - Google Cloud Secret Manager: `google-cloud-secret-manager`
  - AWS Secrets Manager: `boto3`
  - HashiCorp Vault: `hvac`
- Validate that the API key has the expected format before the first API call:

```python
import re

def validate_api_key(key: str) -> None:
    """Basic format check — Google AI keys start with 'AIza'."""
    if not re.match(r'^AIza[0-9A-Za-z_\-]{35}$', key):
        raise RuntimeError(
            "GOOGLE_API_KEY format looks invalid. "
            "Expected format: AIza followed by 35 alphanumeric characters."
        )
```

---

## 7. Storing & Structuring API Responses

### 7.1 Store both the raw envelope and the parsed data

Never throw away the raw API response. It is your audit trail and lets you re-parse without
re-calling the API. Store it alongside the extracted JSON:

```python
import datetime, hashlib

def build_response_envelope(
    file_path: str,
    raw_response,          # google.genai response object
    extracted: dict,
    model_id: str,
) -> dict:
    """Wrap the extracted data with full audit metadata."""
    audio_bytes = open(file_path, "rb").read()
    return {
        "schema_version": "1.0",
        "processed_at": datetime.datetime.utcnow().isoformat() + "Z",
        "source_file": {
            "name": os.path.basename(file_path),
            "sha256": hashlib.sha256(audio_bytes).hexdigest(),
            "size_bytes": len(audio_bytes),
        },
        "model": model_id,
        "usage": {
            "prompt_tokens": getattr(raw_response.usage_metadata, "prompt_token_count", None),
            "response_tokens": getattr(raw_response.usage_metadata, "candidates_token_count", None),
        },
        "finish_reason": str(getattr(raw_response.candidates[0], "finish_reason", "UNKNOWN")),
        "extracted_data": extracted,
    }
```

### 7.2 Validate the extracted JSON against a schema

Use `jsonschema` to assert every required field is present and typed correctly before saving:

```python
# pip install jsonschema
import jsonschema

WORK_ORDER_SCHEMA = {
    "type": "object",
    "required": [
        "vehicle_equipment", "reported_problem", "diagnosis_cause",
        "work_performed", "parts_used", "start_time", "end_time",
        "total_time_spent", "future_recommendations", "remaining_tasks"
    ],
    "properties": {
        "vehicle_equipment":      {"type": "string"},
        "reported_problem":       {"type": "string"},
        "diagnosis_cause":        {"type": "string"},
        "work_performed":         {"type": "string"},
        "parts_used":             {"type": "string"},
        "start_time":             {"type": "string", "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$"},
        "end_time":               {"type": "string", "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$"},
        "total_time_spent":       {"type": "string"},
        "future_recommendations": {"type": "string"},
        "remaining_tasks":        {"type": "string"},
    },
    "additionalProperties": False
}

def validate_extracted_data(data: dict) -> None:
    """Raises jsonschema.ValidationError if extracted data doesn't match schema."""
    jsonschema.validate(instance=data, schema=WORK_ORDER_SCHEMA)
```

### 7.3 Handle malformed AI responses gracefully

The model may return JSON wrapped in markdown fences (` ```json ... ``` `). Strip them:

```python
import re

def parse_ai_json(raw_text: str) -> dict:
    """Strip markdown fences and parse JSON from AI response text."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"AI returned invalid JSON: {e}\nRaw text: {raw_text[:500]}")
```

---

## 8. Error Handling & Resilience

### 8.1 Typed exception hierarchy

```python
class WorkOrderProcessorError(Exception):
    """Base exception for this application."""

class AudioValidationError(WorkOrderProcessorError):
    """File failed pre-flight validation."""

class APICallError(WorkOrderProcessorError):
    """Gemini API call failed."""

class ResponseParseError(WorkOrderProcessorError):
    """Could not parse the model's response as valid JSON."""

class SchemaValidationError(WorkOrderProcessorError):
    """Extracted JSON does not conform to the work order schema."""
```

### 8.2 Retry with exponential backoff

The Gemini API can return 429 (rate limit) or 503 (transient server error). Retry gracefully:

```python
import time

def call_gemini_with_retry(client, model_id, contents, config, max_retries=3):
    """Call Gemini with exponential backoff on transient errors."""
    retryable_codes = {429, 500, 502, 503, 504}
    for attempt in range(1, max_retries + 1):
        try:
            return client.models.generate_content(
                model=model_id,
                contents=contents,
                config=config,
            )
        except Exception as e:
            status = getattr(e, "status_code", None) or getattr(e, "code", None)
            if status in retryable_codes and attempt < max_retries:
                wait = 2 ** attempt   # 2s, 4s, 8s
                print(f"⚠️  API error (attempt {attempt}/{max_retries}). Retrying in {wait}s…")
                time.sleep(wait)
            else:
                raise APICallError(f"Gemini API call failed after {attempt} attempt(s): {e}") from e
```

### 8.3 Processing status tracking

For batch scenarios, add a `status` field to the output envelope:

```
"status": "completed" | "failed" | "needs_review" | "partial"
```

Set `"needs_review"` when schema validation passes but confidence is low (e.g., many empty
fields returned). This avoids silently storing low-quality extractions.

---

## 9. Logging & Audit Trail

Replace all `print()` calls with Python's `logging` module. This enables log levels, file
output, and structured logging.

```python
import logging, sys

def configure_logging(log_file: str = "processor.log") -> logging.Logger:
    logger = logging.getLogger("work_order_processor")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ"
    )

    # Console: INFO and above
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    # File: DEBUG and above (full audit trail)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger

logger = configure_logging()
```

### What to log (and what NOT to log)

| Log | Level | Include? |
|---|---|---|
| File path, size, detected MIME | INFO | ✅ Yes |
| Model ID, token usage | INFO | ✅ Yes |
| Processing start / end timestamps | INFO | ✅ Yes |
| Validation errors with reason | WARNING | ✅ Yes |
| API retry attempts | WARNING | ✅ Yes |
| Full exception traceback | DEBUG | ✅ Yes (file only) |
| Audio file bytes / content | — | ❌ Never |
| API key value | — | ❌ Never |
| Full raw API response body | — | ❌ Never (store to encrypted file instead) |
| Extracted work order text | DEBUG | ⚠️ Only if log file is access-controlled |

---

## 10. Recommended Project Structure

```
work-order-processor/
├── main.py                   ← Refactored entry point (CLI)
├── processor/
│   ├── __init__.py
│   ├── validator.py          ← validate_audio_file(), validate_extracted_data()
│   ├── gemini_client.py      ← client setup, call_gemini_with_retry()
│   ├── parser.py             ← parse_ai_json(), build_response_envelope()
│   ├── crypto.py             ← get_fernet(), write_encrypted_json(), read_encrypted_json()
│   ├── logging_config.py     ← configure_logging(), sanitize_log_message()
│   └── exceptions.py         ← Typed exception hierarchy
├── schemas/
│   └── work_order_v1.json    ← JSON Schema for validation
├── examples/
│   └── sample_recording.mp3
├── outputs/                  ← .json.enc files written here (gitignored)
├── .env.example              ← Template: GOOGLE_API_KEY=your-key-here
├── .gitignore                ← Must include: .env, *.key, *.enc, outputs/
├── requirements.txt
├── processor.log             ← Audit log (gitignored)
└── IMPLEMENTATION_PLAN.md    ← This document
```

---

## 11. Dependency List

Add to `requirements.txt`:

```
# Existing
google-genai>=1.0.0

# Secrets & config
python-dotenv>=1.0.0

# MIME magic-byte detection (requires libmagic system library)
# Linux:  sudo apt-get install libmagic1
# macOS:  brew install libmagic
# Windows: install python-magic-bin instead of python-magic
python-magic>=0.4.27

# JSON schema validation
jsonschema>=4.23.0

# Encryption at rest
cryptography>=42.0.0

# Audio duration check (optional, for pre-flight duration validation)
mutagen>=1.47.0
```

> **Note on `python-magic`:** On Windows, use `python-magic-bin` which bundles the native
> DLL. On Linux/macOS, install the system `libmagic` package first.

---

## 12. Implementation Checklist

Work through this list in order. Each item maps to a section above.

### Phase 1 — Foundation (no behaviour change, just hardening)
- [ ] Replace custom `.env` parser with `python-dotenv`
- [ ] Add API key format validation (`validate_api_key()`)
- [ ] Replace all `print()` with `logging`
- [ ] Add `sanitize_log_message()` and apply to all log calls
- [ ] Add `.env`, `*.key`, `*.enc`, `outputs/`, `processor.log` to `.gitignore`
- [ ] Pin model ID to a known stable Gemini model string

### Phase 2 — Input Validation
- [ ] Install `python-magic` (and system `libmagic`)
- [ ] Implement `validate_audio_file()` with all 6 checks
- [ ] Wire `validate_audio_file()` into `main()` before `process_audio()` is called
- [ ] Test with: renamed `.exe`, empty file, oversized file, unsupported extension

### Phase 3 — Robust API Call
- [ ] Implement `call_gemini_with_retry()` with 3-attempt exponential backoff
- [ ] Add `http_options={"timeout": 120}` to the Gemini client
- [ ] Implement `parse_ai_json()` with markdown fence stripping
- [ ] Add `validate_extracted_data()` with `jsonschema`
- [ ] Implement `build_response_envelope()` and include SHA-256 of source audio

### Phase 4 — Data at Rest Encryption
- [ ] Install `cryptography`
- [ ] Implement `get_fernet()`, `write_encrypted_json()`, `read_encrypted_json()`
- [ ] Replace `json.dump(..., fh)` in `main()` with `write_encrypted_json()`
- [ ] Set `chmod 600` on `.secret.key`, `chmod 640` on `.enc` outputs
- [ ] Document key rotation procedure in README

### Phase 5 — Production Hardening (optional but recommended)
- [ ] Replace local `.secret.key` with Google Cloud KMS or AWS KMS
- [ ] Add `mutagen` duration check (reject audio > 9.5 hours)
- [ ] Add processing status field to output envelope
- [ ] Add `--output-dir` CLI flag so outputs aren't co-located with audio files
- [ ] Write unit tests for `validator.py` and `parser.py`
- [ ] Run `bandit -r .` (Python security linter) and resolve HIGH findings

---

*Last updated: 2026-05-27 | Covers Gemini API as of May 2026*
