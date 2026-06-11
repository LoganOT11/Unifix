# CLAUDE.md — Unifix Work Order Processor (Odoo 19)

The product is the **Odoo 19 module `unifix_odoo`**: users upload audio, image, or
video through the web UI and Gemini extracts structured work-order data. The
extraction engine (formerly the standalone `workorder_processing/` CLI) is now
**vendored inside the module** at `unifix_odoo/processing/`. The CLI has been
retired — there is one copy of the logic, and it lives in the module.

## Environment

```bash
conda activate unifix     # has Odoo deps AND the engine deps (genai, cv2, magic, rapidfuzz, …)
```

Odoo source: `/home/logan/Repos/odoo` (module symlinked into `addons/unifix_odoo`).
Dev database: `unifix_test` (Postgres 18).

> **Always export `OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1`** when running Odoo here.
> The image pipeline imports OpenCV/numpy→OpenBLAS, which otherwise spawns one
> thread per core and segfaults on thread-constrained hosts (WSL). The module also
> sets these defensively in `unifix_odoo/__init__.py`.

## Auth — Vertex AI + ADC (no API key)

This host authenticates to Gemini via **Vertex AI + Application Default Credentials**,
not an API key. `.env` (repo root) sets `GOOGLE_GENAI_USE_VERTEXAI=True`,
`GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION=global`. The worker builds the client
with `genai.Client(vertexai=True, project=…, location=…)` — **no `api_key`**.

Config params (`ir.config_parameter`, seeded in `data/default_params.xml`):

| Param | Default | Purpose |
|---|---|---|
| `unifix.use_vertexai` | `True` | Use Vertex/ADC (else fall back to `unifix.gemini_api_key`) |
| `unifix.gcp_project` / `unifix.gcp_location` | *(env)* | Override project/location |
| `unifix.gemini_model` | `gemini-2.5-pro` | Model ID |
| `unifix.reference_provider` | `memory` | Field validation source: `memory` (fixtures) or `odoo` (live records) |
| `unifix.max_{video,audio,image}_size_mb` | 1024 / 200 / 25 | Upload caps |

## Module structure

```
unifix_odoo/
├── __init__.py                 ← caps BLAS threads + puts processing/ on sys.path
├── __manifest__.py             ← external_dependencies declared here
├── controllers/upload.py       ← /unifix/upload — auto-detects audio|image|video
├── models/
│   ├── workorder_job.py        ← unifix.workorder.job — lifecycle + _process() dispatch
│   ├── workorder_task.py       ← unifix.workorder.task — image task rows (schema v3)
│   ├── reference_provider.py   ← OdooReferenceDataProvider (live-DB veracity seam)
│   ├── video_segment.py / video_keyframe.py  ← dormant; for future video keyframes
│   └── res_config_settings.py
├── data/        ← cron.xml, default_params.xml
├── views/       ← workorder_job_views.xml, _menu.xml, res_config_settings_views.xml
├── security/    ← ir.model.access.csv, security.xml
├── tests/       ← Odoo TransactionCase tests (+ fixtures/: test_audio.wav, sample_form.jpg)
└── processing/                 ← VENDORED extraction engine (source root on sys.path)
    ├── pipeline/ {base,audio,video,image}.py   ← get_pipeline(mode,cfg,provider).run(ctx)
    ├── config/   {__init__.py, document_types/*.yaml}  ← load_document_config()
    ├── processor/ {gemini_client,parser,validator,image_*,veracity,crypto,…}.py
    ├── validator/ {fuzzy_resolver,time_validator,resolution,work_order_validator}.py
    ├── db/        {protocol,memory,reference_data}.py   ← ReferenceDataProvider
    ├── schemas/   work_order_v1.json (audio/video flat) · work_order_v3.json (image nested)
    ├── prompts/   *.txt
    └── tests/     321 pytest engine tests (+ pytest.ini, test_data/)
```

## How processing works

`controllers/upload.py` streams the upload to a temp file, auto-detects `media_kind`,
and creates a `unifix.workorder.job` in state `received`. The cron worker (every minute)
calls `job._process()`, which:

1. Resolves a file path (video: temp file; audio/image: stored `media_file` → temp copy).
2. Builds a Vertex client (`_gemini_client`) and a provider (`_reference_provider`).
3. Runs `get_pipeline(mode, cfg, provider).run(ctx)` with `PipelineContext(serialize=False,
   safe_root=<temp dir>)` — the engine's template: validate → preprocess → Gemini →
   parse → schema-validate → (audio: fuzzy/time/veracity) → envelope.
4. Maps `envelope['extracted_data']` onto the record (`_apply_workorder_flat` for
   audio/video, `_apply_image` for image → header + `task_ids` + `extra_data_json`).

**Media storage:** audio & image media persist in `media_file` (Binary `attachment=True`
→ filestore). **Video is never stored** — audio is extracted, processed, and the temp
video deleted. Keyframe extraction from video is a planned future feature (the
segment/keyframe models are kept dormant for it).

**Modes/configs:** audio & video → `audio_v1` (schema v1, fuzzy+time+veracity);
image → `image_v3` (schema v3 nested tasks/travel/expenses, OpenCV preprocess, no fuzzy).

## Run & verify (through Odoo)

```bash
cd /home/logan/Repos/odoo && conda activate unifix
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1

# Install/upgrade (recreate dev DB if a model was renamed):
python odoo-bin -d unifix_test -u unifix_odoo --stop-after-init --no-http --log-level=warn

# End-to-end via odoo shell (load .env first for Vertex auth):
set -a && . /home/logan/Repos/Unifix/.env && set +a
python odoo-bin shell -d unifix_test --no-http   # create a job, set media_file, call job._process()
```

## Tests

```bash
# Engine tests (pure pytest, no Odoo) — run from the vendored source root:
cd unifix_odoo/processing && python -m pytest tests/ -q          # 321 tests

# Odoo model tests (mock the pipeline, no Gemini calls):
cd /home/logan/Repos/odoo && OPENBLAS_NUM_THREADS=1 \
  python odoo-bin -d unifix_test -u unifix_odoo --test-enable --stop-after-init --no-http
```

## Adding a new document type (engine)

Drop a `processing/config/document_types/{type}.yaml` + `processing/schemas/{schema}.json`
+ `processing/prompts/{prompt}.txt`, and register a `pipeline/{type}.py` with
`@register("{type}")`. Then map it in `workorder_job._run_pipeline`.

## Validation details (audio v1)

Fuzzy DB match: `worker, company, location, vehicle_equipment, parts_used`.
Time normalise: `start_time, end_time, total_time_spent`. Everything else passes through.
Scores: EXACT ≥ 100 · HIGH ≥ 85 · LOW 60–84 · NO_MATCH < 60 (Gemini conf: HIGH +5, LOW −10).
Edit weights/thresholds in `processing/config/document_types/audio_v1.yaml`. Switch
`unifix.reference_provider` to `odoo` to validate against live records via
`OdooReferenceDataProvider` (maps entities → `hr.employee`, `res.partner`, `fleet.vehicle`,
`product.product`; guarded so missing models degrade to no candidates).
```
