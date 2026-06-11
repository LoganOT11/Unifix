# Unifix — Media Storage & Display Design

Status: **design / investigation** (2026-06-11). Nothing here is implemented yet
except the current single `media_file` Binary field on `unifix.workorder.job`.
This doc captures the decisions and the plan for how source media (phone photos
of worksheets, voice notes, video) is stored, transferred, and displayed.

---

## 1. Decisions made

| Topic | Decision |
|---|---|
| Storage backend | **Filestore (disk)**, the Odoo default. NOT Postgres `bytea`. |
| Attachments per workorder | **Multiple** — move from one `media_file` Binary to a child model. |
| Originals | **Not kept in cloud Odoo.** Cloud stores only the *compressed* derivative + extracted data. Full-res originals stay local where captured. |
| Audio compression | Transcode to **Opus, mono, ~16–24 kbps** on ingest (~100× smaller than WAV). |
| Image compression | Resize (cap ~2048 px longest edge) + re-encode JPEG/WebP q≈80, strip EXIF. |
| Image cropping | Detect + perspective-crop the worksheet from phone photos (see §5). |
| Video | Not stored; audio extracted → Opus. Keyframes are a future feature. |

---

## 2. Storage architecture — filestore, not DB blobs

Today `media_file = Binary(attachment=True)` already stores bytes as an
`ir.attachment` in the **filestore on disk**
(`~/.local/share/Odoo/filestore/<db>/<sha1[:2]>/<sha1>`); Postgres holds only
metadata (checksum, size, mimetype, path). Verified: filestore blobs are **raw
binary**, and identical files **de-duplicate** by SHA-1.

Do **not** set `ir_attachment.location = db`. Putting bytes in Postgres:
- bloats the DB → every `pg_dump` carries all media; restores get huge/slow,
- stores them **base64-encoded** in `db_datas` (~33% larger),
- loses filestore de-dup and streaming.

"In the database" should mean *managed by Odoo and bound to the record* — which
filestore attachments already are. Odoo's standard backup bundles filestore + DB.
Future scale/offsite path is **object storage (GCS/S3)** via OCA `ir_attachment_*`
modules, not Postgres blobs.

---

## 3. Multiple attachments per workorder

Replace the single Binary with a child model:

```
unifix.workorder.media
  job_id        Many2one(unifix.workorder.job, ondelete=cascade)
  role          Selection: source_audio | source_image | source_video | cropped | derived
  attachment_id Many2one(ir.attachment)         # filestore-backed bytes
  mimetype, file_size, original_filename, width/height/duration (optional)
  is_primary    Boolean
```

Benefits over a lone Binary: many files per workorder, re-uploads add rows
(history) instead of overwriting, and each row records *which* derivative it is
(compressed / cropped). Deleting the job cascades to media rows and their
attachments (no orphans). Alternative: Odoo **chatter** (`mail.thread`) documents
area — good free UX, but a dedicated model gives us role/variant tracking.

---

## 4. Ingest compression (cloud gets only the derivative)

ffmpeg (already a dep, used for video) + Pillow (via OpenCV/numpy stack):

- **Audio:** `ffmpeg -i in -ac 1 -c:a libopus -b:a 24k out.opus`
  → ~5 MB WAV becomes ~40–60 KB. Gemini re-extracts fine from Opus/OGG.
- **Image:** cap longest edge ~2048 px, JPEG/WebP q≈80, strip metadata
  → multi-MB photos become ~200–500 KB.

The **original is discarded after extraction** (stays local per decision). The
compressed derivative is what we attach. Extraction runs on the cleaned image, so
storage and extraction share one preprocessing pass.

---

## 5. Cropping the worksheet from a phone photo

Classic **document-scanner** flow in OpenCV (extends `processing/processor/
image_preprocessor.py`): grayscale → blur → Canny → largest convex 4-point contour
→ `warpPerspective` to a flat top-down scan → optional contrast/shadow cleanup.

Running it **before** extraction also improves Gemini's read (less background,
deskewed). **Caveat:** edge detection is reliable when the sheet is the dominant
bright rectangle on a contrasting surface, fully visible; it struggles with low
contrast, edge-to-edge framing (no border), shadows, crumpled paper — so it needs
a **confidence fallback to the un-cropped (just deskewed) image.**

Implementation options (converge on the same stored output):
1. **OpenCV server-side** (recommended start) — no new deps, fast, fallback on low confidence.
2. **Client-side crop+compress before upload** — best fit for "upload local, cloud gets compressed only"; original never leaves the device; user can confirm the crop. More UI work.
3. **Ask Gemini for the 4 corner coordinates** — reuses the existing call, often more robust on messy photos; good fallback when (1) fails.

Plan: ship (1) with a fallback; keep (3) as an upgrade path.

---

## 6. Data transfer & display — the base64 question

**Belief:** "Odoo handles all file uploads using base64." **Partly true, and
mostly not a concern — here's the precise picture.**

Where base64 **is** used:
- **ORM/field layer:** `Binary` field *values* are base64 in Python and over
  **JSON-RPC** (`web_save`/`read`) — JSON can't carry raw bytes. So saving a
  Binary field through the standard web widget ships base64 (~+33%).
- **`db_datas` column** (only if you store in DB, which we don't).
- **Our upload controller** base64-encodes the temp file to *set* the field
  (`upload.py:97`).

Where base64 is **NOT** used (the parts that matter for cloud display):
- **Filestore at rest:** raw binary (verified), de-duplicated.
- **Displaying images:** the web client points `<img src>` at
  **`/web/image/<model>/<id>/<field>`** (`web/controllers/binary.py`), which
  **streams raw bytes** with `Content-Type`, `ETag`/`Last-Modified` (→ `304 Not
  Modified` on re-view) and caching. No base64 over the wire to the browser.
- **Downloading audio/files:** **`/web/content/<attachment_id>?download=true`**
  streams raw bytes with **HTTP range support** (HTML5 `<audio>` can seek).
- **Thumbnails:** `fields.Image` auto-generates resized variants (1920/1024/512/
  256/128); list/kanban request the small size, not the full image.

**So is base64 a cause for concern?** Bounded, and mitigated:
- Real risk is **large-file *writes/uploads* through JSON-RPC**: the whole file is
  held in memory as base64 (~2.3× transiently) and the RPC payload is +33%. This
  bites for big raw uploads (e.g. 200 MB audio) via the standard Binary widget.
- **Mitigations already/planned in place:**
  - Our `/unifix/upload` controller streams **multipart/form-data in chunks to
    disk** — the HTTP upload itself is raw, not a base64 JSON-RPC write.
  - Compression-on-ingest means what we store/transfer is tiny (Opus/compressed
    JPEG), so base64's 33% on a 50 KB file is negligible.
  - **Display & download never use base64** — raw streaming + HTTP caching.
  - Server-side, set bytes via `ir.attachment(raw=<bytes>)` instead of base64 to
    skip the encode/decode round-trip (TODO: replace `upload.py:97`
    `b64encode(f.read())` with a `raw=` attachment create).

**Cloud specifics:** filestore lives on the server disk (or object storage);
`/web/image` + `/web/content` stream with ETag caching (and a CDN/proxy on
Odoo.sh), so images/audio load efficiently from the cloud while Postgres stays
small (metadata only). Net: with compression + raw streaming, base64 is not a
practical bottleneck.

---

## 7. Open decisions & next steps

- [ ] Confirm child-model approach (`unifix.workorder.media`) vs chatter documents.
- [ ] Compression targets (Opus 24 kbps? image 2048 px / q80?).
- [ ] Crop: server-side OpenCV first; decide if/when to add client-side or Gemini-corners.
- [ ] Replace `upload.py` base64 field-set with `ir.attachment(raw=…)`.
- [ ] **Prototype document-crop on a real worksheet photo** (de-risk reliability) before the schema change.

---

## 8. Cloud (Odoo.sh) deployment constraints

User story: laptop → Odoo web login on **Odoo.sh** (cloud); the `unifix_odoo` module
runs server-side. No local component in the base story.

- **Locked platform.** Odoo.sh allows **pip deps only** (`requirements.txt`), **no
  `apt`/system binaries**. So `ffmpeg` is not present by default → ship it via the
  `imageio-ffmpeg` pip package (`imageio_ffmpeg.get_ffmpeg_exe()`), and use
  `opencv-python-headless` (no GUI libs).
- **Never process in the HTTP request.** The reverse proxy enforces request
  timeouts; heavy/slow work (transcode, Gemini calls) must run in the **cron/queue
  worker**, not the upload request.
- **Proxy body-size limits.** Large uploads (200 MB+ video) through Odoo's endpoint
  are unreliable → for big files use **direct-to-object-storage** (signed-URL upload
  to GCS), not a multipart POST to Odoo. Audio/images (small, compressed) are fine
  through the multipart controller.
- **Auth differs in cloud.** Vertex **ADC is a local dev credential**; on Odoo.sh use
  a **service-account key** (`GOOGLE_APPLICATION_CREDENTIALS`) or an API key.
  (`_gemini_client()` already supports the non-Vertex/API-key fallback.) On **Cloud
  Run**, ADC works natively via the service account — no key file.
- **Filestore counts against disk quota** → compress before storing.

**Verdict:** audio & images can be processed entirely **on Odoo.sh** (cron worker +
pip ffmpeg/opencv). Video is the only hard case (see §9–§10).

## 9. Video token economics + recommended strategy

**Tokens scale with video DURATION (× fps × resolution), NOT file MB.** Google's
Gemini tokenization: ~**1 fps** sampling, ~**258 tokens/frame** (~66 at *low* media
resolution), audio ~**32 tokens/sec** → ~**290 tokens/sec** of video at defaults.

A 200 MB phone video ≈ ~1–5 min ≈ **~17k–87k input tokens** if you send the whole
video (+ ~1–2k output). Order-of-magnitude: cents on flash, ~10–15¢ on pro/video —
verify current Vertex pricing.

**Strategy — don't send the video to Gemini:**
- **Workorder data:** send **audio only** (~32 tok/sec, ~9× cheaper than video). The
  structured fields come from narration anyway — this is the current design.
- **Keyframes:** extract candidate frames **locally** (ffmpeg scene-change
  `select='gt(scene,0.4)'` + Laplacian-variance sharpness filter), send ~**10 stills**
  (~2.6k tokens) — ~30× cheaper than full video, and you control quality.
- **If you must send video:** drop to **0.2–0.5 fps**, **low media resolution**
  (~66 tok/frame), **clip** to relevant ranges, use **flash**.

Trade-off: native full-video understanding is highest quality but most expensive and
heaviest to run; **audio + locally-extracted frames** is the pragmatic sweet spot for
cost *and* Odoo.sh constraints.

## 10. External processing service — when, requirements, Cloud Run vs VM

**Not required for MVP.** Needed only for **server-side heavy/large-video** at scale.
"External" = a backend microservice Odoo.sh calls server-to-server; **users never
touch it** — compatible with the Odoo.sh-only access model.

Three architectures:
1. **All on Odoo.sh** — audio/image in cron worker; video only if small or
   pre-extracted client-side. *No external service.*
2. **Client-side preprocessing** — browser extracts audio + frames before upload;
   Odoo.sh receives only small artifacts. *No external service.*
3. **External service** — for large raw video processed server-side.

**External-service requirements (if pursued):**
- **GCS bucket** + **signed-URL** direct upload from the laptop (keeps 200 MB off
  Odoo.sh — the real fix for the upload-size limit).
- **Compute** (Cloud Run service/job or VM) — container with ffmpeg, opencv, genai;
  triggered by a **GCS finalize event** or an **HTTP call from Odoo.sh**.
- **Identity** — service account (read GCS, call Vertex). On Cloud Run this is **ADC
  natively** → existing `_gemini_client(vertexai=True)` works with no key file.
- **Auth** between Odoo.sh ↔ service (shared secret / OIDC).
- **Callback** into Odoo via **JSON-RPC external API** (API user) to attach compressed
  media + frames + extracted JSON.
- **Odoo wiring** — signed-URL mint endpoint, result-ingest endpoint, job-status field.

**Cloud Run vs VM:** prefer **Cloud Run** (scales to zero, ADC-native, ffmpeg in the
container, multi-minute jobs; Cloud Run *Jobs* for longer batch). A **VM** only for
constant high throughput or persistent state/GPU.

**Recommendation:** MVP = all on Odoo.sh. Add **GCS signed-URL upload + Cloud Run**
only when server-side large-video becomes a real requirement. Don't provision a VM.
