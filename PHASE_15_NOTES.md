# Phase 15 — Infrastructure scaling

Status: deferred. Triggered when home-machine ops become limiting
or when channels 1-3 are all running and operational pressure
demands always-on infrastructure.

## Scope (high level)

### VPS migration
Move VideoForge from local home machine to VPS (Hetzner /
DigitalOcean). HTTPS via Let's Encrypt or Cloudflare. 24/7
scheduler not dependent on home machine being awake. Domain
configuration for dashboard access.

### Database migration
SQLite to PostgreSQL. Multi-process safety. Concurrent operation
support. Migration script preserves all Phase 13 data.

### Deployment automation
Docker compose or systemd-managed services. Health checks.
Automated restarts on failure. Log rotation.

## Audit gaps deferred from Phase 13

### B.2 partial — R2 delete operation tracking
modules/r2_storage.py delete calls in retention sweep not
currently instrumented in api_usage table. Zero cost impact
(deletes are free in R2). Worth adding for completeness when
infrastructure work happens.

### B.2 partial — YouTube OAuth refresh tracking
OAuth token refresh calls not currently instrumented in
api_usage. Zero cost impact. Worth adding when infrastructure
work happens.

## Cleanup deferred from Phase 14

### config_loader.py:99 — stale filename mapping
`utils/config_loader.py` line 99 maps `reddit_rewrite_prompt.txt` →
`reddit_prompt_file` as a channel-specific override check. The root
`prompts/reddit_rewrite_prompt.txt` was removed in the Phase 14
cleanup commit. The line is harmless (no channel dir has this file)
but the mapping should be updated to `reddit_generation_prompt.txt`
to keep the channel-override discovery consistent with the renamed
file. Low priority — only relevant if a channel prompt dir is
manually seeded with a file using the old name.

## Documentation deferred from Phase 13

### CLAUDE.md refresh
Current CLAUDE.md describes the pre-Phase-12 single-channel
engineering pipeline. Update to reflect multi-channel,
templates, kill metrics, dual output, R2, api_usage tracker
after channel 1 launch.