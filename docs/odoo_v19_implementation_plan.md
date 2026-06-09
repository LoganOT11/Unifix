# Option B (v19): Thin Controller + Local Processing + Keyframe Extraction — Implementation Plan

**Document type:** Architecture & implementation plan
**Date:** 2026-06-09
**Status:** Draft — pending team verification against the codebase
**Supersedes:** `odoo_option_b_plan.md` (Option B draft)
**Related:** `odoo_plan.md` (investigation brief), `odoo-video-pipeline-investigation.md` (gap analysis)

> **How to use this document.** Every code block is a *specification to adapt*, not a drop-in. Wherever a fact about the existing system is assumed, it is tagged **[VERIFY]**. Resolve all **[VERIFY]** tags against the real codebase before implementation. All XML/Python here targets **Odoo 19.0** conventions explicitly.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [What Changed vs the Option B Draft](#2-what-changed-vs-the-option-b-draft)
3. [Hard Constraints (carried forward, one clarified)](#3-hard-constraints)
4. [Architecture Overview](#4-architecture-overview)
5. [Byte Lifecycle — Revised Ordering](#5-byte-lifecycle--revised-ordering)
6. [Odoo Module Structure](#6-odoo-module-structure)
7. [Component Specifications](#7-component-specifications)
   - 7.1 [Upload Controller (v19 `type='http'`)](#71-upload-controller)
   - 7.2 [Job Model](#72-job-model)
   - 7.3 [Segment Model](#73-segment-model)
   - 7.4 [Keyframe Model (new)](#74-keyframe-model-new)
   - 7.5 [Async Worker / Cron — Reordered](#75-async-worker--cron--reordered)
   - 7.6 [ffmpeg Audio + Frame Extraction](#76-ffmpeg-audio--frame-extraction)
   - 7.7 [Gemini Call & Response Contract](#77-gemini-call--response-contract)
   - 7.8 [Cleanup Cron](#78-cleanup-cron)
8. [Views & UI (v19 `<list>`, inline attributes)](#8-views--ui)
9. [Configuration & Secrets](#9-configuration--secrets)
10. [Security Considerations](#10-security-considerations)
11. [Error Handling & Retry](#11-error-handling--retry)
12. [Testing Strategy (guard test corrected)](#12-testing-strategy)
13. [Phase 1 vs Phase 2 Scope](#13-phase-1-vs-phase-2-scope)
14. [Open Decisions](#14-open-decisions)
15. [Assumptions & [VERIFY] Checklist](#15-assumptions--verify-checklist)
16. [Appendix A: File Map](#appendix-a-file-map)
17. [Appendix B: Configuration Parameters](#appendix-b-configuration-parameters)
18. [Appendix C: External-Worker Variant (callback + HMAC)](#appendix-c-external-worker-variant)

---

## 1. Executive Summary

This plan implements the Unifix ephemeral video pipeline as an Odoo 19 module. A thin HTTP controller receives an upload, streams it to a temporary file, and returns immediately. A cron-driven, in-process worker then: extracts audio, sends audio to Gemini for a **timestamped transcript + curated keyframe timestamps + structured work-order fields**, **extracts video frames at those timestamps**, **stores the frames in Odoo as images**, and **only then deletes the video file**.

**Key properties (unchanged where noted):**

- The **video** never becomes a persisted `ir.attachment`, binary field, or filestore object.
- Derived data is persisted: transcript text, timestamped segments, extracted fields, and **extracted still frames (images)**.
- Heavy compute (ffmpeg, Gemini) runs in the cron worker, never in an HTTP request worker.
- The video exists on local disk only for the processing window, now extended to cover frame extraction.

**The defining change from the prior draft:** the video is deleted **after** Gemini returns timestamps and frames are extracted, not before. This is required by the timestamps → frames → upload → delete flow and is the single most important ordering correction in this document.

---

## 2. What Changed vs the Option B Draft

| # | Area | Draft (Option B) | This plan (v19 + keyframes) |
|---|------|------------------|-----------------------------|
| C1 | Video deletion point | Deleted **before** Gemini call | Deleted **after** frame extraction |
| C2 | Keyframes | Deferred to Phase 2 | **In Phase 1**: extract at Gemini timestamps, store as images |
| C3 | `attrs="{...}"` in views | Used throughout | **Removed** — inline `invisible=` / `readonly=` / `required=` / `column_invisible=` with Python expressions (required since v17) |
| C4 | `<tree>` view tag | Used throughout | **Renamed to `<list>`** (required since v18), including embedded One2many |
| C5 | Worker ↔ callback | In-process cron *and* a redundant HMAC HTTP callback | **Collapsed to in-process worker**; worker writes via ORM directly. Callback kept only as an external-worker variant (Appendix C) |
| C6 | Callback controller | `type='json'`, `request.jsonrequest`, tuple status codes | If reintroduced: `type='http'` + manual `Response` (since `jsonrpc` always returns HTTP 200) |
| C7 | Retry mechanism | Mixed `cron` + `with_delay()` | Consistent: cron-only for Phase 1; `with_delay` only if `queue_job` is actually installed |
| C8 | `source_size` display | `widget="integer" options="{'unit':'MB'}"` (not real) | Computed `source_size_mb` display field |
| C9 | Guard test | Forbids **all** Binary fields / **all** attachments | Forbids binary on the **job** model and any **`video/*`** attachment; **allows image attachments** |
| C10 | Byte-lifecycle guarantee | "video gone before Gemini" | Re-stated honestly: video persists through the Gemini round-trip, still ephemeral (2–5 min), still never in filestore |

---

## 3. Hard Constraints

Inherited from `odoo_plan.md`. Constraint #1 is **clarified** (not weakened) to reflect keyframes.

| # | Constraint | Implication |
|---|------------|-------------|
| 1 | **No video in Odoo storage.** The *video* must never become a persisted attachment or stored binary. **Derived still images (keyframes) are permitted**, exactly like the transcript is permitted. | No binary field on the job model; no `video/*` attachment ever; keyframe images stored via an image field on the keyframe model are allowed. |
| 2 | **Heavy compute outside HTTP workers.** | ffmpeg (audio + frames) and Gemini run in the cron worker. |
| 3 | **Async, not synchronous.** | Controller enqueues and returns; worker runs later. |
| 4 | **Odoo is system-of-record for derived data only.** | Transcript, segments, fields, keyframe images — yes. Video — no. |
| 5 | **v19 conformance from the start.** | `<list>`, inline view attributes, `type='http'`/`type='jsonrpc'`, ORM-only, `unifix.*` naming. |

> **Constraint #1 reading that matters for review:** storing keyframe *images* does create `ir.attachment` rows under the hood (if using `fields.Image`). That is allowed — the prohibition is specifically on the **video**. The guard test in §12 enforces the distinction by mimetype.

---

## 4. Architecture Overview

```
Browser (authenticated user)
  1. Opens upload form / OWL component
  2. POST multipart/form-data to /unifix/upload  (CSRF token included)
        │
        ▼
Odoo HTTP Controller  /unifix/upload   (type='http', auth='user')
  3. Streams video → <temp_dir>/unifix_<uuid>.<ext>   (8KB chunks)
  4. Validates extension, size, magic bytes
  5. Creates unifix.video.job (state='received', tmp_path=...)
  6. Returns JSON {job_id, state}  — IMMEDIATELY
        │
        ▼
Odoo DB: unifix.video.job (state='received', tmp_path set, no binary)
        │
        │  Cron "Process pending jobs" (every 1 min) picks it up
        ▼
Cron Worker  unifix.video.job._process_video()   (in-process, ORM access)
  7.  state='processing'
  8.  SHA-256 (streaming)
  9.  ffmpeg → audio.wav            (VIDEO KEPT)
  10. Gemini(audio) → transcript + segments[start,end] + keyframes[t]
                       + work-order fields
  11. delete audio.wav
  12. for each keyframe timestamp: ffmpeg seek → frame.jpg → bytes
        → create unifix.video.keyframe (image = bytes)        ← FRAMES UPLOADED
  13. delete video file              ← VIDEO GONE (now safe)
  14. write transcript + fields; create unifix.video.segment rows
  15. state='done'
        │
        ▼
Browser: state → "Done"; transcript, segments, and frame gallery visible.
  In DB: text + image attachments only.  In filestore: no video.  On disk: nothing.
```

---

## 5. Byte Lifecycle — Revised Ordering

| Step | Location | Format | Lifetime | Cleanup |
|------|----------|--------|----------|---------|
| Upload in progress | HTTP stream | Original | Seconds–minutes | Stream ends |
| Controller writes | `<temp_dir>/unifix_<uuid>.<ext>` | Original | Until worker finishes frames | Worker deletes at step 13 |
| ffmpeg reads video (audio) | RAM buffer | Original | Seconds | GC'd |
| Audio file | `<temp_dir>/unifix_<uuid>.wav` | PCM 16k mono | Until Gemini responds | Worker deletes at step 11 |
| Gemini processing | Google cloud | Audio bytes | Seconds | Google handles |
| **Video frames** | RAM → `unifix.video.keyframe.image` | JPEG | Permanent (derived) | N/A (kept by design) |
| **Video file (kept for frames)** | `<temp_dir>/unifix_<uuid>.<ext>` | Original | **Through entire Gemini round-trip + frame extraction** | Worker deletes at step 13 |
| Results | PostgreSQL (text) + image attachments | Text + JPEG | Permanent | N/A |

**Revised guarantees (state these honestly in review):**
- Total time video exists on disk: ~2–5 min (now spans the Gemini round-trip).
- Peak disk usage: one video + one audio + a handful of small JPEGs (~1.1× video size).
- Video in Odoo filestore/DB: **never**.
- Keyframe images in Odoo: **yes, by design** (derived data).

---

## 6. Odoo Module Structure

```
unifix_video/
├── __init__.py
├── __manifest__.py
├── controllers/
│   ├── __init__.py
│   └── upload.py                  # POST /unifix/upload (type='http', auth='user')
├── models/
│   ├── __init__.py
│   ├── video_job.py               # unifix.video.job + worker logic
│   ├── video_segment.py           # unifix.video.segment
│   ├── video_keyframe.py          # unifix.video.keyframe  (NEW)
│   └── res_config_settings.py     # settings fields
├── data/
│   ├── cron.xml                   # process cron + cleanup cron
│   └── default_params.xml         # default ir.config_parameter values
├── security/
│   ├── ir.model.access.csv
│   └── security.xml               # groups: unifix_user, unifix_manager
├── views/
│   ├── video_job_views.xml        # list, form (with Frames tab), search
│   ├── video_job_menu.xml
│   └── res_config_settings_views.xml
├── prompts/
│   └── transcript_v1.txt          # Gemini prompt (transcript + keyframes + fields)
├── static/
│   └── description/icon.png
└── tests/
    ├── __init__.py
    ├── test_upload.py
    ├── test_worker.py
    ├── test_keyframes.py          # NEW: frame extraction + ordering
    └── test_no_video_storage.py   # CORRECTED guard test
```

> **[VERIFY]** The investigation brief implies existing `workorder_processing` / `video_extractor.py` logic. Confirm whether `_extract_audio`, the Gemini client wrapper, and the prompt should be *imported/refactored* from that package rather than rewritten here. This plan assumes the worker logic is folded into `video_job.py`; adapt imports accordingly.

---

## 7. Component Specifications

### 7.1 Upload Controller

**File:** `controllers/upload.py` · **Route:** `POST /unifix/upload` · **v19:** `type='http'`, `auth='user'`.

```python
import os
import uuid
import tempfile
from odoo import http
from odoo.http import request

ALLOWED_VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.mov', '.avi', '.webm'}
# Magic-byte prefixes are coarse; treat as a sanity check, not a guarantee. [VERIFY]
_CHUNK = 8192


class UnifixUploadController(http.Controller):

    @http.route('/unifix/upload', type='http', auth='user',
                methods=['POST'], csrf=True)
    def upload_video(self, video_file=None, **kwargs):
        if not video_file:
            return self._err(400, 'No file provided')

        ext = os.path.splitext(video_file.filename or '')[1].lower()
        if ext not in ALLOWED_VIDEO_EXTENSIONS:
            return self._err(400, f'Unsupported video format: {ext}')

        ICP = request.env['ir.config_parameter'].sudo()
        temp_dir = ICP.get_param('unifix.temp_dir', tempfile.gettempdir())
        max_mb = int(ICP.get_param('unifix.max_video_size_mb', '1024'))

        os.makedirs(temp_dir, exist_ok=True)
        tmp_path = os.path.join(temp_dir, f'unifix_{uuid.uuid4().hex}{ext}')

        # Stream to disk; never buffer the whole file in memory.
        size = 0
        with open(tmp_path, 'wb') as f:
            os.chmod(tmp_path, 0o600)
            while True:
                chunk = video_file.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)

        if size < 10240:
            os.unlink(tmp_path)
            return self._err(400, 'File too small')
        if size > max_mb * 1024 * 1024:
            os.unlink(tmp_path)
            return self._err(400, f'File too large (max {max_mb} MB)')

        job = request.env['unifix.video.job'].create({
            'name': f'Video Job {uuid.uuid4().hex[:8]}',
            'state': 'received',
            'tmp_path': tmp_path,
            'source_size': size,
            'source_filename': video_file.filename,
        })

        return request.make_json_response({
            'job_id': job.id,
            'state': 'received',
            'message': 'Video uploaded. Processing will begin shortly.',
        })

    def _err(self, code, msg):
        return request.make_json_response({'error': msg}, status=code)
```

**v19 notes**
- `type='http'` (not `json`) so the body is `multipart/form-data` and we control the JSON via `make_json_response`.
- `csrf=True`: the Odoo web client / OWL component must send the CSRF token (`odoo.csrf_token`). **[VERIFY]** how the upload UI submits — if it is a non-Odoo external client, you must instead use a `bearer`/token scheme rather than disabling CSRF.
- No `ir.attachment`, no binary field is ever touched here.

### 7.2 Job Model

**File:** `models/video_job.py` · **Model:** `unifix.video.job`

| Field | Type | Notes |
|-------|------|-------|
| `name` | Char (required) | Auto-generated |
| `state` | Selection | `draft → received → processing → done \| failed \| cancelled` |
| `tmp_path` | Char | Path string only (not filestore) |
| `source_filename` | Char | Original name |
| `source_size` | Integer | Bytes |
| `source_size_mb` | Float (compute, store=False) | Display helper (replaces the bogus widget option) |
| `source_hash` | Char | SHA-256, set by worker |
| `transcript` | Text | Full transcript |
| `segment_ids` | One2many → `unifix.video.segment` | Timestamped segments |
| `keyframe_ids` | One2many → `unifix.video.keyframe` | **NEW** — extracted frames |
| `keyframe_count` | Integer (compute) | For the smart button / UI |
| work-order fields | Char/Text | `worker_name, company, location, reported_problem, diagnosis_cause, work_performed, parts_used, start_time, end_time, total_time_spent, future_recommendations, remaining_tasks` |
| `error_message` | Text | On failure |
| `retry_count` | Integer | Attempts |
| `processed_at` | Datetime | Completion time |

```python
import os, json, hashlib, logging
from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class UnifixVideoJob(models.Model):
    _name = 'unifix.video.job'
    _description = 'Unifix Video Processing Job'
    _order = 'create_date desc'

    name = fields.Char(required=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('received', 'Received'),
        ('processing', 'Processing'), ('done', 'Done'),
        ('failed', 'Failed'), ('cancelled', 'Cancelled'),
    ], default='draft', required=True, index=True)

    tmp_path = fields.Char()
    source_filename = fields.Char()
    source_size = fields.Integer()
    source_size_mb = fields.Float(compute='_compute_size_mb')
    source_hash = fields.Char(index=True)

    transcript = fields.Text()
    segment_ids = fields.One2many('unifix.video.segment', 'job_id')
    keyframe_ids = fields.One2many('unifix.video.keyframe', 'job_id')
    keyframe_count = fields.Integer(compute='_compute_keyframe_count')

    worker_name = fields.Char()
    company = fields.Char()
    location = fields.Char()
    reported_problem = fields.Text()
    diagnosis_cause = fields.Text()
    work_performed = fields.Text()
    parts_used = fields.Text()
    start_time = fields.Char()
    end_time = fields.Char()
    total_time_spent = fields.Char()
    future_recommendations = fields.Text()
    remaining_tasks = fields.Text()

    error_message = fields.Text()
    retry_count = fields.Integer(default=0)
    processed_at = fields.Datetime()

    @api.depends('source_size')
    def _compute_size_mb(self):
        for rec in self:
            rec.source_size_mb = (rec.source_size or 0) / (1024.0 * 1024.0)

    @api.depends('keyframe_ids')
    def _compute_keyframe_count(self):
        for rec in self:
            rec.keyframe_count = len(rec.keyframe_ids)

    # --- methods specified in §7.5 ---
```

**Methods:** `_process_video()`, `_extract_audio(path)`, `_extract_frames(path, timestamps)`, `_call_gemini(audio_path)`, `_write_results(result)`, `_cleanup_temp_files()`, `action_retry()`, `action_cancel()`.

### 7.3 Segment Model

**File:** `models/video_segment.py` · **Model:** `unifix.video.segment` — unchanged from the draft except the embedded view becomes `<list>` (see §8).

| Field | Type | Notes |
|-------|------|-------|
| `job_id` | Many2one → job (required, ondelete='cascade') | Parent |
| `sequence` | Integer | Order |
| `start_time` / `end_time` | Float | Seconds |
| `start_time_display` / `end_time_display` | Char | `mm:ss` |
| `text` | Text (required) | Segment text |
| `confidence` | Float | 0.0–1.0 |

### 7.4 Keyframe Model (NEW)

**File:** `models/video_keyframe.py` · **Model:** `unifix.video.keyframe`
**Purpose:** one extracted still image per selected timestamp; the "frames uploaded to Odoo" target.

| Field | Type | Notes |
|-------|------|-------|
| `job_id` | Many2one → job (required, ondelete='cascade') | Parent |
| `segment_id` | Many2one → segment (optional, ondelete='set null') | Link to the segment the frame illustrates |
| `sequence` | Integer | Display order |
| `timestamp` | Float (required) | Seconds into the video |
| `timestamp_display` | Char | `mm:ss` |
| `image` | Image | The extracted frame (JPEG). **Derived data — permitted.** |
| `image_filename` | Char | e.g. `frame_000123.jpg` |
| `reason` | Char | Why this moment was chosen (from Gemini), optional |

```python
from odoo import models, fields


class UnifixVideoKeyframe(models.Model):
    _name = 'unifix.video.keyframe'
    _description = 'Unifix Video Keyframe'
    _order = 'job_id, sequence, timestamp'

    job_id = fields.Many2one('unifix.video.job', required=True,
                             ondelete='cascade', index=True)
    segment_id = fields.Many2one('unifix.video.segment', ondelete='set null')
    sequence = fields.Integer(default=10)
    timestamp = fields.Float(required=True)
    timestamp_display = fields.Char()
    # max_width/height keep stored frames small; tune to UI needs. [VERIFY]
    image = fields.Image(max_width=1280, max_height=720)
    image_filename = fields.Char()
    reason = fields.Char()
```

> **Storage decision.** `fields.Image` stores the frame as an attachment-backed binary (creates `ir.attachment` with an `image/*` mimetype). This satisfies Constraint #1 (no *video*). Alternatives if your team rejects image attachments entirely: `fields.Binary(attachment=False)` (inline in the DB column — bloats the DB), or store frames in an object bucket and keep only a URL (Option-A style). Primary recommendation: `fields.Image`. See Open Decision D7.

**Security (`ir.model.access.csv`):**
```csv
id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
access_keyframe_user,unifix.video.keyframe.user,model_unifix_video_keyframe,base.group_user,1,0,0,0
access_keyframe_manager,unifix.video.keyframe.manager,model_unifix_video_keyframe,unifix_video.group_unifix_manager,1,1,1,1
```

### 7.5 Async Worker / Cron — Reordered

**File:** `models/video_job.py`. The ordering below is the heart of this plan: **video deleted only after frames are extracted.**

```python
def _process_video(self):
    self.ensure_one()
    audio_path = None
    try:
        self.write({'state': 'processing'})

        tmp_path = self.tmp_path
        if not tmp_path or not os.path.exists(tmp_path):
            raise FileNotFoundError(f"Temp video file not found: {tmp_path}")

        # 1) Integrity hash (streaming)
        sha = hashlib.sha256()
        with open(tmp_path, 'rb') as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                sha.update(chunk)
        self.source_hash = sha.hexdigest()

        # 2) Extract audio — KEEP the video for frame extraction
        audio_path = self._extract_audio(tmp_path)

        # 3) Gemini: transcript + segment timestamps + keyframe timestamps + fields
        result = self._call_gemini(audio_path)

        # 4) Audio no longer needed
        os.unlink(audio_path)
        audio_path = None

        # 5) Decide which timestamps to capture as frames
        timestamps = self._select_keyframe_timestamps(result)

        # 6) Extract frames at those timestamps -> store as keyframe records
        frames = self._extract_frames(tmp_path, timestamps)   # [(t, jpeg_bytes, reason)]
        self._store_keyframes(frames, result)

        # 7) NOW the video is safe to delete
        os.unlink(tmp_path)
        self.tmp_path = False

        # 8) Persist transcript, segments, work-order fields
        self._write_results(result)

        self.write({'state': 'done',
                    'processed_at': fields.Datetime.now()})

    except Exception as e:
        _logger.exception("Unifix job %s failed", self.id)
        self.write({'state': 'failed',
                    'error_message': str(e),
                    'retry_count': self.retry_count + 1})
        self._cleanup_temp_files(audio_path)
        # Do not re-raise inside cron loop; the cron catches per-job (see cron.xml)
```

**Keyframe timestamp selection** (`_select_keyframe_timestamps`): prefer Gemini's curated `keyframes` list; fall back to one frame per segment start, capped by `unifix.max_keyframes`.

```python
def _select_keyframe_timestamps(self, result):
    max_kf = int(self.env['ir.config_parameter'].sudo()
                 .get_param('unifix.max_keyframes', '12'))
    curated = result.get('keyframes') or []
    if curated:
        items = [(float(k['t']), k.get('reason')) for k in curated]
    else:
        items = [(float(s['start']), None) for s in result.get('segments', [])]
    # de-duplicate near-identical timestamps (< 0.5s apart) and cap
    items.sort(key=lambda x: x[0])
    deduped, last = [], -10.0
    for t, reason in items:
        if t - last >= 0.5:
            deduped.append((t, reason))
            last = t
    return deduped[:max_kf]
```

**Store keyframes** (`_store_keyframes`): map each extracted frame to a record, link to the nearest segment, base64-encode for the `Image` field.

```python
import base64

def _store_keyframes(self, frames, result):
    Keyframe = self.env['unifix.video.keyframe']
    seg_by_start = sorted(self.segment_ids, key=lambda s: s.start_time or 0.0)
    for i, (t, jpeg_bytes, reason) in enumerate(frames):
        seg = self._nearest_segment(seg_by_start, t)
        Keyframe.create({
            'job_id': self.id,
            'segment_id': seg.id if seg else False,
            'sequence': (i + 1) * 10,
            'timestamp': t,
            'timestamp_display': self._fmt_ts(t),
            'image': base64.b64encode(jpeg_bytes),
            'image_filename': f'frame_{int(t*1000):08d}.jpg',
            'reason': reason,
        })
```

> Note: segments are created in `_write_results` (step 8) *after* keyframes (step 6) in the code above. If you want `segment_id` links on keyframes, either (a) create segments before keyframes, or (b) link keyframes to segments after both exist. **Recommended:** create segments first, then keyframes — reorder steps 6 and 8 accordingly. The ordering that matters for the *constraint* is only that the **video deletion (step 7) stays after frame extraction (step 6).**

**Cron (`data/cron.xml`)** — note correct XML escaping for `<`:

```xml
<record id="ir_cron_process_video_jobs" model="ir.cron">
    <field name="name">Unifix: Process pending video jobs</field>
    <field name="model_id" ref="model_unifix_video_job"/>
    <field name="state">code</field>
    <field name="code">
jobs = env['unifix.video.job'].search(
    [('state', '=', 'received')], order='create_date asc', limit=5)
for job in jobs:
    try:
        job._process_video()
        env.cr.commit()
    except Exception:
        env.cr.rollback()
        job.write({'state': 'failed',
                   'error_message': 'Worker crashed; see server log.',
                   'retry_count': job.retry_count + 1})
        env.cr.commit()
    </field>
    <field name="interval_number">1</field>
    <field name="interval_type">minutes</field>
    <field name="numbercall">-1</field>
    <field name="active">True</field>
</record>
```

> **[VERIFY]** `env.cr.commit()` inside cron is appropriate here because each job is an independent unit and we don't want one failure to roll back others. Confirm this matches your transaction conventions. If migrating to OCA `queue_job` later, drop the manual commits and use `with_delay()` — **[VERIFY] queue_job availability for 19.0 before relying on it.**

### 7.6 ffmpeg Audio + Frame Extraction

**Audio** (`_extract_audio`) — unchanged logic, kept for reference:

```python
import subprocess

def _extract_audio(self, video_path):
    audio_path = video_path.rsplit('.', 1)[0] + '.wav'
    proc = subprocess.run(
        ['ffmpeg', '-i', video_path, '-vn',
         '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
         '-y', audio_path],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg audio failed: {proc.stderr[:500]}")
    return audio_path
```

**Frames** (`_extract_frames`) — NEW. One JPEG per timestamp, returned as in-memory bytes (no extra temp files needed because we pipe to stdout):

```python
def _extract_frames(self, video_path, timestamps):
    """Return [(timestamp, jpeg_bytes, reason)]. Accurate per-timestamp seek."""
    q = self.env['ir.config_parameter'].sudo().get_param('unifix.frame_quality', '3')
    out = []
    for t, reason in timestamps:
        proc = subprocess.run(
            ['ffmpeg',
             '-ss', f'{t:.3f}',     # before -i = fast seek; see note below
             '-i', video_path,
             '-frames:v', '1',
             '-q:v', str(q),        # JPEG quality (2=high .. 31=low)
             '-f', 'image2pipe', '-vcodec', 'mjpeg',
             'pipe:1'],
            capture_output=True)
        if proc.returncode != 0 or not proc.stdout:
            _logger.warning("Frame at %.3fs failed: %s",
                            t, proc.stderr[:300])
            continue
        out.append((t, proc.stdout, reason))
    return out
```

> **Seek accuracy decision.** `-ss` *before* `-i` is fast but snaps to the nearest keyframe (can be off by a second or two). For frames that line up precisely with transcript timestamps, put `-ss` *after* `-i` (decode-accurate, slower) or add `-accurate_seek`. Given videos are short and frame count is capped, **accurate seek is recommended**; benchmark on real footage. **[VERIFY]** ffmpeg build supports `mjpeg` + `image2pipe` (standard, but confirm on Odoo.sh).

### 7.7 Gemini Call & Response Contract

```python
def _call_gemini(self, audio_path):
    from google import genai
    ICP = self.env['ir.config_parameter'].sudo()
    api_key = ICP.get_param('unifix.gemini_api_key')
    model = ICP.get_param('unifix.gemini_model', 'gemini-2.5-pro')   # [VERIFY] model id
    prompt = ICP.get_param('unifix.transcript_prompt')

    client = genai.Client(api_key=api_key)
    with open(audio_path, 'rb') as f:
        audio_bytes = f.read()

    response = client.models.generate_content(
        model=model,
        contents=[
            genai.types.Part.from_bytes(data=audio_bytes, mime_type='audio/wav'),
            prompt,
        ],
        config=genai.types.GenerateContentConfig(
            response_mime_type='application/json',
            temperature=0.2,
        ),
    )
    return json.loads(response.text)
```

**Required JSON shape from Gemini** (enforce via the prompt in `prompts/transcript_v1.txt`):

```json
{
  "transcript": "full text ...",
  "segments": [
    {"start": 0.0, "end": 4.2, "text": "...", "confidence": 0.93}
  ],
  "keyframes": [
    {"t": 12.5, "reason": "technician points at the failed valve"}
  ],
  "worker": "...", "company": "...", "location": "...",
  "reported_problem": "...", "diagnosis_cause": "...",
  "work_performed": "...", "parts_used": "...",
  "start_time": "...", "end_time": "...", "total_time_spent": "...",
  "future_recommendations": "...", "remaining_tasks": "..."
}
```

> **[VERIFY] (1)** Gemini operates on the *audio* — it can return spoken-content timestamps but cannot "see" the video to choose visually meaningful keyframes. If you need visually-curated frames, either (a) send the *video* to Gemini (conflicts with keeping compute/audio-only and increases upload cost), or (b) accept audio-derived timestamps (segment starts / moments the speaker describes an action) as the keyframe anchors. This plan assumes (b). Revisit in Open Decision D2.
> **[VERIFY] (2)** Model id and SDK: confirm the current `google-genai` SDK signature and a valid model name at implementation time; both move quickly. For audio larger than the inline request limit, switch to the Files API.

### 7.8 Cleanup Cron

Removes orphaned temp files (worker crash, container restart). Patterns must include all allowed extensions plus `.wav`.

```xml
<record id="ir_cron_cleanup_temp_videos" model="ir.cron">
    <field name="name">Unifix: Cleanup orphaned temp files</field>
    <field name="model_id" ref="model_unifix_video_job"/>
    <field name="state">code</field>
    <field name="code">
import os, glob, time, tempfile
ICP = env['ir.config_parameter'].sudo()
tmp_dir = ICP.get_param('unifix.temp_dir', tempfile.gettempdir())
age_h = int(ICP.get_param('unifix.cleanup_age_hours', '1'))
cutoff = time.time() - age_h * 3600
for pat in ['unifix_*.mp4','unifix_*.mkv','unifix_*.mov','unifix_*.avi','unifix_*.webm','unifix_*.wav']:
    for path in glob.glob(os.path.join(tmp_dir, pat)):
        try:
            if os.path.getmtime(path) &lt; cutoff:
                os.unlink(path)
        except OSError:
            pass
stale = env['unifix.video.job'].search([
    ('state', 'in', ['received', 'processing']),
    ('create_date', '&lt;', fields.Datetime.subtract(fields.Datetime.now(), hours=age_h)),
])
for job in stale:
    if not job.tmp_path or not os.path.exists(job.tmp_path):
        job.write({'state': 'failed',
                   'error_message': 'Temp file expired before processing completed.'})
    </field>
    <field name="interval_number">1</field>
    <field name="interval_type">hours</field>
    <field name="numbercall">-1</field>
    <field name="active">True</field>
</record>
```

---

## 8. Views & UI

All views use v19 syntax: `<list>` (not `<tree>`), inline `invisible=`/`readonly=` (not `attrs`), and `column_invisible=` where a whole list column must hide.

### 8.1 List view

```xml
<record id="view_video_job_list" model="ir.ui.view">
    <field name="name">unifix.video.job.list</field>
    <field name="model">unifix.video.job</field>
    <field name="arch" type="xml">
        <list>
            <field name="name"/>
            <field name="source_filename"/>
            <field name="create_date"/>
            <field name="state" widget="badge"
                   decoration-success="state == 'done'"
                   decoration-warning="state == 'processing'"
                   decoration-danger="state == 'failed'"
                   decoration-info="state == 'received'"/>
            <field name="worker_name"/>
            <field name="company"/>
            <field name="keyframe_count"/>
            <field name="total_time_spent"/>
        </list>
    </field>
</record>
```

### 8.2 Form view (with Frames tab; inline attributes)

```xml
<record id="view_video_job_form" model="ir.ui.view">
    <field name="name">unifix.video.job.form</field>
    <field name="model">unifix.video.job</field>
    <field name="arch" type="xml">
        <form>
            <header>
                <button name="action_retry" string="Retry" type="object"
                        class="btn-warning" invisible="state != 'failed'"/>
                <button name="action_cancel" string="Cancel" type="object"
                        class="btn-secondary"
                        invisible="state in ('done', 'cancelled')"/>
                <field name="state" widget="statusbar"
                       statusbar_visible="received,processing,done"/>
            </header>
            <sheet>
                <div class="oe_title"><h1><field name="name"/></h1></div>
                <group>
                    <group string="Source">
                        <field name="source_filename"/>
                        <field name="source_size_mb" string="Size (MB)"/>
                        <field name="source_hash" groups="base.group_no_one"/>
                    </group>
                    <group string="Processing">
                        <field name="create_date"/>
                        <field name="processed_at"/>
                        <field name="retry_count"/>
                        <field name="keyframe_count"/>
                    </group>
                </group>
                <notebook>
                    <page string="Transcript" name="transcript">
                        <field name="transcript" widget="text"/>
                    </page>
                    <page string="Segments" name="segments">
                        <field name="segment_ids">
                            <list>
                                <field name="sequence" widget="handle"/>
                                <field name="start_time_display"/>
                                <field name="end_time_display"/>
                                <field name="text"/>
                                <field name="confidence" widget="progressbar"/>
                            </list>
                        </field>
                    </page>
                    <page string="Frames" name="frames">
                        <field name="keyframe_ids" mode="kanban">
                            <kanban>
                                <field name="timestamp_display"/>
                                <field name="reason"/>
                                <field name="image"/>
                                <templates>
                                    <t t-name="card">
                                        <div class="o_kanban_image">
                                            <img t-att-src="kanban_image('unifix.video.keyframe', 'image', record.id.raw_value)"
                                                 alt="frame" class="o_image_64_max"/>
                                        </div>
                                        <div>
                                            <strong><field name="timestamp_display"/></strong>
                                            <div><field name="reason"/></div>
                                        </div>
                                    </t>
                                </templates>
                            </kanban>
                        </field>
                    </page>
                    <page string="Work Order" name="work_order">
                        <group>
                            <group string="Personnel">
                                <field name="worker_name"/>
                                <field name="company"/>
                                <field name="location"/>
                            </group>
                            <group string="Time">
                                <field name="start_time"/>
                                <field name="end_time"/>
                                <field name="total_time_spent"/>
                            </group>
                        </group>
                        <group string="Details">
                            <field name="reported_problem"/>
                            <field name="diagnosis_cause"/>
                            <field name="work_performed"/>
                            <field name="parts_used"/>
                        </group>
                        <group string="Follow-up">
                            <field name="future_recommendations"/>
                            <field name="remaining_tasks"/>
                        </group>
                    </page>
                    <page string="Error" name="error" invisible="state != 'failed'">
                        <field name="error_message" readonly="1"/>
                    </page>
                </notebook>
            </sheet>
        </form>
    </field>
</record>
```

> **[VERIFY]** the kanban `t-name="card"` template and `kanban_image(...)` helper against the exact v19 kanban API; v19 standardized kanban card templates (`<t t-name="card">`). Adjust if your build differs. `widget="html"` on `transcript` is also fine if you prefer rich text; `text` keeps it plain.

### 8.3 Search view & menu

```xml
<record id="view_video_job_search" model="ir.ui.view">
    <field name="name">unifix.video.job.search</field>
    <field name="model">unifix.video.job</field>
    <field name="arch" type="xml">
        <search>
            <field name="name"/>
            <field name="source_filename"/>
            <field name="worker_name"/>
            <field name="company"/>
            <field name="transcript"/>
            <filter name="f_received" string="Received" domain="[('state','=','received')]"/>
            <filter name="f_processing" string="Processing" domain="[('state','=','processing')]"/>
            <filter name="f_done" string="Done" domain="[('state','=','done')]"/>
            <filter name="f_failed" string="Failed" domain="[('state','=','failed')]"/>
            <group expand="0" string="Group By">
                <filter name="g_state" string="State" context="{'group_by':'state'}"/>
                <filter name="g_company" string="Company" context="{'group_by':'company'}"/>
                <filter name="g_date" string="Created" context="{'group_by':'create_date:month'}"/>
            </group>
        </search>
    </field>
</record>
```

> **[VERIFY]** the window action's `view_mode` should reference the list view (`list,form`), not `tree,form`, in v19.

---

## 9. Configuration & Secrets

`res.config.settings` uses the v19 `<app>/<block>/<setting>` layout (already correct in the draft). Add keyframe settings:

```xml
<block title="Keyframes" name="keyframe_config">
    <setting string="Max Frames" help="Max keyframes extracted per video">
        <field name="unifix_max_keyframes"/>
    </setting>
    <setting string="Frame Quality" help="JPEG quality (2=best, 31=worst)">
        <field name="unifix_frame_quality"/>
    </setting>
</block>
```

Settings fields on `res.config.settings` map to `ir.config_parameter` via `config_parameter=` on each field. See Appendix B for the full key list.

---

## 10. Security Considerations

| Surface | Concern | Mitigation |
|---------|---------|------------|
| Upload | Unauthenticated upload | `auth='user'` |
| Upload | CSRF | `csrf=True` + token from the Odoo client **[VERIFY]** |
| Upload | Path traversal | `temp_dir` + UUID name; never use client filename for the path |
| Upload | Malicious content | Extension allow-list + size bounds (+ magic-byte sanity check) |
| Disk | Exhaustion | Size cap; cleanup cron; single-concurrency cron initially |
| Temp files | Leftovers / perms | `chmod 0600`; hourly cleanup cron |
| Keyframes | Oversized image storage | `Image(max_width, max_height)` downscales; cap frame count |
| Access | RBAC | `base.group_user` read/create jobs; `unifix_manager` full + settings; keyframes read-only for users |

No external callback endpoint exists in the primary design, so its attack surface (HMAC replay, signature spoofing) is **removed**. If the external-worker variant is adopted later, see Appendix C for the corrected, v19-safe webhook.

---

## 11. Error Handling & Retry

| Failure | State | Recovery |
|---------|-------|----------|
| Temp file missing | failed | User re-uploads (cannot retry without bytes) |
| ffmpeg audio fails | failed | Retry/re-upload |
| ffmpeg frame fails (single) | continue | Skip that frame, log; job still completes |
| Gemini API/transport error | failed | Manual retry while temp file still present; auto-retry only for transient codes |
| Invalid Gemini JSON | failed | Manual retry |

```python
def action_retry(self):
    self.ensure_one()
    if self.state != 'failed':
        raise UserError("Only failed jobs can be retried.")
    if not self.tmp_path or not os.path.exists(self.tmp_path):
        raise UserError("The original video has been cleaned up. Please upload again.")
    self.write({'state': 'received', 'error_message': False})
```

> Auto-retry with exponential backoff requires a scheduler. With cron-only (Phase 1), implement backoff by leaving the job in `received` and gating on `retry_count` + a `next_attempt` datetime checked in the cron domain. Do **not** call `with_delay()` unless `queue_job` is installed. **[VERIFY]**

---

## 12. Testing Strategy

### 12.1 Corrected guard test — allows images, forbids video

```python
from odoo.tests.common import TransactionCase
from odoo import fields

class TestNoVideoStorage(TransactionCase):
    """Video must never persist. Derived images (keyframes) are allowed."""

    def test_no_binary_field_on_job_model(self):
        job = self.env['unifix.video.job']
        for name, field in job._fields.items():
            self.assertNotIsInstance(
                field, fields.Binary,
                f"Binary field '{name}' on job model — video must not be storable here.")

    def test_no_video_attachment_created(self):
        """No attachment with a video/* mimetype may exist for a job."""
        job = self.env['unifix.video.job'].create({'name': 'T', 'state': 'received'})
        vids = self.env['ir.attachment'].search([
            ('res_model', '=', 'unifix.video.job'),
            ('res_id', '=', job.id),
            ('mimetype', '=ilike', 'video/%'),
        ])
        self.assertFalse(vids, "A video/* attachment exists — violates Constraint #1.")

    def test_keyframe_image_attachment_is_allowed(self):
        """An image keyframe is permitted derived data, not a violation."""
        import base64
        # 1x1 white JPEG (smallest valid-ish payload for the test). [VERIFY] use a real fixture.
        job = self.env['unifix.video.job'].create({'name': 'T', 'state': 'done'})
        kf = self.env['unifix.video.keyframe'].create({
            'job_id': job.id, 'timestamp': 1.0,
            'image': base64.b64encode(b'\xff\xd8\xff\xd9'),
        })
        self.assertTrue(kf.image, "Keyframe image should be stored.")
```

### 12.2 Ordering test — video survives until frames are extracted

```python
from unittest.mock import patch, MagicMock

class TestKeyframeOrdering(TransactionCase):
    def test_video_deleted_after_frames(self):
        """Assert _extract_frames is called while the video still exists,
        and the unlink happens after."""
        # [VERIFY] wire mocks to your actual method names; this is a skeleton.
        ...
```

### 12.3 Worker & upload tests
Keep the draft's `test_worker.py` (ffmpeg arg assertions) and `test_upload.py` (HttpCase: returns `job_id`, rejects bad extension). Update the embedded-view assertions if any reference `<tree>`.

> **[VERIFY]** Odoo 19 test base classes (`TransactionCase`, `HttpCase`) and `url_open` signature are stable, but confirm fixtures and that `HttpCase` upload tests pass the CSRF token.

---

## 13. Phase 1 vs Phase 2 Scope

### Phase 1 (this plan) — Transcript + Keyframes + Ephemeral Video
Upload controller; job/segment/**keyframe** models; cron worker with **reordered deletion**; ffmpeg audio **and frame** extraction; Gemini transcript+keyframes+fields; cleanup cron; list/form (**Frames tab**)/search views; settings; security groups; corrected guard test + ordering test.

### Phase 2 (future)
OWL transcript viewer with inline frame thumbnails synced to playback; smarter "important moment" detection (visual model on frames, not just audio); external/clustered worker (Appendix C); object-bucket frame storage option; multi-concurrency via `queue_job`.

---

## 14. Open Decisions

| # | Decision | Options | Recommendation |
|---|----------|---------|----------------|
| D1 | Worker mechanism | Cron vs OCA `queue_job` | Cron for Phase 1; `queue_job` later **[VERIFY 19.0 availability]** |
| D2 | Keyframe anchoring | Audio-derived timestamps vs send video to Gemini for visual selection | Audio-derived (segment starts / described actions) for Phase 1; visual selection is Phase 2 |
| D3 | Seek accuracy | Fast (`-ss` before `-i`) vs accurate (after `-i`) | Accurate — videos short, frame count capped |
| D4 | Max keyframes | 6 / 12 / per-segment | 12, configurable |
| D5 | Frame format/size | JPEG 1280×720 vs full-res PNG | JPEG, downscaled via `Image(max_width,max_height)` |
| D6 | Temp dir | `/tmp` vs custom | `/tmp` default, configurable |
| D7 | Frame storage | `fields.Image` (attachment) vs `Binary(attachment=False)` vs bucket URL | `fields.Image` |
| D8 | Max video size | 512MB / 1GB / 2GB | 1GB, configurable |

---

## 15. Assumptions & [VERIFY] Checklist

| # | Assumption | Verify by |
|---|------------|-----------|
| A1 | Existing `workorder_processing` / `video_extractor.py` logic is refactored into the worker | Read that package; decide import vs rewrite |
| A2 | Gemini returns audio-timestamped data reliably | Test the new prompt on real audio |
| A3 | ffmpeg present on the Odoo server with mjpeg/image2pipe | `ffmpeg -version` on target; check Odoo.sh |
| A4 | Single-server deployment (`/tmp` local) | Confirm topology; cluster ⇒ Appendix A-style bucket |
| A5 | Videos ≤ 1 GB | Confirm upload limits / disk headroom |
| A6 | Target is Odoo **19.0** | Confirm `odoo-bin --version` / branch |
| A7 | Deployment target is Odoo.sh | Confirm; affects ffmpeg + temp dir + worker model |
| A8 | `google-genai` SDK + model id current | Check SDK docs at build time |
| A9 | Upload UI is the Odoo web client (CSRF token available) | Confirm; external client ⇒ bearer auth |

**Concrete [VERIFY] tags to resolve before coding:** upload UI/CSRF (7.1), worker refactor source (6), commit-in-cron convention (7.5), `queue_job` availability (7.5/14), ffmpeg seek + mjpeg pipe (7.6), Gemini model/SDK/visual-keyframe question (7.7), kanban card template API (8.2), action `view_mode=list,form` (8.3), image fixture in tests (12.1).

---

## Appendix A: File Map

```
unifix_video/
├── __init__.py                          # from . import controllers, models
├── __manifest__.py                      # depends: ['base']; data: views/security/data
├── controllers/{__init__.py, upload.py}
├── models/{__init__.py, video_job.py, video_segment.py, video_keyframe.py, res_config_settings.py}
├── data/{cron.xml, default_params.xml}
├── security/{ir.model.access.csv, security.xml}
├── views/{video_job_views.xml, video_job_menu.xml, res_config_settings_views.xml}
├── prompts/transcript_v1.txt
├── static/description/icon.png
└── tests/{__init__.py, test_upload.py, test_worker.py, test_keyframes.py, test_no_video_storage.py}
```

---

## Appendix B: Configuration Parameters

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `unifix.gemini_api_key` | password | *(required)* | Gemini API key |
| `unifix.gemini_model` | char | `gemini-2.5-pro` **[VERIFY]** | Model id |
| `unifix.transcript_prompt` | text | *(built-in)* | Transcript + keyframes + fields prompt |
| `unifix.max_video_size_mb` | int | `1024` | Max upload size (MB) |
| `unifix.temp_dir` | char | `/tmp` | Temp directory |
| `unifix.cleanup_age_hours` | int | `1` | Orphan temp file age |
| `unifix.allowed_extensions` | char | `.mp4,.mkv,.mov,.avi,.webm` | Allowed extensions |
| `unifix.max_keyframes` | int | `12` | Max frames per video |
| `unifix.frame_quality` | int | `3` | ffmpeg `-q:v` (2=best..31=worst) |

---

## Appendix C: External-Worker Variant (callback + HMAC)

Only if compute moves out of Odoo (separate service, clustered deploy). The original draft's callback was broken for v19; this is the corrected form.

- Route is **`type='http'`** (not `json`/`jsonrpc`) so you can set real HTTP status codes; `jsonrpc` routes always return HTTP 200 wrapped in a `result`/`error` envelope.
- Read the body with `request.httprequest.get_data()`; verify HMAC over the raw bytes; return a hand-built `Response`.
- **Frames must travel in the payload** (base64 JPEGs) or via a separate authenticated multipart upload, since an external worker cannot `create()` records directly.

```python
import hmac, hashlib, json
from odoo import http
from odoo.http import request, Response

class UnifixCallback(http.Controller):
    @http.route('/unifix/callback', type='http', auth='public',
                methods=['POST'], csrf=False)
    def receive(self, **kw):
        body = request.httprequest.get_data()  # raw bytes
        sig = request.httprequest.headers.get('X-Unifix-Signature', '')
        secret = request.env['ir.config_parameter'].sudo().get_param('unifix.callback_secret')
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not (secret and hmac.compare_digest(sig, expected)):
            return Response(json.dumps({'error': 'invalid signature'}),
                            status=401, content_type='application/json')
        data = json.loads(body or b'{}')
        # ... resolve job, verify state=='processing', write results,
        #     create segments, decode+create keyframes from base64 ...
        return Response(json.dumps({'status': 'ok'}),
                        status=200, content_type='application/json')
```

> Add a signed timestamp to the payload and reject stale requests to prevent replay.

---

**End of document**
