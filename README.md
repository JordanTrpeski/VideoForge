# VideoForge

A multi-channel AI video production system. Topic discovery → script
generation → TTS narration → video assembly → captioning → multi-platform
upload, with a human review gate, kill-metrics analytics, and a Cloudflare R2
cloud preview so the operator can approve videos from any device. Built to run
three faceless YouTube / TikTok / Instagram channels (Reddit Stories, Dark
Psychology, Sleep Lore) from a single local machine.

> **Status:** Phase 13 complete. The system is built; no channel has shipped
> at scale yet. Reddit Stories is the first production target and is in
> dry-run prep on the Engineering Brief channel.

---

## What it is

VideoForge takes a topic (typed, scored from Google Trends, scraped from
Reddit, mined from YouTube comments, or fast-tracked from a priority alert)
and runs it through a deterministic pipeline:

```
Topic
  │
  ├─[1] Script           Claude → structured JSON (narration + 8 image prompts
  │                                or 5 candidate hooks for Reddit rewrites)
  │
  ├─[2] Voice            ElevenLabs / OpenAI TTS / Kokoro (local) → MP3
  │
  ├─[3] Images           Leonardo.AI → 1080×1920 portrait PNGs
  │       (skipped when visual_mode = background_loop / long_form_ambient)
  │
  ├─[4] Assembly         MoviePy → raw MP4
  │       (image slideshow │ looped background clip │ ambient + overlay)
  │
  ├─[5] Captions         faster-whisper → word timestamps → burn-in
  │       (skipped when template caption_mode = off)
  │
  ├─[6] Metadata + Thumbnail   Claude → SEO JSON / PIL → thumbnail variants
  │
  ├─[*] R2 cloud preview      Optional — upload to Cloudflare R2 so the
  │                            dashboard can play the video from anywhere
  │
  ├──── ▶ Review gate (human) ◀ ── nothing uploads until you click Approve
  │
  ├─[7] Upload           YouTube Data API v3 → long-form
  │                       TikTok + Instagram → teaser short, scheduled +6h
  │
  └─[8] Kill metrics     YouTube Analytics v2 → v15 / v30 / d60 verdicts
```

Every stage is a Python module that returns a `{success, ...}` dict and stops
the pipeline on failure. Missing API keys cause a stage to skip cleanly rather
than crash. All tunables live in `config.json` (and per-channel overlays);
nothing is hardcoded.

---

## Architecture

### Multi-channel overlay

Each channel lives under `channels/<slug>/`:

```
channels/
  reddit_stories/
    config.json              # overlay — deep-merged on top of root config.json
    prompts/
      script_prompt.txt      # overrides global prompt for this channel
      reddit_rewrite_prompt.txt
    assets/
      backgrounds/           # gameplay clips for background_loop mode
      music/                 # palette for this channel
    youtube_token.json       # platform credentials live with the channel
    tiktok_token.json
    instagram_token.json
```

`utils/config_loader.load_channel_config(slug)` returns the merged dict and
injects the channel slug + credential paths under a `_channel` block so
downstream modules (`upload_engine`, `r2_storage`, `tiktok_upload`, …) read
the right files without knowing about the multi-channel layer.

The dashboard has a channel switcher in the navbar; selecting a channel
filters every page (Jobs, Templates, API usage, Analytics, Topic bank,
Trends) to that channel's data. The **config and prompt editors write to
the active channel's overlay** when one is selected, with a coloured badge
showing scope (`editing reddit_stories overlay` vs `editing global
defaults`).

### Content templates (Phase 13 — Block A)

A template is a per-channel preset stored in the `content_templates` table.
Each template defines:

| Field | Purpose |
|---|---|
| `visual_mode` | `images` · `background_loop` · `long_form_ambient` |
| `length_min_seconds` / `length_max_seconds` | The variation engine samples uniformly in this window |
| `hook_style_pool` | JSON array of hook style names; one is picked per job |
| `music_palette` | Folder under the channel's `assets/music/` |
| `thumbnail_mode` | `frame_capture` · `text_template` · `off` |
| `caption_mode` | `on` · `off` (sleep content skips burn-in) |
| `dual_output` | When `true`, the long-form job auto-spawns a paired teaser short |
| `active` | The variation engine only picks among active templates |

Channels opt into the system by listing template names in
`channels/<slug>/config.json` → `templates: [...]` (an allow-list). The
script engine resolves the template before calling Claude, picks length +
hook from its pools, and stamps `template_id` / `template_name` on the
job row. The CLI accepts `--template <name>` to override the random pick.

### Cross-platform distribution (Block D)

When a long-form job uploads successfully to YouTube, the linked teaser
short's `scheduled_upload_at` is set to **+6 hours** (avoids platform
duplicate-detection penalties). The 15-minute scheduler tick picks the
teaser up and pushes it to TikTok via the Content Posting API v2 and to
Instagram via the Graph API (`media` → poll → `media_publish`). The
long-form YouTube URL is injected into both captions.

Per-platform enable flags live in `config.upload.tiktok` and
`config.upload.instagram`; missing per-channel credential files cause the
upload to skip cleanly rather than fail the pipeline.

### Cloud preview + retention (Blocks F + G)

After captioning, the final MP4 and chosen thumbnail are uploaded to
Cloudflare R2 under `previews/<channel>/<job_id>/`. The dashboard prefers
the R2 URL over the local file path (badge: `R2 cloud` / `local`), so the
operator can review and approve from a phone on cellular without exposing
the home machine.

Per-channel `r2.retention_days` (default `7`) sets the expiry timestamp.
A nightly sweep at 03:00 deletes expired objects from R2 and nulls the
`preview_url` on the job. When `r2.keep_after_youtube_upload` is `false`
(default), the expiry is compressed to +24h after a successful YouTube
upload so R2 storage doesn't accumulate.

### Database

SQLite. Path is `VIDEOFORGE_DB_PATH` from `.env` if set (Dropbox / Drive
for cross-device sync), otherwise `videoforge.db` in the project root.
Tables: `channels`, `jobs`, `analytics`, `trend_scans`, `priority_alerts`,
`topic_bank`, `content_templates`, `api_usage`, `api_usage_daily`,
`r2_objects`. Schema migrations are idempotent on every startup.

Job status flow:
```
queued → scripting → script_done (Reddit hook gate) → voiced → imaging →
assembling → captioning → metadata → review → uploading →
scheduled_upload → posted   (or → failed at any point)
```

---

## Tech stack

| Layer | Technology |
|---|---|
| Script generation | Anthropic Claude (`claude-sonnet-4-6` by default; configurable) |
| Voice | ElevenLabs · OpenAI TTS · Kokoro (local, zero-cost) — selected per channel |
| Images | Leonardo.AI |
| Captioning | faster-whisper (word-level timestamps) |
| Video assembly | MoviePy 2.x + ffmpeg |
| Web dashboard | Flask + Jinja templates + vanilla JS |
| Database | SQLite (migration path to Postgres in Phase 14) |
| Scheduling | APScheduler |
| Trend monitoring | pytrends (Google Trends) |
| Reddit ingestion | PRAW |
| Cloud storage | Cloudflare R2 via boto3 (S3-compatible) |
| Uploads | YouTube Data API v3 · TikTok Content Posting API v2 · Instagram Graph API v19 |
| Analytics | YouTube Analytics API v2 + manual CTR/CSV import |

> `requirements.txt` still lists `openai-whisper` for historical reasons; the
> caption engine uses `faster-whisper`. Both work; faster-whisper is the
> active import.

---

## Setup

### Requirements

- Python 3.10+
- ffmpeg on `PATH` (MoviePy + faster-whisper need it)
- API keys for whichever stages you want to actually run (everything else
  skips gracefully)

### Install

```bash
git clone https://github.com/JordanTrpeski/VideoForge.git
cd VideoForge
pip install -r requirements.txt
cp .env.example .env
```

### Environment variables

`.env.example` lists every key. The system checks presence at runtime — if
a key is missing for a stage, that stage logs a clear skip message and the
pipeline continues.

```env
# Always required
ANTHROPIC_API_KEY=
FLASK_SECRET_KEY=                # python -c "import secrets; print(secrets.token_hex(32))"

# Voice — pick at least one
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=
OPENAI_API_KEY=
# Kokoro is local-only — no key required

# Images (only needed for visual_mode = images)
LEONARDO_API_KEY=

# Uploads (each channel can have its own — see channels/<slug>/)
TIKTOK_CLIENT_KEY=
TIKTOK_CLIENT_SECRET=
TIKTOK_ACCESS_TOKEN=
YOUTUBE_CLIENT_SECRETS_FILE=client_secrets.json

# Reddit story ingestion
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=

# Cloudflare R2 (Block F — cloud preview)
R2_ACCOUNT_ID=
R2_ACCESS_KEY=
R2_SECRET=
R2_BUCKET=

# Optional
VIDEOFORGE_DB_PATH=              # Path to a Dropbox/Drive file for cross-device sync
FLASK_PORT=5000
```

### Start the dashboard

```bash
python app.py
# → http://localhost:5000
```

The dashboard registers the APScheduler background jobs on startup
(per-channel batch runs, weekly analytics pull, comment mining, calendar
auto-fill, the 15-minute scheduled-upload sweep, nightly usage rollup at
02:00, and the R2 retention sweep at 03:00).

### Or run from the CLI

```bash
# Full pipeline for one topic on a specific channel + template
python main.py pipeline "A roommate who never paid rent" \
    --channel reddit_stories --template narrative

# Or the staged commands
python main.py generate-script "Why bridges sway" --channel engineering_brief
python main.py generate-voice 001
python main.py assemble 001
python main.py add-captions 001
python main.py generate-metadata 001
python main.py generate-thumbnail 001
python main.py upload 001

# Batch the queue
python main.py batch --count 5 --channel reddit_stories

# Topic discovery
python main.py scan-trends
python main.py scan-reddit --subs tifu,AmItheAsshole --min-upvotes 2000
python main.py mine-comments

# Templates
python main.py template list
python main.py template create --channel reddit_stories --name narrative \
    --visual-mode background_loop --length-min 480 --length-max 720 \
    --hook-pool shocking_revelation,unexpected_twist,dramatic_opening \
    --dual-output

# Health
python main.py test-connections
```

---

## Channel configuration

### The overlay model

`config.json` at the repo root is the global default — every value can be
overridden per channel by a deep-merged overlay at
`channels/<slug>/config.json`. The merged dict is what every pipeline module
receives.

Recommended per-channel keys:

```jsonc
{
  "channel":  { "name": "Reddit Stories", "platforms": [...] },
  "pipeline": { "visual_mode": "background_loop" },
  "voice":    { "provider": "elevenlabs", "voice_id": "<channel-locked id>" },
  "templates": ["narrative"],                   // allow-list (Block A)
  "r2": {
    "retention_days": 7,
    "keep_after_youtube_upload": false
  },
  "usage": {
    "monthly_budget_cents": { "claude": 500, "elevenlabs": 1000 }
  },
  "upload": { "tiktok": true, "instagram": true },
  "metadata": { "description_skeletons": [...] },
  "thumbnail": { "mode": "text_template", "text_template": {...} }
}
```

### Adding a new channel

```bash
python main.py create-channel sleep_lore "Sleep Lore" \
    --niche "ambient mythology" --format single_narrator
```

This scaffolds the directory tree, registers the channel in the `channels`
table, and gives you a minimal `config.json` overlay to fill in. Add
templates via the dashboard at `/templates` or `python main.py template
create ...`. Drop platform credential JSON files into the channel directory
(`youtube_token.json`, `tiktok_token.json`, `instagram_token.json`); the
upload modules pick them up automatically.

### Dashboard pages

| URL | Purpose |
|---|---|
| `/` | Pipeline status, review gate, kill-metrics calendar |
| `/jobs`, `/jobs/<id>` | Queue + detail (script, video, logs, story-pair review) |
| `/jobs/new` | New job form, bulk add |
| `/templates` | Content templates CRUD per channel (Block A) |
| `/config` | Config editor — channel-scoped when a channel is selected (Block E) |
| `/prompts` | Prompt editor — same scoping rules |
| `/research/trends`, `/research/topics` | Google Trends alerts + scored topic bank |
| `/api-usage` | Per-channel × per-provider cost view + R2 storage (Blocks B + G) |
| `/analytics` | Views, retention, CTR, kill-metric verdicts |
| `/health` | Live status + latency for each external API |
| `/logs` | Live log stream, filterable by level / module / job |

---

## Operating philosophy

**Editorial gate is real.** No video uploads without a human clicking
Approve. The dashboard makes the review trivial — script + thumbnail + a
10-second preview from R2. The cost of a bad upload (channel strike,
audience trust) is permanent; the cost of a 30-second review is not. If
the gate ever slips, kill the weakest channel before scaling.

**Kill metrics, not vibes.** Every posted video gets graded at v15 (15-min
checkpoint), v30, and d60 against per-channel CTR / retention / watch-hour
thresholds. Verdicts (`ON TRACK` / `WARN` / `KILL-REVIEW` / `INSUFFICIENT
DATA`) appear on the dashboard calendar. The d60 review is a hard
calendar event — non-negotiable, scheduled in advance.

**Cost discipline.** The whole stack runs ~$10–25/month total across three
channels. Defaults are biased toward zero-cost providers (Kokoro instead of
ElevenLabs for long-form; R2 free tier with 7-day retention; SQLite
instead of hosted Postgres until Phase 14 ships). The `/api-usage` page
gives month-to-date cost per channel × provider and fires an 80%-of-budget
amber alert from per-channel monthly budgets.

**Inauthentic content policy defense.** YouTube's inauthentic-content
policy is the existential risk for AI-generated channels. Defense is
*editorial fingerprint per channel*: distinct voice, distinct visual
language, distinct content category, and human judgment on every video.
Channels run on separate Google accounts so a single strike doesn't
cascade. Content quality rules — original rewrites only (Reddit), no
copyrighted footage, no medical/political claims — are enforced in the
prompts and in the review gate, not as an afterthought.

**Human in the loop, by design.** The pipeline can produce a video without
a human, but it can't *publish* one. Approval is the moment the system
hands control back. Everything upstream is recoverable; everything
downstream is a public record. The review gate is where that line sits.

---

## Project status

### Complete (Phases 1–13)

| Phase | Scope |
|---|---|
| 1–8 | Core seven-stage pipeline + analytics pull |
| 9 | Flask dashboard at `localhost:5000` |
| 10 | APScheduler weekly batches + analytics + comment mining + calendar fill |
| 11.v1 | Device sync · priority alerts · similarity detection · topic bank |
| 11.v2 | Research scoring · comment mining · auto-fill calendar · score accuracy feedback |
| 12 | Multi-channel architecture · per-channel overlay · dashboard channel switcher |
| Reddit Stories | PRAW scraper · rewrite prompt · background-loop visual mode · hook selection · multi-provider TTS |
| Analytics + kill metrics | YouTube Analytics v2 · CTR import · v15/v30/d60 verdicts |
| Dual output | Long + teaser pair · YouTube URL injection · scheduled upload |
| Compliance pack | Thumbnail picker · metadata skeletons · title uniqueness · AI disclosure · permanent archive |
| **13** | **Templates · API usage tracking · long-form ambient · TikTok + IG upload · channel-aware editors · R2 cloud preview · R2 lifecycle** |

The Phase 13 audit ran read-only against 34 line-item pass criteria across
the seven blocks. Two items are tracked as nice-to-have gaps deferred to
Phase 14; nothing on the launch path is missing.

### Deferred to Phase 14

- Instrumentation gap: `r2.delete_object` calls (retention sweep) and
  YouTube OAuth token refreshes are not tracked in `api_usage`. Both are
  zero or near-zero cost, so the reported month-to-date totals are
  accurate within rounding.
- Server migration: the whole pipeline runs on a local home machine. Phase
  14 will move it to a VPS with HTTPS, 24/7 scheduling, and a hosted
  Postgres database. Triggered when channels 1–3 are all running and the
  home machine becomes the constraint.

### Channels

| Channel | Format | Voice | Status |
|---|---|---|---|
| The Engineering Brief | Single narrator, 60–90s shorts | ElevenLabs | Dry-run target only; private + abandoned once Channel 1 launches |
| **Reddit Stories** | First-person drama, 20–40 min long-form + 45–60s teaser | Kokoro (female, locked) | Scaffold complete; launching after dry run |
| Dark Psychology / Philosophy | Essay format, 8–15 min long-form | Kokoro (male, calm/intellectual) | Planned month 4–5 |
| Sleep Lore | Long-form ambient mythology, 1–3 hr | Kokoro (neutral calm) | Planned month 8–9 |

No channel has shipped video at production cadence yet. The launch sequence
is in `ROADMAP.md`.

---

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the full plan: channel launch sequence,
Phase 14 (server migration), Phase 15 (intelligence layer — self-improving
score weights, hook A/B testing, series detection), and future channel
candidates after the first three prove out.

---

## Logging

Every module writes three log streams:

```
logs/<module>.log    DEBUG+    module-scoped, detailed
logs/main.log        INFO+     combined pipeline view
logs/errors.log      ERROR+    just errors with full tracebacks
```

Format: `2026-04-10 14:32:01 | script_engine | INFO | [JOB 001] message`.

View live and filtered in the dashboard at `/logs`.

---

## Contributing

This is a single-maintainer project right now and isn't accepting drive-by
contributions. If you've forked it and built something interesting, open an
issue describing what you did — happy to talk.

If you've spotted a bug or have a design question about the architecture
(channel overlays, the template system, the kill-metrics pipeline), an
issue is the right place.

## License

See `LICENSE`.
