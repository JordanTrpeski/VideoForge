# VideoForge

VideoForge is a local AI video production system for faceless short-form and
long-form channels. It takes a topic or source story, generates a script,
creates narration, assembles a video, captions it when appropriate, prepares
metadata and thumbnails, gates everything behind human review, and then uploads
approved work to YouTube, TikTok, and Instagram.

The original project started as an engineering explainer pipeline for
**The Engineering Brief**. The current codebase has grown into a multi-channel
operator console with channel overlays, Reddit story ingestion, content
templates, dual long/short outputs, API usage tracking, kill-metric analytics,
scheduled cross-platform posting, and optional Cloudflare R2 review previews.

Current status: the implementation contains Phases 1-13 from the roadmap. The
first production target is the `reddit_stories` channel; `engineering_brief`
remains the default channel and a useful dry-run target.

## What It Does

At a high level, VideoForge runs this pipeline:

```text
Topic, trend alert, topic-bank row, or Reddit candidate
  |
  v
Script generation
  - Standard mode: Claude produces explainer script JSON plus visual prompts.
  - Reddit mode: Claude rewrites a source story and produces hook options.
  |
  v
Voice generation
  - ElevenLabs, OpenAI TTS, or local Kokoro, selected per channel.
  |
  v
Visual production
  - Leonardo image set for image-based shorts.
  - Background-loop clips for Reddit-style story videos.
  - Long-form ambient clips for sleep or lore formats.
  |
  v
MoviePy assembly
  - Raw MP4 from narration, visuals, optional music, and template settings.
  |
  v
Captioning
  - faster-whisper word timestamps and burn-in captions.
  - Skips cleanly when a template sets caption_mode = off.
  |
  v
Metadata and thumbnail
  - Claude SEO metadata.
  - Frame-capture or text-template thumbnail variants.
  |
  v
Optional R2 preview
  - Uploads review video and thumbnail to Cloudflare R2.
  |
  v
Human review gate
  - Nothing uploads until approved in the dashboard.
  |
  v
Upload and follow-up distribution
  - YouTube upload for the approved video.
  - Linked teaser jobs can be scheduled for TikTok and Instagram.
  |
  v
Analytics, usage tracking, and kill metrics
```

Every major stage is a Python module and returns a result dictionary with a
`success` flag. The pipeline stops on the first hard failure. Missing optional
credentials are handled as skips instead of crashes where that is safe.

## Current Architecture

### Channels

The root `config.json` is the global default. Each channel can override any
setting with `channels/<slug>/config.json`; the merge is handled by
`utils/config_loader.py`.

Checked-in channel overlays:

| Channel slug | Purpose | Current role |
|---|---|---|
| `engineering_brief` | Engineering explainers and shorts | Default channel and dry-run target |
| `reddit_stories` | Reddit-style long-form story videos plus teasers | First production target |

The loader also supports optional per-channel prompt, asset, and credential
paths:

```text
channels/<slug>/
  config.json
  prompts/                  optional prompt overrides
  assets/
    backgrounds/            optional background clips
    music/                  optional music palette
  client_secrets.json       local only, never commit
  youtube_token.json        local only, never commit
  tiktok_token.json         local only, never commit
  instagram_token.json      local only, never commit
```

Only the config overlays are currently checked in. Runtime credentials and
generated assets should stay local.

### Content Templates

Templates live in the SQLite `content_templates` table and can be managed from
the `/templates` dashboard page or with `python main.py template ...`.

A template controls:

| Field | Meaning |
|---|---|
| `visual_mode` | `images`, `background_loop`, or `long_form_ambient` |
| `length_min_seconds`, `length_max_seconds` | Target length window for variation |
| `hook_style_pool` | Hook styles to randomly choose from |
| `music_palette` | Channel music folder or palette label |
| `thumbnail_mode` | `frame_capture`, `text_template`, or `off` |
| `caption_mode` | `on` or `off` |
| `prompt_overrides` | JSON prompt override data |
| `dual_output` | Whether to create a linked teaser job |
| `active` | Whether it is eligible for random selection |

Channels can restrict random template selection by listing allowed template
names in their overlay config under `templates`.

### Job Modes

VideoForge currently supports two script-generation modes:

| Mode | Source | Review behavior |
|---|---|---|
| `standard` | A normal topic string | Flows through the normal video pipeline |
| `reddit` | A Reddit candidate with `source_selftext` | Stops at `script_done` so the operator can choose or edit a hook |

Reddit mode can create a linked pair: one long-form story job and one teaser
short. The long job stores `story_role = long`; the teaser stores
`story_role = short`; both point at each other with `linked_job_id`.

### Storage

SQLite is the source of truth. `database.py` reads the path from
`VIDEOFORGE_DB_PATH`; if that variable is blank, it uses `videoforge.db` in
the project root. This makes Dropbox, Google Drive, or a future hosted database
path possible without changing callers.

Important tables:

| Table | Purpose |
|---|---|
| `channels` | Channel registry |
| `jobs` | Every video through the pipeline |
| `analytics` | YouTube/TikTok stats and manual CTR/import fields |
| `topic_bank` | Manual, trend, comment-mined, and Reddit candidate topics |
| `trend_scans`, `priority_alerts` | Google Trends monitoring and fast-track alerts |
| `content_templates` | Per-channel template presets |
| `api_usage`, `api_usage_daily` | Provider usage and estimated cost tracking |
| `r2_objects` | Cloud preview objects and lifecycle state |

Generated files are written under `output/`. Logs are written under `logs/`.
Successful upload archives are copied into `archive/<channel>/<job>/` by
`modules/upload_engine.py`; treat that folder as publish artifacts and review
before committing anything from it.

### Status Flow

Common job statuses:

```text
queued
  -> scripting
  -> script_done      # Reddit hook gate
  -> voiced
  -> imaging
  -> assembling
  -> captioning
  -> metadata
  -> review
  -> uploading
  -> scheduled_upload # linked teaser waiting for TikTok/Instagram fan-out
  -> posted
```

Jobs can also become `failed` or `archived`.

## Project Map

```text
VideoForge/
  app.py                         Flask dashboard
  main.py                        CLI entry point
  database.py                    SQLite schema, migrations, and queries
  scheduler.py                   APScheduler jobs
  webhook.py                     Make.com/new-topic webhook
  config.json                    Global defaults
  requirements.txt               Python dependencies
  AGENTS.md                      Local build instructions and roadmap notes
  ROADMAP.md.txt                 Launch and future-phase plan
  PHASE_14_NOTES.md.txt          Deferred server-migration notes

  channels/
    engineering_brief/config.json
    reddit_stories/config.json

  modules/
    script_engine.py             Claude script/rewrite generation
    voice_engine.py              ElevenLabs/OpenAI/Kokoro TTS
    image_engine.py              Leonardo image generation
    assembly_engine.py           MoviePy video assembly
    caption_engine.py            faster-whisper captions
    metadata_engine.py           Claude metadata generation
    thumbnail_engine.py          Thumbnail variants
    upload_engine.py             YouTube/TikTok upload and permanent archive
    tiktok_upload.py             Scheduled teaser upload to TikTok
    instagram_upload.py          Scheduled teaser upload to Instagram
    r2_storage.py                Cloudflare R2 preview uploads
    analytics_engine.py          YouTube/TikTok analytics pulls
    kill_metrics.py              v15/v30/d60 verdict logic
    trend_monitor.py             Google Trends priority alerts
    similarity_engine.py         Topic similarity checks
    research_engine.py           Topic scoring
    comment_miner.py             YouTube comment mining
    reddit_engine.py             Reddit story candidate scanner

  utils/
    config_loader.py             Global + channel config merge
    template_engine.py           Template selection and variation
    usage_tracker.py             API usage/cost rows
    logger.py                    Shared logging setup

  templates/                     Flask/Jinja dashboard pages
  prompts/                       Global prompt templates
  assets/                        Local visual/audio source assets
  tests/                         Connectivity checks
```

## Dashboard

Run the dashboard locally:

```bash
python app.py
```

Default URL: `http://localhost:5000`

Main pages:

| Route | Purpose |
|---|---|
| `/` | Overview, live pipeline state, review gate, alerts, kill metrics |
| `/jobs` | Job queue |
| `/jobs/<id>` | Script, video preview, thumbnail variants, linked job, logs, retry controls |
| `/jobs/new` | Single and bulk job creation |
| `/templates` | Content template CRUD |
| `/config` | Config editor, scoped to selected channel when applicable |
| `/prompts` | Prompt editor and prompt test endpoint |
| `/logs` | Filtered live logs |
| `/analytics` | Analytics, score accuracy, kill metrics |
| `/api-usage` | Provider usage, estimated cost, R2 storage summary |
| `/health` | API health and TikTok re-auth |
| `/research/trends` | Trend scanner, priority alerts, fast-track controls |
| `/research/topics` | Topic bank, scoring, Reddit candidate approval |

The navbar has a channel switcher. When a channel is selected, most pages and
editor writes are scoped to that channel.

## CLI

The CLI is defined in `main.py`.

```bash
# Health and status
python main.py test-connections
python main.py list-channels
python main.py list-jobs --channel reddit_stories
python main.py status 001

# Full pipeline for one topic
python main.py pipeline "Why phone chargers get warm" --channel engineering_brief --bucket elec

# Full Reddit-style pipeline with a template override
python main.py pipeline "A roommate who never paid rent" --channel reddit_stories --bucket reddit --template narrative

# Run individual stages
python main.py generate-script "Why bridges sway" --channel engineering_brief --bucket infra
python main.py generate-voice 001
python main.py generate-images 001
python main.py assemble 001
python main.py add-captions 001
python main.py generate-metadata 001
python main.py generate-thumbnail 001
python main.py upload 001

# Queue processing
python main.py batch --count 5 --channel reddit_stories

# Research and topic bank
python main.py scan-trends
python main.py list-alerts
python main.py fast-track --alert-id 1 --channel engineering_brief
python main.py scan-reddit --subs tifu,AmItheAsshole --min-upvotes 2000
python main.py add-topic "Why skyscrapers sway in the wind" --bucket infra --channel engineering_brief
python main.py score-topic "Why EV batteries catch fire" --bucket vehicle
python main.py score-unscored --limit 20
python main.py mine-comments
python main.py fill-calendar --n 5
python main.py export-topics --output topics_export.csv

# Channel and template management
python main.py create-channel sleep_lore "Sleep Lore" --niche "ambient mythology" --format single_narrator
python main.py template list --channel reddit_stories
python main.py template create --channel reddit_stories --name narrative --visual-mode background_loop --length-min 480 --length-max 720 --hook-pool shocking_revelation,unexpected_twist --dual-output
python main.py template clone --id 1 --name narrative_alt
python main.py template activate --id 1
python main.py template deactivate --id 1
python main.py template delete --id 1
```

## Setup

### Requirements

- Python 3.10+
- ffmpeg and ffprobe on `PATH`
- API credentials only for the providers you intend to use

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Caption note: `modules/caption_engine.py` imports `faster_whisper`. The current
`requirements.txt` still includes `openai-whisper` from the earlier build. If
you run the caption stage, install `faster-whisper` in the environment as well.

Kokoro note: local TTS support is optional. Install `kokoro` and `soundfile`
only if a channel uses `voice.provider = "kokoro"`.

### Environment

Create a local `.env` from the template:

```bash
copy .env.example .env
```

Do not commit `.env`. The code loads secrets with `python-dotenv` and reads
keys at runtime.

Key groups used by the code:

| Group | Variables |
|---|---|
| Claude | `ANTHROPIC_API_KEY` |
| Voice | `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `OPENAI_API_KEY` |
| Images | `LEONARDO_API_KEY` |
| Reddit | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` |
| YouTube | `YOUTUBE_CLIENT_SECRETS_FILE` or channel-local `client_secrets.json` |
| TikTok | `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, token files |
| Cloudflare R2 | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY`, `R2_SECRET`, `R2_BUCKET` |
| Flask | `FLASK_SECRET_KEY`, `FLASK_PORT` |
| Database sync | `VIDEOFORGE_DB_PATH` |

Per-channel OAuth/token JSON files belong in `channels/<slug>/` and should
never be committed.

## External Services

| Capability | Provider/module |
|---|---|
| Script and metadata generation | Anthropic Claude via `anthropic` |
| Voice generation | ElevenLabs, OpenAI TTS, or Kokoro |
| Image generation | Leonardo.AI |
| Captions | faster-whisper |
| Video assembly | MoviePy and ffmpeg |
| Reddit story mining | PRAW |
| Trend scanning | pytrends |
| Uploads | YouTube Data API, TikTok Content Posting API, Instagram Graph API |
| Analytics | YouTube Analytics API plus manual/CSV CTR import |
| Cloud preview | Cloudflare R2 via boto3-compatible S3 client |

Most provider integrations skip gracefully when credentials are absent. The
script and metadata stages require a Claude key because they are the core
generation steps.

## Scheduler

`scheduler.py` starts with the Flask app and handles recurring work:

- queued batch runs
- weekly analytics pulls
- YouTube comment mining
- topic calendar auto-fill
- scheduled teaser uploads every 15 minutes
- nightly API usage rollup
- nightly R2 retention cleanup

The scheduled upload loop picks up teaser jobs with
`status = scheduled_upload` and `scheduled_upload_at <= now`, then calls the
TikTok and Instagram upload modules.

## Safety and Review Model

Publishing is intentionally human-gated. The system can generate assets
automatically, but upload only happens after approval from the dashboard or an
explicit CLI upload command.

Safety rules baked into the project:

- Secrets stay in `.env` or local token JSON files.
- `.env`, token files, generated output, logs, and database files are ignored.
- Jobs stop on hard stage failures and store `error_module` plus
  `error_message`.
- Missing optional API keys skip the affected integration instead of exposing
  secrets or crashing unrelated stages.
- Review pages show script, preview, thumbnail, metadata, linked teaser status,
  and logs before upload.
- Reddit stories keep the raw source story in the job row for rewrite context;
  do not treat the database as public.

## Logging

All modules use `utils/logger.py` and write to:

```text
logs/<module>.log    module-specific DEBUG+
logs/main.log        shared INFO+
logs/errors.log      shared ERROR+
```

Log line shape:

```text
2026-04-10 14:32:01 | script_engine | INFO | [JOB 001] message
```

The dashboard `/logs` page can filter by module, level, and job id.

## Roadmap

See `ROADMAP.md.txt` for the launch plan and future phases.

Current deferred items are tracked in `PHASE_14_NOTES.md.txt`:

- VPS/HTTPS/PostgreSQL migration when local operation becomes the bottleneck.
- Instrumentation refinements for zero-cost R2 deletes and OAuth refreshes.
- Future intelligence layer: self-improving score weights, hook and thumbnail
  testing, series detection, competitor gap analysis, and seasonal scheduling.

## Notes for Future Maintainers

- `AGENTS.md` and `CLAUDE.md` preserve the original phase-by-phase build brief.
  They are useful history, but this README is the current operational map.
- Keep new tunables in `config.json` or channel overlays, not hardcoded in
  modules.
- Keep provider credentials and OAuth tokens out of Git.
- Before pushing, run a staged secret scan if you touched configs, docs, upload
  code, or channel directories.

