# Investigation & Groundwork — Ephemeral Video Processing Pipeline (Odoo v19)

**Pass type:** Read-only investigation. No code was modified.
**Date:** 2026-06-09
**Scope:** Map current state of the `Unifix` repository, compare against the target
"ephemeral video → transcript (Phase 1) → keyframes (Phase 2)" Odoo architecture, and
produce a gap analysis + phased plan.

---

## ⚠️ Headline finding (read this first)

**There is no Odoo project in this repository.** The brief is written as an audit of an
existing Odoo v19 module, but no Odoo code exists here to audit:

- No `__manifest__.py` / `__openerp__.py` anywhere (`find` returned nothing).
- No `models.Model`, `fields.*`, `@http.route`, `ir.attachment`, `ir.cron`, `queue.job`,
  or OWL — a repo-wide signature grep across `*.py/*.xml/*.js/*.md/*.yaml` matched only
  `docs/architecture.md`, `CLAUDE.md`, and an incidental token in
  `workorder_processing/processor/validator.py` — never actual Odoo source.
- No Odoo module directory layout (`models/ controllers/ views/ security/ data/ wizard/`).
- Git has a single branch `master`, remote `git@github.com:LoganOT11/Unifix.git`. No
  `odoo.sh` remote, no Odoo branch, no version tags.

What **does** exist is a mature **standalone Python CLI** (`workorder_processing/`) that
turns an audio / **video** / image file into a structured, schema-validated, Fernet-encrypted
JSON work-order record using the Gemini API.

**Consequence for this engagement:** Sections A (module structure) and G (v19 conformance)
have essentially nothing to grade — the Odoo side is **greenfield**. The real work is to
(1) build the Odoo module from scratch and (2) **adapt the existing processor into the
external worker** the target architecture calls for. The good news: the existing code already
satisfies several hard constraints *in spirit* (heavy compute is already outside any Odoo;
the video is never copied into a managed store). It is a strong worker candidate, not a
liability to refactor.

---

## 1. Current-state inventory (what exists today, with file paths)

### Entry point & dispatch
- **CLI only.** `workorder_processing/main.py:87` — `argparse` takes a single positional
  `input_file` (`main.py:91`). Mode is auto-detected from extension
  (`main.py:54` `_auto_detect_mode`): video extensions → `"video"`, image → `"image"`, else
  `"audio"`. There is **no web route, no RPC, no form, no upload widget**.
- Pipelines are registered via an `@register(...)` decorator and dispatched by
  `get_pipeline(mode, config, provider)` (`main.py:112`). Execution is a single synchronous
  `pipeline.run(ctx)` call (`main.py:128`).

### Pipeline architecture (the template that processes a file)
- `pipeline/base.py:45` `BasePipeline` — template-method `run()` (`base.py:60`):
  `validate_input → preprocess → _load_prompt → extract → _parse → _validate_schema →
  _validate_fields → _run_veracity → _build_envelope → _serialize`.
- `pipeline/audio.py:16` `AudioPipeline` — reads bytes, calls Gemini.
- `pipeline/video.py:18` `VideoPipeline(AudioPipeline)` — overrides only input-validation and
  preprocessing (audio extraction); inherits everything else.
- `pipeline/image.py` — `ImagePipeline` (OpenCV enhance + nested schema).

### Processing internals
- `processor/video_extractor.py:27` `extract_audio_from_video()` — ffmpeg `-vn` extract.
- `processor/validator.py` — `validate_audio_file()` / `validate_video_file()` pre-flight.
- `processor/gemini_client.py` — `call_gemini_with_retry()` (3 attempts, exp backoff on 429/5xx).
- `processor/parser.py` — `parse_ai_json()`, `validate_extracted_data()` (jsonschema),
  `build_response_envelope()`.
- `processor/crypto.py` — Fernet encrypt/decrypt; key at `~/.workorder_processor.key`.
- `processor/veracity.py` — optional second Gemini pass (audio only).

### Data definitions
- `schemas/work_order_v1.json` — audio/video schema: **13 flat string fields**
  (vehicle_equipment, reported_problem, diagnosis_cause, work_performed, parts_used,
  start_time, end_time, total_time_spent, future_recommendations, remaining_tasks, worker,
  company, location). **No transcript, no segments, no timestamps.**
- `schemas/work_order_v3.json` — image schema: **nested** (`tasks[]` array, each with a
  `time{start,end,total}` object + `work_order_ref`). Demonstrates the repo already does
  one-to-many nested schemas — relevant precedent for Phase 2.
- `prompts/audio_extraction_v2.txt` — instructs Gemini to emit the 13 work-order fields +
  five `__confidence` markers. **It does not ask for a transcript or any timing.**
- `config/document_types/*.yaml` — field weights, thresholds, fuzzy/time routing.

### Outputs
- `pipeline/base.py:184` `_serialize()` — writes Fernet-encrypted `<stem>.json.enc` (chmod
  0640) to the output dir (default = the input file's own directory), plus an optional
  plaintext `<stem>.json` sidecar when `--plaintext`. Sample artifacts already committed:
  `workorder_processing/outputs/8224.enc`, `8224.json`, `test_audio.enc`, `test_audio.json`.

### Secrets & config
- `.env` loaded by walking up from `main.py` (`main.py:34`), `python-dotenv`. Required:
  `GOOGLE_API_KEY`; model via `GEMINI_MODEL`; key path via `ENCRYPTION_KEY_PATH`. No vault,
  no Odoo config params.

### Existing docs
- `docs/architecture.md` — "Architectural Design Report" for the *current processor* (not an
  Odoo plan; mostly headers). `findings-veracity-disambiguation.md` — confidence-scoring notes.

---

## 2. Priority findings (anything violating the hard constraints)

### 🟢 P0 — Video is **not** persisted today (constraint #1 already met in this CLI)
Traced end-to-end:
1. `VideoPipeline.validate_input` (`video.py:22`) → `validate_video_file` (`validator.py`)
   only **stats and sniffs** the file (path-traversal guard to cwd, extension allowlist,
   10 KB–2 GB bounds, libmagic MIME check). It returns a metadata dict and **does not copy**
   the video anywhere.
2. `VideoPipeline.preprocess` (`video.py:31`) → `extract_audio_from_video` (`video_extractor.py:27`)
   runs `ffmpeg -i <video> -vn -acodec pcm_s16le -ar 16000 -ac 1 <tmp.wav>`
   (`video_extractor.py:62`). ffmpeg reads the original video **in place**; only the extracted
   **audio** WAV is written, to `tempfile.mkstemp(suffix=".wav")` (`video_extractor.py:59`).
3. The temp audio path is stashed on `ctx.extra["_tmp_audio_path"]` and **reliably deleted in
   a `finally` block** (`video.py:44`) regardless of success/exception. ✅
4. The encrypted envelope stores only `extracted_data` + a SHA-256 hash + size + token usage
   (`parser.py build_response_envelope`). **No media bytes are persisted.** ✅

**So the video bytes never land in any managed store in the current CLI.** The only persistence
of video is the *user's own input file* on their workstation, which the program treats as
read-only source.

### 🟡 P1 — Whole-video read into RAM twice (memory, not storage; will matter at scale)
- ffmpeg streams the file, but `build_response_envelope` does
  `source_bytes = Path(source_path).read_bytes()` to compute the SHA-256
  (`parser.py`), loading the **entire video into memory** (up to the 2 GB cap). On a
  constrained worker this is a footgun. Hashing should stream in chunks. (Not a storage-cost
  violation, but flagged because the target worker will be resource-limited.)

### 🟡 P1 — Temp WAV lives on local disk during processing
- Acceptable per constraint #1 ("may transit a temp path… unlinked immediately"), and cleanup
  is reliable. Flagged only so the future Odoo.sh design does **not** route this temp file
  through an Odoo request worker or the filestore.

### 🔴 P2 — No transcript/timestamps are produced at all
- The current Gemini call returns **work-order fields, not a timestamped transcript**
  (`audio_extraction_v2.txt`, `work_order_v1.json`). Phase 1's core deliverable
  ("transcription with timestamps") and Phase 2's prerequisite (word/segment timestamps to
  seek frames) have **zero existing support**. This is the biggest functional gap, but it is a
  *missing feature*, not a constraint violation.

> No code path currently writes video bytes to a filestore or DB — because there is no Odoo and
> no web upload yet. The risk is entirely **forward-looking**: a naive Odoo integration that
> accepts the upload via a binary field would immediately violate constraint #1. See §3.

---

## 3. Gap analysis — current state vs target architecture (per constraint)

| # | Hard constraint | Target | Current state | Gap |
|---|---|---|---|---|
| 1 | No video in Odoo storage | Video transits temp/bucket, never a persisted `ir.attachment`/binary | No Odoo; CLI never copies video; temp audio cleaned up | **Greenfield risk.** Must design the Odoo upload path so the video is streamed to temp/bucket and never bound to a record. The *processor* already conforms; the *Odoo entry point* doesn't exist. |
| 2 | Heavy compute outside Odoo | ffmpeg/ASR in external worker | ffmpeg + Gemini already run in a standalone Python process | **Mostly met.** Repackage the CLI as a callable worker/service. No Odoo container runs ffmpeg today (because no Odoo). |
| 3 | Async, not synchronous | Long job off the HTTP request | Fully synchronous `pipeline.run()` | **Missing.** No queue, no async, no job-state model. |
| 4 | Odoo = system-of-record for derived data | Transcript/segments/keyframes in Odoo models | Output is a local encrypted `.json.enc` file | **Missing.** No Odoo models; persistence is flat files, not the DB. |
| 5 | Don't block v19 conformance later | Follow v19 conventions | N/A — no Odoo yet | **Neutral.** Greenfield; easy to start conformant. Note existing Python naming is PEP-8, not Odoo. |

### Per investigation area

- **A. Baseline:** No `__manifest__.py`, no version pin → Odoo version is **undetermined**
  (open question). Deployment target unknown — no `odoo.sh`/container/worker-limit config in
  repo. The only runtime config is conda + `.env`.
- **B. Video handling:** Entry = CLI arg (not a controller/binary field/form/RPC). Bytes end
  up: original stays at the user's path (read-only); a temp WAV is created then deleted; the
  encrypted JSON holds only derived data + a hash. **No filestore/DB persistence of video.**
- **C. Data model:** No ORM models. Schemas are JSON-Schema files. `work_order_v1` is flat with
  **no transcript/segment/timestamp fields**; `work_order_v3` proves nested arrays are already
  in use (good precedent). Current schema **does not** support one-video→many-segments→
  many-keyframes.
- **D. Processing/orchestration:** Gemini integration exists (`gemini_client.py`) but is
  **synchronous** and returns structured fields, not a transcript. **No job queue. No callback
  endpoint.** Secrets via `.env`.
- **E. Frontend:** None. Results are a `print()` summary (`main.py:69`). No views, no OWL, no
  web — nothing to render keyframes/transcript yet.
- **F. Security:** No endpoints (CLI), so no `auth=`/record-rules surface. Existing safeguards:
  path-traversal guard to cwd, libmagic MIME sniff, Fernet-at-rest, key chmod 0600.
- **G. v19 conformance:** N/A (no Odoo). Record for later: pick `o_unifix` CSS prefix, model
  `unifix.*` naming, ORM-only, transient models in `wizard/`, bundle all assets.

---

## 4. Component action table (CHANGE / IMPLEMENT / VERIFY)

| Component | Action | Rationale (one line) |
|---|---|---|
| Odoo module skeleton (`unifix_video/`) | **IMPLEMENT** | Nothing exists; greenfield manifest + dir layout. |
| Upload entry (thin `http` controller *or* direct-to-bucket) | **IMPLEMENT** | No web entry today; must stream-to-temp + hand off, never bind to a record. |
| `ir.attachment` / binary field for video | **VERIFY (must stay absent)** | Constraint #1 — guard that no future model gains a video binary/`res_field`. |
| External worker service | **CHANGE** | Repackage existing CLI (`pipeline/`, `processor/`) as a queue-driven worker/HTTP service. |
| Whole-video `read_bytes()` SHA-256 (`parser.py`) | **CHANGE** | Stream-hash in chunks; don't load ≤2 GB into RAM on a constrained worker. |
| ffmpeg audio extract (`video_extractor.py`) | **VERIFY/REUSE** | Already correct (`-vn`, temp, reliable `finally` cleanup); reuse as-is in worker. |
| Transcription-with-timestamps | **IMPLEMENT** | No timestamps produced today; need ASR returning word/segment timing. |
| New prompt + transcript schema | **IMPLEMENT** | `work_order_v1` has no transcript/segments; add a timestamped-transcript schema. |
| Odoo data model (job + transcript + segment[]+ keyframe[]) | **IMPLEMENT** | DB is system-of-record for derived data; nested model needed for Phase 2. |
| Async job + state machine (`received→processing→done|failed`) | **IMPLEMENT** | Processing is synchronous today; must not block HTTP. |
| Job queue (`queue_job` or external) | **IMPLEMENT** | None present. |
| Authenticated callback/result-ingestion endpoint | **IMPLEMENT** | No endpoint exists; worker must write results back securely. |
| Secrets handling | **CHANGE** | Move from local `.env` to Odoo config params / worker env / vault. |
| Frontend (views/OWL for transcript + keyframes) | **IMPLEMENT** | Only a CLI `print` exists today. |
| Encryption (`crypto.py`) | **VERIFY** | Reusable for derived data at rest; decide if Odoo DB encryption supersedes Fernet. |
| Phase-2 keyframe extraction | **IMPLEMENT** | No frame-extraction code; ffmpeg seek-by-timestamp to be added in worker. |

---

## 5. Open decisions for the team (these block implementation)

1. **Confirm the actual target.** Is the intent to build a *new* Odoo v19 module that wraps this
   processor, or to keep the CLI and only bolt on storage? The brief assumes Odoo exists; it
   does not. (Highest-priority decision.)
2. **Upload path:** direct browser→bucket (best for constraint #1, keeps bytes entirely out of
   Odoo) **vs** thin Odoo `http` controller that streams to a temp path then deletes.
3. **ASR/transcription service:** keep Gemini (already integrated, but returns fields not
   timestamps — needs a transcript prompt/mode) **vs** a dedicated ASR (Whisper self-hosted,
   Deepgram/AssemblyAI API) that natively returns word/segment timestamps.
4. **Job queue:** OCA `queue_job` (in-Odoo, simplest for callbacks) **vs** external queue
   (SQS/Redis/Celery) feeding the standalone worker.
5. **Where keyframes live (Phase 2):** small `ir.attachment`/`Image` in Odoo **vs** bucket/CDN +
   URL field (cheaper, keeps filestore lean).
6. **Callback auth scheme:** shared-secret HMAC header, signed JWT, or Odoo API key on a
   dedicated `auth="user"` technical account.
7. **Odoo version / deployment:** confirm v19 specifically, and Odoo.sh vs self-hosted (drives
   worker limits, max upload size, whether ffmpeg can run anywhere near Odoo).
8. **Hashing/integrity:** keep SHA-256 of the source video? If so, stream it (don't buffer 2 GB).

---

## 6. Phased plan

### Phase 0 — Decisions & skeleton (unblocks everything)
- Resolve §5 #1–#4 and #7. Scaffold `unifix_video/` Odoo module (manifest, dirs, security).
- **Risk:** building on the wrong upload/queue choice forces rework — settle #2 and #4 first.

### Phase 1 — Transcript + ephemeral video
1. **Upload + hand-off (no persistence).** Implement the chosen entry path; video goes to a
   temp path/bucket, **never** an `ir.attachment`/binary field. Add a test asserting no
   attachment with a `res_field` is created.
   *Risk:* an accidental binary field or website-form attachment silently persists the video.
2. **Job model + state machine.** `unifix.video.job` with `state` (received/processing/done/
   failed), source hash, timestamps. Enqueue async.
   *Risk:* request worker time/size limits on Odoo.sh — keep the controller thin.
3. **Worker repackage.** Wrap `pipeline/processor` as a queue-driven worker; add an ASR mode
   that returns **transcript + word/segment timestamps**; stream-hash the source.
   *Risk:* current Gemini call doesn't emit timestamps — may require a different ASR (decision #3).
4. **Authenticated callback.** Worker POSTs results to an Odoo endpoint (auth per decision #6);
   Odoo writes transcript + segments; the temp video/audio is expired/unlinked at hand-off.
   *Risk:* unauthenticated/public callback = data-integrity hole — gate it before shipping.
5. **Minimal presentation.** Read-only view of transcript + segments.
   *Risk:* none material.

### Phase 2 — Keyframes
1. **Schema/model for keyframes.** Extend the data model to one-job→many-segments→many-keyframes
   (the `work_order_v3` nested pattern is the precedent). Store small `Image`/`ir.attachment`
   **or** bucket URL per decision #5.
2. **Worker frame extraction.** Add ffmpeg seek-by-timestamp (`-ss <t> -frames:v 1`) at the
   important segment times; return stills.
   *Risk:* picking "important" moments reliably; clear vs blurry frame selection.
3. **Callback + storage of stills.** Same authenticated endpoint stores keyframes (or URLs).
   *Risk:* keyframes re-introduce storage cost — keep them small/offloaded (decision #5).
4. **OWL presentation.** Render transcript with inline keyframe thumbnails.
   *Risk:* v19 OWL conformance — build native from the start (don't port legacy JS).

---

## 7. Assumptions (made because the code didn't say)

- **A1.** The brief's "Odoo v19" target is aspirational; **no Odoo exists in the repo yet**, so I
  treated the Odoo side as greenfield rather than auditing a nonexistent module.
- **A2.** The existing `workorder_processing` package is intended to **become / feed the external
  worker**, not be discarded. (Inferred from it already running ffmpeg+ASR outside any Odoo.)
- **A3.** "Transcription with timestamps" means a real transcript + word/segment timing — which
  the current field-extraction prompt/schema does **not** produce; treated as net-new.
- **A4.** Deployment is Odoo.sh (per the brief's storage-cost rationale), though no `odoo.sh`
  config, container file, or worker-limit setting exists in the repo to confirm it.
- **A5.** The 2 GB video cap (`video_extractor.py:18`) and `ffmpeg` availability assumptions
  carry over to the worker environment.
- **A6.** "Important moments" for Phase 2 keyframes will be derived from transcript timestamps;
  no heuristic for selecting them exists yet, so it is treated as new design work.
- **A7.** Fernet at-rest encryption (`crypto.py`) may or may not be retained once Odoo's DB
  becomes the store; left as a VERIFY item.
