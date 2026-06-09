# Investigation & Groundwork Brief — Ephemeral Video Processing Pipeline (Odoo v19)

## Role
You are a senior Odoo engineer doing a **read-only investigation pass**. Do **not** write or
modify code in this pass. Your job is to map the current state of this project, compare it against
the target architecture below, and produce a gap analysis + phased plan so implementation can
begin with no surprises.

## Product goal (what we are building)
A pipeline where a user submits a video, but **the video is never persisted in Odoo / Odoo.sh
storage**. Instead:

- **Phase 1 (now / prototype):** the video is processed (transcription with timestamps),
  the transcript and other relevant derived data are returned and stored in Odoo, and the
  video file is discarded. Nothing video-sized ever lands in the filestore.
- **Phase 2 (next):** using the audio/transcript timestamps, the processor extracts clear
  still frames at the moments where important information is spoken, and returns those images.
  Odoo stores the stills so there is a visual reference without paying video-scale storage.

## Hard constraints (the north star — measure every finding against these)
1. **No video in Odoo storage.** The video must never become a persisted `ir.attachment`
   (especially not one with a `res_field`) or a stored binary field. It may transit a temp path
   or external bucket, but must be unlinked/expired immediately after hand-off. Storage cost on
   Odoo.sh (billed per GB; filestore + DB both count) is the whole reason this project exists.
2. **Heavy compute lives outside Odoo.** Transcription and frame extraction (ffmpeg/ASR) should
   run in an external worker/service, not inside Odoo HTTP request workers. Assume Odoo.sh
   containers cannot reliably run these workloads and that request workers have hard
   time/size limits.
3. **Async, not synchronous.** Long-running processing must not block an Odoo HTTP request.
4. **Odoo is the system-of-record for derived data only** (transcript, segments, metadata,
   later keyframe stills).
5. **v19 architecture conformance is a *later* milestone**, but do not introduce anything now
   that will make that conformance harder. Note conformance gaps; don't fix them yet.

## Target architecture (reference for the gap analysis)
- Browser → (direct upload to external service or short-lived bucket) **or** Odoo thin `http`
  controller that streams to a temp path and hands off, then deletes.
- Async job (e.g. OCA `queue_job` or an external queue) tracks state:
  `received → processing → done | failed`.
- External worker performs transcription (returns transcript + word/segment timestamps) and,
  in Phase 2, frame extraction at chosen timestamps.
- Worker calls back into Odoo (authenticated endpoint) with results; Odoo writes the derived
  records. Keyframe stills stored as small `Image`/`ir.attachment` records, or as URLs if
  offloaded.

---

## Investigation tasks
Work through each area. For every finding, **cite the actual file path** (and line ranges where
useful). Where the code does not let you determine something, record it as an **open question**
rather than guessing.

### A. Project baseline
- Confirm the exact Odoo version (`__manifest__.py`, branch, any version pins).
- List the custom module(s) involved and their dependencies (`depends` in the manifest).
- Compare module directory structure against v19 conventions
  (`models/`, `controllers/`, `data/`, `views/`, `static/`, `wizard/`, `security/`,
  `__init__.py`, `__manifest__.py`). Note deviations — don't fix.
- Identify the deployment target (Odoo.sh? self-hosted? containerized?) and any worker/limit
  configuration you can find (`limit_time_real`, `limit_request`, max upload size, number of
  workers, cron workers).

### B. Current video handling  ← highest priority
- Trace exactly how a video enters the system today: controller route? binary field +
  `widget="binary"`? website form? RPC call?
- Determine **where the bytes end up**: `ir.attachment`? a binary/Image field? a `res_field`
  set? filestore vs DB? a temp dir?
- **Flag as a priority finding anything that currently persists the video in the filestore or
  database**, and quantify the storage exposure if you can.
- Identify whether any cleanup/`unlink`/auto-vacuum currently removes it, and whether that is
  reliable.

### C. Data model
- Inventory existing models related to video / transcript / results: fields, types, relations.
- Identify where the transcript, timestamps/segments, and metadata would live, and whether the
  current schema supports a one-video → many-segments → many-keyframes structure for Phase 2.
- Note any binary/Image fields that would persist large data.

### D. Processing & orchestration
- Is there any existing transcription/processing integration? Where is it called, sync or async?
- Is a job queue present (`queue_job` or other)? If not, note its absence as a gap.
- How are external service credentials/secrets handled today (config params, env, hardcoded)?
- Is there a callback/result-ingestion endpoint? How is it authenticated?

### E. Frontend / presentation
- How are results currently displayed (views, OWL components, widgets)?
- For Phase 2, what would render the keyframe stills + transcript? Note current vs OWL-based
  approach and any non-OWL legacy JS.

### F. Security & access
- Auth on any upload/result endpoints (`auth=` on routes), record rules, who can submit.
- Any public routes that should not be public.

### G. v19 conformance checklist (note gaps only, do not fix)
- Naming conventions (models, fields, files, xml ids, CSS class `o_<module>` prefix).
- ORM usage vs raw SQL.
- `_inherit` chains (v19 reportedly validates these more strictly — flag conflicts).
- Transient models located in `wizard/` and named correctly.
- No external-linked assets (images/libs referenced by URL instead of bundled).
- Model attribute ordering and linter cleanliness.

---

## Deliverables (produce these, in this order)
1. **Current-state inventory** — what exists today, with file paths.
2. **Priority findings** — anything violating the hard constraints (esp. persisted video).
3. **Gap analysis** — current state vs target architecture, per constraint.
4. **Component action table** — every component categorized as **CHANGE / IMPLEMENT / VERIFY**,
   with a one-line rationale each.
5. **Open decisions for the team** — the choices that block implementation, e.g.:
   - upload path: direct-to-bucket vs through-Odoo-then-delete;
   - which transcription/ASR service; self-hosted vs API;
   - job queue choice (`queue_job` vs external);
   - where keyframes live (Odoo `ir.attachment` vs CDN/bucket + URL);
   - callback authentication scheme.
6. **Phased plan** — Phase 1 (transcript + ephemeral video) and Phase 2 (keyframes), with
   sequencing, dependencies, and the main risk per step.
7. **Assumptions** — an explicit list of everything you assumed because the code didn't say.

## Rules of engagement
- Read-only this pass. No code changes, no migrations.
- Prefer citing repository evidence over general Odoo knowledge; when you rely on a convention,
  say so.
- Treat any persisted video bytes as a red-flag finding and surface it first.
- Do not perform v19 refactors now; only record conformance gaps for the later milestone.
- When uncertain, list an open question instead of guessing.