# Architectural Design Report — Unifix Work Order Processor

**Version:** 1.0  
**Date:** 2026-06-01  
**Status:** Current  

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Context](#2-system-context)
3. [Architecture Goals and Constraints](#3-architecture-goals-and-constraints)
4. [High-Level Component Map](#4-high-level-component-map)
5. [Pipeline Architecture](#5-pipeline-architecture)
   - 5.1 [Template-Method Pattern](#51-template-method-pattern)
   - 5.2 [Pipeline Registry](#52-pipeline-registry)
   - 5.3 [Audio Pipeline](#53-audio-pipeline)
   - 5.4 [Video Pipeline](#54-video-pipeline)
   - 5.5 [Image Pipeline](#55-image-pipeline)
6. [Configuration Layer](#6-configuration-layer)
7. [Processor Subsystem](#7-processor-subsystem)
   - 7.1 [Gemini Client](#71-gemini-client)
   - 7.2 [Prompt Loader](#72-prompt-loader)
   - 7.3 [Parser](#73-parser)
   - 7.4 [Image Preprocessor](#74-image-preprocessor)
   - 7.5 [Input Validator](#75-input-validator)
   - 7.6 [Video Extractor](#76-video-extractor)
8. [Validation Subsystem](#8-validation-subsystem)
   - 8.1 [FuzzyResolver](#81-fuzzyresolver)
   - 8.2 [Time Validator](#82-time-validator)
   - 8.3 [Veracity Engine](#83-veracity-engine)
   - 8.4 [Resolution and Status Model](#84-resolution-and-status-model)
9. [Database Layer](#9-database-layer)
10. [Security Layer](#10-security-layer)
    - 10.1 [Output Encryption](#101-output-encryption)
    - 10.2 [PII Sanitization](#102-pii-sanitization)
11. [Data Models](#11-data-models)
    - 11.1 [Schema v1 — Audio / Video](#111-schema-v1--audio--video)
    - 11.2 [Schema v3 — Image (Uni-Fix Form)](#112-schema-v3--image-uni-fix-form)
    - 11.3 [Output Envelope](#113-output-envelope)
12. [Error Handling and Exit Codes](#12-error-handling-and-exit-codes)
13. [Observability](#13-observability)
14. [Extensibility Model](#14-extensibility-model)
15. [Testing Architecture](#15-testing-architecture)
16. [Dependency Overview](#16-dependency-overview)
17. [Known Limitations and Future Considerations](#17-known-limitations-and-future-considerations)

---

## 1. Executive Summary

The Unifix Work Order Processor is a Python command-line application that extracts structured work-order data from unstructured field-service inputs — audio recordings, video files, and scanned form images — using Google Gemini as the AI extraction engine. The extracted data is validated against a reference database via a configurable fuzzy-matching pipeline, verified through an optional second AI pass (the "veracity" check), and then written as an encrypted, auditable JSON artifact.

The system is designed around a **Template-Method pipeline pattern** with a **decorator-based registry**, making it straightforward to add new document types without modifying existing code. Configuration is entirely data-driven: a YAML file per document type controls prompts, schema versions, field routing, fuzzy-matching algorithm weights, score thresholds, and veracity triggers.

---

## 2. System Context

```
┌─────────────────────────────────────────────────────────────┐
│                        Field Technician                      │
│   Audio recorder / mobile video / printed Uni-Fix form       │
└────────────────────────┬────────────────────────────────────┘
                         │  raw media file
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              Unifix Work Order Processor (CLI)               │
│                                                              │
│  ┌──────────┐  ┌───────────┐  ┌─────────────────────────┐  │
│  │  Validate │─▶│ Preprocess│─▶│  Gemini API (extract)   │  │
│  └──────────┘  └───────────┘  └────────────┬────────────┘  │
│                                             │ structured JSON│
│  ┌───────────────────────────────────────── ▼ ─────────┐   │
│  │  Validate (schema + fuzzy DB + time normalisation)   │   │
│  └──────────────────────────┬────────────────────────── ┘   │
│                             │ REVIEW/FAIL or LOW confidence  │
│                             ▼                               │
│  ┌────────────────────────────────────────────────────── ┐  │
│  │  Veracity Pass (optional second Gemini call)          │  │
│  └──────────────────────────┬─────────────────────────── ┘  │
│                             │                               │
│                             ▼                               │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  Encrypt & Write  (.json.enc  +  optional .json)     │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
 Encrypted output file           Google Gemini API
 (audit trail, SHA-256,          (gemini-flash / pro)
  token counts)
```

**External dependencies:**

| Dependency | Role |
|---|---|
| Google Gemini API | Multimodal AI extraction (audio, image, veracity) |
| ffmpeg | Audio extraction from video containers |
| Fernet / cryptography | AES-128-CBC + HMAC-SHA256 output encryption |
| rapidfuzz | Fuzzy string matching against reference data |
| OpenCV (`cv2`) | Image quality assessment and enhancement |
| jsonschema | Structural validation of AI-extracted JSON |

---

## 3. Architecture Goals and Constraints

### Goals

| Goal | How it is achieved |
|---|---|
| **Accuracy** | Two-pass AI extraction with a configurable veracity check; weighted fuzzy ensemble against a reference DB |
| **Extensibility** | `@register()` decorator + YAML config = new document type in two files |
| **Security** | Fernet-encrypted output; PII-sanitizing log formatter; API key redaction; path-traversal guard |
| **Auditability** | Every envelope carries SHA-256 of the source file, Gemini token counts, finish reason, validation trail, and veracity outcome |
| **Resilience** | Exponential-backoff retry on transient Gemini errors (429, 5xx); distinct exit codes per failure class |
| **Testability** | `ReferenceDataProvider` is a `Protocol`; `InMemoryProvider` substitutes in tests without a DB |

### Constraints

- **Single-process, single-file, synchronous** — designed for CLI invocation on one file at a time; no batch scheduling, no async I/O.
- **Gemini API required** — there is no offline fallback for extraction.
- **ffmpeg must be on `PATH`** — video mode depends on a system binary.
- **Python ≥ 3.11** implied by `Path | None` union syntax throughout.

---

## 4. High-Level Component Map

```
workorder_processing/
│
├── main.py                      CLI entry point — arg parsing, mode detection,
│                                client init, pipeline dispatch, error mapping
│
├── config/                      ─── Configuration Layer ───────────────────────
│   ├── __init__.py              DocumentConfig + child dataclasses; load_document_config()
│   ├── settings.py              Module-level constants (DEFAULT_MODEL, IMAGE_EXTENSIONS)
│   └── document_types/
│       ├── audio_v1.yaml        Field weights, thresholds, veracity triggers (audio)
│       └── image_v3.yaml        Same structure for image
│
├── pipeline/                    ─── Pipeline Layer ────────────────────────────
│   ├── __init__.py              _REGISTRY dict; @register() decorator; get_pipeline()
│   ├── base.py                  BasePipeline ABC + PipelineContext dataclass
│   ├── audio.py                 AudioPipeline (@register("audio"))
│   ├── video.py                 VideoPipeline (@register("video"), extends Audio)
│   └── image.py                 ImagePipeline (@register("image"))
│
├── processor/                   ─── Processor Subsystem ───────────────────────
│   ├── gemini_client.py         create_client(); call_gemini_with_retry(); call_gemini_generic()
│   ├── prompt_loader.py         load_prompt() — template read + schema block injection
│   ├── parser.py                parse_ai_json(); validate_extracted_data(); build_response_envelope()
│   ├── veracity.py              should_run_veracity(); run_veracity_check(); apply_veracity_corrections()
│   ├── image_preprocessor.py   assess_quality(); preprocess_image() → PreprocessResult
│   ├── image_processor.py      validate_image_file(); process_image()
│   ├── validator.py             validate_audio_file(); validate_video_file()
│   ├── video_extractor.py       extract_audio_from_video(); is_video_extension()
│   ├── exceptions.py            WorkOrderProcessorError hierarchy
│   ├── logging_config.py        configure_logging(); _SanitizingFormatter
│   └── crypto.py               get_fernet(); write_encrypted_json(); read_encrypted_json()
│
├── validator/                   ─── Validation Subsystem ──────────────────────
│   ├── models.py                MatchStatus, OverallStatus, FieldResult, ValidationResult
│   ├── fuzzy_resolver.py        FuzzyResolver — config-driven rapidfuzz ensemble
│   ├── time_validator.py        normalise_clock_time(); normalise_duration()
│   ├── resolution.py            compute_overall_status() → PASS / REVIEW / FAIL
│   └── work_order_validator.py  validate_work_order() — orchestrates all field validation
│
├── db/                          ─── Database Layer ────────────────────────────
│   ├── protocol.py              ReferenceDataProvider Protocol
│   ├── memory.py                InMemoryProvider (wraps reference_data.py)
│   └── reference_data.py        Static fixture data (workers, companies, locations, …)
│
├── prompts/                     Runtime prompt templates
│   ├── audio_extraction_v2.txt  Active audio extraction prompt
│   ├── image_extraction_v3.txt  Active image extraction prompt
│   └── veracity_v1.txt          Veracity QA prompt
│
└── schemas/
    ├── work_order_v1.json        JSON Schema — 13 flat string fields (audio/video)
    └── work_order_v3.json        JSON Schema — nested tasks[], travel, expenses (image)
```

---

## 5. Pipeline Architecture

### 5.1 Template-Method Pattern

`BasePipeline` (`pipeline/base.py`) implements a fixed `run()` method that calls a sequence of protected hooks in a defined order. Three of these hooks are **abstract** (must be overridden by every subclass); the rest have **default implementations** that subclasses can selectively override.

```
BasePipeline.run(ctx: PipelineContext) → dict
│
├── 1. validate_input(ctx)          [ABSTRACT] → validated_meta dict
├── 2. preprocess(ctx, meta)        [ABSTRACT] → (media_bytes, mime_type)
├── 3. _load_prompt(ctx)            [default]  → prompt str
├── 4. extract(ctx, bytes, mime,    [ABSTRACT] → GenerateContentResponse
│          prompt)
├── 5. _parse(ctx, response)        [default]  → (work_order dict,
│                                                  confidences dict)
├── 6. _validate_schema(ctx, wo)    [default]  → (raises SchemaValidationError)
├── 7. _validate_fields(ctx, wo,    [default]  → (val_result, confidences)
│          confidences)
├── 8. _run_veracity(ctx, bytes,    [default]  → ver_result dict | None
│          mime, wo, val, conf)
├── 9. _build_envelope(ctx, wo,     [default]  → envelope dict
│          val, ver, response)
└── 10. _serialize(ctx, envelope)   [default]  → (writes .json.enc)
```

This design keeps per-format logic minimal and co-located while sharing the expensive orchestration code.

### 5.2 Pipeline Registry

`pipeline/__init__.py` holds a module-level `_REGISTRY: dict[str, type]` dictionary. The `@register(name)` class decorator populates it at import time. `main.py` imports all submodules at startup to ensure decorators fire, then calls `get_pipeline(document_type, config, provider)` which looks up and instantiates the correct class.

```python
# Decorator populates _REGISTRY at class definition time
@register("audio")
class AudioPipeline(BasePipeline): ...

# Factory performs a plain dict lookup
def get_pipeline(document_type, config, provider) -> BasePipeline:
    cls = _REGISTRY[document_type]   # KeyError → ValueError with hint
    return cls(config, provider)
```

Adding a new document type requires only two files: a YAML config and a new pipeline subclass with `@register("new_type")`.

### 5.3 Audio Pipeline

`AudioPipeline` is the canonical pipeline and the template against which all other pipelines are measured.

**`validate_input`** calls `validate_audio_file()`:
- Resolves the absolute path.
- Guards against path traversal (resolved path must start with `safe_root`).
- Checks extension against an allowlist (`.wav`, `.mp3`, `.m4a`, `.aac`, `.flac`, `.ogg`).
- Checks file size (min 1 KB, max 100 MB).
- Reads the first 16 bytes and sniffs the MIME type against known audio magic bytes; rejects MIME mismatches.

**`preprocess`** reads raw bytes and returns them directly with the detected MIME type. No transformation is applied to audio.

**`extract`** calls `call_gemini_with_retry()` — see §7.1.

### 5.4 Video Pipeline

`VideoPipeline` (`@register("video")`) **extends** `AudioPipeline`, inheriting all shared steps. It overrides only `validate_input` and `preprocess`:

- **`validate_input`** delegates to `validate_video_file()`, which mirrors the audio validator but targets the video extension allowlist (`.mp4`, `.mkv`, `.mov`, `.avi`, `.webm`, `.m4v`, `.3gp`, `.wmv`, `.flv`) and a 2 GB size cap.
- **`preprocess`** calls `extract_audio_from_video()` (see §7.6), which invokes ffmpeg to produce a temporary WAV file; that file's bytes are returned as the media payload to Gemini. The temporary file path is tracked in `ctx.extra["tmp_audio_path"]` and cleaned up after serialization.

From step 3 onward, the video pipeline is identical to the audio pipeline.

### 5.5 Image Pipeline

`ImagePipeline` (`@register("image")`) overrides all three abstract hooks and additionally overrides `_validate_fields` and `_run_veracity` to skip them (image schema v3 uses a different field structure; a future image-specific validator has not yet been built).

- **`validate_input`** delegates to `validate_image_file()`: extension allowlist (`.jpg`, `.jpeg`, `.png`, `.webp`, `.heic`, `.heif`, `.pdf`), size cap 20 MB, magic-byte MIME sniff.
- **`preprocess`** calls `preprocess_image()` (see §7.4): quality is assessed; enhancement operations (deskew, CLAHE, denoise, binarize) are applied according to quality tier; returns JPEG bytes.
- **`extract`** inlines the image bytes directly as a `types.Part.from_bytes` with `image/jpeg` MIME.

---

## 6. Configuration Layer

Every aspect of a document type's behavior is defined in a single YAML file under `config/document_types/`. Loading is performed by `load_document_config(name: str) → DocumentConfig`.

### DocumentConfig dataclass hierarchy

```
DocumentConfig
├── document_type: str          "audio" | "image" | …
├── schema_version: str         "v1" | "v3"
├── envelope_version: str       Written to output ("1.0", "3.0")
├── prompt: str                 Prompt filename stem
├── schema_format: str          "simplified" | "full"
├── preprocessing_enabled: bool
├── validation: ValidationConfig | None
│   ├── fuzzy_fields: list[FuzzyFieldConfig]
│   │   └── FuzzyFieldConfig
│   │       ├── name: str
│   │       ├── weights: dict[algo_name → weight]
│   │       └── scoring: "maximum" | "minimum"
│   ├── time_fields: list[str]
│   ├── free_text_fields: list[str]
│   ├── thresholds: dict         {exact, high_conf, low_conf}
│   ├── required_resolved_fields: set[str]
│   └── review_trigger_fields: set[str]
└── veracity: VeracityConfig
    ├── enabled: bool
    ├── trigger_statuses: list[str]   ["REVIEW", "FAIL"]
    └── trigger_confidences: list[str] ["LOW"]
```

### audio_v1.yaml — annotated

```yaml
document_type: audio
schema_version: v1
envelope_version: "1.0"
prompt: audio_extraction_v2     # → prompts/audio_extraction_v2.txt
schema_format: simplified       # {field: type} dict injected into prompt
preprocessing_enabled: false

validation:
  fuzzy_fields:
    - name: worker
      weights: {jaro_winkler: 0.40, token_sort_ratio: 0.35, ratio: 0.25}
      scoring: maximum           # best candidate wins
    - name: company
      weights: {token_set_ratio: 0.40, WRatio: 0.35, partial_ratio: 0.25}
      scoring: maximum
    - name: location
      weights: {partial_ratio: 0.40, token_set_ratio: 0.35, token_sort_ratio: 0.25}
      scoring: maximum
    - name: vehicle_equipment
      weights: {token_set_ratio: 0.40, partial_ratio: 0.30, ratio: 0.30}
      scoring: maximum
    - name: parts_used
      weights: {token_set_ratio: 0.45, WRatio: 0.35, partial_ratio: 0.20}
      scoring: minimum           # strictest algorithm wins (multi-item lists)

  time_fields: [start_time, end_time, total_time_spent]
  free_text_fields:              # pass-through; no resolution attempted
    - reported_problem
    - diagnosis_cause
    - work_performed
    - future_recommendations
    - remaining_tasks

  thresholds:
    exact: 100
    high_conf: 85
    low_conf: 60

  required_resolved_fields:     # FAIL if unresolved
    [worker, company, location, vehicle_equipment, start_time, end_time]
  review_trigger_fields:        # REVIEW if unresolved
    [parts_used, total_time_spent]

veracity:
  enabled: true
  trigger_statuses: [REVIEW, FAIL]
  trigger_confidences: [LOW]
```

All thresholds and weights can be tuned without touching Python source code.

---

## 7. Processor Subsystem

### 7.1 Gemini Client

**File:** `processor/gemini_client.py`

Provides two public call wrappers built on the Google `genai` SDK.

**`create_client(api_key, timeout_ms=600_000)`**  
Validates the API key format (regex checks for legacy `AIza…` or new `AQ.…` keys), then constructs a `genai.Client` with a pinned `api_version="v1"` and the provided HTTP timeout.

**`call_gemini_with_retry(client, model_id, media_bytes, mime_type, prompt, max_retries=3)`**  
Wraps `call_gemini_generic()` for the common single-media-file case. Constructs a `Part.from_bytes` part and sets `response_mime_type="application/json"` and `temperature=0.2`.

**`call_gemini_generic(client, model_id, contents, config, max_retries=3)`**  
The core retry loop:

```
for attempt in 1..max_retries:
    try:
        return client.models.generate_content(...)
    except Exception as exc:
        status = _extract_status(exc)   # parses HTTP status from exception text
        if status in {429, 500, 502, 503, 504} and attempt < max_retries:
            sleep(2 ** attempt)          # 2s, 4s (exponential backoff)
        else:
            raise APICallError(...)
```

Non-retryable status codes (400, 401, 403, 404) cause an immediate `APICallError`.

### 7.2 Prompt Loader

**File:** `processor/prompt_loader.py`

`load_prompt(name, schema_version=None, schema_format="simplified") → str`

Reads `prompts/{name}.txt`. If `schema_version` is provided and the template contains the literal string `{schema_block}`, the placeholder is replaced with a rendered view of the corresponding JSON Schema:

- `schema_format="simplified"` → `{"field_name": "type_string", …}` — a compact dict suitable for inline prompts.
- `schema_format="full"` → the complete `work_order_{version}.json` document — used by image prompts that need the full nested schema.

Prompt templates are loaded once per pipeline invocation, not cached globally, to simplify development iteration.

### 7.3 Parser

**File:** `processor/parser.py`

Three functions form the post-extraction processing chain:

**`parse_ai_json(raw_text) → dict`**  
Strips leading/trailing markdown code fences (` ```json … ``` ` or ` ``` … ``` `) using a multiline regex, then calls `json.loads()`. Raises `ResponseParseError` on empty input or JSON decode failure.

**`extract_confidence_markers(raw_dict) → (work_order, confidences)`**  
Gemini is instructed to return each field as a `field__confidence` key pair (e.g., `"worker__confidence": "HIGH"`). This function splits those keys into two dicts:
- `work_order` — the field values.
- `confidences` — the confidence labels (`HIGH`, `MEDIUM`, `LOW`).

**`build_response_envelope(source_path, raw_response, extracted, model_id, validation_result, veracity_info, schema_version)`**  
Assembles the final output artifact (see §11.3).

**`validate_extracted_data(data, schema_version)`**  
Loads the matching `work_order_{version}.json` schema and calls `jsonschema.validate()`. Raises `SchemaValidationError` on failure.

### 7.4 Image Preprocessor

**File:** `processor/image_preprocessor.py`

Applies adaptive image enhancement before passing to Gemini Vision.

**Quality assessment** — computes a Laplacian variance score (sharpness proxy) and a histogram spread (contrast proxy). Thresholds classify the image into three tiers:

| Tier | Condition | Operations applied |
|---|---|---|
| `GOOD` | Sharp + adequate contrast | None |
| `FAIR` | Slightly blurry or low-contrast | Deskew (Hough transform), light denoise |
| `POOR` | Blurry or flat histogram | Deskew, CLAHE, denoise, adaptive binarize |

**Operations:**
- **Deskew** — detects dominant line angle via Hough Transform; rotates to align.
- **CLAHE** — Contrast Limited Adaptive Histogram Equalization; improves local contrast.
- **Denoise** — `cv2.fastNlMeansDenoising`.
- **Binarize** — `cv2.adaptiveThreshold` (Gaussian method, block 11×11).

Output is always re-encoded as JPEG at quality 92 to produce a consistent input for Gemini.

`PreprocessResult` carries `quality_before`, `operations_applied`, and `estimated_improvement` — these are included in the output envelope for auditability.

### 7.5 Input Validator

**File:** `processor/validator.py`

`validate_audio_file(path, safe_root)` and `validate_video_file(path, safe_root)` perform:

1. **Path traversal guard** — `Path(path).resolve()` must start with `safe_root` (which is `os.getcwd()` in production). Blocks `../../etc/passwd`-style inputs.
2. **Extension allowlist** — hard-coded sets per media type.
3. **Size bounds** — per-type minimum (1 KB audio, 10 KB video) and maximum (100 MB audio, 2 GB video).
4. **MIME sniff** — reads the first 16–32 bytes and matches against known magic-byte signatures. Returns a `validated_meta` dict including `detected_mime`, `size_bytes`, and resolved `path`.

### 7.6 Video Extractor

**File:** `processor/video_extractor.py`

`extract_audio_from_video(video_path) → (audio_bytes, mime_type, tmp_path)`

1. Confirms ffmpeg is available on `PATH` via `subprocess.run(["ffmpeg", "-version"])`.
2. Creates a `tempfile.NamedTemporaryFile` with suffix `.wav`.
3. Runs:
   ```
   ffmpeg -i <video_path> -vn -acodec pcm_s16le -ar 16000 -ac 1 <tmp.wav>
   ```
   (strips video, outputs mono 16 kHz WAV for best Gemini ASR compatibility).
4. Reads the WAV bytes into memory and returns them with `audio/wav` MIME type.
5. Returns `tmp_path` so `VideoPipeline` can delete it after serialization.

---

## 8. Validation Subsystem

### 8.1 FuzzyResolver

**File:** `validator/fuzzy_resolver.py`

The `FuzzyResolver` class is the central component for matching AI-extracted field values against known reference data. It is config-driven: the algorithm weights come directly from the `FuzzyFieldConfig` objects loaded from YAML.

**Algorithm ensemble** — for each `(query, candidate)` pair, all configured algorithms are scored and combined as a weighted sum:

```
composite = Σ (algo_score × weight)
```

Six algorithms are available:

| Alias | rapidfuzz function | Best for |
|---|---|---|
| `ratio` | `fuzz.ratio` | Exact character overlap |
| `partial_ratio` | `fuzz.partial_ratio` | Substring containment |
| `token_sort_ratio` | `fuzz.token_sort_ratio` | Word-order-independent names |
| `token_set_ratio` | `fuzz.token_set_ratio` | Subset/superset strings |
| `WRatio` | `fuzz.WRatio` | General weighted hybrid |
| `jaro_winkler` | `JaroWinkler.similarity × 100` | Human names (prefix bias) |

**Scoring strategy** — controlled per field by `scoring: "maximum" | "minimum"`:
- `maximum` — the candidate with the highest composite score wins (best-match semantics, suitable for unique entities like `worker` or `location`).
- `minimum` — the candidate with the lowest composite score wins (strictest algorithm semantics, suitable for multi-item list fields like `parts_used` where even a partial match should be scrutinised).

**Gemini confidence adjustment** — Gemini's self-reported confidence for each field modifies the final composite score:

| Gemini confidence | Score adjustment |
|---|---|
| `HIGH` | +5.0 |
| `MEDIUM` | 0.0 |
| `LOW` | -10.0 |

**Score thresholds** produce a `MatchStatus`:

| Status | Score |
|---|---|
| `EXACT` | = 100.0 |
| `HIGH_CONF` | ≥ 85.0 |
| `LOW_CONF` | 60.0 – 84.9 |
| `NO_MATCH` | < 60.0 |

**`resolve_parts_used`** handles the special case where `parts_used` is a comma-separated list. Each part is resolved independently, and the per-part `FieldResult` objects are combined.

**`resolve_field(field_name, raw_value)`** — public entry point. Looks up candidates from the `ReferenceDataProvider`, scores all of them, and returns a `FieldResult` with the best match, score, and per-algorithm breakdown.

### 8.2 Time Validator

**File:** `validator/time_validator.py`

`normalise_clock_time(raw) → str | None`  
Accepts a wide range of input formats and normalises to `HH:MM` (24-hour):

| Input examples | Output |
|---|---|
| `8am`, `8:00am`, `08:00` | `08:00` |
| `08:30 PM`, `20:30:00` | `20:30` |
| `1300`, `13h00` | `13:00` |

`normalise_duration(raw) → str | None`  
Normalises duration strings to `Xh Ym`:

| Input examples | Output |
|---|---|
| `90m`, `90 min`, `1.5 hours` | `1h 30m` |
| `2.5 hours`, `150 minutes` | `2h 30m` |
| `1:30`, `1h30m` | `1h 30m` |
| `100:30` | `100h 30m` (valid for long jobs) |

Returns `None` if the input cannot be parsed; the field is then treated as unresolved.

### 8.3 Veracity Engine

**File:** `processor/veracity.py`

The veracity engine is a **second-pass quality gate** that re-submits the original media along with the first-pass extraction result to Gemini, asking it to identify and correct any incorrect or hallucinated fields.

**Trigger conditions** — `should_run_veracity(validation_result, gemini_confidences)`:
- `validation_result.overall_status` is `REVIEW` or `FAIL`, **or**
- Any field in `gemini_confidences` has value `LOW`.

**Veracity prompt** (`prompts/veracity_v1.txt`) is a structured QA prompt that includes:
- The original transcribed content (or `[See attached audio]` if unavailable).
- The complete first-pass JSON result.
- Instructions to return a JSON object identifying incorrect fields with corrections.

**`apply_veracity_corrections(work_order, veracity_result)`** merges corrections back into the work order dict.

**`_run_veracity` in `BasePipeline`** re-runs `_validate_fields` after corrections are applied and stores the post-veracity validation result in `veracity_info["validation_post_veracity"]`.

The veracity call uses `call_gemini_generic()` with `temperature=0.1` (lower than the extraction pass at 0.2) to prioritise factual precision over generation diversity.

### 8.4 Resolution and Status Model

**File:** `validator/models.py`, `validator/resolution.py`

```
MatchStatus (enum)
  EXACT      → score = 100
  HIGH_CONF  → score ≥ 85
  LOW_CONF   → score ≥ 60
  NO_MATCH   → score < 60

FieldResult (dataclass)
  field_name: str
  raw_value: str
  resolved_value: str
  score: float
  status: MatchStatus
  algo_scores: dict[str, float]
  top_candidates: list[dict]

ValidationResult (dataclass)
  field_results: dict[str, FieldResult]
  overall_status: OverallStatus
  unresolved_fields: list[str]
  review_fields: list[str]

OverallStatus (enum)
  PASS    → all required fields resolved
  REVIEW  → required fields resolved but review_trigger_fields unresolved
  FAIL    → one or more required_resolved_fields unresolved
```

`compute_overall_status(field_results, required_fields, review_trigger_fields)`:
- Any required field with `NO_MATCH` → `FAIL`.
- Any review-trigger field with `NO_MATCH` (but no required-field failure) → `REVIEW`.
- Otherwise → `PASS`.

---

## 9. Database Layer

The database layer is abstracted behind a `Protocol` to allow the current in-memory implementation to be swapped for a real database (PostgreSQL, SQLite, etc.) without touching any upstream code.

### ReferenceDataProvider Protocol

```python
@runtime_checkable
class ReferenceDataProvider(Protocol):
    def get_names(self, entity: str) -> list[str]:
        """Return all known names/strings for an entity type."""

    def resolve_canonical(self, entity: str, matched_name: str) -> dict | None:
        """Return the canonical DB record for a matched name, or None."""
```

Entity types consumed by the current configuration: `worker`, `company`, `location`, `vehicle_equipment`, `parts`.

### InMemoryProvider

`db/memory.py` wraps the static lists in `db/reference_data.py`. `get_names` returns the flat list of strings for the given entity; `resolve_canonical` does a linear scan to return the full record dict.

`db/reference_data.py` contains representative fixture data:
- Workers: 8 technician names.
- Companies: 6 service companies.
- Locations: various depot and site names.
- Vehicle/equipment: truck models, equipment codes.
- Parts: common HVAC and mechanical parts.

**Production migration path:** replace `InMemoryProvider` with a class that queries a real database. No other code changes are required because `FuzzyResolver` and `validate_work_order` both accept any object satisfying `ReferenceDataProvider`.

---

## 10. Security Layer

### 10.1 Output Encryption

**File:** `processor/crypto.py`

All output is written as Fernet-encrypted `.json.enc` files using **AES-128-CBC + HMAC-SHA256** from the `cryptography` library.

**Key management:**

```
1. Check ENCRYPTION_KEY_PATH env var (or default ~/.workorder_processor.key)
2. If file exists → load key bytes
3. If file absent → generate Fernet.generate_key() → write with chmod 600
```

The key file is never committed to version control (protected by `.gitignore`). `write_encrypted_json` writes the output with `chmod 640`; `read_encrypted_json` requires the same key to decrypt.

The optional `--plaintext` / `-p` flag writes an unencrypted `.json` sidecar alongside the encrypted file for development and debugging.

### 10.2 PII Sanitization

**File:** `processor/logging_config.py`

`_SanitizingFormatter` wraps Python's standard `logging.Formatter`. Before formatting each log record, it passes `record.msg` through `sanitize_log_message()`:

```python
_SENSITIVE_PATTERNS = [
    (re.compile(r'(api[_-]?key["\s:=]+)[^\s&"]+', re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(AIza[0-9A-Za-z_\-]{35})"),                      "[REDACTED_API_KEY]"),
    (re.compile(r'(x-goog-api-key["\s:=]+)[^\s&"]+', re.IGNORECASE), r"\1[REDACTED]"),
]
```

This means that even if an API key leaks into a log message via an exception or debug dump, it is automatically redacted before it reaches any log sink (console or file).

The logger hierarchy:
- All application code uses `logging.getLogger("work_order_processor")`.
- Console handler: `INFO` level.
- File handler (optional, configured via `PROCESSOR_LOG`): `DEBUG` level.
- Both handlers use `_SanitizingFormatter`.

---

## 11. Data Models

### 11.1 Schema v1 — Audio / Video

13 flat string fields, all optional in the JSON Schema (Gemini may not extract every field from every recording):

| Field | Type | Routing |
|---|---|---|
| `worker` | string | Fuzzy DB match |
| `company` | string | Fuzzy DB match |
| `location` | string | Fuzzy DB match |
| `vehicle_equipment` | string | Fuzzy DB match |
| `parts_used` | string | Fuzzy DB match (list) |
| `start_time` | string | Time normalise → `HH:MM` |
| `end_time` | string | Time normalise → `HH:MM` |
| `total_time_spent` | string | Duration normalise → `Xh Ym` |
| `reported_problem` | string | Pass-through (free text) |
| `diagnosis_cause` | string | Pass-through (free text) |
| `work_performed` | string | Pass-through (free text) |
| `future_recommendations` | string | Pass-through (free text) |
| `remaining_tasks` | string | Pass-through (free text) |

Gemini is prompted to emit each field as `field` and its companion `field__confidence` (`HIGH`/`MEDIUM`/`LOW`).

### 11.2 Schema v3 — Image (Uni-Fix Form)

Nested structure matching the Uni-Fix paper work-order form layout:

```json
{
  "company": "string",
  "client": "string",
  "location": "string",
  "date": "string",
  "worker": "string",
  "travel": {
    "legs": [{ "from": "string", "to": "string", "km": "string" }],
    "total_km": "string"
  },
  "labour_summary": {
    "start": "string",
    "end": "string",
    "total_hours": "string"
  },
  "expenses": {
    "repas": "string",
    "logement": "string",
    "avion": "string",
    "autres": "string",
    "total": "string"
  },
  "tasks": [{
    "unit": "string",
    "time": { "start": "string", "end": "string", "total": "string" },
    "description": "string",
    "work_order_ref": "string"
  }],
  "future_recommendations": "string",
  "remaining_tasks": "string"
}
```

### 11.3 Output Envelope

Every processed file produces an envelope dict that is Fernet-encrypted to disk:

```json
{
  "schema_version": "1.0",
  "processed_at": "2026-06-01T14:30:00Z",
  "source_file": {
    "name": "site_visit.wav",
    "sha256": "<64-hex-chars>",
    "size_bytes": 2456789
  },
  "model_id": "gemini-3.1-flash-lite",
  "finish_reason": "STOP",
  "token_usage": {
    "prompt_tokens": 1842,
    "response_tokens": 312
  },
  "extracted_data": { /* work_order fields */ },
  "gemini_confidences": {
    "worker": "HIGH",
    "parts_used": "LOW",
    ...
  },
  "validation": {
    "overall_status": "REVIEW",
    "field_results": { /* per-field FieldResult */ },
    "unresolved_fields": ["parts_used"],
    "review_fields": ["parts_used"]
  },
  "veracity_info": {
    "triggered": true,
    "trigger_reason": "LOW confidence on parts_used",
    "corrections": { "parts_used": "Compressor Filter 3/4in" },
    "validation_post_veracity": { /* same structure as validation */ }
  }
}
```

---

## 12. Error Handling and Exit Codes

Exception handling is centralised in `main.py`. Each exception class maps to a distinct exit code, allowing shell scripts and CI pipelines to distinguish failure modes:

| Exit code | Exception class | Meaning |
|---|---|---|
| `0` | — | Success |
| `1` | `Exception` (unexpected) | Unhandled error; full traceback logged |
| `2` | `InputValidationError` | File failed pre-flight check |
| `2` | `VideoExtractionError` | ffmpeg failed or video unreadable |
| `3` | `APICallError` | Gemini API unreachable or rate-limited after retries |
| `4` | `ResponseParseError` | AI returned non-JSON or empty output |
| `5` | `CryptoError`, `WorkOrderProcessorError` | Encryption or general processing failure |
| `6` | `SchemaValidationError` | Extracted JSON did not conform to work-order schema |

The exception hierarchy rooted at `WorkOrderProcessorError` ensures that all application errors can be caught with a single `except WorkOrderProcessorError` at the top level while still allowing fine-grained handling at intermediate layers.

---

## 13. Observability

**Structured logging** — all log records use the format:
```
2026-06-01T14:30:00Z [INFO] work_order_processor — Validated audio file: site.wav (audio/wav, 2,456,789 bytes)
```

**Log levels by concern:**

| Level | Content |
|---|---|
| `DEBUG` | Parsed extraction dict, per-field scores, veracity prompt content |
| `INFO` | File validated, Gemini response received (with token counts), encrypted output written |
| `WARNING` | API retry attempt, schema soft warnings |
| `ERROR` | Validation failure, API exhaustion, unexpected exceptions |

**Audit trail in envelope** — the output envelope is itself the primary audit record, containing:
- Source file identity (name + SHA-256 + size).
- Model version and token consumption.
- Finish reason from Gemini.
- Full per-field validation results with algorithm-level score breakdowns.
- Veracity trigger reason and correction diff.

---

## 14. Extensibility Model

The system is designed for low-friction extension along three axes:

### Adding a new document type

1. Create `config/document_types/{type}_v1.yaml` — define prompts, schema version, fuzzy fields, thresholds.
2. Create `pipeline/{type}.py` — subclass `BasePipeline`, apply `@register("{type}")`, implement the three abstract hooks.
3. No other files need to change.

### Adding a new fuzzy-matching algorithm

1. Add the algorithm lambda to `_ALGO_MAP` in `validator/fuzzy_resolver.py`.
2. Reference it by name in any document type YAML `weights` dict.

### Swapping the reference database

1. Implement a class satisfying `ReferenceDataProvider` protocol.
2. Instantiate it in `main.py` instead of `InMemoryProvider`.

### Adding a new prompt version

1. Create `prompts/{name}_v{N}.txt`.
2. Update the `prompt:` field in the relevant YAML config.
3. No Python changes required.

---

## 15. Testing Architecture

The test suite lives in `workorder_processing/tests/` and comprises **321 tests** across four files.

| File | Count | Scope |
|---|---|---|
| `test_edge_cases.py` | 159 | End-to-end pipeline edge cases: malformed inputs, encoding, boundary values, error propagation |
| `test_fuzzy_resolver.py` | 68 | `FuzzyResolver` — exact match, typos, partial names, phonetic similarity, threshold boundary behaviour |
| `test_veracity.py` | 24 | Veracity trigger conditions, correction application, post-veracity re-validation |
| `test_image_processing.py` | 36 | Image quality assessment, preprocessing operations, MIME validation |

**Test isolation** — Gemini API calls are mocked via `unittest.mock.patch`. The `InMemoryProvider` is used directly for reference data, ensuring tests do not require any external services.

**Test fixtures** — the test module-level setup block loads `audio_v1` config and constructs a shared `FuzzyResolver` and `InMemoryProvider`, allowing assertion-level tests without pipeline overhead.

**Running tests:**
```bash
cd workorder_processing
python -m pytest tests/ -v                          # full suite
python -m pytest tests/test_fuzzy_resolver.py -v   # fuzzy-matching only
python -m pytest tests/test_edge_cases.py -v       # edge cases only
```

---

## 16. Dependency Overview

| Package | Version constraint | Role |
|---|---|---|
| `google-genai` | current | Gemini API client |
| `cryptography` | current | Fernet encryption |
| `rapidfuzz` | current | Fuzzy string matching ensemble |
| `opencv-python-headless` | current | Image quality assessment + enhancement |
| `Pillow` | current | Image format support |
| `numpy` | current | OpenCV array operations |
| `jsonschema` | current | JSON Schema validation |
| `python-dotenv` | current | `.env` loading |
| `pyyaml` | current | Document type config loading |
| `ffmpeg` | system binary | Audio extraction from video |

Runtime environment managed via Conda (`environment.yml`) with pip for any packages not in the Conda channel (`requirements.txt`).

---

## 17. Known Limitations and Future Considerations

### Current limitations

| Limitation | Impact | Mitigation path |
|---|---|---|
| **No batch processing** | One file per CLI invocation | Add an `--input-dir` flag; run pipelines concurrently via `concurrent.futures` |
| **No database integration** | Reference data is static fixtures | Implement a `PostgresProvider` or `SQLiteProvider` satisfying `ReferenceDataProvider` |
| **Image validation skips fuzzy matching** | Image v3 fields are not DB-resolved | Build an `ImageValidator` and wire it into `ImagePipeline._validate_fields` |
| **ffmpeg is a runtime system dependency** | Breaks on systems without ffmpeg | Bundle via Conda's `ffmpeg` package; document clearly in CI setup |
| **Veracity pass doubles API cost** | On REVIEW/FAIL/LOW, two Gemini calls are made | Consider a confidence threshold below which veracity is always skipped |
| **Static key at rest** | The Fernet key is a persistent file on disk | Integrate with a secrets manager (Vault, AWS Secrets Manager, GCP Secret Manager) |
| **No streaming / partial results** | Full file must be processed before any output | Not an issue for current use; relevant if real-time feedback is desired |
| **Single model, single temperature** | All extractions use the same Gemini model | Per-document-type model selection is easy to add to `DocumentConfig` |

### Architectural improvement opportunities

1. **Async pipeline** — the `extract()` step is entirely I/O-bound. Wrapping `call_gemini_generic` in `asyncio` would allow concurrent batch processing with minimal code changes, as all other steps are CPU-light.

2. **Confidence-gated veracity** — rather than a binary `LOW → always trigger`, a minimum-field-count threshold would avoid triggering the expensive second pass on a single low-confidence free-text field.

3. **Schema versioning strategy** — the current v1/v3 gap (v2 was skipped for images) suggests a need for a clear versioning policy with explicit migration paths when field schemas evolve.

4. **Production observability** — the audit envelope is a good foundation for structured metrics (token cost per document, validation pass-rate per document type, veracity trigger rate). Emitting these to a time-series sink would enable operational dashboards.

5. **Image validator** — a dedicated `validator/image_validator.py` analogous to the audio validator, with v3-specific field routing defined in `image_v3.yaml`, would bring image processing to parity with the audio pipeline.
