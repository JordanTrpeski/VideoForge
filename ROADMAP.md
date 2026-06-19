# VideoForge Roadmap

A multi-channel AI video production system. Pulls topics, generates
scripts with Claude, narrates with TTS, assembles with MoviePy or
FFmpeg, captions with faster-whisper, and uploads to YouTube /
TikTok / Instagram. Reviewed and approved via web dashboard.

---

## Current state (entering Phase 14)

### Completed phases

**Phases 1–11.v2** — Core pipeline, dashboard, scheduler, topic
scoring, research dashboard, comment mining, score accuracy
feedback, device sync via Google Drive, Priority Alerts.

**Phase 12** — Multi-channel architecture. Per-channel config
overlay, prompts, credentials, assets. Dashboard channel switcher.
CLI --channel flag on all commands. Channel-scoped database fields.

**Phase 13** — Multi-channel readiness. Seven blocks delivered:
per-channel video templates, API usage tracking with budget alerts,
long-form ambient visual mode (up to 3hr videos), TikTok and
Instagram upload modules with 6hr offset scheduling, channel-aware
config and prompt editors, Cloudflare R2 cloud preview, R2
lifecycle with nightly cleanup. Audit: 33 PASS, 2 PARTIAL (deferred
to Phase 15), 0 MISSING.

### Audit state

Phase 13 is committed. System is launch-ready for a single Reddit
Stories channel after Phase 14 completes the pre-launch readiness
work and a dry run is performed.

---

## Phase 14 — Pre-launch readiness

Goal: complete VideoForge so channel 1 (Reddit Stories,
betrayal/revenge lane) can launch with optimized rendering,
research-backed editorial prompts, symbolic-object thumbnails, and
post-upload review tracking.

Status: planned, not yet implemented.

### Block 1 — FFmpeg renderer migration
Replace MoviePy final rendering with FFmpeg-direct command
templates. Audio mastering via FFmpeg loudnorm targeting -14 LUFS.
Three render formats supported. MoviePy retained as fallback.

### Block 2 — Editorial additions from creator research
Original-fiction script generation, promise-system self-check,
aggressive 0-5s irreversible-event hook structure, three title and
three thumbnail text candidates with alternates for A/B testing.

### Block 3 — Betrayal/revenge lane configuration
Emotional lane locked. Subgenre weighting (50% family / 30%
romantic / 20% legal-proof). Bureaucratic-revenge climax
requirement. Template library from research. Length range
1440-1920 seconds default. Kokoro speed tuned to 180-200 WPM.

### Block 4 — Symbolic-object thumbnail visual_mode
New visual_mode generates clean single-object thumbnails via
Leonardo with PIL text overlay. Two variants for picker.

### Block 5 — Production evidence metadata log
Per-video JSON archive with full production metadata for compliance
and YPP review preparedness.

### Block 6 — Post-upload review tracking (48h window)
Dashboard surfaces videos awaiting 48-hour review with four key
metrics from YouTube Analytics API.

### Block 7 — Export-prompt / import-script flow
Manual model escalation path for hero videos via browser-based
stronger models (Opus 4.7, GPT-5 Pro) using existing subscriptions.

See PHASE_14_PLAN.md for full block specifications and pass
conditions.

---

## Channels — strategy

### Channel 1: Reddit Stories
First-person AI-written drama fiction in Reddit style. Betrayal/
revenge emotional lane with moral-clarity payoffs. Subgenre mix:
50% family/inheritance/property, 30% romantic/loyalty, 20%
legal-proof. 24-32 minute long-form (with occasional 40-56 min)
plus 45-60s teaser shorts. Female Kokoro voice, locked. Backgrounds
mix gameplay, satisfying, and ambient content (categorized
structure planned post-launch). Cross-platform funnel to TikTok and
Instagram Reels with YouTube long-form CTA.

### Channel 2: Dark Psychology / Philosophy
Video essay format on Mindplicit / Dark Psychology Coded model.
8-15 min long-form. Male Kokoro voice, calm and intellectual.
Leonardo cinematic imagery. 70/30 mix of hard-edge dark psychology
and Kee-style introspective content. Light teaser shorts.

### Channel 3: Sleep Lore
Pure mythology and folklore long-form ambient. 1-3 hour episodes.
Neutral calm Kokoro voice. Looped fireplace / forest / rain
visuals. No captions. Public domain source material. Podcast
distribution to Spotify and Apple Podcasts planned post-launch
(not currently built).

### Launch sequence
1. Phase 14 build completes
2. Dry run channel 1 on existing Engineering Brief channel — full
   pipeline end-to-end on a real story, output reviewed but not
   uploaded publicly. Verifies pipeline integration before real
   launch.
3. Engineering Brief set to private and abandoned after dry run
4. Launch channel 1 on fresh Google account
5. Run until 4-5 long-form videos shipped with editorial gate
   holding under real production conditions
6. Open channel 2 (Dark Psychology) on separate fresh Google
   account, same pattern
7. Open channel 3 (Sleep Lore) on separate fresh Google account,
   same pattern

### Identity / account strategy
Setup C — separate Google account per channel. Separate TikTok and
Instagram per channel. Channels can be created and operated in
parallel; AdSense linkage only occurs when a channel hits YPP
threshold (1K subs + 4K watch hours) and applies to monetization.
Engineering Brief used as dry-run target only, then private and
abandon. Main email reserved for future human-made channel.

---

## Future phases (not in scope for Phase 14)

### Phase 15 — Infrastructure scaling
VPS migration (Hetzner / DigitalOcean), HTTPS, PostgreSQL multi-
process safety, 24/7 scheduler not dependent on home machine.
Triggered when home-machine ops become limiting or when channels
1-3 are all running. Also captures deferred Phase 13 audit gaps
(R2 delete tracking, OAuth refresh tracking) and CLAUDE.md refresh.
See PHASE_15_NOTES.md.

### Phase 16 — Intelligence layer
Self-improving score weights based on accumulated performance data,
internal A/B testing infrastructure for hooks and thumbnails,
series detection, competitor gap analysis. Triggered after 6+
months of production data across multiple channels. See
PHASE_16_NOTES.md.

### Future channels (sequential, after channels 1-3 prove)
- Fandom Sleep ASMR (Good Knight Sleep model with original
  in-universe stories)
- HFY sci-fi narration (SciFiStories1977 model)
- Warm psychology (Kee model — backup if Dark Psychology has tone
  issues, or as parallel softer-tone channel)

---

## Operating principles

- Editorial gate stays real per channel. If it slips, kill the
  weakest channel before scaling.
- Kill metrics calibrated to CTR and AVD signals (not vanity view
  counts). Day 60 review is hard, on the calendar, non-negotiable.
- AdSense single-publisher rule applies: one AdSense per legal
  payee (Google policy). When monetized, all channels link to the
  same AdSense account. Channel-level strikes affect that channel;
  AdSense-level penalties affect all linked channels. Channels can
  be created and operated in parallel before YPP application;
  monetization linkage is the actual risk concentration point.
- YouTube inauthentic content policy is the existential risk.
  Defense is editorial fingerprint per channel: distinct voice,
  distinct visual language, distinct content category, human
  judgment on every video. Variation engine rotates length, hook
  style, structure to break template patterns.
- Cost discipline: ~$30-50/month total at full three-channel
  operation. R2 free tier with 7-day retention. Kokoro primary
  TTS; OpenAI tts-1 paid fallback; ElevenLabs only for selective
  premium use.
- Content originality: all scripts AI-generated original fiction.
  No narration of scraped source material. Production evidence
  archived per video for YPP review preparedness.