# Unifix — Design Decisions

Why the `unifix_odoo` module is built the way it is. Pairs with
[`architecture.md`](architecture.md) (the *what*) and `/CLAUDE.md` (how to run/verify).
The **code is the source of truth**; this records the rationale and the measured findings
that aren't obvious from reading it.

---

## 1. Vendor the extraction engine into the module

The extraction logic was a standalone `workorder_processing/` CLI. It is now **vendored
inside the module** at `unifix_odoo/processing/` and the CLI is retired.

- **Why:** one copy of the logic, installed as a normal Odoo addon with no external setup.
  `unifix_odoo/__init__.py` puts `processing/` on `sys.path` so the engine keeps its
  source-root imports (`from processor…`, `from pipeline…`) and stays **framework-free** —
  importable and unit-testable without Odoo.
- **Consequence:** the engine must not import Odoo. Where it needs host data (editable
  prompts, live reference data) it exposes a **seam** the Odoo layer plugs into (see §5, §6).

## 2. Storage: filestore, compressed derivatives, video not stored

- **Stored in the Odoo filestore, not the DB.** `media_file` is `attachment=True`; keyframe
  images are `fields.Image`. Binary stays on disk, not in Postgres.
- **Compress for storage only.** Extraction always runs on the **original** (full quality);
  the worker then stores a small derivative — audio→**Opus** mono
  (`processor.compression.compress_audio_to_opus`), image→**resized JPEG** (`compress_image`).
  Measured reduction: **audio ~98%, image ~89%**. Re-extracting from the compressed copy is
  lossless in practice.
- **Compression ≠ Gemini token savings (measured).** Sending a compressed audio/image to
  Gemini does **not** reduce token cost — tokens are priced on decoded content/duration, not
  bytes. So compression is purely a *storage* optimisation; never compress before extraction.
- **Video is never persisted.** Only the derived **keyframe stills + transcript + segments**
  are kept (storage constraint + privacy). The temp video is deleted after processing.
- **Best-effort.** A transcript / keyframe / compression failure must **not** fail an
  otherwise-successful job — these run inside `try/except` and degrade gracefully.

## 3. Field confidence is deterministic, not LLM self-reported

Earlier prompts asked Gemini to emit `field__confidence` (HIGH/MEDIUM/LOW) per field.

- **Why changed:** LLM verbalised self-confidence is **poorly calibrated**, and for the
  image pipeline it was extracted and thrown away entirely. For fields whose valid values
  are a known set (worker, company, client, location, equipment, parts), there is already a
  **grounded** signal: the fuzzy-match score against the reference data.
- **Decision:** confidence = the deterministic **match status** (EXACT ≥100 · HIGH ≥85 ·
  LOW 60–84 · NO_MATCH <60). The `<confidence_markers>` prompt blocks are removed; the
  Gemini-confidence score boost is gone; **veracity re-runs only on REVIEW/FAIL** status.
- **Empty master data is a distinct state.** If a reference set has no candidates (e.g. the
  `odoo` provider before master data exists), the field is `PASS_THROUGH` ("unverified"),
  **not** `NO_MATCH` — an empty list must never flag every record. A genuinely new
  client/part also reads as unverified → review / new-record, not "extraction wrong".
- **Surfaced:** per-field status/score is persisted (`validation_json`) and shown on the
  job's *Field Validation* tab. Implication: accuracy of these fields scales with how
  complete the master data is.

## 4. Video keyframes: AI picks the moment, CV/AI picks the frame

The hard part is that the *moment something is described* (audio) is not the *moment it is
best framed* (video), and the sharpest frame near a timestamp may not show the subject.

- **Composite frame quality.** `KeyframeExtractor` scores frames by **sharpness gated by
  exposure + contrast**, not bare Laplacian variance — which otherwise favours busy
  backgrounds over a clear subject.
- **Three selectable modes** (`unifix.video_keyframe_mode` default + per-job
  `video_keyframe_mode` field):
  - **`audio_cues`** — cues from audio only; best-scoring frame per cue. Cheapest, no video
    tokens, but "sharp ≠ relevant" can still bite.
  - **`verified`** *(default)* — audio cues → CV proposes top-K candidates in a wider window
    → **one cheap Gemini image call** (low-res thumbnails) picks the candidate that actually
    shows the captioned object, or drops the cue. Stores the **full-res** winner. Fixes
    say↔show lag and relevance; falls back to the CV best if the call fails.
  - **`gemini_video`** — Gemini watches the whole video (duration-priced) and commits to
    timestamps; a tight CV window grabs the frame. Best captions, most tokens; for short clips.
- **Default = `verified`** for accuracy, but the upgrade does **not** silently flip an
  existing deployment's mode (that would add cost unannounced) — only fresh installs start
  there; every job can override in the UI.
- **Storage:** each chosen frame → a `unifix.video.keyframe` (full-res JPEG in the
  filestore) plus a **1:1 `unifix.video.segment`** at the same timestamp, so a segment is
  always verifiable against its frame.

## 5. Editable, DB-backed prompts & schemas

Prompts (`prompts/*.txt`) and JSON schemas (`schemas/*.json`) ship as files but are
**editable in the backend** (`unifix.ai.prompt` / `unifix.ai.schema`, `mail.thread`
history; Unifix → Configuration).

- **Why:** operators can tune extraction without code changes or restarts, with an audit
  trail of who changed what.
- **How (keeps the engine Odoo-free):** `processor.prompt_loader` exposes
  `set_override_resolver`; `models/ai_prompt.py` registers a **thread-local-bound** resolver
  that `_run_pipeline` activates for the duration of a job. The engine resolves **DB-first
  with file fallback**; the validator reads schemas through the same `load_schema` so the
  prompt's `{schema_block}` and output validation never drift.
- **Seeding:** `post_init_hook` (fresh install) and the `19.0.3.0.0` migration (existing DBs)
  populate records from the files idempotently; a *Reload from File* button resets one.

## 6. Other seams & defaults

- **Auth = Vertex AI + ADC, no API key** on this host (`genai.Client(vertexai=True, …)`);
  `_gemini_client()` falls back to a service-account key or `unifix.gemini_api_key` for
  Odoo.sh/cloud where ADC isn't available.
- **Reference-data provider is pluggable** (`unifix.reference_provider`): `memory` (bundled
  fixtures, works out of the box) or `odoo` (live `hr.employee` / `res.partner` /
  `fleet.vehicle` / `product.product`, each guarded so a missing model degrades gracefully).
- **Shared header across schemas.** Audio (v1) and image (v3) share a `worker / company /
  client / location / date` header through one `_apply_header()` mapping; only the body
  differs (audio = flat narrative; image = nested tasks/travel/expenses). `client`/`date`
  are optional in v1 (a voice memo may omit them; a printed form always has them).
- **"Process Now" only queues.** The button sets `state=received` and the per-minute cron
  runs the (multi-second) AI call — never inside the HTTP request.

---

## Known issues / roadmap

Prioritised; the headline items the next contributor should know.

- **P0.1 — video temp-file handoff is not Odoo.sh-safe.** The upload writes the video to
  local `/tmp` and stores the path; on Odoo.sh the HTTP and cron workers are **separate
  dynos** that don't share `/tmp` (only the filestore). Video jobs will fail there with
  "Temp file not found". Fix: stage the upload as a filestore attachment (or object storage)
  and stream it back in the worker. Audio/image are safe (they live in `media_file`).
- **P1.1 — god model.** `workorder_job.py` still mixes ORM + auth + orchestration + I/O.
  Keyframe/compression logic is already extracted into framework-free services
  (`processor/keyframes.py`, `compression.py`); continue with per-kind processors and a
  build-the-client-once factory.
- **P1.3 — no record-level security.** All internal users can see every job (media +
  transcripts). Add an owner field + `ir.rule`s (and `company_id` if multi-company).
- **P1.4 — Gemini client rebuilt 2–3× per job.** Build once per `_process` and pass it down
  (the `verified` frame-verification call adds another rebuild today).
- **P1.5 — two audio calls per audio/video job** (work-order + transcript/cues). A
  **combination call** would merge them and ~halve audio input tokens.
- **P1.6 — reprocess double-compresses** (opus→opus / re-JPEG). Guard with an
  "already compressed" marker.
- **P2 — polish:** use `fields.Json` for the JSON text fields; make `validation_status` a
  Selection for badges; reconcile `max_keyframes` defaults (code `8` vs data `12`); prune
  dead engine code (`processor/crypto.py`, CLI-era config); rename `unifix.video.*` →
  `unifix.workorder.*`.

## Keyframe accuracy — possible next step

`verified` mode closes most of the relevance gap. If frames still come back off-subject, the
next lever is a lightweight subject check (avoid "technician selfie" frames via face
detection / centre-weighting) before the verification call, or returning Gemini's
bounding evidence in `gemini_video` mode to extract the exact validated frame.
