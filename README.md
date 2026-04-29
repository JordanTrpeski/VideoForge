# VideoForge

An end-to-end AI video production pipeline for **The Engineering Brief** — a faceless short-form video channel explaining everyday engineering and technology on YouTube Shorts and TikTok.

Feed it a topic. Get a fully edited, captioned, SEO-tagged video ready to post.

---

## What It Does

VideoForge takes a topic string and automatically runs it through seven sequential stages:

```
Topic string
    │
    ▼
[1] Script Engine      Claude API → structured JSON (hook / explanation / payoff / 8 image prompts)
    │
    ▼
[2] Voice Engine       ElevenLabs → MP3 voiceover
    │
    ▼
[3] Image Engine       Leonardo.AI → 8 portrait images (1080×1920)
    │
    ▼
[4] Assembly Engine    MoviePy → raw MP4 (images + audio + background music)
    │
    ▼
[5] Caption Engine     Whisper → word-level timestamps → burn-in captions
    │
    ▼
[6] Metadata Engine    Claude API → YouTube title, TikTok title, hashtags, description
    │
    ▼
[7] Upload Engine      YouTube Data API v3 + TikTok Content Posting API → posted video
```

All stages are controlled from a local web dashboard at `localhost:5000`.

---

## Channel Details

| Field | Value |
|---|---|
| Channel name | The Engineering Brief |
| YouTube | @HowThingsWorkEng |
| TikTok | @HowThingsWorkEng |
| Content | Faceless — AI voiceover + AI images + burn-in captions |
| Format | 60–90 second portrait video (1080×1920) |
| Target | 5 videos per week |
| Buckets | Electrical · Infrastructure · Vehicles · The Flaw |
| Owner | Skopje, Macedonia |

---

## Architecture

```
videoforge/
├── app.py                    # Flask dashboard (localhost:5000)
├── main.py                   # CLI entry point (20+ commands)
├── database.py               # SQLite schema + all DB operations
├── scheduler.py              # APScheduler — Sunday 22:00 batch, Monday analytics
├── webhook.py                # Make.com integration endpoint
├── config.json               # All pipeline parameters
│
├── modules/
│   ├── script_engine.py      # Stage 1 — Claude → script JSON
│   ├── voice_engine.py       # Stage 2 — ElevenLabs → MP3
│   ├── image_engine.py       # Stage 3 — Leonardo.AI → PNGs
│   ├── assembly_engine.py    # Stage 4 — MoviePy → raw MP4
│   ├── caption_engine.py     # Stage 5 — Whisper → captioned MP4
│   ├── metadata_engine.py    # Stage 6 — Claude → SEO JSON
│   ├── thumbnail_engine.py   # Stage 6b — Frame capture → JPEG
│   ├── upload_engine.py      # Stage 7 — YouTube + TikTok upload
│   ├── analytics_engine.py   # Stats pull from both platforms
│   ├── trend_monitor.py      # Google Trends spike detection
│   ├── similarity_engine.py  # Duplicate topic detection
│   ├── research_engine.py    # 4-dimension topic scoring (0–10)
│   └── comment_miner.py      # YouTube comment → topic suggestions
│
├── prompts/
│   ├── script_prompt.txt     # Script generation prompt
│   └── metadata_prompt.txt   # SEO metadata prompt
│
├── templates/                # Flask HTML (12 pages)
└── utils/
    └── logger.py             # Unified logging setup
```

---

## Dashboard Pages

| URL | Purpose |
|---|---|
| `/` | Overview — pipeline status, review gate, stat strip |
| `/jobs` | Job queue — filterable table of all jobs |
| `/jobs/<id>` | Job detail — script, images, video player, logs |
| `/jobs/new` | New job — topic input, bulk add, run mode |
| `/config` | Config editor — all parameters grouped by section |
| `/prompts` | Prompt editor — script + metadata prompts with live test |
| `/logs` | Log viewer — live stream, filter by level / module / job |
| `/analytics` | Stats — views, revenue, platform comparison, top videos |
| `/health` | API health — status + latency for all 6 external APIs |
| `/research/trends` | Trend scanner — Google Trends alerts, scan history |
| `/research/topics` | Topic bank — scored topics, archive, export to CSV |

The pipeline has a **manual review gate** between the `captioning` and `uploading` stages. Nothing uploads until you click Approve in the dashboard.

---

## Implemented Phases

### Core pipeline (Phases 1–8)
All seven production stages plus analytics pull — fully implemented.

### Web dashboard (Phase 9)
Flask app with all 12 pages, API endpoints, and a Make.com webhook.

### Automation (Phase 10)
APScheduler batch runs (Sunday 22:00 Europe/Skopje) and analytics pull (Monday 06:00).

### Phase 11.v1 — Week-1 intelligence (no historical data needed)

| Feature | Status | What it does |
|---|---|---|
| **A — Device sync** | ✅ Done | `VIDEOFORGE_DB_PATH` in `.env` points the database at a Dropbox/Drive folder — both devices share the same database |
| **B — Priority alerts** | ✅ Done | Monitors Google Trends; when a topic spikes >100% above baseline and scores ≥7/10 channel fit, shows an amber banner on the dashboard with a 48-hour countdown and a Fast-track button |
| **C — Similarity detection** | ✅ Done | Every new topic is compared against the last 50 jobs via Claude; >70% similar shows a warning before adding |
| **D — Topic bank** | ✅ Done | Archive flag, topic bank page, CSV export, CLI commands |

### Phase 11.v2 — Post-launch intelligence (needs ≥10 real videos)

| Feature | Status | What it does |
|---|---|---|
| **A — Research scoring** | ✅ Done | Scores any topic 0–10 across trend (30%), competition (30%), channel fit (25%), own channel performance (15%) |
| **C — Comment mining** | ✅ Done | Pulls YouTube comments weekly, sends batches to Claude, adds extracted topic suggestions to the topic bank |
| **D — Calendar auto-fill** | ✅ Done | Every Sunday morning selects the top 5 scored topics balanced across buckets and queues them for review |
| **E — Score accuracy feedback** | ✅ Done | After 48 hours of analytics, compares predicted score against actual performance; shows correlation in the Analytics page |

### Phase 12 — Multi-channel (planned, month 4–5)
Not yet built. Will add `channels/` folder structure, per-channel config/prompts, two-voice dialogue format (Alex + Sam), and a dashboard channel switcher.

---

## Database

SQLite. Path controlled by `VIDEOFORGE_DB_PATH` in `.env` (falls back to `videoforge.db` in the project root).

**Job status flow:**
```
queued → scripting → voiced → imaging → assembling → captioning → metadata → review → uploading → posted → failed
```

**Tables:** `jobs` · `analytics` · `trend_scans` · `priority_alerts` · `topic_bank`

---

## Setup

### Requirements

- Python 3.10+
- **ffmpeg** installed on system PATH (required by pydub)
- API keys for the stages you want to run (see `.env.example`)

### Install

```bash
git clone https://github.com/JordanTrpeski/VideoForge.git
cd VideoForge
pip install -r requirements.txt
cp .env.example .env
# Fill in your API keys in .env
```

### Environment variables

```env
ANTHROPIC_API_KEY=          # Required from day 1 — script + metadata + research
ELEVENLABS_API_KEY=         # Phase 2 — voice synthesis
ELEVENLABS_VOICE_ID=        # Pick a voice from ElevenLabs Voice Library
LEONARDO_API_KEY=           # Phase 3 — image generation
PEXELS_API_KEY=             # Phase 3 — fallback stock images
TIKTOK_CLIENT_KEY=          # Phase 7 — TikTok upload
TIKTOK_CLIENT_SECRET=
TIKTOK_ACCESS_TOKEN=
YOUTUBE_CLIENT_SECRETS_FILE=client_secrets.json  # Phase 7 — YouTube upload
FLASK_SECRET_KEY=changethis
FLASK_PORT=5000
VIDEOFORGE_DB_PATH=         # Optional — point to Dropbox/Drive for cross-device sync
```

Keys are checked at runtime; missing keys skip that stage gracefully instead of crashing.

### Start the dashboard

```bash
python app.py
# → http://localhost:5000
```

### Or use the CLI

```bash
# Run a full job
python main.py generate-script "Why skyscrapers sway in the wind" --bucket infra
python main.py generate-voice 001
python main.py generate-images 001
python main.py assemble 001
python main.py add-captions 001
python main.py generate-metadata 001
python main.py generate-thumbnail 001
python main.py upload 001

# Or run the whole batch
python main.py batch --count 5

# Research
python main.py scan-trends
python main.py list-alerts
python main.py fast-track --alert-id 1
python main.py score-topic "Why USB-C gets warm"
python main.py add-topic "Why bridges vibrate" --bucket infra
python main.py mine-comments

# Utilities
python main.py test-connections
python main.py export-topics --output topics.csv
```

---

## Configuration

All tunable values are in `config.json` — never hardcoded. Key sections:

| Section | Key parameters |
|---|---|
| `script` | Claude model, temperature, word count target, prompt file |
| `voice` | ElevenLabs stability, similarity boost, style exaggeration |
| `visuals` | Leonardo model, style preset, guidance scale, image size |
| `video` | Resolution, FPS, codec, music volume, crossfade duration |
| `captions` | Font, size, position, stroke, Whisper model |
| `posting` | Post times, batch size per week, timezone |
| `research` | Trend alert threshold, scoring weights, seed keywords |

Edit live from the dashboard at `/config` without touching files.

**Parameters left blank intentionally** (fill in after testing):
- `visuals.style_preset_id` — test 5 Leonardo styles, pick the most consistent
- `voice.voice_id` — listen to ElevenLabs voices, pick one
- `thumbnail.frame_capture_at_seconds` — watch first 5 videos, pick frame
- `posting.tiktok_post_times` — adjust after 4 weeks of analytics data

---

## Script Format

Every generated script follows this structure:

| Section | Timing | Rule |
|---|---|---|
| **Hook** | 0–3 s | One sentence. Surprising fact or wrong belief. ≤15 words. |
| **Setup** | 3–15 s | What most people think. 2–3 sentences. |
| **Explanation** | 15–55 s | The real engineering answer. Simple language. One analogy. |
| **Payoff** | 55–75 s | The mind-blowing implication. 1–2 sentences. |
| **CTA** | optional | "Follow for more" style. 3–5 seconds. |

Claude returns structured JSON with: `hook`, `setup`, `explanation`, `payoff`, `cta`, `full_script`, `visual_brief` (8 image prompts), `metadata_hints`.

---

## Content Rules

- All content must be original — no copying from other sources
- No copyrighted footage, music (royalty-free tracks only from `assets/music/`)
- No recognisable brand logos in generated images
- No political content, no health or medical claims
- All engineering facts must be verifiable
- Minimum 60 seconds for YouTube monetization
- All images generated at 1080×1920 portrait for Shorts and TikTok

---

## Logging

Every module writes to three places simultaneously:

```
logs/script_engine.log    # Module-level (DEBUG+)
logs/main.log             # Combined (INFO+)
logs/errors.log           # Errors only (ERROR+)
```

Format: `2026-04-10 14:32:01 | script_engine | INFO | [JOB 001] message`

View filtered live in the dashboard at `/logs`.

---

## Roadmap

| Phase | Target | Description |
|---|---|---|
| 12 | Month 4–5 | Multi-channel — Two Voice Science (Alex + Sam dialogue) |
| 13 | Month 6+ | Negative space analysis, Reddit monitoring, competitor gap |
| 14 | Month 6+ | Server migration — VPS, HTTPS, 24/7 scheduler, PostgreSQL |
| 15 | Month 8+ | Self-improving score weights, A/B hook testing, series detection |

---

## Tech Stack

| Layer | Technology |
|---|---|
| AI / Script | Anthropic Claude (claude-sonnet-4-6) |
| AI / Voice | ElevenLabs |
| AI / Images | Leonardo.AI |
| AI / Captions | OpenAI Whisper (local) |
| Video editing | MoviePy + ffmpeg |
| Web dashboard | Flask |
| Database | SQLite (upgradeable to PostgreSQL) |
| Scheduling | APScheduler |
| Trend data | pytrends (Google Trends) |
| Image processing | Pillow |
| Upload | YouTube Data API v3, TikTok Content Posting API v2 |
