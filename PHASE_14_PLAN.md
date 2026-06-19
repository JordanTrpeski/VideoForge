# Phase 14 — Pre-launch readiness

Goal: complete VideoForge so channel 1 (Reddit Stories,
betrayal/revenge lane) can launch with optimized rendering,
research-backed editorial prompts, symbolic-object thumbnails,
and post-upload review tracking.

Status: planned, not yet implemented.

## Blocks

### Block 1 — FFmpeg renderer migration
Replace MoviePy final rendering with FFmpeg-direct command
templates. Audio mastering via FFmpeg loudnorm targeting -14 LUFS.
Three render formats: long-form 16:9 with captions + ambient mix,
short 9:16 with captions, long-form ambient 16:9 with optional
overlay and no captions.

### Block 2 — Editorial additions from creator research
Original-fiction script generation (not rewrite), promise-system
self-check (title + thumbnail + 30s opening align on one
emotional promise), aggressive 0-5s irreversible-event hook
structure, three title candidates + three thumbnail text
candidates with alternates stored for YouTube A/B testing.

### Block 3 — Betrayal/revenge lane configuration
Subgenre weighting: 50% family/inheritance/property, 30%
romantic/loyalty, 20% legal-proof. Bureaucratic-revenge climax
requirement (no shouting climaxes, calm precise countermove in
final third). 20 title templates and 30 opening-line templates
from research as eligible vocabulary. Length range 1440-1920
seconds default, occasional 2400-3360 second variants. Kokoro
speed tuned to ~180-200 WPM target.

### Block 4 — Symbolic-object thumbnail visual_mode
New visual_mode 'symbolic_object'. Claude picks 1-2 word
symbolic object from curated library during metadata generation.
Leonardo generates clean object image with template "high
contrast cinematic photograph of [object], dark background,
dramatic lighting, no people, no text". PIL composes final with
thumbnail_text overlay. Two variants for picker.

### Block 5 — Production evidence metadata log
After successful upload, JSON file in archive folder containing
full production metadata: premise, lane, script_origin, prompt
version, voice ID and provider, visual_mode, background used,
title + 2 alternates, thumbnail text + 2 alternates, thumbnail
path, hook chosen, length, AI disclosure flag, upload URL and
timestamp, render command.

### Block 6 — Post-upload review tracking (48h window)
jobs.review_due_at set to upload_time + 48 hours. New dashboard
page /reviews showing videos awaiting review. Fetches from
YouTube Analytics API: CTR Home, CTR Suggested, intro retention
30s, AVD, first major drop-off. Mark-reviewed button with
optional iteration_note. CLI command for spare-minute work
check-ins.

### Block 7 — Export-prompt / import-script flow
CLI commands to export resolved script generation prompt for
manual escalation to stronger models via browser chat (Opus 4.7,
GPT-5 Pro), and to import resulting JSON back into VideoForge.
Dashboard equivalents on job detail page.

## Discipline rules
- Each block completes with explicit pass conditions before next.
- After each block: summary of files touched and pass condition.
- No scope expansion mid-build. Anything that comes up goes in
  PHASE_15_NOTES.md or PHASE_16_NOTES.md instead.
- Final step: read-only audit against all seven blocks with file
  path and line number evidence per item.

## Pre-build state required
- Phase 13 committed (33 PASS, 2 PARTIAL deferred, 0 MISSING)
- R2 credentials in .env (verified)
- DB snapshot to videoforge_pre_phase14.db before build starts
- Git tree clean
- Boto3 installed (already done in Phase 13)