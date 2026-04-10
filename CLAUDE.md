# VideoForge — Master Project Brief for Claude Code
## AI-Powered Automated Video Production System

---

## IMPORTANT — READ THIS FIRST

This document is the single source of truth for the VideoForge project. Every decision about architecture, naming, logging, error handling, and code style is defined here. Before writing any code, read this document in full. When in doubt, refer back here.

---

## 1. Project Description

VideoForge is a Python-based automated pipeline that takes a topic string and produces a fully edited, captioned, SEO-tagged short-form video ready to post on TikTok and YouTube Shorts. It is built for the channel **"The Engineering Brief"** (@HowThingsWorkEng) which publishes educational engineering explainer content.

The system is designed to run on a personal computer (Windows/Mac/Linux), be operated through a web dashboard at localhost:5000, and produce 5 videos per week in a single Sunday batch session with minimal human involvement beyond a 15-minute quality review per video.

**The owner has an electrical engineering background.** All content is educational, faceless, and AI-generated. No personal footage is used.

---

## 2. Goals

**Primary goal:** Produce 5 publish-ready videos per week automatically from a list of topics, post them to TikTok and YouTube Shorts, and generate passive income through YouTube AdSense and brand deals.

**Secondary goals:**
- Keep monthly running costs under $40
- Make the system operable with zero coding knowledge after initial setup
- Make every parameter editable through config.json without touching code
- Make every failure diagnosable through logs without needing a developer

**What this system is NOT:**
- Not a content agency tool
- Not multi-user
- Not cloud-hosted (runs locally)
- Not designed for any niche other than engineering education

---

## 3. Channel Identity

| Field | Value |
|---|---|
| Channel name | The Engineering Brief |
| YouTube handle | @HowThingsWorkEng |
| TikTok handle | @HowThingsWorkEng |
| Niche | Engineering education — how everyday things work |
| Content buckets | Electrical, Infrastructure, Vehicles, The Flaw |
| Video length | 60–90 seconds |
| Format | Faceless — AI voiceover + AI images + captions |
| Posting frequency | 5 videos per week |
| Primary monetization | YouTube AdSense (Macedonia eligible) |
| Secondary monetization | Brand deals, TikTok LIVE gifts |

---

## 4. Tech Stack

| Layer | Technology | Version |
|---|---|---|
| Language | Python | 3.11+ |
| Web framework | Flask | Latest |
| Database | SQLite via sqlite3 | Built-in |
| Video editing | MoviePy | Latest |
| Speech-to-text | OpenAI Whisper | Latest |
| Task scheduling | APScheduler | Latest |
| HTTP requests | requests | Latest |
| Audio processing | pydub | Latest |
| Image processing | Pillow | Latest |
| Environment vars | python-dotenv | Latest |
| Google API | google-api-python-client | Latest |

**External APIs:**
| Service | Purpose | Cost |
|---|---|---|
| Anthropic Claude API | Script generation, metadata generation | ~$5/mo |
| ElevenLabs | AI voiceover | $5–22/mo |
| Leonardo.AI | AI image generation | $10/mo |
| Pexels API | Stock B-roll footage fallback | Free |
| YouTube Data API v3 | Video upload | Free |
| YouTube Analytics API | Performance data | Free |
| TikTok Content Posting API | Video upload | Free |

---

## 5. Project File Structure

Every file must live in this exact structure. Do not create files outside these locations.

```
videoforge/
│
├── CLAUDE.md                    ← THIS FILE — master project brief
├── README.md                    ← Setup instructions for humans
├── requirements.txt             ← All Python dependencies
├── config.json                  ← ALL editable parameters (no code needed)
├── .env                         ← API keys — NEVER commit, NEVER log
├── .gitignore                   ← Must include .env, output/, logs/
│
├── main.py                      ← CLI entry point
├── app.py                       ← Flask dashboard (localhost:5000)
├── database.py                  ← SQLite schema + all DB operations
├── scheduler.py                 ← APScheduler batch automation
│
├── modules/                     ← One file per pipeline stage
│   ├── __init__.py
│   ├── script_engine.py         ← Stage 1: Claude API → script JSON
│   ├── voice_engine.py          ← Stage 2: ElevenLabs → MP3
│   ├── image_engine.py          ← Stage 3: Leonardo.AI → images
│   ├── broll_engine.py          ← Stage 3b: Pexels → B-roll fallback
│   ├── assembly_engine.py       ← Stage 4: MoviePy → raw video
│   ├── caption_engine.py        ← Stage 5: Whisper → captioned video
│   ├── thumbnail_engine.py      ← Stage 6: PIL → thumbnail image
│   ├── metadata_engine.py       ← Stage 6b: Claude API → SEO data
│   ├── upload_engine.py         ← Stage 7: TikTok + YouTube upload
│   └── analytics_engine.py      ← Stage 8: Pull performance stats
│
├── prompts/                     ← Plain text prompt templates
│   ├── script_prompt.txt        ← Master script generation prompt
│   └── metadata_prompt.txt      ← SEO metadata generation prompt
│
├── assets/                      ← Static files that never change
│   ├── music/                   ← Royalty-free .mp3 background tracks
│   ├── fonts/                   ← Caption font .ttf files
│   └── thumbnail_template/      ← Overlay PNG for thumbnails
│
├── output/                      ← All generated files (gitignored)
│   ├── scripts/                 ← 001.json, 002.json ...
│   ├── audio/                   ← 001.mp3, 001_hook.mp3 ...
│   ├── images/                  ← 001/img_01.png ... img_08.png
│   ├── videos/                  ← 001_raw.mp4, 001_captioned.mp4
│   ├── thumbnails/              ← 001.jpg ...
│   └── metadata/                ← 001.json (title, desc, hashtags)
│
├── logs/                        ← All log files (gitignored)
│   ├── main.log                 ← Combined log for all modules
│   ├── script_engine.log
│   ├── voice_engine.log
│   ├── image_engine.log
│   ├── assembly_engine.log
│   ├── caption_engine.log
│   ├── thumbnail_engine.log
│   ├── metadata_engine.log
│   ├── upload_engine.log
│   ├── analytics_engine.log
│   └── errors.log               ← Errors only — all modules write here
│
├── templates/                   ← Flask HTML templates
│   ├── base.html
│   ├── dashboard.html
│   ├── job_detail.html
│   ├── config_editor.html
│   └── stats.html
│
└── tests/                       ← One test file per module
    ├── test_connections.py      ← Ping all 4 APIs
    ├── test_script_engine.py
    ├── test_voice_engine.py
    ├── test_image_engine.py
    ├── test_assembly_engine.py
    ├── test_caption_engine.py
    └── test_upload_engine.py
```

---

## 6. Logging Requirements

**This is non-negotiable.** Every module must log every significant action, every API call, every error, and every file it creates. The owner must be able to open a log file and understand exactly what happened and where it failed without reading code.

### Logging Standard

Every module uses this exact logging setup at the top of the file:

```python
import logging
import os
from datetime import datetime

def setup_logger(module_name: str) -> logging.Logger:
    """Set up dual logging: module-specific file + combined main.log + errors.log"""
    
    os.makedirs('logs', exist_ok=True)
    
    logger = logging.getLogger(module_name)
    logger.setLevel(logging.DEBUG)
    
    # Prevent duplicate handlers if function called multiple times
    if logger.handlers:
        return logger
    
    formatter = logging.Formatter(
        '%(asctime)s | %(name)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 1. Module-specific log file
    module_handler = logging.FileHandler(f'logs/{module_name}.log')
    module_handler.setLevel(logging.DEBUG)
    module_handler.setFormatter(formatter)
    
    # 2. Combined main.log
    main_handler = logging.FileHandler('logs/main.log')
    main_handler.setLevel(logging.INFO)
    main_handler.setFormatter(formatter)
    
    # 3. Errors-only log
    error_handler = logging.FileHandler('logs/errors.log')
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    
    # 4. Console output
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    
    logger.addHandler(module_handler)
    logger.addHandler(main_handler)
    logger.addHandler(error_handler)
    logger.addHandler(console_handler)
    
    return logger
```

### What to Log

Every module must log these events:

```python
# Starting a job
logger.info(f"[JOB {job_id}] Starting {module_name} for topic: '{topic}'")

# Before every API call
logger.info(f"[JOB {job_id}] Calling ElevenLabs API — voice_id: {voice_id}, chars: {len(text)}")

# After successful API call
logger.info(f"[JOB {job_id}] ElevenLabs API call succeeded — response time: {elapsed:.2f}s")

# Every file created
logger.info(f"[JOB {job_id}] File created: {filepath} ({file_size_mb:.2f} MB)")

# Every file read
logger.debug(f"[JOB {job_id}] Loading file: {filepath}")

# Config values used
logger.debug(f"[JOB {job_id}] Config: stability={stability}, similarity={similarity}")

# Stage completion
logger.info(f"[JOB {job_id}] {module_name} COMPLETED in {elapsed:.1f}s")

# Warnings (non-fatal)
logger.warning(f"[JOB {job_id}] Image {i} took {elapsed:.1f}s — Leonardo.AI may be slow")

# Errors (caught exceptions)
logger.error(f"[JOB {job_id}] {module_name} FAILED: {str(e)}", exc_info=True)

# API rate limits
logger.warning(f"[JOB {job_id}] Rate limit hit — waiting {retry_after}s before retry")
```

### Log Format

Every log line must include:
- Timestamp: `2026-04-10 14:32:01`
- Module name: `script_engine`
- Level: `INFO` / `DEBUG` / `WARNING` / `ERROR`
- Job ID: `[JOB 001]`
- Human-readable message

Example log output:
```
2026-04-10 14:32:01 | script_engine | INFO  | [JOB 001] Starting script_engine for topic: 'Why phone chargers get warm'
2026-04-10 14:32:01 | script_engine | DEBUG | [JOB 001] Config: model=claude-sonnet-4-6, temperature=0.7, word_count=175
2026-04-10 14:32:01 | script_engine | INFO  | [JOB 001] Calling Claude API — model: claude-sonnet-4-6
2026-04-10 14:32:04 | script_engine | INFO  | [JOB 001] Claude API call succeeded — response time: 3.21s, tokens: 487
2026-04-10 14:32:04 | script_engine | INFO  | [JOB 001] Script parsed — word_count: 182, visual_prompts: 8
2026-04-10 14:32:04 | script_engine | INFO  | [JOB 001] File created: output/scripts/001.json (0.02 MB)
2026-04-10 14:32:04 | script_engine | INFO  | [JOB 001] script_engine COMPLETED in 3.31s
```

---

## 7. Error Handling Requirements

Every module must handle errors at three levels:

### Level 1 — API errors (retry with backoff)
```python
import time

def call_with_retry(func, max_retries=3, backoff_seconds=5):
    """Retry API calls with exponential backoff"""
    for attempt in range(max_retries):
        try:
            return func()
        except RateLimitError as e:
            wait = backoff_seconds * (2 ** attempt)
            logger.warning(f"Rate limit hit — waiting {wait}s (attempt {attempt+1}/{max_retries})")
            time.sleep(wait)
        except APIConnectionError as e:
            wait = backoff_seconds * (2 ** attempt)
            logger.warning(f"API connection error — waiting {wait}s (attempt {attempt+1}/{max_retries}): {e}")
            time.sleep(wait)
    raise Exception(f"API call failed after {max_retries} attempts")
```

### Level 2 — Module errors (fail gracefully, update DB status)
```python
def run(job_id: str, config: dict) -> dict:
    logger.info(f"[JOB {job_id}] Starting script_engine")
    try:
        # ... module logic ...
        db.update_job_status(job_id, 'script_done')
        logger.info(f"[JOB {job_id}] script_engine COMPLETED")
        return {"success": True, "output_path": path}
    except Exception as e:
        logger.error(f"[JOB {job_id}] script_engine FAILED: {str(e)}", exc_info=True)
        db.update_job_status(job_id, 'failed', error_message=str(e))
        return {"success": False, "error": str(e)}
```

### Level 3 — Pipeline errors (stop pipeline, notify dashboard)
If any module returns `{"success": False}`, the pipeline stops immediately. The job status is set to `failed` with the module name and error message. The dashboard shows the failure with a link to the relevant log file.

---

## 8. Code Style Requirements

Every file must follow these rules exactly:

### File header (every .py file must start with this)
```python
"""
module_name.py
==============
Brief description of what this module does.

Input:  What it takes in
Output: What it produces
Logs:   logs/module_name.log

Dependencies:
    - external_library (purpose)
    - another_library (purpose)

Author: VideoForge
Version: 1.0
"""
```

### Function docstrings (every function must have one)
```python
def generate_script(job_id: str, topic: str, config: dict) -> dict:
    """
    Generate a structured video script using Claude API.
    
    Args:
        job_id (str): Unique job identifier e.g. '001'
        topic (str): Video topic e.g. 'Why phone chargers get warm'
        config (dict): Loaded config.json contents
    
    Returns:
        dict: {
            'success': bool,
            'output_path': str,  # path to saved JSON if success
            'error': str         # error message if failed
        }
    
    Raises:
        ValueError: If topic is empty
        APIError: If Claude API call fails after retries
    """
```

### Type hints (all function signatures must have them)
```python
def assemble_video(
    job_id: str,
    audio_path: str,
    images_dir: str,
    music_path: str,
    config: dict
) -> dict:
```

### Constants (never hardcode values — use config or constants)
```python
# WRONG
response = client.messages.create(model="claude-sonnet-4-6", max_tokens=1000)

# CORRECT
model = config['script']['model']
max_tokens = config['script']['max_tokens']
response = client.messages.create(model=model, max_tokens=max_tokens)
```

### Imports (grouped and ordered)
```python
# 1. Standard library
import os
import json
import time
from datetime import datetime
from pathlib import Path

# 2. Third-party libraries
import anthropic
import requests
from dotenv import load_dotenv

# 3. Local modules
from database import update_job_status
from utils.logger import setup_logger
```

---

## 9. Database Schema

```sql
-- jobs table: one row per video
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,       -- e.g. '001', '002'
    topic           TEXT NOT NULL,          -- e.g. 'Why phone chargers get warm'
    bucket          TEXT,                   -- elec / infra / vehicle / flaw
    hook_style      TEXT,                   -- shocking_fact / wrong_assumption / nobody_talks
    status          TEXT DEFAULT 'queued',  -- see status flow below
    error_module    TEXT,                   -- which module failed
    error_message   TEXT,                   -- what went wrong
    script_path     TEXT,                   -- output/scripts/001.json
    audio_path      TEXT,                   -- output/audio/001.mp3
    images_dir      TEXT,                   -- output/images/001/
    raw_video_path  TEXT,                   -- output/videos/001_raw.mp4
    final_video_path TEXT,                  -- output/videos/001_captioned.mp4
    thumbnail_path  TEXT,                   -- output/thumbnails/001.jpg
    metadata_path   TEXT,                   -- output/metadata/001.json
    tiktok_url      TEXT,                   -- published TikTok URL
    youtube_url     TEXT,                   -- published YouTube URL
    tiktok_video_id TEXT,
    youtube_video_id TEXT,
    duration_seconds REAL,                  -- audio duration
    word_count      INTEGER,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- analytics table: performance data pulled from APIs
CREATE TABLE IF NOT EXISTS analytics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT REFERENCES jobs(id),
    platform        TEXT,                   -- tiktok / youtube
    views           INTEGER DEFAULT 0,
    likes           INTEGER DEFAULT 0,
    comments        INTEGER DEFAULT 0,
    shares          INTEGER DEFAULT 0,
    watch_time_avg  REAL,                   -- average watch time in seconds
    pulled_at       TEXT DEFAULT (datetime('now'))
);
```

### Job Status Flow

```
queued
  → scripting       (script_engine running)
  → voiced          (voice_engine running)
  → imaging         (image_engine running)
  → assembling      (assembly_engine running)
  → captioning      (caption_engine running)
  → metadata        (metadata + thumbnail running)
  → review          ← HUMAN REVIEW GATE — nothing uploads until approved
  → uploading       (upload_engine running)
  → posted          (live on both platforms)
  → failed          (any module failed — check error_module + error_message)
```

---

## 10. config.json — Complete Parameter Reference

```json
{
  "channel": {
    "name": "The Engineering Brief",
    "handle": "HowThingsWorkEng",
    "niche": "engineering",
    "target_length_seconds": 70,
    "platforms": ["tiktok", "youtube_shorts"]
  },

  "script": {
    "model": "claude-sonnet-4-6",
    "max_tokens": 1000,
    "temperature": 0.7,
    "word_count_target": 175,
    "hook_style": "shocking_fact",
    "images_to_generate": 8,
    "language": "en",
    "prompt_file": "prompts/script_prompt.txt"
  },

  "voice": {
    "voice_id": "SET_IN_ENV",
    "stability": 0.65,
    "similarity_boost": 0.80,
    "style_exaggeration": 0.20,
    "chunk_by_section": true,
    "output_format": "mp3_44100_128"
  },

  "visuals": {
    "model": "leonardo-diffusion-xl",
    "style_preset_id": "SET_AFTER_TESTING",
    "negative_prompt": "text, watermarks, logos, faces, blurry, low quality, nsfw",
    "width": 1080,
    "height": 1920,
    "guidance_scale": 7,
    "num_inference_steps": 30,
    "num_images": 1
  },

  "video": {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "codec": "h264",
    "bitrate": "8000k",
    "transition_duration": 0.3,
    "music_volume_db": -18,
    "voice_volume_db": 0
  },

  "captions": {
    "whisper_model": "base",
    "font_file": "assets/fonts/Arial-Bold.ttf",
    "font_size": 56,
    "color": "white",
    "stroke_color": "black",
    "stroke_width": 3,
    "position_y_percent": 0.72,
    "max_chars_per_line": 32,
    "max_words_per_line": 4
  },

  "thumbnail": {
    "frame_capture_at_seconds": 5,
    "overlay_template": "assets/thumbnail_template/overlay.png",
    "width": 1080,
    "height": 1920
  },

  "metadata": {
    "prompt_file": "prompts/metadata_prompt.txt",
    "hashtag_count": 10,
    "description_max_chars": 150,
    "youtube_description_max_chars": 500,
    "default_hashtags": [
      "#engineering",
      "#howthingswork",
      "#science",
      "#learnontiktok",
      "#education"
    ]
  },

  "posting": {
    "tiktok_post_times": ["07:00", "13:00", "19:00"],
    "youtube_post_times": ["08:00", "14:00", "20:00"],
    "timezone": "Europe/Skopje",
    "batch_size_per_week": 5,
    "auto_post_after_review": false
  },

  "logging": {
    "level": "DEBUG",
    "max_log_size_mb": 50,
    "keep_logs_days": 30
  }
}
```

---

## 11. .env Template

```
# Anthropic
ANTHROPIC_API_KEY=

# ElevenLabs
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=

# Leonardo.AI
LEONARDO_API_KEY=

# Pexels
PEXELS_API_KEY=

# TikTok (after developer approval)
TIKTOK_CLIENT_KEY=
TIKTOK_CLIENT_SECRET=
TIKTOK_ACCESS_TOKEN=

# YouTube (file path to downloaded JSON)
YOUTUBE_CLIENT_SECRETS_FILE=client_secrets.json

# Flask
FLASK_SECRET_KEY=change_this_to_random_string
FLASK_PORT=5000
```

**Rules for .env:**
- Never log any value from .env
- Never print any value from .env
- Never commit .env to git
- Load with `load_dotenv()` at the top of every file that needs it
- Access with `os.getenv('KEY_NAME')` — never hardcode

---

## 12. Build Phases — Checklist

Build in this exact order. Do not start the next phase until the current one passes its test.

### Phase 1 — Foundation (Days 1–2)
- [ ] Create all directories from File Structure section
- [ ] Create requirements.txt with all dependencies
- [ ] Create config.json with all parameters from Section 10
- [ ] Create .env with all keys from Section 11
- [ ] Create database.py with schema from Section 9
- [ ] Create utils/logger.py with setup_logger from Section 6
- [ ] Create tests/test_connections.py that pings all 4 APIs
- [ ] **PASS CONDITION:** `python tests/test_connections.py` → all APIs return OK

### Phase 2 — Script Engine (Days 3–4)
- [ ] Create modules/script_engine.py
- [ ] Load prompt from prompts/script_prompt.txt
- [ ] Call Claude API with retry logic
- [ ] Parse response into structured JSON
- [ ] Save to output/scripts/NNN.json
- [ ] Update job status in DB
- [ ] Full logging throughout
- [ ] **PASS CONDITION:** `python main.py generate-script "Why phone chargers get warm" --bucket elec` → creates output/scripts/001.json

### Phase 3 — Voice Engine (Days 5–6)
- [ ] Create modules/voice_engine.py
- [ ] Read script JSON
- [ ] Split into 3 chunks if chunk_by_section=true
- [ ] Call ElevenLabs for each chunk with retry
- [ ] Concatenate MP3s with pydub
- [ ] Save to output/audio/NNN.mp3
- [ ] Record duration_seconds in DB
- [ ] **PASS CONDITION:** `python main.py generate-voice 001` → creates output/audio/001.mp3, plays correctly

### Phase 4 — Image Engine (Days 7–8)
- [ ] Create modules/image_engine.py
- [ ] Read visual_brief from script JSON
- [ ] Append style_preset_id and negative_prompt to each prompt
- [ ] Call Leonardo.AI for each with polling until done
- [ ] Download all images to output/images/NNN/
- [ ] Verify all 8 images exist before marking done
- [ ] **PASS CONDITION:** `python main.py generate-images 001` → output/images/001/ contains 8 images

### Phase 5 — Assembly Engine (Days 9–12)
- [ ] Create modules/assembly_engine.py
- [ ] Load audio + get duration
- [ ] Calculate image_duration = audio_duration / num_images
- [ ] Create slideshow with crossfade transitions
- [ ] Mix background music at configured volume
- [ ] Export 1080×1920 MP4 at configured bitrate
- [ ] **PASS CONDITION:** `python main.py assemble 001` → creates output/videos/001_raw.mp4, plays correctly

### Phase 6 — Caption Engine (Day 13)
- [ ] Create modules/caption_engine.py
- [ ] Run Whisper on MP3 → word-level timestamps
- [ ] Group into caption blocks at max_chars_per_line
- [ ] Burn captions into video with configured style
- [ ] Export output/videos/NNN_captioned.mp4
- [ ] **PASS CONDITION:** `python main.py add-captions 001` → captioned video renders correctly, captions readable

### Phase 7 — Metadata + Thumbnail (Day 14)
- [ ] Create modules/metadata_engine.py
- [ ] Create modules/thumbnail_engine.py
- [ ] Metadata: call Claude API with metadata_prompt.txt → parse → save JSON
- [ ] Thumbnail: capture frame at configured second → apply overlay → save JPG
- [ ] **PASS CONDITION:** Both files exist in output/metadata/ and output/thumbnails/

### Phase 8 — Upload Engine (Days 15–17)
- [ ] Create modules/upload_engine.py
- [ ] Implement YouTube OAuth flow (first-run browser auth)
- [ ] Implement YouTube resumable upload
- [ ] Implement TikTok OAuth + upload
- [ ] Store published URLs in DB
- [ ] **PASS CONDITION:** `python main.py upload 001` → video live on both platforms, URLs stored in DB

### Phase 9 — Web Dashboard (Days 18–22)
- [ ] Create app.py with Flask
- [ ] Create templates/dashboard.html — job queue with status badges
- [ ] Create templates/job_detail.html — video preview + approve button
- [ ] Create templates/config_editor.html — editable form for config.json
- [ ] Create templates/stats.html — basic metrics
- [ ] Implement review gate — status stuck at 'review' until Approve clicked
- [ ] **PASS CONDITION:** `python app.py` → localhost:5000 shows dashboard, full pipeline triggerable from browser

### Phase 10 — Automation (Days 23–28)
- [ ] Create scheduler.py with APScheduler
- [ ] Batch job: every Sunday 22:00 CET — process N topics from queue
- [ ] Create webhook.py — Make.com trigger endpoint
- [ ] Create modules/analytics_engine.py — pull stats from both APIs
- [ ] Analytics scheduled: every Monday 06:00 — pull previous week's stats
- [ ] **PASS CONDITION:** Topics added to queue automatically trigger pipeline without manual intervention

---

## 13. CLI Commands Reference

```bash
# Test all API connections (run this first every session)
python main.py test-connections

# Full pipeline for one topic (runs all phases in sequence)
python main.py pipeline "Why phone chargers get warm" --bucket elec

# Run individual phases (for debugging or re-running failed steps)
python main.py generate-script "topic" --bucket elec --hook shocking_fact
python main.py generate-voice 001
python main.py generate-images 001
python main.py assemble 001
python main.py add-captions 001
python main.py generate-metadata 001
python main.py generate-thumbnail 001
python main.py upload 001

# Batch process queue
python main.py batch --count 5

# Show job status
python main.py status 001

# Show all jobs
python main.py list-jobs

# Start web dashboard
python app.py
```

---

## 14. Module Connection Map

```
topic_string + bucket + hook_style
    └─→ script_engine.py
            ├─→ output/scripts/NNN.json
            │       ├─→ voice_engine.py → output/audio/NNN.mp3
            │       ├─→ image_engine.py → output/images/NNN/img_01..08.png
            │       └─→ metadata_engine.py → output/metadata/NNN.json
            │
            └─→ [voice + images + music] → assembly_engine.py
                    └─→ output/videos/NNN_raw.mp4
                            └─→ caption_engine.py
                                    └─→ output/videos/NNN_captioned.mp4
                                            ├─→ thumbnail_engine.py → output/thumbnails/NNN.jpg
                                            └─→ [REVIEW GATE]
                                                    └─→ upload_engine.py
                                                            ├─→ TikTok (URL stored in DB)
                                                            └─→ YouTube (URL stored in DB)
                                                                    └─→ analytics_engine.py
                                                                            └─→ analytics table in DB

config.json ─────────────────────────→ every module (loaded at startup)
.env ────────────────────────────────→ every module (loaded at startup, never logged)
```

---

## 15. Parameters Left to Set Later

These intentionally have placeholder values in config.json and .env:

| Parameter | Where | Why to wait |
|---|---|---|
| `style_preset_id` | config.json visuals | Test 5 Leonardo.AI styles manually on real scripts first. Pick the most consistent one. Never change it after locking. |
| `ELEVENLABS_VOICE_ID` | .env | Listen to 5–10 voices in ElevenLabs Voice Library. Pick the most authoritative-sounding narrator. Copy the Voice ID from the URL. |
| `frame_capture_at_seconds` | config.json thumbnail | Watch first 5 assembled videos. Find which second has the most visually striking frame. |
| `tiktok_post_times` | config.json posting | After 4 weeks check TikTok analytics for when your audience is most active. Adjust times accordingly. |
| `music_volume_db` | config.json video | Depends on the track. Start at -18. After assembling first video, listen and adjust ±3dB. |
| `transition_duration` | config.json video | Start at 0.3. If video feels rushed increase to 0.4. If slow decrease to 0.25. |
| `TIKTOK_CLIENT_KEY` | .env | After TikTok developer application is approved (7–10 days). |
| `TIKTOK_CLIENT_SECRET` | .env | Same as above. |

---

## 16. Security Rules

1. Never log API keys, tokens, or secrets — not even the first 4 characters
2. Never print API keys to console
3. Always load secrets from .env — never hardcode in any file
4. Add .env to .gitignore before the first commit
5. Add logs/ and output/ to .gitignore
6. client_secrets.json must also be in .gitignore
7. If a key is accidentally logged, rotate it immediately

---

## 17. Content Rules

These rules protect monetization and must be enforced in the script prompt:

1. All content must be original — no copying from other sources
2. No movie clips, TV clips, or copyrighted footage
3. No copyrighted music — only royalty-free tracks from assets/music/
4. No recognisable brand logos in any generated image
5. No political content
6. No health/medical claims
7. All engineering facts must be verifiable — the owner reviews for accuracy
8. Videos must be at minimum 60 seconds to qualify for YouTube monetization
9. All images generated at 1080×1920 (portrait) for Shorts/TikTok format

---

## 18. Accounts and Credentials Summary

| Service | Account | Purpose |
|---|---|---|
| Google / YouTube | trpeski.jordan@gmail.com | YouTube channel owner, API auth |
| YouTube channel | The Engineering Brief @HowThingsWorkEng | Content channel |
| Google Cloud project | VideoForge | API project |
| TikTok | @HowThingsWorkEng | Content + growth |
| Anthropic | TBD | Script + metadata generation |
| ElevenLabs | TBD | Voiceover |
| Leonardo.AI | TBD | Image generation |
| Pexels | TBD | B-roll footage |
| Make.com | TBD | Automation webhooks |

---

*VideoForge CLAUDE.md v1.0 — Last updated April 2026*
*This document must be kept up to date as the project evolves.*
*When Claude Code asks "what should I do?", the answer is in this file.*

---

## 19. Dashboard — Full Specification (app.py + templates/)

The dashboard runs at localhost:5000 via Flask. It is the only interface the owner uses after initial setup. Every action — adding jobs, editing config, reviewing videos, reading logs — happens here. No terminal needed for day-to-day operation.

### Navigation structure

8 pages, always visible in the left sidebar:

```
Main
  ├── Overview          /                    Live pipeline status + review gate
  ├── Job queue         /jobs                All jobs with status + filters
  └── New job           /jobs/new            Add topic to queue

Tools
  ├── Config editor     /config              Edit all config.json parameters in UI
  ├── Prompt editor     /prompts             Edit script_prompt.txt + metadata_prompt.txt
  └── Log viewer        /logs                Live log tail with filters

Insights
  ├── Analytics         /analytics           Views, subs, revenue, top videos by bucket
  └── API health        /health              Status + latency of all 6 APIs + re-auth
```

---

### Page 1 — Overview (/)

**Purpose:** First thing you see. Shows what's happening right now.

**Components:**

Stat strip (4 cards):
- Total videos made (count from jobs DB)
- Posted this week (jobs with status=posted AND created_at >= Monday)
- In queue (jobs with status=queued)
- Awaiting review (jobs with status=review) — shown in amber if > 0

Pipeline status card:
- Shows live stage progress for the currently-running job
- Each stage row: colored dot (done=green / running=blue / pending=gray / failed=red) + stage name + status message + elapsed time
- Auto-refreshes every 5 seconds via JS fetch to /api/pipeline-status

Review gate card (only shown when a job is at status=review):
- Shows topic, word count, audio duration
- Two buttons: "Approve + schedule" → sets status=uploading and triggers upload_engine
- "Reject — redo" → sets status=queued and clears all output files for that job so it reruns from scratch

---

### Page 2 — Job queue (/jobs)

**Purpose:** Full list of all jobs, filterable by status.

**Components:**

Filter pills: All / Queued / Running / Review / Posted / Failed

Job table rows (one per job):
- Job ID (monospace, 3 digits)
- Topic (truncated if too long)
- Status badge (color-coded)
- Created date
- Clicking a row → goes to job detail page /jobs/NNN

Job detail page (/jobs/NNN):
- Topic, bucket, hook style
- Stage-by-stage timeline with timestamps and durations
- Script preview — shows full generated script text
- Visual brief — shows all 8 generated image prompts
- Generated metadata — shows title, description, hashtags
- Video preview — HTML5 video player if captioned video exists
- Thumbnail preview
- Published URLs if posted
- Raw log output filtered to this job ID
- If status=failed: shows error_module + error_message prominently with a "Retry from this stage" button
- If status=review: shows Approve + Reject buttons (same as Overview)

---

### Page 3 — New job (/jobs/new)

**Purpose:** Add a topic to the pipeline without using the terminal.

**Fields:**
- Topic (text input, required)
- Bucket (dropdown: Electrical / Infrastructure / Vehicles / The Flaw)
- Hook style (dropdown: Shocking fact / Wrong assumption / Nobody talks about this)
- Run mode (dropdown):
  - "Add to queue" — adds with status=queued, runs in next batch
  - "Run pipeline now" — immediately triggers full pipeline
  - "Script only" — runs script_engine only, pauses at status=scripted for review before continuing

Submit button: "Add job"

Bulk add section (below the form):
- Textarea — paste multiple topics, one per line
- Same bucket and hook style applied to all
- "Add N jobs" button

---

### Page 4 — Config editor (/config)

**Purpose:** Edit all config.json parameters without touching files. No coding required.

**Layout:** Grouped sections matching config.json structure:

Groups shown:
- Script (Claude API) — model, temperature, word_count_target, images_to_generate
- Voice (ElevenLabs) — stability, similarity_boost, style_exaggeration, chunk_by_section
- Visuals (Leonardo.AI) — style_preset_id, guidance_scale, num_inference_steps, negative_prompt
- Video assembly — transition_duration, music_volume_db, fps, bitrate
- Captions — font_size, stroke_width, position_y_percent, whisper_model, max_chars_per_line
- Thumbnail — frame_capture_at_seconds
- Posting — batch_size_per_week, timezone, hashtag_count, tiktok_post_times, youtube_post_times

Each parameter row shows:
- Parameter name (monospace)
- Input field (pre-filled with current value from config.json)
- Short note explaining what it does and safe range

Bottom buttons:
- "Save config" — writes changes back to config.json
- "Reset to defaults" — restores original values with confirmation prompt

**Important:** Config changes take effect on the NEXT job run. Currently-running jobs use the config that was loaded when they started.

---

### Page 5 — Prompt editor (/prompts)

**Purpose:** Edit the master script prompt and metadata prompt directly in the browser. This is the most powerful page — changing the script prompt changes the tone, structure, and quality of every future video.

**Components:**

Script prompt editor:
- Full textarea showing current contents of prompts/script_prompt.txt
- Syntax highlighting not required — plain monospace textarea is fine
- "Save prompt" button — overwrites the file
- "Test with last topic" button — runs script_engine on the most recent job's topic using the current (unsaved) prompt text and shows the result in a preview panel below
- "Reset to default" button with confirmation

Metadata prompt editor:
- Full textarea showing current contents of prompts/metadata_prompt.txt
- "Save prompt" button
- "Test with last topic" button — generates metadata preview

Prompt variable reference (shown below editors):
- Table of all available variables: {topic}, {bucket}, {hook_style}, {word_count_target}, {hashtag_count}, etc.
- Shows current value of each variable so you can see what will be injected

**Why this page matters:** If videos feel robotic, the fix is in the script prompt. If titles aren't getting clicks, fix the metadata prompt. The owner can iterate on both without touching any Python code.

---

### Page 6 — Log viewer (/logs)

**Purpose:** Read any log file and debug failures without opening the terminal.

**Components:**

Filter controls:
- Level filter pills: All / INFO / WARNING / ERROR / DEBUG
- Module dropdown: All modules / script_engine / voice_engine / image_engine / assembly_engine / caption_engine / upload_engine / errors only
- Job ID filter: text input — filter to show only lines containing [JOB NNN]
- Auto-refresh toggle: ON/OFF — when ON, fetches new log lines every 3 seconds

Log display:
- Monospace font, small text
- Color-coded by level: INFO=blue / WARNING=amber / ERROR=red / DEBUG=gray
- Newest lines at top (reverse chronological)
- Shows last 200 lines by default
- "Load more" button to show older lines

Quick access buttons (top right):
- "Open errors.log" — jumps to errors-only view
- "Clear logs" — archives current logs to logs/archive/ with timestamp and starts fresh (with confirmation)

**What this replaces:** Opening Terminal → navigating to folder → running tail -f logs/errors.log. The owner sees the same information in the browser.

---

### Page 7 — Analytics (/analytics)

**Purpose:** Track channel performance and understand which content works best.

**Data sources:** YouTube Analytics API + TikTok API, pulled every Monday at 06:00 and stored in analytics table.

**Components:**

Stat strip (4 cards):
- Total views (all time, both platforms combined)
- Total subscribers / followers
- Estimated revenue this month (YouTube AdSense estimate based on avg CPM)
- Average video completion rate

Top performing videos table:
- Rank, topic, platform (YT+TT badge), total views, completion rate
- Sorted by views descending
- Shows last 30 days

Performance by content bucket:
- Horizontal bar chart — avg views per video for each bucket
- Updates automatically as more data comes in
- Use this to decide which bucket to post more of

Platform comparison:
- YouTube vs TikTok: avg views, completion rate, subscriber gain per video
- Helps decide where to focus promotion effort

Post timing heatmap:
- Shows which days/times got highest views
- Useful for adjusting tiktok_post_times and youtube_post_times in config

"Refresh analytics now" button — manually triggers analytics_engine.py pull outside the scheduled Monday run.

---

### Page 8 — API health (/health)

**Purpose:** Check all 6 API connections before starting a batch session. Catch token expiry, credit exhaustion, and rate limits before they crash a pipeline run.

**Components:**

API status table — one row per service:
- Service name
- Status badge: OK (green) / Token expired (red) / Rate limited (amber) / Unreachable (red)
- Details: plan tier, credits/quota remaining, token expiry date
- Response latency (ms)
- "Test" button — pings the API and updates the row

Services shown:
- Claude API — shows model availability
- ElevenLabs — shows plan, characters remaining this month
- Leonardo.AI — shows credits remaining today (resets daily)
- Pexels — always free, just confirms connectivity
- YouTube API — shows OAuth token validity + daily quota remaining (10,000 units/day)
- TikTok API — shows access token expiry (tokens expire frequently — this is the most common failure)

"Run full connection test" button:
- Executes tests/test_connections.py
- Shows pass/fail for each API
- Displays the same output you'd see in terminal, in the browser

Re-auth flow for TikTok:
- When TikTok token is expired, a "Re-auth" button appears
- Clicking it opens the TikTok OAuth flow in a new tab
- After completing auth, the new token is saved to .env automatically

**Rule:** Always check this page before starting a Sunday batch session. A failed TikTok token discovered mid-batch wastes hours.

---

### Flask routes summary

```python
# Main pages
GET  /                        → overview
GET  /jobs                    → job queue
GET  /jobs/new                → new job form
POST /jobs/new                → create job(s)
GET  /jobs/<job_id>           → job detail
POST /jobs/<job_id>/approve   → approve for upload
POST /jobs/<job_id>/reject    → reject and requeue
GET  /config                  → config editor
POST /config                  → save config.json
GET  /prompts                 → prompt editor
POST /prompts/script          → save script_prompt.txt
POST /prompts/metadata        → save metadata_prompt.txt
GET  /logs                    → log viewer
GET  /analytics               → analytics page
GET  /health                  → API health page
POST /health/reauth/tiktok    → trigger TikTok re-auth

# API endpoints (called by JS for live updates)
GET  /api/pipeline-status     → current running job stage + progress (JSON)
GET  /api/logs                → last N log lines with filters (JSON)
GET  /api/analytics           → analytics data (JSON)
GET  /api/health              → all API statuses (JSON)
POST /api/test-prompt         → test prompt with last topic, return preview (JSON)
POST /api/refresh-analytics   → trigger analytics pull now

# Webhook (for Make.com automation)
POST /webhook/new-topic       → add topic from Google Sheets trigger
```

---

### Dashboard security note

The dashboard has no login — it only runs on localhost:5000, never exposed to the internet. If you ever want to access it remotely (from another device on the same network), Flask can be configured to bind to 0.0.0.0, but add a simple password via Flask-Login before doing so.

