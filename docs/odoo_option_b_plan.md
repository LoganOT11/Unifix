# Option B: Thin Controller + Local Processing — Implementation Plan

**Document type:** Architecture & implementation plan
**Date:** 2026-06-09
**Status:** Draft — pending team approval
**Supersedes:** N/A (new document)
**Related:** `odoo_plan.md` (investigation brief), `odoo-video-pipeline-investigation.md` (gap analysis)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Hard Constraints (carried forward)](#2-hard-constraints-carried-forward)
3. [Why Option B](#3-why-option-b)
4. [Architecture Overview](#4-architecture-overview)
5. [Byte Lifecycle — Where the Video Lives at Each Step](#5-byte-lifecycle--where-the-video-lives-at-each-step)
6. [Odoo Module Structure](#6-odoo-module-structure)
7. [Component Specifications](#7-component-specifications)
   - 7.1 [Upload Controller](#71-upload-controller)
   - 7.2 [Job Model](#72-job-model)
   - 7.3 [Segment Model](#73-segment-model)
   - 7.4 [Async Worker / Cron](#74-async-worker--cron)
   - 7.5 [Callback Endpoint](#75-callback-endpoint)
   - 7.6 [Cleanup Cron](#76-cleanup-cron)
8. [Views & User Interface](#8-views--user-interface)
9. [Configuration & Secrets](#9-configuration--secrets)
10. [Security Considerations](#10-security-considerations)
11. [Error Handling & Retry](#11-error-handling--retry)
12. [Testing Strategy](#12-testing-strategy)
13. [Phase 1 vs Phase 2 Scope](#13-phase-1-vs-phase-2-scope)
14. [Open Decisions](#14-open-decisions)
15. [Assumptions](#15-assumptions)
16. [Appendix A: File Map](#appendix-a-file-map)
17. [Appendix B: Configuration Parameters](#appendix-b-configuration-parameters)

---

## 1. Executive Summary

This document specifies the implementation plan for **Option B** of the Unifix ephemeral video processing pipeline. Option B uses a **thin Odoo HTTP controller** that receives video uploads, streams them to a temporary file on the local filesystem, hands off to an asynchronous worker, and deletes the temporary file immediately after processing.

**Key properties:**

- Video bytes **never** enter Odoo's database (`ir.attachment`, binary fields, or filestore)
- Video bytes exist on disk only for the duration of processing (typically 2–5 minutes)
- Heavy compute (ffmpeg, Gemini API) runs outside the Odoo HTTP request cycle
- Only derived data (transcript, metadata, extracted fields) is persisted in Odoo
- Minimal infrastructure requirements (no external object storage needed)

**Why Option B over Option A (direct-to-bucket):**

| Factor | Option A (GCS/S3) | Option B (Thin Controller) |
|--------|-------------------|---------------------------|
| Infrastructure | Requires bucket, IAM, presigned URLs | Just `/tmp` directory |
| Dependencies | Google Cloud SDK / boto3 | None |
| Latency | Upload → bucket → download → process | Upload → process |
| Cost | Storage + egress charges | No extra cost |
| Odoo.sh compatibility | May require custom setup | `/tmp` always available |
| Complexity | Higher | Lower |

Option B is recommended for single-server deployments where videos are under 2 GB and temporary storage on the Odoo server is acceptable.

---

## 2. Hard Constraints (carried forward)

These constraints are inherited from `odoo_plan.md` and are non-negotiable:

| # | Constraint | Implication for Option B |
|---|------------|--------------------------|
| 1 | **No video in Odoo storage.** The video must never become a persisted `ir.attachment` or stored binary field. | Controller streams to `/tmp`; no binary fields on any model; temp file deleted after processing. |
| 2 | **Heavy compute lives outside Odoo.** Transcription and frame extraction must not run inside Odoo HTTP request workers. | Processing runs via async cron or `queue_job`; HTTP controller returns immediately. |
| 3 | **Async, not synchronous.** Long-running processing must not block an Odoo HTTP request. | Controller enqueues job and returns; worker processes later. |
| 4 | **Odoo is system-of-record for derived data only.** Transcript, segments, metadata live in Odoo; video does not. | Job model stores only text fields; no binary data. |
| 5 | **Don't block v19 conformance later.** Follow v19 conventions from the start. | Module uses `unifix.*` naming, `o_unifix` CSS prefix, ORM-only, standard directory layout. |

---

## 3. Why Option B

### Option A (Direct-to-Bucket) — Not Chosen

Option A requires:
- A Google Cloud Storage or S3 bucket configured with CORS
- Presigned URL generation (adds a dependency and a round-trip)
- Bucket lifecycle policies to auto-delete expired objects
- The Odoo server to have IAM credentials for the bucket
- The worker to download from the bucket before processing

This is the right choice for **multi-server / clustered deployments** where `/tmp` is not shared, or when you need to retain the video for debugging. For a single Odoo instance, it adds unnecessary complexity.

### Option B (Thin Controller) — Chosen

Option B requires:
- A temporary directory on the Odoo server (`/tmp` or configurable)
- A cron job or `queue_job` for async processing
- A cleanup cron to remove orphaned temp files

This is simpler, has fewer dependencies, and keeps the video bytes entirely under local control. The tradeoff is that temp files live on the Odoo server's disk briefly, but they are reliably cleaned up by the worker and by the cleanup cron.

---

## 4. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Browser (User)                                 │
│                                                                             │
│   1. Opens "New Video Work Order" form                                     │
│   2. Selects video file                                                    │
│   3. Clicks "Upload"                                                       │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │ HTTP POST (multipart/form-data)
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Odoo HTTP Controller                                 │
│                        /unifix/upload                                       │
│                                                                             │
│   4. Streams video to /tmp/unifix_<uuid>.mp4                               │
│   5. Creates job record (state='received')                                 │
│   6. Enqueues async worker                                                 │
│   7. Returns JSON {job_id, state: 'received'}                              │
│                                                                             │
│   ⚠️  Controller returns IMMEDIATELY — does NOT wait for processing        │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Odoo Database                                     │
│                                                                             │
│   unifix.video.job                                                         │
│   ┌─────────────────────────────────────────────────────────────────────┐  │
│   │ id: 42                                                              │  │
│   │ name: "Video Job a3f8b2c1"                                          │  │
│   │ state: "received"        ◄── user sees "Processing..."              │  │
│   │ tmp_path: "/tmp/unifix_a3f8b2c1.mp4"                                │  │
│   │ source_size: 52428800                                               │  │
│   │ transcript: (empty)      ◄── populated after processing             │  │
│   │ segment_ids: []                                                     │  │
│   │ worker_name: (empty)                                                 │  │
│   │ ...                                                                 │  │
│   └─────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                               │ Cron picks up job (every 1 minute)
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Async Worker (Cron)                                  │
│                                                                             │
│   8.  Reads /tmp/unifix_a3f8b2c1.mp4                                       │
│   9.  Computes SHA-256 (streaming, not buffering whole file)               │
│   10. Runs ffmpeg: extracts audio → /tmp/unifix_a3f8b2c1.wav              │
│   11. Deletes /tmp/unifix_a3f8b2c1.mp4  ◄── VIDEO GONE                    │
│   12. Sends .wav audio to Gemini API                                       │
│   13. Gemini returns: transcript + segments + extracted fields             │
│   14. Deletes /tmp/unifix_a3f8b2c1.wav  ◄── AUDIO GONE                    │
│   15. Calls Odoo callback endpoint with results                            │
│                                                                             │
│   ⚠️  By step 14, NO media bytes exist anywhere                            │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │ HTTP POST (JSON)
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Callback Endpoint                                      │
│                       /unifix/callback                                       │
│                                                                             │
│   16. Validates HMAC signature                                             │
│   17. Updates job record with transcript + metadata                        │
│   18. Creates segment records                                              │
│   19. Sets state = 'done'                                                  │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Browser (User)                                 │
│                                                                             │
│   20. Sees state change to "Done"                                          │
│   21. Transcript, segments, and extracted fields are now visible           │
│                                                                             │
│   In Odoo's database: ONLY TEXT DATA                                       │
│   In Odoo's filestore: NOTHING NEW                                         │
│   On disk: temp files DELETED                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Byte Lifecycle — Where the Video Lives at Each Step

| Step | Location | Format | Duration | Cleanup |
|------|----------|--------|----------|---------|
| 1. User selects file | Browser memory | Original format | Until upload completes | Browser handles |
| 2. Upload in progress | HTTP stream | Original format | Seconds to minutes | Stream ends |
| 3. Controller writes | `/tmp/unifix_<uuid>.mp4` | Original format | Until worker starts | Worker deletes at step 11 |
| 4. ffmpeg reads video | Read buffer (RAM) | Original format | Seconds | GC'd |
| 5. ffmpeg writes audio | `/tmp/unifix_<uuid>.wav` | PCM 16kHz mono | Until Gemini responds | Worker deletes at step 14 |
| 6. Gemini processes | Google's cloud servers | Audio bytes | Seconds | Google handles |
| 7. Results written | Odoo PostgreSQL | Text only | Permanent | N/A |

**Total time video exists on disk:** 2–5 minutes (upload + processing)
**Maximum disk usage at any moment:** One video file + one audio file (~1.1× video size)
**Video in Odoo filestore:** Never
**Video in Odoo database:** Never

---

## 6. Odoo Module Structure

```
unifix_video/
├── __init__.py
├── __manifest__.py
├── controllers/
│   ├── __init__.py
│   ├── upload.py              # POST /unifix/upload — receives video
│   └── callback.py            # POST /unifix/callback — receives results
├── models/
│   ├── __init__.py
│   ├── video_job.py           # unifix.video.job — main job model
│   ├── video_segment.py       # unifix.video.segment — transcript segments
│   └── res_config_settings.py # Settings UI for API keys, model, prompts
├── data/
│   ├── cron.xml               # Processing cron + cleanup cron
│   └── default_prompts.xml    # Default transcript prompt (ir.config_parameter)
├── security/
│   ├── ir.model.access.csv    # Access rights for job + segment models
│   └── security.xml           # Groups (unifix_user, unifix_manager)
├── views/
│   ├── video_job_views.xml    # Tree, form, search, kanban views
│   ├── video_job_menu.xml     # Menu items
│   └── res_config_settings_views.xml  # Settings page
├── prompts/
│   └── transcript_v1.txt      # Gemini prompt for transcript extraction
├── static/
│   └── description/
│       └── icon.png           # Module icon
└── tests/
    ├── __init__.py
    ├── test_upload.py         # Upload controller tests
    ├── test_worker.py         # Worker processing tests
    └── test_no_video_storage.py  # Constraint #1 guard tests
```

---

## 7. Component Specifications

### 7.1 Upload Controller

**File:** `controllers/upload.py`

**Purpose:** Receive video uploads, stream to temp file, create job record, enqueue worker.

**Route:** `POST /unifix/upload`

**Authentication:** `auth='user'` — requires logged-in Odoo user

**Input:** `multipart/form-data` with field `video_file`

**Output:** JSON response with `job_id` and `state`

**Key behaviors:**

1. **Stream to disk, don't buffer in memory.** Read in 8KB chunks.
2. **Generate a unique temp filename** using UUID to prevent collisions.
3. **Validate file before writing** — check extension and MIME type (via first bytes).
4. **Create job record** with `state='received'` and `tmp_path`.
5. **Enqueue worker** and return immediately.
6. **Never create `ir.attachment`** — the video bytes must not touch Odoo's filestore.

**Pseudocode:**

```python
@http.route('/unifix/upload', type='http', auth='user', methods=['POST'], csrf=True)
def upload_video(self, video_file=None, **kwargs):
    # 1. Validate input
    if not video_file:
        return error_response(400, 'No file provided')
    
    # 2. Check extension
    ext = os.path.splitext(video_file.filename)[1].lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:  # {'.mp4', '.mkv', '.mov', '.avi', '.webm'}
        return error_response(400, f'Unsupported video format: {ext}')
    
    # 3. Stream to temp file
    tmp_path = os.path.join(tempfile.gettempdir(), f'unifix_{uuid4().hex}{ext}')
    with open(tmp_path, 'wb') as f:
        while chunk := video_file.read(8192):
            f.write(chunk)
    
    # 4. Validate file size
    file_size = os.path.getsize(tmp_path)
    if file_size < 10240:  # 10 KB minimum
        os.unlink(tmp_path)
        return error_response(400, 'File too small')
    if file_size > 2 * 1024 * 1024 * 1024:  # 2 GB maximum
        os.unlink(tmp_path)
        return error_response(400, 'File too large (max 2 GB)')
    
    # 5. Create job record
    job = request.env['unifix.video.job'].create({
        'name': f'Video Job {uuid4().hex[:8]}',
        'state': 'received',
        'tmp_path': tmp_path,
        'source_size': file_size,
        'source_filename': video_file.filename,
    })
    
    # 6. Enqueue async processing
    job.action_enqueue()
    
    # 7. Return immediately
    return request.make_json_response({
        'job_id': job.id,
        'state': 'received',
        'message': 'Video uploaded. Processing will begin shortly.',
    })
```

**Error responses:**

| Status | Condition | Response |
|--------|-----------|----------|
| 400 | No file provided | `{"error": "No file provided"}` |
| 400 | Unsupported format | `{"error": "Unsupported video format: .exe"}` |
| 400 | File too small | `{"error": "File too small"}` |
| 400 | File too large | `{"error": "File too large (max 2 GB)"}` |
| 401 | Not authenticated | Odoo default 401 |
| 500 | Server error | `{"error": "Internal server error"}` |

---

### 7.2 Job Model

**File:** `models/video_job.py`

**Model name:** `unifix.video.job`

**Purpose:** Track video processing jobs from upload to completion.

**Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | `Char` (required) | Job display name (auto-generated) |
| `state` | `Selection` | `draft` → `received` → `processing` → `done` \| `failed` |
| `tmp_path` | `Char` | Path to temp video file (NOT stored in filestore, just the path string) |
| `source_filename` | `Char` | Original filename from upload |
| `source_size` | `Integer` | Video file size in bytes |
| `source_hash` | `Char` | SHA-256 of video file (computed by worker) |
| `transcript` | `Text` | Full transcript text |
| `segment_ids` | `One2many` → `unifix.video.segment` | Timestamped segments |
| `worker_name` | `Char` | Extracted worker name |
| `company` | `Char` | Extracted company name |
| `location` | `Char` | Extracted location |
| `reported_problem` | `Text` | Extracted problem description |
| `diagnosis_cause` | `Text` | Extracted diagnosis |
| `work_performed` | `Text` | Extracted work performed |
| `parts_used` | `Text` | Extracted parts used |
| `start_time` | `Char` | Extracted start time |
| `end_time` | `Char` | Extracted end time |
| `total_time_spent` | `Char` | Extracted total time |
| `future_recommendations` | `Text` | Extracted recommendations |
| `remaining_tasks` | `Text` | Extracted remaining tasks |
| `error_message` | `Text` | Error details if state='failed' |
| `retry_count` | `Integer` | Number of processing attempts |
| `processed_at` | `Datetime` | When processing completed |

**Methods:**

| Method | Purpose |
|--------|---------|
| `action_enqueue()` | Set state to 'received' and trigger async worker |
| `_process_video()` | Main processing logic (runs async) |
| `_extract_audio(video_path)` | ffmpeg audio extraction |
| `_call_gemini(audio_path)` | Gemini API call |
| `_cleanup_temp_files()` | Delete temp video/audio files |
| `action_retry()` | Reset state and re-enqueue failed jobs |
| `action_cancel()` | Cancel job and clean up temp files |

**State machine:**

```
                    ┌──────────┐
                    │  draft   │
                    └────┬─────┘
                         │ action_enqueue()
                         ▼
                    ┌──────────┐
              ┌────▶│ received │
              │     └────┬─────┘
              │          │ worker starts
              │          ▼
              │     ┌──────────┐
              │     │processing│
              │     └────┬─────┘
              │          │
              │     ┌────┴────┐
              │     ▼         ▼
              │ ┌──────┐  ┌──────┐
              │ │ done │  │failed│
              │ └──────┘  └──┬───┘
              │              │
              └──────────────┘  action_retry()
```

**Security:**

```csv
id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
access_video_job_user,unifix.video.job.user,model_unifix_video_job,base.group_user,1,1,1,0
access_video_job_manager,unifix.video.job.manager,model_unifix_video_job,unifix_video.group_unifix_manager,1,1,1,1
```

---

### 7.3 Segment Model

**File:** `models/video_segment.py`

**Model name:** `unifix.video.segment`

**Purpose:** Store timestamped transcript segments.

**Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `job_id` | `Many2one` → `unifix.video.job` (required) | Parent job |
| `sequence` | `Integer` | Display order |
| `start_time` | `Float` | Segment start time in seconds |
| `end_time` | `Float` | Segment end time in seconds |
| `start_time_display` | `Char` | Formatted start time (e.g., "01:23") |
| `end_time_display` | `Char` | Formatted end time (e.g., "01:45") |
| `text` | `Text` (required) | Segment transcript text |
| `confidence` | `Float` | ASR confidence score (0.0–1.0) |

**Security:**

```csv
id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
access_video_segment_user,unifix.video.segment.user,model_unifix_video_segment,base.group_user,1,0,0,0
access_video_segment_manager,unifix.video.segment.manager,model_unifix_video_segment,unifix_video.group_unifix_manager,1,1,1,1
```

---

### 7.4 Async Worker / Cron

**File:** `models/video_job.py` (method `_process_video`)

**Purpose:** Process video jobs outside the HTTP request cycle.

**Two implementation options:**

#### Option B.1: Odoo Cron (simpler)

```xml
<!-- data/cron.xml -->
<record id="ir_cron_process_video_jobs" model="ir.cron">
    <field name="name">Unifix: Process pending video jobs</field>
    <field name="model_id" ref="model_unifix_video_job"/>
    <field name="state">code</field>
    <field name="code">
        jobs = env['unifix.video.job'].search([
            ('state', '=', 'received')
        ], order='create_date asc', limit=5)
        for job in jobs:
            try:
                job._process_video()
            except Exception as e:
                job.write({
                    'state': 'failed',
                    'error_message': str(e),
                    'retry_count': job.retry_count + 1,
                })
    </field>
    <field name="interval_number">1</field>
    <field name="interval_type">minutes</field>
    <field name="numbercall">-1</field>
    <field name="active">True</field>
</record>
```

#### Option B.2: OCA queue_job (better for concurrency)

```python
from odoo.addons.queue_job.job import job

class UnifixVideoJob(models.Model):
    _name = 'unifix.video.job'
    _inherit = ['queue.job']
    
    @job
    def _process_video(self):
        """Process video asynchronously via queue_job."""
        # ... same logic as below
```

**Recommended:** Start with the cron (Option B.1) for simplicity. Migrate to `queue_job` later if you need concurrent processing or better retry semantics.

**Processing logic (`_process_video`):**

```python
def _process_video(self):
    self.ensure_one()
    
    try:
        self.write({'state': 'processing'})
        
        tmp_path = self.tmp_path
        if not tmp_path or not os.path.exists(tmp_path):
            raise FileNotFoundError(f"Temp video file not found: {tmp_path}")
        
        # Step 1: Compute SHA-256 (streaming)
        sha256 = hashlib.sha256()
        with open(tmp_path, 'rb') as f:
            while chunk := f.read(8192):
                sha256.update(chunk)
        self.source_hash = sha256.hexdigest()
        
        # Step 2: Extract audio
        audio_path = self._extract_audio(tmp_path)
        
        # Step 3: Delete video (we're done with it)
        os.unlink(tmp_path)
        self.tmp_path = False
        
        # Step 4: Call Gemini
        result = self._call_gemini(audio_path)
        
        # Step 5: Delete audio
        os.unlink(audio_path)
        
        # Step 6: Write results
        self._write_results(result)
        
        # Step 7: Mark done
        self.write({
            'state': 'done',
            'processed_at': fields.Datetime.now(),
        })
        
    except Exception as e:
        self.write({
            'state': 'failed',
            'error_message': str(e),
            'retry_count': self.retry_count + 1,
        })
        self._cleanup_temp_files()
        raise
```

**Audio extraction (`_extract_audio`):**

```python
def _extract_audio(self, video_path):
    """Extract audio from video using ffmpeg."""
    import subprocess
    
    audio_path = video_path.rsplit('.', 1)[0] + '.wav'
    
    result = subprocess.run([
        'ffmpeg', '-i', video_path,
        '-vn',                    # No video
        '-acodec', 'pcm_s16le',   # PCM 16-bit
        '-ar', '16000',           # 16kHz (good for ASR)
        '-ac', '1',               # Mono
        '-y',                     # Overwrite output
        audio_path
    ], capture_output=True, text=True)
    
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[:500]}")
    
    return audio_path
```

**Gemini call (`_call_gemini`):**

```python
def _call_gemini(self, audio_path):
    """Send audio to Gemini API and get transcript + structured data."""
    from google import genai
    
    api_key = self.env['ir.config_parameter'].get_param('unifix.gemini_api_key')
    model = self.env['ir.config_parameter'].get_param('unifix.gemini_model', 'gemini-2.5-pro')
    
    client = genai.Client(api_key=api_key)
    
    with open(audio_path, 'rb') as f:
        audio_bytes = f.read()
    
    prompt = self.env['ir.config_parameter'].get_param('unifix.transcript_prompt')
    
    response = client.models.generate_content(
        model=model,
        contents=[
            genai.types.Part.from_bytes(audio_bytes, mime_type='audio/wav'),
            prompt
        ],
        config=genai.types.GenerateContentConfig(
            response_mime_type='application/json',
            temperature=0.2,
        )
    )
    
    return json.loads(response.text)
```

---

### 7.5 Callback Endpoint

**File:** `controllers/callback.py`

**Purpose:** Receive results from the worker and update the job record.

**Route:** `POST /unifix/callback`

**Authentication:** HMAC signature verification (shared secret)

**Input:** JSON with `job_id`, `transcript`, `segments`, `extracted_fields`, `source_hash`

**Key behaviors:**

1. **Verify HMAC signature** to ensure the request came from the worker.
2. **Update job record** with transcript and extracted fields.
3. **Create segment records** from the segments array.
4. **Set state to 'done'**.

**Pseudocode:**

```python
@http.route('/unifix/callback', type='json', auth='none', methods=['POST'], csrf=False)
def receive_results(self, **kwargs):
    # 1. Verify HMAC signature
    signature = request.httprequest.headers.get('X-Unifix-Signature')
    if not self._verify_hmac(signature, request.httprequest.data):
        return {'error': 'Invalid signature'}, 401
    
    # 2. Parse payload
    data = request.jsonrequest
    job_id = data.get('job_id')
    if not job_id:
        return {'error': 'Missing job_id'}, 400
    
    # 3. Find job
    job = request.env['unifix.video.job'].sudo().browse(job_id)
    if not job.exists():
        return {'error': 'Job not found'}, 404
    
    # 4. Update job
    job.write({
        'state': 'done',
        'transcript': data.get('transcript'),
        'source_hash': data.get('source_hash'),
        'worker_name': data.get('worker'),
        'company': data.get('company'),
        'location': data.get('location'),
        'reported_problem': data.get('reported_problem'),
        'diagnosis_cause': data.get('diagnosis_cause'),
        'work_performed': data.get('work_performed'),
        'parts_used': data.get('parts_used'),
        'start_time': data.get('start_time'),
        'end_time': data.get('end_time'),
        'total_time_spent': data.get('total_time_spent'),
        'future_recommendations': data.get('future_recommendations'),
        'remaining_tasks': data.get('remaining_tasks'),
        'processed_at': fields.Datetime.now(),
    })
    
    # 5. Create segments
    for seg in data.get('segments', []):
        request.env['unifix.video.segment'].sudo().create({
            'job_id': job.id,
            'start_time': seg.get('start'),
            'end_time': seg.get('end'),
            'text': seg.get('text'),
            'confidence': seg.get('confidence'),
        })
    
    return {'status': 'ok'}
```

**HMAC verification:**

```python
def _verify_hmac(self, signature, body):
    """Verify HMAC-SHA256 signature."""
    import hmac
    import hashlib
    
    secret = request.env['ir.config_parameter'].get_param('unifix.callback_secret')
    if not secret:
        return False
    
    expected = hmac.new(
        secret.encode(),
        body,
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(signature, expected)
```

---

### 7.6 Cleanup Cron

**File:** `data/cron.xml`

**Purpose:** Remove orphaned temp files that were not cleaned up by the worker (e.g., due to crashes).

```xml
<record id="ir_cron_cleanup_temp_videos" model="ir.cron">
    <field name="name">Unifix: Cleanup orphaned temp files</field>
    <field name="model_id" ref="model_unifix_video_job"/>
    <field name="state">code</field>
    <field name="code">
        import os
        import glob
        import time
        
        tmp_dir = tempfile.gettempdir()
        cutoff = time.time() - 3600  # 1 hour ago
        
        # Clean up old temp video files
        for pattern in ['unifix_*.mp4', 'unifix_*.mkv', 'unifix_*.mov', 
                        'unifix_*.avi', 'unifix_*.webm', 'unifix_*.wav']:
            for path in glob.glob(os.path.join(tmp_dir, pattern)):
                if os.path.getmtime(path) < cutoff:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        
        # Also clean up jobs with missing temp files
        stale_jobs = env['unifix.video.job'].search([
            ('state', '=', 'received'),
            ('create_date', '&lt;', fields.Datetime.subtract(fields.Datetime.now(), hours=1))
        ])
        for job in stale_jobs:
            if not job.tmp_path or not os.path.exists(job.tmp_path):
                job.write({
                    'state': 'failed',
                    'error_message': 'Temp file expired before processing could begin.'
                })
    </field>
    <field name="interval_number">1</field>
    <field name="interval_type">hours</field>
    <field name="numbercall">-1</field>
    <field name="active">True</field>
</record>
```

---

## 8. Views & User Interface

### 8.1 Job List View

```xml
<record id="view_video_job_tree" model="ir.ui.view">
    <field name="name">unifix.video.job.tree</field>
    <field name="model">unifix.video.job</field>
    <field name="arch" type="xml">
        <tree>
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
            <field name="total_time_spent"/>
        </tree>
    </field>
</record>
```

### 8.2 Job Form View

```xml
<record id="view_video_job_form" model="ir.ui.view">
    <field name="name">unifix.video.job.form</field>
    <field name="model">unifix.video.job</field>
    <field name="arch" type="xml">
        <form>
            <header>
                <button name="action_retry" string="Retry" type="object"
                        class="btn-warning"
                        attrs="{'invisible': [('state', '!=', 'failed')]}"/>
                <button name="action_cancel" string="Cancel" type="object"
                        class="btn-secondary"
                        attrs="{'invisible': [('state', 'in', ['done', 'cancelled'])]}"/>
                <field name="state" widget="statusbar"
                       statusbar_visible="received,processing,done"/>
            </header>
            <sheet>
                <div class="oe_title">
                    <h1><field name="name"/></h1>
                </div>
                <group>
                    <group string="Source">
                        <field name="source_filename"/>
                        <field name="source_size" widget="integer" 
                               options="{'unit': 'MB'}"/>
                        <field name="source_hash" groups="base.group_no_one"/>
                    </group>
                    <group string="Processing">
                        <field name="create_date"/>
                        <field name="processed_at"/>
                        <field name="retry_count"/>
                    </group>
                </group>
                <notebook>
                    <page string="Transcript" name="transcript">
                        <field name="transcript" widget="html"
                               options="{'height': 400}"/>
                    </page>
                    <page string="Segments" name="segments">
                        <field name="segment_ids">
                            <tree>
                                <field name="sequence" widget="handle"/>
                                <field name="start_time_display"/>
                                <field name="end_time_display"/>
                                <field name="text"/>
                                <field name="confidence" widget="progressbar"/>
                            </tree>
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
                    <page string="Error" name="error"
                          attrs="{'invisible': [('state', '!=', 'failed')]}">
                        <field name="error_message" readonly="1"/>
                    </page>
                </notebook>
            </sheet>
        </form>
    </field>
</record>
```

### 8.3 Search View

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
            <filter name="state_received" string="Received" 
                    domain="[('state', '=', 'received')]"/>
            <filter name="state_processing" string="Processing" 
                    domain="[('state', '=', 'processing')]"/>
            <filter name="state_done" string="Done" 
                    domain="[('state', '=', 'done')]"/>
            <filter name="state_failed" string="Failed" 
                    domain="[('state', '=', 'failed')]"/>
            <group expand="0" string="Group By">
                <filter string="State" name="group_state" 
                        context="{'group_by': 'state'}"/>
                <filter string="Company" name="group_company" 
                        context="{'group_by': 'company'}"/>
                <filter string="Creation Date" name="group_date" 
                        context="{'group_by': 'create_date:month'}"/>
            </group>
        </search>
    </field>
</record>
```

### 8.4 Menu Items

```xml
<record id="menu_video_root" model="ir.ui.menu">
    <field name="name">Unifix</field>
    <field name="sequence" value="90"/>
</record>

<record id="menu_video_jobs" model="ir.ui.menu">
    <field name="name">Video Jobs</field>
    <field name="parent_id" ref="menu_video_root"/>
    <field name="action" ref="action_video_job"/>
    <field name="sequence" value="10"/>
</record>
```

---

## 9. Configuration & Secrets

### 9.1 ir.config_parameter Keys

| Key | Description | Default | Example |
|-----|-------------|---------|---------|
| `unifix.gemini_api_key` | Gemini API key | *(required)* | `AIzaSy...` |
| `unifix.gemini_model` | Gemini model ID | `gemini-2.5-pro` | `gemini-3.1-flash-lite` |
| `unifix.transcript_prompt` | Transcript extraction prompt | *(built-in default)* | See `prompts/transcript_v1.txt` |
| `unifix.callback_secret` | HMAC secret for callback auth | *(auto-generated)* | Random 32-byte hex |
| `unifix.max_video_size_mb` | Max upload size in MB | `2048` | `512` |
| `unifix.temp_dir` | Temp directory for video files | `/tmp` | `/var/unifix/tmp` |
| `unifix.cleanup_age_hours` | Age in hours before temp files are deleted | `1` | `2` |

### 9.2 Settings UI

```xml
<record id="view_res_config_settings_form" model="ir.ui.view">
    <field name="name">res.config.settings.view.form.inherit.unifix</field>
    <field name="model">res.config.settings</field>
    <field name="inherit_id" ref="base.res_config_settings_view_form"/>
    <field name="arch" type="xml">
        <xpath expr="//div[hasclass('settings')]" position="inside">
            <app string="Unifix" data_string="unifix" name="unifix_video"
                 logo="/unifix_video/static/description/icon.png">
                <block title="Gemini Configuration" name="gemini_config">
                    <setting string="API Key" help="Google Gemini API key">
                        <field name="unifix_gemini_api_key" password="True"/>
                    </setting>
                    <setting string="Model" help="Gemini model to use">
                        <field name="unifix_gemini_model"/>
                    </setting>
                </block>
                <block title="Processing" name="processing_config">
                    <setting string="Max Video Size" help="Maximum upload size in MB">
                        <field name="unifix_max_video_size_mb"/>
                    </setting>
                </block>
            </app>
        </xpath>
    </field>
</record>
```

---

## 10. Security Considerations

### 10.1 Upload Endpoint

| Concern | Mitigation |
|---------|------------|
| Unauthenticated uploads | `auth='user'` requires login |
| CSRF attacks | `csrf=True` (Odoo default) |
| Path traversal | Use `tempfile.gettempdir()` + UUID filename |
| Malicious file content | Validate extension + first bytes (magic number) |
| Disk exhaustion | Limit upload size; cleanup cron removes orphans |

### 10.2 Callback Endpoint

| Concern | Mitigation |
|---------|------------|
| Unauthenticated callbacks | HMAC signature verification |
| Replay attacks | Include timestamp in signed payload; reject old requests |
| Job ID manipulation | Verify job exists and is in 'processing' state |
| Data injection | Sanitize all fields before writing to DB |

### 10.3 Temp Files

| Concern | Mitigation |
|---------|------------|
| Leftover files after crash | Cleanup cron runs hourly |
| File permissions | `chmod 0600` on temp files |
| Concurrent access | UUID filenames prevent collisions |
| Disk space | Monitor `/tmp` usage; set alerts |

### 10.4 Access Control

| Group | Permissions |
|-------|-------------|
| `base.group_user` (all internal users) | Read own jobs, create jobs |
| `unifix_video.group_unifix_manager` | Read/write/delete all jobs, view settings |

---

## 11. Error Handling & Retry

### 11.1 Error States

| Error | State | Error Message | Recovery |
|-------|-------|---------------|----------|
| Temp file missing | `failed` | "Temp file expired before processing" | User re-uploads |
| ffmpeg fails | `failed` | "ffmpeg error: {stderr}" | User retries or re-uploads |
| Gemini API error | `failed` | "Gemini API error: {status} {message}" | Auto-retry (up to 3 times) |
| Gemini rate limit | `failed` | "Rate limited. Retry in {seconds}s." | Auto-retry with backoff |
| Invalid response | `failed` | "Failed to parse Gemini response" | User retries |
| Callback fails | `processing` | Worker retries callback | Auto-retry (up to 3 times) |

### 11.2 Retry Logic

```python
def action_retry(self):
    """Manually retry a failed job."""
    self.ensure_one()
    if self.state != 'failed':
        raise UserError("Only failed jobs can be retried.")
    
    # Check if temp file still exists
    if not self.tmp_path or not os.path.exists(self.tmp_path):
        raise UserError(
            "The original video file has been cleaned up. "
            "Please upload the video again."
        )
    
    self.write({
        'state': 'received',
        'error_message': False,
    })
    self.action_enqueue()
```

### 11.3 Auto-Retry (for transient errors)

```python
MAX_RETRIES = 3

def _process_video(self):
    try:
        # ... processing logic ...
    except TransientError as e:
        if self.retry_count < MAX_RETRIES:
            self.write({
                'state': 'received',
                'retry_count': self.retry_count + 1,
            })
            # Re-enqueue with delay
            self.with_delay(eta=60 * (2 ** self.retry_count))._process_video()
        else:
            self.write({
                'state': 'failed',
                'error_message': f"Failed after {MAX_RETRIES} attempts: {e}",
            })
```

---

## 12. Testing Strategy

### 12.1 Constraint #1 Guard Tests

```python
class TestNoVideoStorage(TransactionCase):
    """Ensure video bytes are NEVER stored in Odoo."""
    
    def test_no_binary_field_on_job_model(self):
        """Verify no Binary field exists on the job model."""
        job_model = self.env['unifix.video.job']
        for name, field in job_model._fields.items():
            self.assertNotIsInstance(
                field, fields.Binary,
                f"Found Binary field '{name}' on job model. "
                "Video must never be stored in Odoo."
            )
    
    def test_no_binary_field_on_segment_model(self):
        """Verify no Binary field exists on the segment model."""
        segment_model = self.env['unifix.video.segment']
        for name, field in segment_model._fields.items():
            self.assertNotIsInstance(
                field, fields.Binary,
                f"Found Binary field '{name}' on segment model."
            )
    
    def test_upload_creates_no_ir_attachment(self):
        """Verify upload does not create an ir.attachment for the video."""
        # Create a mock video file
        tmp_path = os.path.join(tempfile.gettempdir(), 'unifix_test.mp4')
        with open(tmp_path, 'wb') as f:
            f.write(b'\x00' * 1024)  # Dummy data
        
        # Create job
        job = self.env['unifix.video.job'].create({
            'name': 'Test Job',
            'state': 'received',
            'tmp_path': tmp_path,
        })
        
        # Check no attachment was created
        attachments = self.env['ir.attachment'].search([
            ('res_model', '=', 'unifix.video.job'),
            ('res_id', '=', job.id),
        ])
        self.assertEqual(len(attachments), 0,
            "ir.attachment created for video job — violates constraint #1")
        
        # Cleanup
        os.unlink(tmp_path)
```

### 12.2 Upload Controller Tests

```python
class TestUploadController(HttpCase):
    def test_upload_returns_job_id(self):
        """Verify upload returns a job ID."""
        video_content = b'\x00' * 10240  # 10KB dummy
        response = self.url_open(
            '/unifix/upload',
            files={'video_file': ('test.mp4', video_content, 'video/mp4')}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('job_id', data)
        self.assertEqual(data['state'], 'received')
    
    def test_upload_rejects_invalid_extension(self):
        """Verify upload rejects non-video files."""
        response = self.url_open(
            '/unifix/upload',
            files={'video_file': ('test.exe', b'\x00' * 1024, 'application/octet-stream')}
        )
        self.assertEqual(response.status_code, 400)
```

### 12.3 Worker Tests

```python
class TestWorker(TransactionCase):
    @patch('subprocess.run')
    def test_extract_audio_calls_ffmpeg(self, mock_run):
        """Verify ffmpeg is called with correct arguments."""
        mock_run.return_value = MagicMock(returncode=0)
        
        job = self.env['unifix.video.job'].create({
            'name': 'Test',
            'state': 'processing',
            'tmp_path': '/tmp/test.mp4',
        })
        
        job._extract_audio('/tmp/test.mp4')
        
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertIn('ffmpeg', args)
        self.assertIn('-vn', args)
```

---

## 13. Phase 1 vs Phase 2 Scope

### Phase 1: Transcript + Ephemeral Video (this plan)

| Component | Included | Notes |
|-----------|----------|-------|
| Upload controller | ✅ | Streams to temp, returns immediately |
| Job model | ✅ | State machine, all work-order fields |
| Segment model | ✅ | Timestamped transcript segments |
| Async worker | ✅ | Cron-based processing |
| ffmpeg audio extraction | ✅ | Reuse existing `video_extractor.py` |
| Gemini transcript extraction | ✅ | New prompt that returns transcript + timestamps |
| Callback endpoint | ✅ | HMAC-authenticated |
| Cleanup cron | ✅ | Hourly orphan cleanup |
| Job list/form views | ✅ | Tree, form, search views |
| Settings UI | ✅ | API key, model, max size |
| Security groups | ✅ | User + manager |
| Tests | ✅ | Constraint guards, upload, worker |

### Phase 2: Keyframes (future)

| Component | Status | Notes |
|-----------|--------|-------|
| Keyframe model | 🔜 | `unifix.video.keyframe` — one per important moment |
| Keyframe extraction | 🔜 | ffmpeg seek-by-timestamp at segment boundaries |
| Keyframe storage | 🔜 | Small `Image` field or bucket URL (decision pending) |
| OWL transcript viewer | 🔜 | Transcript with inline keyframe thumbnails |
| "Important moment" detection | 🔜 | Heuristic or Gemini-based selection |

---

## 14. Open Decisions

These decisions must be resolved before implementation begins:

| # | Decision | Options | Recommendation |
|---|----------|---------|----------------|
| 1 | **Job queue mechanism** | Odoo cron vs OCA `queue_job` | Start with cron; migrate to `queue_job` later if needed |
| 2 | **Transcript prompt** | New prompt vs modify existing | New prompt — existing `audio_extraction_v2.txt` returns fields, not transcript |
| 3 | **ASR service** | Gemini vs Whisper vs Deepgram | Gemini (already integrated; add transcript mode) |
| 4 | **Callback auth** | HMAC vs API key vs JWT | HMAC (simple, stateless, no token expiry) |
| 5 | **Temp directory** | `/tmp` vs custom path | `/tmp` (default); make configurable via `ir.config_parameter` |
| 6 | **Max video size** | 512 MB vs 1 GB vs 2 GB | 1 GB (balance between capability and disk usage) |
| 7 | **Processing concurrency** | 1 job at a time vs parallel | 1 at a time initially; parallel later with `queue_job` |
| 8 | **Keyframe storage (Phase 2)** | Odoo `Image` field vs bucket URL | TBD in Phase 2 planning |

---

## 15. Assumptions

| # | Assumption | Basis |
|---|------------|-------|
| A1 | The existing `workorder_processing` package will be refactored into the Odoo module's worker logic | Inferred from the investigation report (§7, A2) |
| A2 | Gemini can produce timestamped transcripts if prompted correctly | The current prompt asks for fields only; a new prompt is needed |
| A3 | ffmpeg will be available on the Odoo server | Required for audio extraction; must be pre-installed |
| A4 | Odoo is deployed on a single server (not clustered) | Option B assumes `/tmp` is local; cluster would need Option A |
| A5 | Videos are under 1 GB | Aligns with Odoo.sh upload limits and disk constraints |
| A6 | The Odoo server has sufficient disk space for temporary video files | At peak: ~1.1× largest video size |
| A7 | The Odoo version is v19 | Per the investigation brief; not yet confirmed in code |
| A8 | The deployment target is Odoo.sh | Per the investigation brief; not yet confirmed |

---

## Appendix A: File Map

Complete list of files to create:

```
unifix_video/
├── __init__.py                              # Import controllers, models
├── __manifest__.py                          # Module manifest
├── controllers/
│   ├── __init__.py                          # Import controllers
│   ├── upload.py                            # POST /unifix/upload
│   └── callback.py                          # POST /unifix/callback
├── models/
│   ├── __init__.py                          # Import models
│   ├── video_job.py                         # unifix.video.job
│   ├── video_segment.py                     # unifix.video.segment
│   └── res_config_settings.py              # Settings fields
├── data/
│   ├── cron.xml                             # Processing + cleanup crons
│   └── default_prompts.xml                  # Default ir.config_parameter values
├── security/
│   ├── ir.model.access.csv                  # Access rights
│   └── security.xml                         # Groups
├── views/
│   ├── video_job_views.xml                  # Tree, form, search views
│   ├── video_job_menu.xml                   # Menu items
│   └── res_config_settings_views.xml       # Settings page
├── prompts/
│   └── transcript_v1.txt                    # Gemini transcript prompt
├── static/
│   └── description/
│       └── icon.png                         # Module icon
└── tests/
    ├── __init__.py
    ├── test_upload.py                       # Controller tests
    ├── test_worker.py                       # Worker tests
    └── test_no_video_storage.py             # Constraint guard tests
```

---

## Appendix B: Configuration Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `unifix.gemini_api_key` | String (password) | *(required)* | Google Gemini API key |
| `unifix.gemini_model` | String | `gemini-2.5-pro` | Gemini model ID |
| `unifix.transcript_prompt` | Text | *(built-in)* | Prompt for transcript extraction |
| `unifix.callback_secret` | String (password) | *(auto-generated)* | HMAC secret for callback auth |
| `unifix.max_video_size_mb` | Integer | `1024` | Max upload size in MB |
| `unifix.temp_dir` | String | `/tmp` | Temp directory for video files |
| `unifix.cleanup_age_hours` | Integer | `1` | Age before temp files are deleted |
| `unifix.allowed_extensions` | String | `.mp4,.mkv,.mov,.avi,.webm` | Allowed video extensions |

---

**End of document**
