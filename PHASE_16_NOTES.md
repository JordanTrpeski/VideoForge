# Phase 16 — Intelligence layer

Status: deferred. Triggered after 6+ months of production data
across multiple channels, when there's enough signal to train
on.

## Scope (high level)

### Self-improving score weights
Current topic scoring uses fixed weights. After accumulating
performance data, weights should adjust based on which signals
actually predict channel success.

### A/B testing infrastructure for hooks and thumbnails
Currently relies on YouTube Studio's built-in A/B feature
(manual). Build internal A/B testing for hook patterns and
thumbnail variants pre-publication, using accumulated retention
data as ground truth.

### Series detection
Identify when multiple videos form a thematic series and adjust
metadata, end-screens, and playlist organization to amplify
binge behavior.

### Competitor gap analysis
Track topic coverage across competitor channels in same niche.
Identify topics with audience demand but low supply.

## Future channels queue (revisit after channels 1-3 prove)

- Fandom Sleep ASMR (Good Knight Sleep model with original
  in-universe stories)
- HFY sci-fi narration (SciFiStories1977 model)
- Warm psychology (Kee model — backup if Dark Psychology has
  tone issues)