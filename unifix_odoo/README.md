# Unifix Odoo Module

Odoo 19 module for AI-powered video processing and work-order extraction using Google Gemini.

## Overview

This module provides an ephemeral video processing pipeline where:
- Videos are uploaded via a thin HTTP controller
- Audio is extracted using ffmpeg
- Gemini AI transcribes audio and extracts structured work-order fields
- Keyframes are extracted at important timestamps
- Only derived data (transcript, segments, keyframes, fields) is stored in Odoo
- **Video files are never persisted** in Odoo storage

## Installation

### Prerequisites

- Odoo 19.0
- PostgreSQL
- ffmpeg (on PATH)
- Google Gemini API key

### Setup

1. **Symlink or copy** the module to Odoo's addons directory:
   ```bash
   ln -s /path/to/unifix_odoo /path/to/odoo/addons/unifix_odoo
   ```

2. **Install the module** via CLI:
   ```bash
   python odoo-bin --addons-path=addons -d your_database -i unifix_odoo --stop-after-init
   ```

3. **Configure** in Odoo:
   - Go to **Settings → Unifix**
   - Enter your Gemini API key
   - Adjust processing settings as needed

### Starting Odoo

```bash
# Activate conda environment
source ~/miniconda3/etc/profile.d/conda.sh && conda activate unifix

# Start Odoo in background
cd /home/logan/Repos/odoo
nohup python odoo-bin \
  --addons-path=/home/logan/Repos/odoo/addons,/home/logan/Repos/odoo/odoo/addons \
  -d unifix_test \
  --db_user=logan \
  --http-port=8069 \
  > /tmp/odoo.log 2>&1 &

echo "Odoo PID: $!"
sleep 3
tail -5 /tmp/odoo.log
```

### Stopping Odoo

```bash
# Find and kill Odoo process
pkill -f "odoo-bin.*unifix_test"

# Or kill by port
fuser -k 8069/tcp

# Or find PID and kill manually
ps aux | grep odoo-bin | grep -v grep
kill <PID>
```

### Accessing Odoo

- **URL:** http://localhost:8069
- **Database:** unifix_test
- **Username:** admin
- **Password:** admin
- **Navigate to:** Unifix → Video Jobs

## Odoo 19 Compatibility Fixes

The following issues were resolved to ensure compatibility with Odoo 19.0:

### 1. Security Groups (`security/security.xml`)

**Problem:** `category_id` field no longer exists on `res.groups` in Odoo 19.

**Fix:** Use the new `res.groups.privilege` model with `privilege_id` reference:
```xml
<!-- Before (Odoo 18) -->
<record id="group_unifix_user" model="res.groups">
    <field name="category_id" ref="base.module_category_services"/>
</record>

<!-- After (Odoo 19) -->
<record model="res.groups.privilege" id="res_groups_privilege_unifix">
    <field name="name">Unifix</field>
    <field name="category_id" ref="base.module_category_services"/>
</record>

<record id="group_unifix_user" model="res.groups">
    <field name="privilege_id" ref="res_groups_privilege_unifix"/>
</record>
```

### 2. Module Name References (`security/ir.model.access.csv`)

**Problem:** CSV referenced old module name `unifix_video.group_unifix_manager`.

**Fix:** Updated all references to `unifix_odoo.group_unifix_manager`.

### 3. Cron Jobs (`data/cron.xml`)

**Problem 1:** `numbercall` field removed in Odoo 19.

**Fix:** Removed the field entirely.

**Problem 2:** `import` statements forbidden in cron code blocks.

**Fix:** Moved cleanup logic to a model method `_cleanup_orphaned_files()` and call it via `model._cleanup_orphaned_files()`.

```xml
<!-- Before -->
<field name="code">
import os, glob, time, tempfile
# ... cleanup logic ...
</field>

<!-- After -->
<field name="code">model._cleanup_orphaned_files()</field>
```

### 4. Menu XML (`views/video_job_menu.xml`)

**Problem:** `<record>` tags inside `<odoo>` rejected by Odoo 19 schema validation.

**Fix:** Use `<menuitem>` shorthand tag directly:
```xml
<!-- Before -->
<record id="menu_video_root" model="ir.ui.menu">
    <field name="name">Unifix</field>
    <field name="sequence" value="90"/>
</record>

<!-- After -->
<menuitem id="menu_video_root" name="Unifix" sequence="90"/>
```

### 5. Search View (`views/video_job_views.xml`)

**Problem 1:** `expand` attribute on `<group>` not allowed in Odoo 19.

**Fix:** Removed `expand="0"` attribute.

**Problem 2:** Missing `string` attribute on `<search>` element.

**Fix:** Added `string="Video Jobs"` to the search element.

### 6. Settings View (`views/res_config_settings_views.xml`)

**Problem:** XPath `//div[hasclass('settings')]` cannot be located in Odoo 19 parent view.

**Fix:** Use `//form` with `position="inside"` instead:
```xml
<!-- Before -->
<xpath expr="//div[hasclass('settings')]" position="inside">
    <app string="Unifix" ...>

<!-- After -->
<xpath expr="//form" position="inside">
    <app string="Unifix" data_string="unifix" name="unifix_video">
```

## Architecture

```
Browser → POST /unifix/upload → Stream to temp file → Create job record
                                                          ↓
Cron (every 1 min) → _process_video()
    ├── Extract audio (ffmpeg)
    ├── Call Gemini API → transcript + segments + keyframes + fields
    ├── Extract frames at keyframe timestamps
    ├── Delete video file
    └── Store results in Odoo
```

## Models

| Model | Description |
|-------|-------------|
| `unifix.video.job` | Main processing job with state machine |
| `unifix.video.segment` | Timestamped transcript segments |
| `unifix.video.keyframe` | Extracted still frames with timestamps |

## Configuration Parameters

| Key | Default | Description |
|-----|---------|-------------|
| `unifix.gemini_api_key` | *(required)* | Google Gemini API key |
| `unifix.gemini_model` | `gemini-2.5-pro` | Gemini model ID |
| `unifix.transcript_prompt` | *(built-in)* | Custom prompt override |
| `unifix.max_video_size_mb` | `1024` | Max upload size (MB) |
| `unifix.temp_dir` | `/tmp` | Temp directory for videos |
| `unifix.cleanup_age_hours` | `1` | Hours before temp file cleanup |
| `unifix.max_keyframes` | `12` | Max frames per video |
| `unifix.frame_quality` | `3` | JPEG quality (2=best, 31=worst) |

## Testing

```bash
# Install with test data
python odoo-bin --addons-path=addons -d test_db -i unifix_odoo --stop-after-init

# Run tests
python odoo-bin --addons-path=addons -d test_db --test-tags=/unifix_odoo
```

## File Structure

```
unifix_odoo/
├── __init__.py
├── __manifest__.py
├── controllers/
│   ├── __init__.py
│   └── upload.py                  # POST /unifix/upload
├── models/
│   ├── __init__.py
│   ├── video_job.py               # Main job model + worker
│   ├── video_segment.py           # Transcript segments
│   ├── video_keyframe.py          # Extracted frames
│   └── res_config_settings.py     # Settings fields
├── data/
│   ├── cron.xml                   # Processing + cleanup crons
│   └── default_params.xml         # Default config values
├── security/
│   ├── ir.model.access.csv        # Access rights
│   └── security.xml               # Groups (v19 privilege format)
├── views/
│   ├── video_job_views.xml        # List, form, search views
│   ├── video_job_menu.xml         # Menu items
│   └── res_config_settings_views.xml  # Settings page
├── prompts/
│   └── transcript_v1.txt          # Gemini prompt
├── static/
│   └── description/
│       └── icon.png               # Module icon
└── tests/
    ├── __init__.py
    ├── test_upload.py
    ├── test_worker.py
    ├── test_keyframes.py
    └── test_no_video_storage.py
```

## License

LGPL-3
