"""
trend_monitor.py
================
Phase 11.v1.B — Priority Alert system for VideoForge.

Monitors Google Trends for engineering topics that spike above a configured
threshold. When a spike is detected the topic is sent to Claude for a channel
relevance check. If the fit score meets the minimum threshold a priority_alert
record is created with a 48-hour expiry window.

Input:  config dict (research section), seed_keywords per bucket
Output: priority_alert rows in DB, trend_scan history row, optional email

Rate limiting:
  Reads safe_scans_per_hour and safe_scans_per_day from config.
  Blocks the scan and returns early if limits are exceeded.

Email notification:
  Sends a plain-text email via smtplib if notify_email is set in config
  and SMTP_* vars are configured in .env.

Logs:   logs/trend_monitor.log

Dependencies:
    - pytrends (Google Trends unofficial API)
    - anthropic (Claude relevance check)

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

# 2. Third-party libraries

# --- urllib3 2.x compatibility shim -------------------------------------------
# pytrends 4.9.2 passes `method_whitelist` to urllib3.Retry(), but urllib3 2.0
# renamed it to `allowed_methods` and removed the old name entirely.  Patch
# Retry.__init__ once so pytrends works without downgrading urllib3 system-wide.
import urllib3.util.retry as _urllib3_retry
if not hasattr(_urllib3_retry.Retry, '_vf_compat_patched'):
    _orig_retry_init = _urllib3_retry.Retry.__init__

    def _retry_compat(self, *args, method_whitelist=None, allowed_methods=None, **kw):
        if method_whitelist is not None and allowed_methods is None:
            allowed_methods = method_whitelist
        _orig_retry_init(self, *args, allowed_methods=allowed_methods, **kw)

    _urllib3_retry.Retry.__init__ = _retry_compat
    _urllib3_retry.Retry._vf_compat_patched = True
# ------------------------------------------------------------------------------

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

# 3. Local modules
from database import (
    count_scans_since,
    get_last_scan_time,
    insert_priority_alert,
    insert_trend_scan,
)
from utils.logger import setup_logger

logger = setup_logger('trend_monitor')


# ---------------------------------------------------------------------------
# Rate-limit guard
# ---------------------------------------------------------------------------

def _check_rate_limits(config: dict) -> tuple[bool, str]:
    """
    Check whether it is safe to run a scan right now.

    Args:
        config (dict): Loaded config.json contents.

    Returns:
        tuple[bool, str]: (allowed, reason_if_blocked)
    """
    rc = config.get('research', {})
    per_hour = rc.get('safe_scans_per_hour', 5)
    per_day  = rc.get('safe_scans_per_day', 15)

    now = datetime.utcnow()
    hour_ago = (now - timedelta(hours=1)).isoformat()
    day_ago  = (now - timedelta(hours=24)).isoformat()

    scans_hour = count_scans_since(hour_ago)
    scans_day  = count_scans_since(day_ago)

    if scans_hour >= per_hour:
        return False, (
            f"Hourly scan limit reached ({scans_hour}/{per_hour}). "
            "Wait before scanning again."
        )
    if scans_day >= per_day:
        return False, (
            f"Daily scan limit reached ({scans_day}/{per_day}). "
            "Scans will resume tomorrow."
        )

    return True, ''


# ---------------------------------------------------------------------------
# Startup cooldown check
# ---------------------------------------------------------------------------

def should_scan_on_startup(config: dict) -> bool:
    """
    Return True if an automatic startup scan should run, based on the
    scan_on_startup and scan_on_startup_cooldown_hours settings.

    Args:
        config (dict): Loaded config.json contents.

    Returns:
        bool: True if a startup scan is appropriate.
    """
    rc = config.get('research', {})
    if not rc.get('scan_on_startup', True):
        return False

    cooldown_hours = rc.get('scan_on_startup_cooldown_hours', 2)
    last = get_last_scan_time()

    if last is None:
        logger.info("trend_monitor: No previous scan found — startup scan permitted")
        return True

    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True

    age_hours = (datetime.utcnow() - last_dt).total_seconds() / 3600
    if age_hours >= cooldown_hours:
        logger.info(
            f"trend_monitor: Last scan was {age_hours:.1f}h ago "
            f"(cooldown: {cooldown_hours}h) — startup scan permitted"
        )
        return True

    logger.info(
        f"trend_monitor: Last scan was {age_hours:.1f}h ago "
        f"(cooldown: {cooldown_hours}h) — startup scan skipped"
    )
    return False


# ---------------------------------------------------------------------------
# Google Trends query
# ---------------------------------------------------------------------------

def _query_trends(keywords: list[str], timeframe: str = 'today 3-m') -> dict:
    """
    Query Google Trends for a list of keywords and return raw interest data.

    Args:
        keywords (list[str]): Up to 5 keywords for a single pytrends request.
        timeframe (str):      pytrends timeframe string.

    Returns:
        dict: {keyword: {'recent_avg': float, 'baseline_avg': float,
                          'spike_percent': float}}
              Returns empty dict if pytrends is unavailable or request fails.
    """
    try:
        from pytrends.request import TrendReq
    except ImportError:
        logger.error(
            "trend_monitor: pytrends not installed. "
            "Run: pip install pytrends"
        )
        return {}

    try:
        logger.info(
            f"trend_monitor: Querying Google Trends — keywords: {keywords}"
        )
        t0 = time.time()

        pt = TrendReq(hl='en-US', tz=60, timeout=(10, 25), retries=2, backoff_factor=0.5)
        pt.build_payload(kw_list=keywords[:5], timeframe=timeframe)
        df = pt.interest_over_time()

        elapsed = round(time.time() - t0, 2)
        logger.info(
            f"trend_monitor: Google Trends response received in {elapsed:.2f}s "
            f"— {len(df)} data points"
        )

        if df.empty:
            logger.warning("trend_monitor: Google Trends returned empty dataframe")
            return {}

        results = {}
        # Split into recent 7 days vs prior 30 days for spike calculation
        if len(df) < 8:
            logger.warning("trend_monitor: Not enough data points for spike calculation")
            return {}

        for kw in keywords[:5]:
            if kw not in df.columns:
                continue
            series      = df[kw]
            recent      = series.iloc[-7:].mean()
            baseline    = series.iloc[-37:-7].mean() if len(series) >= 37 else series.iloc[:-7].mean()
            spike_pct   = ((recent - baseline) / max(baseline, 1)) * 100
            results[kw] = {
                'recent_avg':   round(float(recent), 1),
                'baseline_avg': round(float(baseline), 1),
                'spike_percent': round(float(spike_pct), 1),
            }
            logger.debug(
                f"trend_monitor: '{kw}' — recent: {recent:.1f}, "
                f"baseline: {baseline:.1f}, spike: {spike_pct:.1f}%"
            )

        return results

    except Exception as exc:
        logger.error(f"trend_monitor: Google Trends query failed: {exc}", exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# Claude relevance check
# ---------------------------------------------------------------------------

def _check_relevance(topic: str, bucket: str, config: dict) -> dict | None:
    """
    Send a trending topic to Claude and ask for channel relevance scoring.

    Args:
        topic (str):   Trending search keyword.
        bucket (str):  Content bucket the keyword came from.
        config (dict): Loaded config.json.

    Returns:
        dict with channel_fit, reframed_angle, hook_suggestion, fits_channel
        or None if Claude is unavailable or returns unparseable output.
    """
    api_key = os.getenv('ANTHROPIC_API_KEY', '').strip()
    if not api_key:
        logger.warning("trend_monitor: ANTHROPIC_API_KEY not set — skipping relevance check")
        return None

    try:
        import anthropic

        model = config.get('script', {}).get('model', 'claude-sonnet-4-6')

        prompt = f"""You are the content strategist for "The Engineering Brief" — a YouTube Shorts and TikTok channel that explains how everyday engineering and technology works. The audience is general public (not engineers). Videos are 60–90 seconds, faceless, AI-generated voiceover.

A trending topic has been detected: "{topic}" (category: {bucket})

Respond with a JSON object containing exactly these fields:
- "channel_fit": integer 1-10
- "fits_channel": true or false
- "urgency": string — see rules below
- "why_trending": string — see rules below
- "why_relevant": string — see rules below
- "reframed_angle": string — see rules below
- "hook_suggestion": string — see rules below
- "angle_options": array of 3 objects — see rules below

CHANNEL_FIT SCORING:
9–10: Perfect fit — directly about how something works, strong everyday engineering angle
7–8: Good fit — engineering angle is clear but needs framing
5–6: Possible fit — engineering angle exists but is a stretch
1–4: Poor fit — political, medical, entertainment, or no engineering angle

URGENCY — pick one:
"high": breaking news in last 48h, something just happened
"medium": rising trend, story developing over days
"low": seasonal or cyclical pattern, evergreen interest spike

WHY_TRENDING — 1-2 sentences max:
Explain what real-world event or pattern is causing this search spike right now.
Be specific. Name the event if there is one.
GOOD: "A major automotive recall was announced this week affecting 400,000 vehicles. The story is leading tech and consumer news."
BAD: "This topic is trending because people are searching for it." (useless)

WHY_RELEVANT — 1 sentence max:
Explain specifically why this topic fits The Engineering Brief's format, not just that it does.
GOOD: "Product recalls are driven by failure mode analysis — a core engineering process your audience has never seen explained."
BAD: "This fits the channel because it is an engineering topic." (too vague)

REFRAMED_ANGLE — hard rules:
- Under 10 words. Hard limit.
- Punchy title, not an explanation. No "because" or "here's why".
- Leads with the engineering concept, not the news event.
GOOD: "Why Bridges Are Built to Fail"
GOOD: "The Flaw Hiding in Every Recall"
BAD: "Why engineers build failure thresholds that trigger a product recall" (too long)

HOOK_SUGGESTION — hard rules:
- Single sentence. Under 15 words. Hard limit.
- Stops a scroll. Challenges a belief or states a surprising fact.
- Ends with tension, not a conclusion.
GOOD: "Your car was already broken. You just didn't know it yet."
GOOD: "Engineers design bridges to collapse — on purpose."
BAD: "A tiny flaw slips past testing, here's the system designed to catch it." (gives away the answer)

ANGLE_OPTIONS — array of exactly 3 objects, each with "title" and "hook":
- Three distinct video angles on this topic — different framings, not variations of the same idea
- "title": under 8 words, follows REFRAMED_ANGLE rules above
- "hook": single sentence under 15 words, follows HOOK_SUGGESTION rules above
- First angle: the most direct engineering explanation
- Second angle: the human failure / design flaw angle
- Third angle: the surprising implication most people don't know
Example format:
[
  {{"title": "Why Safety Systems Always Fail Twice", "hook": "The first failure is built in. The second one is the one that kills you."}},
  {{"title": "The Flaw Hiding in Every Recall", "hook": "Your car passed every test. Then it killed someone."}},
  {{"title": "How Recalls Actually Keep You Safe", "hook": "The recall isn't the failure. Not recalling is."}}
]

Respond with only valid JSON. No explanation outside the JSON."""

        logger.info(
            f"trend_monitor: Calling Claude API for relevance check — topic: '{topic}', "
            f"model: {model}"
        )
        t0 = time.time()

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=800,
            messages=[{'role': 'user', 'content': prompt}],
        )

        elapsed = round(time.time() - t0, 2)
        logger.info(
            f"trend_monitor: Claude response received in {elapsed:.2f}s "
            f"for topic: '{topic}'"
        )

        text = response.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]

        data = json.loads(text)
        required = {'channel_fit', 'fits_channel', 'reframed_angle', 'hook_suggestion'}
        if not required.issubset(data.keys()):
            logger.warning(
                f"trend_monitor: Claude response missing fields for '{topic}': {data}"
            )
            return None

        # Normalise angle_options to a JSON string for DB storage
        raw_angles = data.get('angle_options', [])
        if isinstance(raw_angles, list):
            data['angle_options_json'] = json.dumps(raw_angles)
        else:
            data['angle_options_json'] = '[]'

        logger.debug(
            f"trend_monitor: Relevance — '{topic}' -> fit: {data['channel_fit']}, "
            f"urgency: {data.get('urgency','?')}, fits: {data['fits_channel']}, "
            f"angle: '{data['reframed_angle']}'"
        )
        return data

    except json.JSONDecodeError as exc:
        logger.error(
            f"trend_monitor: Could not parse Claude JSON for '{topic}': {exc}"
        )
        return None
    except Exception as exc:
        logger.error(
            f"trend_monitor: Claude relevance check failed for '{topic}': {exc}",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Email notification
# ---------------------------------------------------------------------------

def _send_alert_email(alerts: list[dict], config: dict) -> None:
    """
    Send a plain-text email listing new priority alerts via smtplib.

    Only runs if notify_email is set in config AND the following .env vars
    are configured: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD.

    Args:
        alerts (list[dict]): List of newly-created alert dicts.
        config (dict):       Loaded config.json.
    """
    notify_email = config.get('research', {}).get('notify_email', '').strip()
    if not notify_email:
        return

    smtp_host = os.getenv('SMTP_HOST', '').strip()
    smtp_port = int(os.getenv('SMTP_PORT', '587'))
    smtp_user = os.getenv('SMTP_USER', '').strip()
    smtp_pass = os.getenv('SMTP_PASSWORD', '').strip()

    if not smtp_host or not smtp_user or not smtp_pass:
        logger.warning(
            "trend_monitor: notify_email is set but SMTP_HOST/SMTP_USER/SMTP_PASSWORD "
            "are not configured in .env — skipping email notification"
        )
        return

    flask_port = int(os.getenv('FLASK_PORT', 5000))
    dashboard_url = f"http://localhost:{flask_port}/research/trends"

    lines = [
        f"VideoForge Priority Alert — {len(alerts)} new trending topic(s)\n",
        f"Dashboard: {dashboard_url}\n",
        "=" * 60,
    ]
    for a in alerts:
        lines += [
            f"\nTopic:        {a['topic']}",
            f"Reframed:     {a.get('reframed_angle', '')}",
            f"Hook:         {a.get('hook_suggestion', '')}",
            f"Spike:        {a.get('spike_percent', 0):.0f}%",
            f"Channel fit:  {a.get('channel_fit', 0)}/10",
            f"Expires:      {a.get('expires_at', '')}",
            "-" * 40,
        ]

    body = "\n".join(lines)

    try:
        msg = MIMEText(body, 'plain')
        msg['Subject'] = f"[VideoForge] {len(alerts)} Priority Alert(s) — action needed"
        msg['From'] = smtp_user
        msg['To'] = notify_email

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [notify_email], msg.as_string())

        logger.info(
            f"trend_monitor: Alert email sent to {notify_email} "
            f"({len(alerts)} alert(s))"
        )
    except Exception as exc:
        logger.error(
            f"trend_monitor: Failed to send alert email: {exc}",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Main scan function
# ---------------------------------------------------------------------------

def run_scan(config: dict) -> dict:
    """
    Run a full trend scan across all configured seed keyword buckets.

    Steps:
    1. Check rate limits — abort if exceeded
    2. Query Google Trends per bucket (batched to respect 5-keyword API limit)
    3. Filter results above priority_alert_threshold
    4. For each spike: call Claude for channel relevance check
    5. If channel_fit >= priority_alert_fit_minimum: create priority_alert
    6. Log the scan to trend_scans table
    7. Send email notification if any new alerts were created

    Args:
        config (dict): Loaded config.json contents.

    Returns:
        dict: {
            'success':      bool,
            'topics_found': int,
            'new_alerts':   int,
            'blocked':      bool,
            'reason':       str,   # set if blocked or error
            'alerts':       list,  # newly created alert dicts
        }
    """
    from database import init_db
    init_db()

    logger.info("trend_monitor: Starting trend scan")
    t_start = time.time()

    # --- Rate limit check ---
    allowed, reason = _check_rate_limits(config)
    if not allowed:
        logger.warning(f"trend_monitor: Scan blocked — {reason}")
        insert_trend_scan(topics_found=0, new_alerts=0, status='blocked')
        return {
            'success': False, 'blocked': True, 'reason': reason,
            'topics_found': 0, 'new_alerts': 0, 'alerts': [],
        }

    rc            = config.get('research', {})
    threshold     = rc.get('priority_alert_threshold', 150)
    fit_minimum   = rc.get('priority_alert_fit_minimum', 7.0)
    window_hours  = rc.get('fast_track_window_hours', 48)
    seed_keywords = rc.get('seed_keywords', {})

    topics_found = 0
    new_alerts   = 0
    new_alert_dicts: list[dict] = []
    buckets_scanned: list[str]  = []

    for bucket, keywords in seed_keywords.items():
        if not keywords:
            continue

        buckets_scanned.append(bucket)
        logger.info(
            f"trend_monitor: Scanning bucket '{bucket}' "
            f"({len(keywords)} keywords)"
        )

        # pytrends supports max 5 keywords per request — batch if needed
        for i in range(0, len(keywords), 5):
            batch = keywords[i:i + 5]
            trend_data = _query_trends(batch)

            # Small delay between batches to avoid rate limiting pytrends
            if i + 5 < len(keywords):
                time.sleep(2)

            for kw, data in trend_data.items():
                spike_pct = data['spike_percent']
                if spike_pct < threshold:
                    logger.debug(
                        f"trend_monitor: '{kw}' spike {spike_pct:.0f}% "
                        f"below threshold {threshold}% — skipping"
                    )
                    continue

                topics_found += 1
                logger.info(
                    f"trend_monitor: SPIKE detected — '{kw}' at {spike_pct:.0f}% "
                    f"(threshold: {threshold}%) — checking channel relevance"
                )

                # Claude relevance check
                relevance = _check_relevance(kw, bucket, config)
                if relevance is None:
                    continue

                channel_fit = float(relevance.get('channel_fit', 0))
                fits        = bool(relevance.get('fits_channel', False))

                if channel_fit < fit_minimum or not fits:
                    logger.info(
                        f"trend_monitor: '{kw}' — fit {channel_fit}/10 below "
                        f"minimum {fit_minimum} or fits_channel=False — skipped"
                    )
                    continue

                # Create alert
                expires_at = (
                    datetime.utcnow() + timedelta(hours=window_hours)
                ).isoformat(timespec='seconds')

                alert_id = insert_priority_alert(
                    topic=kw,
                    bucket=bucket,
                    spike_percent=spike_pct,
                    channel_fit=channel_fit,
                    hook_suggestion=relevance.get('hook_suggestion', ''),
                    reframed_angle=relevance.get('reframed_angle', ''),
                    window_hours=window_hours,
                    expires_at=expires_at,
                    why_trending=relevance.get('why_trending', ''),
                    why_relevant=relevance.get('why_relevant', ''),
                    angle_options=relevance.get('angle_options_json', '[]'),
                    urgency=relevance.get('urgency', 'medium'),
                )
                new_alerts += 1
                new_alert_dicts.append({
                    'id':             alert_id,
                    'topic':          kw,
                    'bucket':         bucket,
                    'spike_percent':  spike_pct,
                    'channel_fit':    channel_fit,
                    'hook_suggestion': relevance.get('hook_suggestion', ''),
                    'reframed_angle': relevance.get('reframed_angle', ''),
                    'why_trending':   relevance.get('why_trending', ''),
                    'why_relevant':   relevance.get('why_relevant', ''),
                    'urgency':        relevance.get('urgency', 'medium'),
                    'expires_at':     expires_at,
                })
                logger.info(
                    f"trend_monitor: ALERT created — '{kw}' (fit: {channel_fit}/10, "
                    f"spike: {spike_pct:.0f}%, expires: {expires_at})"
                )

    elapsed = round(time.time() - t_start, 1)

    # Log the scan to DB
    insert_trend_scan(
        topics_found=topics_found,
        new_alerts=new_alerts,
        buckets_scanned=','.join(buckets_scanned),
        status='complete',
    )

    logger.info(
        f"trend_monitor: Scan complete — {topics_found} spike(s) found, "
        f"{new_alerts} alert(s) created, {elapsed}s elapsed"
    )

    # Email notification
    if new_alert_dicts:
        _send_alert_email(new_alert_dicts, config)

    return {
        'success':      True,
        'blocked':      False,
        'reason':       '',
        'topics_found': topics_found,
        'new_alerts':   new_alerts,
        'alerts':       new_alert_dicts,
    }
