# VideoForge — Codex Instructions

## Project Overview

You are building VideoForge — an automated AI video production pipeline for an educational engineering YouTube and TikTok channel called "The Engineering Brief" (@HowThingsWorkEng). The owner has an electrical engineering background and creates faceless short-form videos (60–90 seconds) explaining how everyday engineering and technology works.

The system takes a topic string and automatically produces a fully edited, captioned, SEO-tagged video ready to post on TikTok and YouTube Shorts. It runs locally on the owner's computer and is operated through a web dashboard at localhost:5000.

---

## Build Instructions

### General rules
- Build one phase at a time. Do not start the next phase until the current one passes its test
- Every phase must have a clear pass condition — a command that proves it works
- If a phase fails its test, fix it before moving on
- Never hardcode API keys, model names, temperatures, or any tunable value — always read from config.json or .env
- Always load .env with python-dotenv at the top of every file that needs API keys
- Never log, print, or expose any value from .env

### Phase order
Build in this exact sequence:
1. Foundation — project structure, config, database, logger, connection test
2. Script engine — Codex API → structured script JSON
3. Voice engine — ElevenLabs → MP3
4. Image engine — Leonardo.AI → image set
5. Assembly engine — MoviePy → raw video
6. Caption engine — Whisper → captioned video
7. Metadata + thumbnail engine — Codex API → SEO data + PIL → thumbnail
8. Upload engine — YouTube Data API v3 + TikTok Content Posting API
9. Web dashboard — Flask app at localhost:5000
10. Automation — APScheduler batch runs + Make.com webhook + analytics pull

---

## API Keys Available Right Now

Only the Anthropic API key is available at this stage. All other keys will be added when their phase is reached. Write the code so it checks if a key exists before using it and skips gracefully if it does not.

Current .env:
```
ANTHROPIC_API_KEY=sk-ant-...        ← available now
ELEVENLABS_API_KEY=                 ← leave blank, Phase 3
ELEVENLABS_VOICE_ID=                ← leave blank, Phase 3
LEONARDO_API_KEY=                   ← leave blank, Phase 4
PEXELS_API_KEY=                     ← leave blank, Phase 4
TIKTOK_CLIENT_KEY=                  ← leave blank, Phase 8
TIKTOK_CLIENT_SECRET=               ← leave blank, Phase 8
TIKTOK_ACCESS_TOKEN=                ← leave blank, Phase 8
YOUTUBE_CLIENT_SECRETS_FILE=client_secrets.json  ← leave blank, Phase 8
FLASK_SECRET_KEY=changethis
FLASK_PORT=5000
```

---

## Logging Instructions

Every module must have its own log file plus write to a shared main.log and errors.log. Use this exact setup at the top of every module:

```python
import logging
import os

def setup_logger(module_name: str) -> logging.Logger:
    os.makedirs('logs', exist_ok=True)
    logger = logging.getLogger(module_name)
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger
    formatter = logging.Formatter(
        '%(asctime)s | %(name)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    for path, level in [
        (f'logs/{module_name}.log', logging.DEBUG),
        ('logs/main.log', logging.INFO),
        ('logs/errors.log', logging.ERROR),
    ]:
        h = logging.FileHandler(path)
        h.setLevel(level)
        h.setFormatter(formatter)
        logger.addHandler(h)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)
    return logger
```

Every module must log:
- Start of job with job ID and topic
- Every API call before it happens (endpoint, key parameters)
- Every API response (success, response time)
- Every file created (path, size in MB)
- Stage completion with total elapsed time
- All warnings (rate limits, slow responses, retries)
- All errors with full traceback using exc_info=True

Log format for every line:
```
2026-04-10 14:32:01 | script_engine | INFO | [JOB 001] Calling Codex API — model: Codex-sonnet-4-6
```

---

## Error Handling Instructions

### API calls — always retry with backoff
```python
import time

def call_with_retry(func, max_retries=3, backoff=5):
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait = backoff * (2 ** attempt)
            logger.warning(f"Attempt {attempt+1} failed — retrying in {wait}s: {e}")
            time.sleep(wait)
```

### Module functions — always return a result dict
Every module's main run function must return this structure:
```python
# Success
return {"success": True, "output_path": "/path/to/output"}

# Failure
return {"success": False, "error": "Human readable error message"}
```

### Pipeline — stop on first failure
If any module returns success: False, the pipeline stops immediately. Update the job status in the database to "failed" with the module name and error message stored. Never silently continue past a failure.

---

## Code Style Instructions

### Every file must start with a header
```python
"""
module_name.py
==============
What this module does in one sentence.

Input:  What it takes in
Output: What it produces
Logs:   logs/module_name.log
"""
```

### Every function must have a docstring
```python
def generate_script(job_id: str, topic: str, config: dict) -> dict:
    """
    Generate a structured video script using Codex API.

    Args:
        job_id: Unique job identifier e.g. '001'
        topic: Video topic e.g. 'Why phone chargers get warm'
        config: Loaded config.json contents

    Returns:
        dict with 'success' bool and either 'output_path' or 'error'
    """
```

### All functions must have type hints
### Never hardcode any value that belongs in config.json
### Group imports: stdlib first, third-party second, local third

---

## Database Instructions

Use SQLite. All database operations go in database.py. The jobs table tracks every video through the pipeline:

```sql
CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    topic             TEXT NOT NULL,
    bucket            TEXT,
    hook_style        TEXT,
    status            TEXT DEFAULT 'queued',
    error_module      TEXT,
    error_message     TEXT,
    script_path       TEXT,
    audio_path        TEXT,
    images_dir        TEXT,
    raw_video_path    TEXT,
    final_video_path  TEXT,
    thumbnail_path    TEXT,
    metadata_path     TEXT,
    tiktok_url        TEXT,
    youtube_url       TEXT,
    duration_seconds  REAL,
    word_count        INTEGER,
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS analytics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT REFERENCES jobs(id),
    platform        TEXT,
    views           INTEGER DEFAULT 0,
    likes           INTEGER DEFAULT 0,
    comments        INTEGER DEFAULT 0,
    shares          INTEGER DEFAULT 0,
    watch_time_avg  REAL,
    pulled_at       TEXT DEFAULT (datetime('now'))
);
```

Status flow in order:
queued → scripting → voiced → imaging → assembling → captioning → metadata → review → uploading → posted → failed

The review status is a manual gate. Nothing uploads until the owner clicks Approve in the dashboard.

---

## Dashboard Instructions (Phase 9)

Build a Flask web app at localhost:5000 with these 8 pages:

**Overview (/)** — live pipeline status showing current job stage progress with colored dots (green=done, blue=running, gray=pending, red=failed), stat strip (total videos, posted this week, in queue, awaiting review), and a review gate card when a job needs approval with Approve and Reject buttons. Auto-refresh pipeline status every 5 seconds.

**Job queue (/jobs)** — table of all jobs filterable by status badge. Clicking a job opens a detail page (/jobs/NNN) showing the full generated script, all 8 image prompts, metadata preview, HTML5 video player if the video exists, thumbnail preview, published URLs, and log lines filtered to that job ID. Failed jobs show the error module and message with a Retry from this stage button.

**New job (/jobs/new)** — form with topic text input, bucket dropdown (Electrical / Infrastructure / Vehicles / The Flaw), hook style dropdown (Shocking fact / Wrong assumption / Nobody talks about this), and run mode dropdown (Add to queue / Run now / Script only). Also a bulk add textarea for pasting multiple topics at once.

**Config editor (/config)** — editable form showing every parameter from config.json grouped into sections: Script, Voice, Visuals, Video assembly, Captions, Thumbnail, Posting. Each parameter shows the name in monospace, an input field with the current value, and a short note on what it does. Save button writes to config.json. Reset button restores defaults with a confirmation prompt.

**Prompt editor (/prompts)** — two textareas showing the full contents of prompts/script_prompt.txt and prompts/metadata_prompt.txt. Save button for each. Test button runs the prompt against the last job topic and shows a preview of the output below. A reference table shows all available template variables and their current values.

**Log viewer (/logs)** — live log output with filter controls for level (INFO / WARNING / ERROR / DEBUG), module dropdown, and job ID text filter. Color coded: INFO=blue, WARNING=amber, ERROR=red, DEBUG=gray. Auto-refresh toggle updates every 3 seconds. Quick access button for errors.log only.

**Analytics (/analytics)** — stat strip with total views, subscribers, estimated revenue, and average completion rate. Top performing videos table sorted by views. Performance by content bucket as a horizontal bar chart so the owner can see which bucket gets the most views. Platform comparison between YouTube and TikTok.

**API health (/health)** — one row per API (Codex, ElevenLabs, Leonardo.AI, Pexels, YouTube, TikTok) showing status badge, details like credits remaining and token expiry, latency in ms, and a Test button that pings the API live. A Run full connection test button at the bottom. A Re-auth button appears next to TikTok when its token has expired.

---

## Channel and Account Details

- Channel name: The Engineering Brief
- YouTube handle: @HowThingsWorkEng
- TikTok handle: @HowThingsWorkEng
- Content: Educational engineering — how everyday things work
- Content buckets: Electrical, Infrastructure, Vehicles, The Flaw
- Target video length: 60–90 seconds
- Format: Faceless — AI voiceover + AI images + burn-in captions
- Primary monetization: YouTube AdSense
- Owner location: Skopje, Macedonia
- Posting target: 5 videos per week

---

## Script Format

Every generated script must follow this exact structure:

- HOOK (0–3 sec): One sentence. Surprising fact or challenge a wrong belief. Under 15 words.
- SETUP (3–15 sec): What most people think. 2–3 sentences.
- EXPLANATION (15–55 sec): The real engineering answer. Simple language. One analogy.
- PAYOFF (55–75 sec): The mind-blowing implication. 1–2 sentences.
- CTA (optional): Follow for more style, 3–5 seconds.

The Codex API call for script generation must return structured JSON with these keys:
hook, setup, explanation, payoff, cta, full_script, visual_brief (array of 8 image prompts), metadata_hints

---

## Content Rules

These protect monetization and must be enforced in the script prompt:
- All content must be original — no copying from other sources
- No movie clips, TV clips, or copyrighted footage
- No copyrighted music — only royalty-free tracks from assets/music/
- No recognisable brand logos in any generated image
- No political content, no health or medical claims
- All engineering facts must be verifiable
- Videos must be minimum 60 seconds for YouTube monetization
- All images generated at 1080x1920 portrait for Shorts and TikTok

---

## Parameters Left Intentionally Blank

These are placeholders in config.json that the owner will fill in after testing:
- visuals.style_preset_id — test 5 Leonardo.AI styles, pick the most consistent, never change it
- voice.voice_id / ELEVENLABS_VOICE_ID — pick after listening to voices in ElevenLabs Voice Library
- thumbnail.frame_capture_at_seconds — set after watching first 5 assembled videos
- posting.tiktok_post_times — adjust after 4 weeks of analytics data
- video.music_volume_db — tune by ear after first assembly, -18 is the starting default

Do not try to fill these in. Leave them as placeholder strings and add a comment in the config explaining why.

---

*Drop this file in the root of the videoforge/ folder.*
*Codex reads AGENTS.md automatically at the start of every session.*
*Start by saying: "Read AGENTS.md then build Phase 1."*

---

## Phase 11.v1 — Immediate priorities (week 1)

Build this alongside getting the first real videos posted. Every feature here works from day one with zero historical data needed. Nothing in this phase depends on having analytics or a big topic bank.

---

### 11.v1.A — Device sync (build first — do before anything else)

You use two devices. The database is the single most important file in the project — it has every job, every script path, every analytics record. Right now if you switch devices you lose all of it unless you manually copy it.

**How it works:**
Update database.py to read the database path from VIDEOFORGE_DB_PATH in .env, falling back to videoforge.db in the project root if not set. This lets the database live anywhere — a Dropbox folder, a Google Drive folder, a USB drive — while the code stays in Git.

**Setup on each device:**
1. Put videoforge.db in a shared Dropbox or Google Drive folder
2. Add to .env on both machines: VIDEOFORGE_DB_PATH=C:/Users/you/Dropbox/VideoForge/videoforge.db
3. Both machines now share the same database automatically

**What syncs between devices:**
- All jobs and their status
- All topic bank entries
- All analytics data
- All scan history

**What stays local per device:**
- .env (API keys — never synced, filled in separately on each machine)
- output/ folder (generated videos and images — large files, regeneratable)
- logs/ folder (local only)
- config.json (stays in Git, same on both)

**Add to .env on both machines:**
```
VIDEOFORGE_DB_PATH=
```
Leave blank to use default local path. Fill in the Dropbox/Drive path to enable sync.

**Future migration path:**
When you add a server later, change VIDEOFORGE_DB_PATH to point to a hosted PostgreSQL or Supabase connection string. The rest of the code stays unchanged.

**Pass condition:** Change VIDEOFORGE_DB_PATH to a different folder path, start app.py, confirm database loads from the new path and all jobs are visible.

---

### 11.v1.B — Priority Alert system (build second — highest immediate value)

The single most valuable feature for a new channel. One viral trending video can generate more views in 48 hours than 10 regular videos combined. This works from day one with no historical data.

**What it does:**
Monitors Google Trends for engineering topics that spike above a threshold. When a spike is detected that matches your channel format, shows a prominent alert in the dashboard with a countdown. You can fast-track the video through the pipeline immediately.

**New database table:**
```sql
CREATE TABLE IF NOT EXISTS trend_scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at      TEXT DEFAULT (datetime('now')),
    topics_found    INTEGER DEFAULT 0,
    new_alerts      INTEGER DEFAULT 0,
    buckets_scanned TEXT,
    status          TEXT DEFAULT 'complete'
);

CREATE TABLE IF NOT EXISTS priority_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    topic           TEXT NOT NULL,
    bucket          TEXT,
    spike_percent   REAL,
    channel_fit     REAL,
    hook_suggestion TEXT,
    reframed_angle  TEXT,
    window_hours    INTEGER DEFAULT 48,
    triggered_at    TEXT DEFAULT (datetime('now')),
    expires_at      TEXT,
    status          TEXT DEFAULT 'active',
    job_id          TEXT,
    dismissed_at    TEXT
);
```

**New module: modules/trend_monitor.py**

Input: config dict
Output: list of priority_alert records saved to database

Process:
1. Query Google Trends using pytrends for seed keywords per bucket from config
2. For each result calculate spike percentage: (last 7 days avg) vs (previous 30 days avg)
3. If spike exceeds priority_alert_threshold (default 150%) send topic to Codex for channel relevance check
4. Codex returns: channel_fit score 1-10, reframed everyday angle, hook suggestion, yes/no on whether it fits the channel
5. If channel_fit >= 7.0 create a priority_alert record with 48-hour expiry window
6. Log the scan to trend_scans table regardless of results

**Seed keywords in config.json — new research section:**
```json
"research": {
  "priority_alert_threshold": 150,
  "priority_alert_fit_minimum": 7.0,
  "fast_track_window_hours": 48,
  "safe_scans_per_hour": 5,
  "safe_scans_per_day": 15,
  "scan_on_startup": true,
  "scan_on_startup_cooldown_hours": 2,
  "notify_email": "",
  "seed_keywords": {
    "elec": ["electrical engineering", "battery", "circuit", "power grid", "electronics failure"],
    "infra": ["bridge engineering", "building collapse", "construction failure", "dam", "skyscraper"],
    "vehicle": ["car engineering", "aircraft failure", "electric vehicle", "train derailment", "engine"],
    "flaw": ["engineering failure", "design flaw", "product recall", "structural failure", "engineering disaster"]
  }
}
```

**Channel relevance check — Codex prompt:**
Send the trending topic plus channel context to Codex. Ask:
- Does this fit "The Engineering Brief" — how everyday things work — score 1-10
- Can it be explained in 70 seconds to a non-engineer — yes/no
- Reframe this trending event as an everyday engineering concept — one sentence
- Suggest a hook line for this reframed angle

Example: "Baltimore bridge collapse" → reframed as "Why engineers build bridges to absorb impact — not resist it"

**Scan history and rate limiting:**
Track every scan in trend_scans table. Before each scan check:
- How many scans in the last hour — block if >= safe_scans_per_hour
- How many scans today — warn if >= safe_scans_per_day
- When was the last scan — if scan_on_startup is true, only auto-scan if last scan was more than scan_on_startup_cooldown_hours ago

**Dashboard changes — add to existing pages:**

Overview page additions:
- Priority Alert banner at the very top when active alerts exist — amber background, shows topic, spike percentage, channel fit score, and countdown timer to window close
- "Fast-track this video" button on the banner — skips the queue and runs the pipeline immediately in foreground
- "Dismiss" button — marks alert as dismissed, removes banner

New Research section in sidebar navigation:
- Trend Scanner page (/research/trends)

**New page: Trend Scanner (/research/trends)**

Scan status card:
```
Last scan: Today 09:14 — 2 alerts found
Scans today: 2 of 15 safe limit
Next auto-scan: on startup if > 2 hours since last
[ Scan now ]
```

Active alerts table — shows all active (non-expired, non-dismissed) alerts:
- Topic (reframed angle)
- Original trending event
- Spike percentage
- Channel fit score
- Time remaining in window
- Fast-track button
- Dismiss button

Scan history table — one row per scan:
- Date and time
- Topics found
- New alerts created
- Clicking a row expands to show which topics were found

**Fast-track pipeline:**
When fast-track is triggered the pipeline runs differently from the normal batch:
- Script generated immediately and shown in browser for review
- Owner reviews script, edits hook if needed, clicks Approve Script
- Voice + Images run in parallel after script approval
- Assembly starts when both complete
- Captions + metadata run immediately
- Simplified review gate — 10-second preview + title, one click approve
- Upload immediately at next optimal time within 6 hours

**Email notification:**
When a Priority Alert fires and notify_email is set in config, send a plain text email via Python's built-in smtplib. No external service needed. Email contains the topic, spike percentage, channel fit score, and dashboard URL.

**CLI commands:**
```bash
# Run a trend scan manually
python main.py scan-trends

# Show active priority alerts
python main.py list-alerts

# Fast-track a specific alert to the pipeline
python main.py fast-track --alert-id 1
```

**Pass conditions for 11.v1.B:**
- python main.py scan-trends runs without error, logs scan to trend_scans table
- If a spike is detected above threshold, priority_alerts record is created with correct expiry
- /research/trends page loads showing scan history and any active alerts
- Scan now button in dashboard triggers a scan and updates the page
- Rate limit correctly blocks scans when hourly limit is reached
- Fast-track button on an alert creates a job and starts the pipeline immediately

---

### 11.v1.C — Similarity detection (build third — cheap and prevents waste)

One Codex API call per new topic. Prevents you from spending pipeline credits making a video you've essentially already made with different wording.

**How it works:**
Every time a topic is added to the queue — whether manually, from fast-track, or from the bulk adder — send it to Codex along with the last 50 topics from the jobs table and topic_bank table. Codex returns a similarity score and the most similar existing topic title.

If similarity is above 70% show a warning before adding:
```
Similar topic detected
"Why USB chargers heat up" is 84% similar to your existing video
"Why phone chargers get warm" (posted 3 weeks ago, 42K views)

[ Add anyway ]  [ Cancel ]  [ Use different angle ]
```

"Use different angle" sends both topics to Codex and asks it to suggest a fresh angle that's distinct from the existing video.

**Where it runs:**
- When adding a job from the New Job form
- When fast-tracking a Priority Alert
- When adding topics from the bulk adder
- Does NOT run in the background — only on explicit add actions

**Add to database.py:**
```sql
ALTER TABLE jobs ADD COLUMN similarity_checked INTEGER DEFAULT 0;
ALTER TABLE jobs ADD COLUMN similar_to_job TEXT;
ALTER TABLE jobs ADD COLUMN similarity_score REAL;
```

**Pass condition:** Adding "Why USB chargers heat up" when "Why phone chargers get warm" exists in jobs table triggers the similarity warning.

---

### 11.v1.D — Archive and topic bank foundation (build fourth)

Simple database changes that lay the foundation for the full topic bank in v2 without building the whole scoring engine yet.

**Database changes:**
```sql
-- Add archive flag to topic_bank
ALTER TABLE topic_bank ADD COLUMN archived INTEGER DEFAULT 0;
ALTER TABLE topic_bank ADD COLUMN archived_at TEXT;
ALTER TABLE topic_bank ADD COLUMN archive_reason TEXT;

-- Add manual topics (not yet scored) support
-- status can now be: pending / scored / queued / made / archived
```

**Dashboard changes:**
- Add Archive button to each topic row in Research pages
- Add "Show archived" toggle that unhides archived topics
- Archived topics shown with strikethrough and muted styling

**New Research page foundation: /research/topics**
Simple topic bank table showing all topics — scored and unscored. Columns: topic, bucket, score (or "not scored"), status, date added. Buttons: Add to queue, Archive, Delete. This page will be expanded significantly in v2.

**CLI commands:**
```bash
# Add a topic to the bank without scoring (for manual entry)
python main.py add-topic "Why skyscrapers sway in the wind" --bucket infra

# Archive a topic
python main.py archive-topic --id 5 --reason "Already covered similar angle"

# Export topic bank to CSV
python main.py export-topics --output topics_export.csv
```

**Pass condition:** Adding a topic, archiving it, and confirming it disappears from the main view but appears with Show Archived toggle.

---

### Phase 11.v1 build order

Build in this exact sequence — each one is independent but builds on the previous:

1. 11.v1.A — Device sync (VIDEOFORGE_DB_PATH in .env and database.py) — 30 minutes
2. 11.v1.B — Trend monitor module + priority alerts table + scan history table
3. 11.v1.B — Trend Scanner dashboard page + alert banner on Overview
4. 11.v1.B — Fast-track pipeline flow
5. 11.v1.B — Email notification
6. 11.v1.C — Similarity detection on topic add
7. 11.v1.D — Archive flag + topic bank foundation page
8. Update requirements.txt — add pytrends
9. Update config.json — add research section with all parameters
10. Run all pass conditions

---

## Phase 11.v2 — 2–4 week priorities

Build after v1 is stable and you have at least 10 real videos posted. These features need real data to be useful.

---

### 11.v2.A — Full topic scoring engine

The research_engine.py module with all four data sources — Google Trends + YouTube search + Codex scoring + own channel analytics. Full scored report with 0–10 score, hook suggestion, three alt angles, competition level, and channel fit confidence badge.

Exactly as specced in the original Phase 11 spec above. Build this once you have real analytics data coming in from posted videos so the channel performance component of the score is meaningful.

---

### 11.v2.B — Research dashboard page (full version)

Expand /research/topics into the full Research page with:
- Single topic scorer — type a topic, get full scored report in 30 seconds
- Bulk scorer — paste 20 topics, score them all, get ranked table
- "Add top 5 to queue" button
- Re-score button on each topic
- Filter by bucket, status, score range
- Sort by score, date, status

---

### 11.v2.C — Comment mining

Pull comments from your YouTube videos weekly using the YouTube Data API. Send batches to Codex to identify questions and topic suggestions from your own audience. Add flagged suggestions to topic bank with status "audience-requested". These videos almost always outperform because viewers literally asked for them.

---

### 11.v2.D — Auto-fill weekly calendar

Every Sunday morning at 09:00 — before the 22:00 batch run — the system automatically selects the top 5 scored topics from the topic bank that haven't been made yet, balancing across buckets. Shows them in the dashboard for your review. You can swap any you don't like. At 22:00 the batch runs on whatever is in the queue.

---

### 11.v2.E — Performance feedback loop

After a video gets its first 48 hours of analytics, the system compares how it performed against its pre-production score. Did high-scored topics actually perform better? Shows this correlation in the Analytics page as "Score accuracy" so you can see if the scoring is working and manually adjust weights if needed.

---

## Phase 12 — Multi-Channel Support

Build after Phase 11.v2 is working and you are ready to launch a second channel (month 4–5). Full architectural change — channels/ folder structure, channel_id on all tables, dashboard channel switcher, two-voice dialogue format.

### Planned channels

| Channel | Format | Content | Launch |
|---|---|---|---|
| The Engineering Brief | Single narrator | How everyday things work | Live now |
| Two Voice Science | Alex + Sam dialogue | Same topics, more entertaining | Month 4–5 |
| Money Mechanics | Single narrator | How financial systems work | Month 8–9 |

### 12.A — Database migration

```sql
CREATE TABLE IF NOT EXISTS channels (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    handle_yt   TEXT,
    handle_tt   TEXT,
    format      TEXT DEFAULT 'single_narrator',
    active      INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now'))
);

ALTER TABLE jobs        ADD COLUMN channel_id TEXT DEFAULT 'engineering_brief';
ALTER TABLE analytics   ADD COLUMN channel_id TEXT DEFAULT 'engineering_brief';
ALTER TABLE topic_bank  ADD COLUMN channel_id TEXT DEFAULT 'engineering_brief';

INSERT OR IGNORE INTO channels (id, name, handle_yt, handle_tt, format)
VALUES ('engineering_brief', 'The Engineering Brief', '@HowThingsWorkEng', '@HowThingsWorkEng', 'single_narrator');
```

### 12.B — File structure migration

Move all channel-specific files into channels/ directory:
```
channels/
  engineering_brief/
    config.json
    prompts/
      script_prompt.txt
      metadata_prompt.txt
  two_voice_science/
    config.json
    prompts/
      script_prompt.txt   ← dialogue format
      metadata_prompt.txt
```

Root config.json becomes global only — logging, Flask port, scheduler timing.

### 12.C — Pipeline changes

Every module receives channel_id and loads config and prompts from channels/{channel_id}/. No logic changes — only file path changes.

### 12.D — Two-voice dialogue format

Script prompt for dialogue channels returns dialogue array instead of single narration:
```json
{
  "dialogue": [
    {"character": "ALEX", "line": "Wait — phone chargers get warm?"},
    {"character": "SAM", "line": "Every single one. And most people have no idea why."}
  ],
  "visual_brief": [...],
  "full_script": "combined text for captions"
}
```

Voice engine loops through dialogue array and calls ElevenLabs with voice_id_alex or voice_id_sam per line. Two voice IDs in channel config.

### 12.E — Dashboard channel switcher

Dropdown in navbar. Switching channel filters all pages — job queue, analytics, config editor, prompt editor, research — to that channel's data. Adding a job defaults to active channel.

### 12.F — Cross-channel trend routing

When Phase 12 is live, the trend monitor evaluates each trending topic against all active channels simultaneously and routes it to the channel with the highest fit score. "Federal Reserve rate decision" → routes to Money Mechanics. "EV battery failure" → routes to Engineering Brief. Shown in the Priority Alert with which channel it was routed to.

### 12.G — new-channel CLI command

```bash
python main.py new-channel "two_voice_science" --name "Two Voice Science" --format dialogue
```

Creates channels/two_voice_science/ by copying engineering_brief as template. Registers in channels table. Owner edits prompts and voice IDs.

### Phase 12 build order
1. Database migration with channel_id columns
2. File structure migration — move engineering_brief into channels/
3. Update all modules to accept channel_id
4. Dashboard channel switcher
5. Two-voice voice engine update
6. Cross-channel trend routing
7. new-channel CLI command
8. Full pass condition test

---

## Future phases (month 6+)

**Phase 13 — Advanced intelligence:**
- Negative space analysis — find topics with high search volume and weak/old competition
- Search autocomplete harvesting — YouTube and Google suggest what people are typing
- Seasonal calendar auto-population — pre-schedule topics for predictable annual spikes
- Reddit monitoring — r/engineering, r/mildlyinteresting as leading indicators
- Competitor gap analysis — find topics competitors covered that you haven't

**Phase 14 — Server migration:**
- Move dashboard from localhost to a VPS (Hetzner/DigitalOcean $5/month)
- HTTPS with Let's Encrypt
- Access from any device via browser — no local install needed
- Scheduler runs 24/7 without your computer being on
- Migrate database from Dropbox sync to hosted PostgreSQL

**Phase 15 — Scale:**
- Self-improving score weights based on your own channel performance data
- Series detection — group related topics into multi-part series
- Cross-platform signal correlation — Reddit spike predicts YouTube trend 48 hours later
- Automated A/B testing of hook styles
