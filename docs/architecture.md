# Unifix — Architecture

Current architecture of the **Odoo 19 module `unifix_odoo`**. Pairs with
[`design-decisions.md`](design-decisions.md) (the *why*) and `/CLAUDE.md` (how to run and
verify). The **code is the source of truth**; this is the map.

---

## What it is

Users upload **audio, image, or video** through the Odoo web UI. Gemini extracts structured
work-order data (plus, for audio/video, a transcript, and for video, captioned keyframe
stills). The extraction engine is **vendored inside the module** at `unifix_odoo/processing/`
(the old standalone CLI is retired).

## Two layers

```
┌─────────────────────────── Odoo layer (unifix_odoo) ───────────────────────────┐
│ controllers/upload.py   → creates a job (auto-detects media kind)              │
│ models/workorder_job.py → coordinator: state machine, dispatch, auth, mapping, │
│                            compression, keyframe modes, token accounting        │
│ models/ai_prompt.py     → editable prompts/schemas + engine resolver binding    │
│ models/*                → tasks, segments, keyframes, reference provider, config │
│ views / security / data / migrations                                            │
└───────────────────────────────────┬─────────────────────────────────────────────┘
                                     │ get_pipeline(mode, cfg, provider).run(ctx)
┌───────────────────────────────────▼─── Vendored engine (processing/) ───────────┐
│ pipeline/  base · audio · video · image   (template-method, @register)          │
│ processor/ gemini_client · parser · prompt_loader · veracity · keyframes ·      │
│            compression · video_extractor                                         │
│ validator/ fuzzy_resolver · time_validator · resolution · work_order_validator  │
│ config/ (document_types/*.yaml) · schemas/*.json · prompts/*.txt · db/ (provider)│
└─────────────────────────────────────────────────────────────────────────────────┘
```

The engine is **framework-free** (no Odoo imports). `unifix_odoo/__init__.py` puts
`processing/` on `sys.path` so its source-root imports resolve, and caps BLAS threads
(`OPENBLAS/OMP_NUM_THREADS=1`) to avoid an OpenCV/OpenBLAS segfault on this WSL host.

## Request → result flow

1. **Upload** — `controllers/upload.py` streams the file to a temp path, **auto-detects**
   `media_kind` (audio/image/video), and creates a `unifix.workorder.job` in state
   `received`.
2. **Queue, don't block** — the **"Process Now"** button only sets `state=received`; the
   per-minute **cron** runs the actual (multi-second) AI work. Never inside the HTTP request.
3. **`_process()`** — writes `media_file` to a temp file → runs the engine pipeline →
   maps `extracted_data` onto fields → kind-specific extras (transcript / keyframes) →
   compresses & stores the small derivative → records token usage → `state=done`.
   Each extra step is **best-effort** (its failure doesn't fail the job).

### Per kind

| Kind | Stored | Gemini calls | Extra outputs |
|---|---|---|---|
| **audio** | media → **Opus** | 2 (work-order + transcript) | transcript |
| **image** | media → **resized JPEG** | 1 (image pipeline, schema v3) | task rows + travel/expenses JSON |
| **video** | **not stored** (keyframes only) | 2–3 (audio work-order + cue/transcript, +1 frame-verification in `verified` mode) | transcript + segments + keyframe stills |

## The engine pipeline (template method)

`pipeline/base.py:BasePipeline.run(ctx)` defines the shared sequence; subclasses override the
input-specific hooks:

```
validate_input → preprocess → _load_prompt → extract (Gemini) → parse →
schema-validate → field-validate (audio/video) → veracity? → build envelope
```

- `AudioPipeline` reads audio bytes; `VideoPipeline` extends it (extracts audio from the
  video first); `ImagePipeline` runs the schema-v3 image path. `get_pipeline(mode,…)` selects
  by `@register("audio"|"video"|"image")`.
- Document behaviour is config-driven: `config/document_types/{audio_v1,image_v3,…}.yaml`
  binds a prompt + schema + validation/veracity policy (`load_document_config`).
- The envelope returned to Odoo carries `extracted_data`, `validation` (overall status +
  per-field detail), `usage` (tokens), and `veracity_pass`.

## Framework-free services

Extracted from the model so they're unit-testable without Odoo or real media:

- `processor/keyframes.py:KeyframeExtractor` — OpenCV frame scoring (composite
  sharpness+exposure+contrast), candidate selection, aHash dedup, JPEG encode. Drives all
  three keyframe modes (see design-decisions §4).
- `processor/compression.py` — ffmpeg→Opus audio, cv2→resized JPEG image. Storage only.

## Data model

- **`unifix.workorder.job`** — the record/coordinator: media, state, extracted header +
  body fields, `extracted_json`, `validation_json` (per-field deterministic match detail),
  token counts, `video_keyframe_mode`.
- **`unifix.workorder.task`** — image work-order task rows (schema v3 nested table).
- **`unifix.video.segment`** / **`unifix.video.keyframe`** — transcript segments and keyframe
  stills, linked **1:1** at the same timestamp so each segment is verifiable against its frame.
- **`unifix.ai.prompt`** / **`unifix.ai.schema`** — editable, DB-backed prompts/schemas
  (`mail.thread` history) read by the engine DB-first with file fallback.
- **`OdooReferenceDataProvider`** (plain helper, not a model) — the live-DB reference seam.

## Validation & confidence

For audio/video, closed-set fields (`worker, company, location, vehicle_equipment,
parts_used`) are resolved against a **reference data provider**; the **deterministic match
status is the confidence** (EXACT/HIGH/LOW/NO_MATCH) — LLM self-confidence is not used. Time
fields are normalised; everything else passes through. An empty reference set →
`PASS_THROUGH` ("unverified"), not a failure. `REVIEW`/`FAIL` triggers a veracity re-check
against the source audio. Provider is selectable via `unifix.reference_provider`
(`memory` fixtures / `odoo` live records). See design-decisions §3.

## Auth

Gemini via **Vertex AI + Application Default Credentials** by default
(`genai.Client(vertexai=True, project, location)`, no API key). `_gemini_client()` falls
back to a service-account key or `unifix.gemini_api_key` for cloud where ADC isn't available.

## Deployment notes

- **Filestore, not DB**, for all binary (media + keyframes).
- **Odoo.sh caveat:** HTTP and cron run as separate workers that share only the filestore —
  the current video temp-file handoff via `/tmp` is **not** safe there (design-decisions,
  P0.1). Audio/image are safe.
- New **stored** fields require a module upgrade (`-u unifix_odoo`); editable
  prompts/schemas are seeded by `post_init_hook` / the `19.0.3.0.0` migration.

## Tests

- Engine + services: pure pytest from `unifix_odoo/processing/` (`python -m pytest tests/ -q`,
  331 tests) — no Odoo, no Gemini.
- Odoo models: `odoo-bin -d unifix_test -u unifix_odoo --test-enable` (pipeline mocked).
- End-to-end: process a real job via `odoo-bin shell` (see `/CLAUDE.md`).
