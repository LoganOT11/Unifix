# CLAUDE.md — Unifix Work Order Processor (Odoo 19)

The product is the **Odoo 19 module `unifix_odoo`**: users upload **audio, image, or
video** through the web UI; Gemini extracts structured work-order data (and, for
audio/video, a transcript; for video, captioned keyframe stills). The extraction
engine (formerly the standalone `workorder_processing/` CLI) is **vendored inside the
module** at `unifix_odoo/processing/` — one copy of the logic, the CLI is retired.

> **For deeper context, read these in-repo docs:**
> - `docs/architecture.md` — current architecture report: components, per-kind processing
>   flow, the vendored engine pipeline, the data model, and deployment notes.
> - `docs/design-decisions.md` — the *why*: storage/compression (incl. the measured
>   "compression ≠ token savings" finding), deterministic field confidence, the three
>   keyframe modes, editable DB-backed prompts/schemas, plus the known-issues roadmap.

Status: active dev on branch `feature/odoo-v19-implementation`. Changes are typically
left **uncommitted** in the working tree for review unless asked. Sample media fixtures
are gitignored.

---

## Environment & how to run

```bash
conda activate unifix        # has Odoo deps AND engine deps (genai, cv2, magic, rapidfuzz, ffmpeg, watchdog…)
```
- Odoo source: `/home/logan/Repos/odoo` (module symlinked into `addons/unifix_odoo`).
- Dev database: **`unifix_test`** (Postgres 18, running locally).
- Auth env: repo-root `.env` (Vertex/ADC — see below). Load with `set -a && . ./.env && set +a`.

> ⚠️ **Always export `OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1`** before running Odoo.
> OpenCV/numpy→OpenBLAS otherwise spawns one thread per core and **segfaults** on this
> WSL host. `unifix_odoo/__init__.py` also sets these defensively.

**Run the dev server (background) + verify pattern used throughout this project:**
```bash
cd /home/logan/Repos/odoo && conda activate unifix
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
set -a && . /home/logan/Repos/Unifix/.env && set +a
nohup python odoo-bin -d unifix_test --dev=all --http-port=8069 --log-level=info \
  >/tmp/odoo_server.log 2>&1 & disown
# UI: http://localhost:8069  (login admin / admin)   Menu: Unifix → Work Order Jobs
```
**End-to-end verification via odoo shell** (processes a real job through Gemini):
```bash
python odoo-bin shell -d unifix_test --no-http --log-level=warn <<'PY'
Job = env['unifix.workorder.job']
import base64
raw = open('/home/logan/Repos/Unifix/unifix_odoo/tests/fixtures/test_audio.wav','rb').read()
j = Job.create({'name':'t','media_kind':'audio','state':'draft',
                'media_file':base64.b64encode(raw),'media_filename':'test_audio.wav',
                'source_size':len(raw)})
j._process(); print(j.state, j.location, j.token_cost_display); env.cr.commit()
PY
```

### Dev-workflow gotchas (important)
- **`--dev=all` hot-reloads XML/views** from file on next request — but **NOT Python**:
  watchdog watches the *symlinked* addon dir, and edits land on the real file outside it,
  so **after a `.py` change you must restart the server** (kill the pid on :8069, relaunch).
- **New *stored* fields require a module upgrade** (`-u unifix_odoo`), not just a restart —
  the DB column won't exist otherwise (`UndefinedColumn` on commit). Computed/non-stored
  fields don't.
- `curl`/`wget` to localhost are intercepted by a hook here — use Python `urllib` or
  `odoo-bin shell` to exercise HTTP/data instead.
- `pkill -f odoo-bin` will match (and kill) your own shell command — kill by pid via
  `ss -ltnp | grep :8069` instead.

---

## Auth — Vertex AI + ADC (no API key)

This host authenticates to Gemini via **Vertex AI + Application Default Credentials**.
`.env` sets `GOOGLE_GENAI_USE_VERTEXAI=True`, `GOOGLE_CLOUD_PROJECT`,
`GOOGLE_CLOUD_LOCATION=global`. The worker builds `genai.Client(vertexai=True, project=…,
location=…)` — **no `api_key`**. On Odoo.sh/cloud, ADC isn't available — use a
service-account key (`GOOGLE_APPLICATION_CREDENTIALS`) or an API key
(`unifix.gemini_api_key`); `_gemini_client()` already supports the fallback.

---

## How processing works

`controllers/upload.py` streams the upload to a temp file, **auto-detects `media_kind`**,
and creates a `unifix.workorder.job` in state `received`. The **cron worker** (every
minute) — or the **"Process Now"** button, which now just *queues* `received` — runs
`job._process()`. Per kind:

| Kind | Stored | Gemini calls | Extra outputs |
|---|---|---|---|
| **audio** | media (→ **Opus**) | 2 (work-order + transcript) | transcript |
| **image** | media (→ **resized JPEG**) | 1 (image pipeline, schema v3) | tasks + travel/expenses JSON |
| **video** | **not stored** (keyframes only) | 2–3 (work-order audio + cue/transcript, **+1 frame-verification in `verified` mode**) | transcript + segments + keyframe stills |

`_process()` flow: write `media_file` to a temp file → run the engine
`get_pipeline(mode, cfg, provider).run(ctx)` (validate → preprocess → Gemini → parse →
schema-validate → audio: fuzzy/time/veracity → envelope) → map `extracted_data` onto
fields → **transcript/keyframes** (video) or **transcript** (audio) → **compress &
store** the small derivative → record token usage. Steps are best-effort (a transcript/
keyframe/compression failure does **not** fail an otherwise-successful job).

### Video keyframes — three selectable modes
`unifix.video_keyframe_mode` (a global default **and** a per-job field
`video_keyframe_mode` on the form) chooses how frames are picked. OpenCV
(`processor.keyframes.KeyframeExtractor`) scores frames by a **composite of sharpness +
exposure + contrast** (not bare Laplacian). Each keyframe gets a **1:1 segment** at the
same timestamp, so a segment is verifiable against its frame. The chosen **full-res**
frame is stored on `unifix.video.keyframe.image` (filestore); video itself is not kept.

- **`audio_cues`** — cues from the AUDIO only (no video tokens); best-scoring frame near
  each cue. Cheapest.
- **`verified`** *(default, recommended)* — audio cues → CV proposes top-K candidates per
  cue → **one cheap Gemini image call** picks the candidate that actually shows the
  captioned object (or drops the cue). Sends low-res thumbnails to choose; stores the
  full-res winner. Falls back to the CV best if the call fails.
- **`gemini_video`** — Gemini watches the whole video and commits to timestamps
  (duration-priced); a tight CV window grabs the frame. Best captions, most tokens.

### Compression (storage only — verified NOT to reduce Gemini tokens)
Extraction runs on the **original** (full quality); the worker then compresses for
storage: audio→**Opus** mono (`processor.compression.compress_audio_to_opus`),
image→**resized JPEG** (`compress_image`). The UI shows `Original → Stored (% smaller)`.
Measured: audio ~98%, image ~89%. **Re-extracting from the compressed copy is lossless.**
See `docs/design-decisions.md` ("Storage & compression").

### Token usage
Input/output tokens are summed across all of a job's Gemini calls (`input_tokens` /
`output_tokens`) and shown in the form ("Gemini tokens", with a rough $ estimate).
Note: audio & `audio_cues`-video jobs make **two** audio calls (work-order + transcript/
cues) — the planned **combination call** (see design-decisions "Known issues") would merge them.

---

## Config params (`ir.config_parameter`, seeded in `data/default_params.xml`)

| Param | Default | Purpose |
|---|---|---|
| `unifix.use_vertexai` | `True` | Vertex/ADC (else `unifix.gemini_api_key`) |
| `unifix.gcp_project` / `unifix.gcp_location` | *(env)* | Vertex project/location override |
| `unifix.gemini_model` | `gemini-3.1-flash-lite` | Model ID (`_DEFAULT_MODEL` in code) |
| `unifix.reference_provider` | `memory` | Field validation: `memory` fixtures / `odoo` live records |
| `unifix.enable_keyframes` | `True` | Extract video keyframes |
| `unifix.max_keyframes` | `8` | Cap per video |
| `unifix.video_keyframe_mode` | `verified` | `audio_cues` / `verified` / `gemini_video` (per-job overridable via the job's `video_keyframe_mode` field) |
| `unifix.audio_opus_bitrate` | `24k` | Stored-audio bitrate |
| `unifix.image_max_dim` / `unifix.image_jpeg_quality` | `2048` / `80` | Stored-image resize/quality |
| `unifix.token_price_per_million` | `0.30` | Blended $/M for the UI cost estimate |
| `unifix.max_{video,audio,image}_size_mb` | 1024 / 200 / 25 | Upload caps |
| `unifix.temp_dir` / `unifix.cleanup_age_hours` | tmp / 1 | Temp handling |

### Editable prompts & schemas (DB-backed)
Prompts and JSON schemas are **editable in the backend** (Unifix → Configuration → AI
Prompts / AI Schemas; `unifix.ai.prompt` / `unifix.ai.schema`, `mail.thread` history).
The vendored engine reads them **DB-first with file fallback** via a resolver registered
on `processor.prompt_loader` (`models/ai_prompt.py` binds the env during `_run_pipeline`);
records are seeded from the on-disk files by `post_init_hook` / the `19.0.3.0.0` migration.
Editing a prompt/schema in the UI takes effect on the next job — no restart.

---

## Module structure

```
unifix_odoo/
├── __init__.py                 ← caps BLAS threads · puts processing/ on sys.path · post_init_hook (seeds AI prompts/schemas)
├── __manifest__.py             ← depends: base, mail · post_init_hook · external_dependencies (genai, cv2, magic, rapidfuzz, jsonschema, …)
├── ai_seed.py                  ← seeds unifix.ai.prompt / unifix.ai.schema from the vendored prompt/schema files
├── migrations/19.0.3.0.0/      ← post-migrate: seed AI prompt/schema records on upgrade of existing DBs
├── controllers/upload.py       ← /unifix/upload — auto-detects audio|image|video
├── models/
│   ├── workorder_job.py        ← unifix.workorder.job — the coordinator (state machine,
│   │                              dispatch, Gemini auth, mapping, keyframe modes, token usage)
│   ├── ai_prompt.py            ← unifix.ai.prompt / unifix.ai.schema (editable; registers the engine resolver)
│   ├── workorder_task.py        ← unifix.workorder.task — image task rows (schema v3)
│   ├── video_segment.py / video_keyframe.py  ← transcript segments + keyframe stills (ACTIVE)
│   ├── reference_provider.py   ← OdooReferenceDataProvider (live-DB veracity seam)
│   └── res_config_settings.py
├── data/        ← cron.xml, default_params.xml
├── views/       ← workorder_job_views.xml, workorder_job_menu.xml, ai_prompt_views.xml, res_config_settings_views.xml
├── security/    ← ir.model.access.csv, security.xml   (NOTE: no record rules yet — see review P1.3)
├── tests/       ← Odoo TransactionCase tests + fixtures/ (test_audio.wav, sample_form.jpg, sample_video.mp4)
└── processing/                 ← VENDORED engine (source root on sys.path)
    ├── pipeline/   {base,audio,video,image}.py     ← get_pipeline(mode,cfg,provider).run(ctx)
    ├── config/     document_types/*.yaml           ← load_document_config()
    ├── processor/  gemini_client, parser, validator, image_*, veracity, prompt_loader,
    │               crypto, video_extractor, keyframes.py, compression.py
    ├── validator/  fuzzy_resolver, time_validator, resolution, work_order_validator
    ├── db/         protocol, memory, reference_data  ← ReferenceDataProvider
    ├── schemas/    work_order_v1.json (audio/video flat) · work_order_v3.json (image nested)
    ├── prompts/    *.txt
    └── tests/      engine pytest suite (incl. test_media.py for keyframes/compression)
```

`processor.keyframes` (OpenCV `KeyframeExtractor`) and `processor.compression`
(ffmpeg/cv2) are **framework-free services** extracted from the model so they're
unit-testable without Odoo (part of the ongoing god-model decomposition — review P1.1).

---

## Tests

```bash
# Engine + service tests (pure pytest, no Odoo) — from the vendored source root:
cd unifix_odoo/processing && python -m pytest tests/ -q          # 331 tests

# Odoo model tests (mock the pipeline, no Gemini calls):
cd /home/logan/Repos/odoo && OPENBLAS_NUM_THREADS=1 \
  python odoo-bin -d unifix_test -u unifix_odoo --test-enable --stop-after-init --no-http
```

## Adding a new document type (engine)
Drop `processing/config/document_types/{type}.yaml` + `processing/schemas/{schema}.json`
+ `processing/prompts/{prompt}.txt`; register `pipeline/{type}.py` with `@register("{type}")`;
map it in `workorder_job._run_pipeline`.

## Validation details (audio v1) — confidence is deterministic
Fuzzy DB match: `worker, company, location, vehicle_equipment, parts_used`. The field's
**match status against the reference data IS the confidence** — LLM self-reported
confidence is no longer requested or used (it's poorly calibrated). Per-field detail is
stored on the job (`validation_json`, shown on the "Field Validation" tab). Time normalise:
`start_time, end_time, total_time_spent`; everything else (incl. the shared `client`/`date`
header) passes through. Scores: EXACT ≥ 100 · HIGH ≥ 85 · LOW 60–84 · NO_MATCH < 60. If a
reference set is empty (master data not loaded) the field is `PASS_THROUGH` ("unverified"),
not failed — so an empty master list never flags every record. Veracity re-runs only on
REVIEW/FAIL. Tune in `processing/config/document_types/audio_v1.yaml`. Set
`unifix.reference_provider=odoo` to validate against live records (`hr.employee`,
`res.partner`, `fleet.vehicle`, `product.product`; guarded so missing models degrade gracefully).

## Known issues / what's next
See **`docs/design-decisions.md`** ("Known issues / roadmap"). Headlines: P0.1 video
temp-file handoff is **not Odoo.sh-safe** (HTTP and cron are separate workers — use the
filestore, not `/tmp`); finish the god-model decomposition (P1.1); add **record-level
security** (P1.3); the **combination call** to stop sending audio twice (P1.5).
