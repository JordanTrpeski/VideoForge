"""
kill_metrics.py
===============
Per-channel kill-metrics verdict engine.

Evaluates channel health at three sequential checkpoints:
  V15  (≥15 posted videos): Early CTR + retention signal
  V30  (≥30 posted videos): Breakout check — any video over threshold?
  D60  (≥60 days since first upload): Monetization pace

Key design rule — separating what the creator controls from what the
algorithm controls:
  - CTR and retention are CREATOR metrics (hook quality, content quality)
  - Impressions are ALGORITHM metrics (distribution, not quality)
  If impressions are below the minimum threshold, the verdict is always
  INSUFFICIENT DATA regardless of CTR or retention.  Never punish a
  channel for algorithm under-distribution.

Verdict values (ordered by severity):
  ON TRACK          — all checks pass, no action needed
  INSUFFICIENT DATA — not enough impression data to evaluate fairly
  WARN              — one signal is weak, monitor closely
  KILL-REVIEW       — multiple signals weak, evaluate whether to pivot

Input:  posted jobs with latest analytics snapshot from database
Output: verdict dict per channel

Logs: via parent caller (analytics_engine or app.py)
"""

# 1. Standard library
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_avg(values: list) -> Optional[float]:
    """Return mean of a list of numbers, ignoring None values. None if empty."""
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def _days_since(iso_str: str) -> Optional[float]:
    """Return float days since an ISO datetime string (UTC assumed)."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return delta.total_seconds() / 86400
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main verdict function
# ---------------------------------------------------------------------------

def compute_verdict(
    channel_id: str,
    jobs_with_analytics: list,
    kill_cfg: dict,
) -> dict:
    """
    Compute the kill-metrics verdict for one channel.

    Args:
        channel_id (str):          Channel slug being evaluated.
        jobs_with_analytics (list): Output of database.get_latest_analytics_per_job()
                                    — one dict per posted job with latest snapshot.
        kill_cfg (dict):           config['kill_metrics'] block.

    Returns:
        dict with keys:
          verdict      str   — 'ON TRACK' | 'WARN' | 'KILL-REVIEW' | 'INSUFFICIENT DATA'
          rule_fired   str   — human-readable reason for the verdict
          checkpoint   str   — 'NONE' | 'V15' | 'V30' | 'D60'
          video_count  int   — posted videos for this channel
          days_live    float | None
          metrics      dict  — raw numbers used in the decision
    """
    min_impressions = kill_cfg.get('minimum_impressions_for_verdict', 500)
    v15_cfg = kill_cfg.get('checkpoint_v15', {})
    v30_cfg = kill_cfg.get('checkpoint_v30', {})
    d60_cfg = kill_cfg.get('checkpoint_d60', {})

    # Separate this channel's jobs from the list
    jobs = [j for j in jobs_with_analytics if j.get('channel_id') == channel_id]
    video_count = len(jobs)

    # Days since first upload
    all_dates = [j.get('job_created_at') for j in jobs if j.get('job_created_at')]
    days_live = _days_since(min(all_dates)) if all_dates else None

    # Aggregate CTR — only from rows that have manual/CSV CTR data
    ctr_vals      = [j.get('ctr') for j in jobs if j.get('ctr') is not None]
    retention_vals = [j.get('avg_view_percentage') for j in jobs
                      if j.get('avg_view_percentage') is not None]
    views_vals     = [j.get('views') or 0 for j in jobs]
    impressions_vals = [j.get('impressions') for j in jobs
                        if j.get('impressions') is not None]

    avg_ctr       = _safe_avg(ctr_vals)
    avg_retention = _safe_avg(retention_vals)
    total_impressions = sum(impressions_vals) if impressions_vals else 0
    max_views     = max(views_vals) if views_vals else 0

    # Total estimated watch hours (using avg_view_duration * views / 3600)
    watch_hours = 0.0
    for j in jobs:
        v = j.get('views') or 0
        dur = j.get('avg_view_duration')
        if dur and v:
            watch_hours += (v * dur) / 3600.0

    metrics = {
        'video_count':        video_count,
        'days_live':          round(days_live, 1) if days_live is not None else None,
        'avg_ctr':            round(avg_ctr * 100, 2) if avg_ctr is not None else None,
        'avg_retention_pct':  round(avg_retention, 1) if avg_retention is not None else None,
        'total_impressions':  total_impressions,
        'max_views':          max_views,
        'watch_hours':        round(watch_hours, 1),
        'ctr_data_points':    len(ctr_vals),
        'retention_data_pts': len(retention_vals),
    }

    def _result(verdict, rule, checkpoint='NONE'):
        return {
            'verdict':     verdict,
            'rule_fired':  rule,
            'checkpoint':  checkpoint,
            'video_count': video_count,
            'days_live':   round(days_live, 1) if days_live is not None else None,
            'metrics':     metrics,
        }

    # Not enough videos yet to evaluate any checkpoint
    if video_count < 1:
        return _result(
            'INSUFFICIENT DATA',
            'No posted videos yet — keep creating.',
        )

    # -------------------------------------------------------------------------
    # D60 checkpoint — monetization pace (evaluated independently of video count)
    # -------------------------------------------------------------------------
    d60_fired = False
    d60_rule  = ''
    if days_live is not None and days_live >= 60:
        target_hours = d60_cfg.get('yt_watch_hours_target', 4000)
        target_days  = d60_cfg.get('yt_watch_hours_days', 365)
        pace_target  = target_hours * (days_live / target_days)
        if watch_hours > 0 and watch_hours < pace_target:
            d60_fired = True
            d60_rule = (
                f"Day {int(days_live)}: {watch_hours:.0f} watch hours accumulated, "
                f"need {pace_target:.0f}h for monetization pace "
                f"({target_hours}h target over {target_days} days)"
            )

    # -------------------------------------------------------------------------
    # V30 checkpoint
    # -------------------------------------------------------------------------
    v30_fired = False
    v30_rule  = ''
    if video_count >= 30:
        v30_breakout  = v30_cfg.get('breakout_views_threshold', 10000)
        v30_ctr_kill  = v30_cfg.get('avg_ctr_kill', 0.04)

        # Only evaluate CTR arm if we have meaningful impression data
        has_impression_data = total_impressions >= min_impressions

        if not has_impression_data:
            # Insufficient impression data — can still check views
            if max_views < v30_breakout:
                return _result(
                    'INSUFFICIENT DATA',
                    f"V30: max views {max_views:,} < {v30_breakout:,} but no impression "
                    "data to evaluate CTR — keep posting to build impression volume.",
                    'V30',
                )
        else:
            no_breakout  = max_views < v30_breakout
            ctr_too_low  = avg_ctr is not None and avg_ctr < v30_ctr_kill

            if no_breakout and ctr_too_low:
                v30_fired = True
                v30_rule  = (
                    f"V30: No video over {v30_breakout:,} views (max: {max_views:,}) "
                    f"AND avg CTR {avg_ctr*100:.1f}% < {v30_ctr_kill*100:.0f}% threshold — "
                    "hook quality and topic selection need review."
                )

    # -------------------------------------------------------------------------
    # V15 checkpoint
    # -------------------------------------------------------------------------
    v15_fired = False
    v15_rule  = ''
    if video_count >= 15:
        v15_ctr_warn  = v15_cfg.get('avg_ctr_warn', 0.03)
        v15_ret_warn  = v15_cfg.get('avg_retention_warn_pct', 30.0)

        has_impression_data = total_impressions >= min_impressions

        if not has_impression_data:
            # No meaningful impression volume — cannot evaluate CTR fairly
            return _result(
                'INSUFFICIENT DATA',
                f"V15: Only {total_impressions:,} impressions across all videos "
                f"(minimum {min_impressions:,} needed for a fair CTR verdict) — "
                "algorithm is still distributing your content. Keep posting.",
                'V15',
            )

        ctr_weak = avg_ctr is not None and avg_ctr < v15_ctr_warn
        ret_weak = avg_retention is not None and avg_retention < v15_ret_warn

        if ctr_weak and ret_weak:
            v15_fired = True
            v15_rule  = (
                f"V15: Avg CTR {avg_ctr*100:.1f}% < {v15_ctr_warn*100:.0f}% "
                f"AND avg retention {avg_retention:.0f}% < {v15_ret_warn:.0f}% — "
                "hooks need strengthening and content needs faster pacing."
            )
        elif ctr_weak:
            v15_fired = True
            v15_rule  = (
                f"V15: Avg CTR {avg_ctr*100:.1f}% < {v15_ctr_warn*100:.0f}% — "
                "review hook lines; first 3 words drive thumbnail CTR."
            )
        elif ret_weak:
            v15_fired = True
            v15_rule  = (
                f"V15: Avg retention {avg_retention:.0f}% < {v15_ret_warn:.0f}% — "
                "content loses viewers too early; tighten pacing after the hook."
            )

    # -------------------------------------------------------------------------
    # Combine into final verdict
    # -------------------------------------------------------------------------
    if v30_fired:
        return _result('KILL-REVIEW', v30_rule, 'V30')

    if v15_fired and d60_fired:
        return _result('WARN', f"{v15_rule}  |  {d60_rule}", 'V15')

    if v15_fired:
        return _result('WARN', v15_rule, 'V15')

    if d60_fired:
        return _result('WARN', d60_rule, 'D60')

    # All clear
    if video_count >= 15:
        return _result('ON TRACK', 'All metrics within healthy ranges.', 'V15')
    if video_count >= 1:
        return _result(
            'ON TRACK',
            f"{video_count} video(s) posted — V15 checkpoint active at 15 videos.",
        )

    return _result('ON TRACK', 'Keep posting.')


def compute_all_channel_verdicts(
    channel_ids: list,
    jobs_with_analytics: list,
    kill_cfg: dict,
) -> dict:
    """
    Compute verdicts for a list of channel IDs in one call.

    Args:
        channel_ids (list[str]): Channel slugs to evaluate.
        jobs_with_analytics (list): Full output of get_latest_analytics_per_job() — all channels.
        kill_cfg (dict):  config['kill_metrics'] block.

    Returns:
        dict: {channel_id: verdict_dict}
    """
    return {
        ch: compute_verdict(ch, jobs_with_analytics, kill_cfg)
        for ch in channel_ids
    }
