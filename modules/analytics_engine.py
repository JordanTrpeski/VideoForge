"""
analytics_engine.py
===================
Stage 8 of the VideoForge pipeline. Pulls view counts, likes, comments,
shares, and retention metrics from YouTube Data API v3 + YouTube Analytics
API v2 and TikTok Content Posting API for every posted job.  Stores a
time-stamped snapshot per pull — rows are never updated, always inserted.

Guard conditions (skip gracefully, no exception raised):
  YouTube Data API    — YOUTUBE_CLIENT_SECRETS_FILE missing OR token absent
  YouTube Analytics   — same token; graceful 403 if yt-analytics.readonly
                        scope not yet granted (re-consent required on next
                        upload auth)
  TikTok              — TIKTOK_ACCESS_TOKEN or TIKTOK_CLIENT_KEY not set
                        OR access token is expired (HTTP 401)

Input:  All jobs with status='posted'
        reads youtube_video_id and tiktok_video_id from DB
Output: Rows inserted into the analytics table (snapshot accumulation)
Logs:   logs/analytics_engine.log

Dependencies:
    - google-api-python-client (YouTube Data API v3 + Analytics API v2)
    - google-auth (credentials refresh)
    - requests (TikTok API)
"""

# 1. Standard library
import os
import sys
import time
from pathlib import Path

# 2. Third-party libraries
from dotenv import load_dotenv

load_dotenv()

# Ensure project root is importable regardless of working directory
sys.path.insert(0, str(Path(__file__).parent.parent))

# 3. Local modules
from database import get_all_jobs, insert_analytics
from utils.logger import setup_logger

logger = setup_logger('analytics_engine')

_YOUTUBE_SCOPES = [
    'https://www.googleapis.com/auth/youtube.readonly',
    'https://www.googleapis.com/auth/yt-analytics.readonly',
]


# ---------------------------------------------------------------------------
# Internal: build authenticated YouTube credentials
# ---------------------------------------------------------------------------

def _load_youtube_creds(job_id: str, token_path: Path):
    """
    Load and optionally refresh YouTube OAuth credentials from token_path.

    Returns:
        Credentials object, or None if unavailable.
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GRequest

        logger.debug(f"[JOB {job_id}] Loading YouTube credentials from {token_path}")
        creds = Credentials.from_authorized_user_file(str(token_path), _YOUTUBE_SCOPES)

        if creds.expired and creds.refresh_token:
            logger.info(f"[JOB {job_id}] YouTube credentials expired — refreshing token")
            creds.refresh(GRequest())
            token_path.write_text(creds.to_json(), encoding='utf-8')
            logger.info(f"[JOB {job_id}] YouTube token refreshed and saved to {token_path}")

        return creds

    except Exception as exc:
        logger.error(
            f"[JOB {job_id}] Failed to load YouTube credentials from {token_path}: {exc}",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# YouTube Data API v3 — views, likes, comments
# ---------------------------------------------------------------------------

def _pull_youtube_stats(
    job_id: str,
    video_id: str,
    config: dict | None = None,
) -> dict | None:
    """
    Fetch view count, like count, and comment count for a YouTube video
    using the YouTube Data API v3 videos.list endpoint.

    Args:
        job_id (str):    Job identifier (for log context).
        video_id (str):  YouTube video ID e.g. 'dQw4w9WgXcQ'.
        config (dict):   Merged channel config (contains _channel metadata).

    Returns:
        dict with keys views, likes, comments, shares — or None if skipped/failed.
    """
    channel_meta = (config or {}).get('_channel', {})
    secrets_file = (
        channel_meta.get('youtube_secrets_path')
        or os.getenv('YOUTUBE_CLIENT_SECRETS_FILE', 'client_secrets.json')
    )
    token_path = Path(
        channel_meta.get('youtube_token_path') or 'token.json'
    )

    if not Path(secrets_file).exists():
        logger.warning(
            f"[JOB {job_id}] YouTube client secrets file not found "
            f"('{secrets_file}') — skipping YouTube analytics"
        )
        return None

    if not token_path.exists():
        logger.warning(
            f"[JOB {job_id}] {token_path} not found — run upload first to complete OAuth "
            "— skipping YouTube analytics"
        )
        return None

    try:
        from googleapiclient.discovery import build

        creds = _load_youtube_creds(job_id, token_path)
        if creds is None:
            return None

        logger.info(
            f"[JOB {job_id}] Calling YouTube Data API v3 videos.list "
            f"— video_id: {video_id}"
        )
        t0 = time.time()

        youtube  = build('youtube', 'v3', credentials=creds)
        response = youtube.videos().list(
            part='statistics',
            id=video_id,
        ).execute()

        elapsed = round(time.time() - t0, 2)
        logger.info(
            f"[JOB {job_id}] YouTube Data API call succeeded "
            f"— response time: {elapsed:.2f}s"
        )

        items = response.get('items', [])
        if not items:
            logger.warning(
                f"[JOB {job_id}] YouTube API returned no items for video_id: {video_id} "
                "— video may be private or deleted"
            )
            return None

        stats = items[0].get('statistics', {})
        result = {
            'views':    int(stats.get('viewCount',    0)),
            'likes':    int(stats.get('likeCount',    0)),
            'comments': int(stats.get('commentCount', 0)),
            'shares':   0,
        }
        logger.debug(
            f"[JOB {job_id}] YouTube Data API stats — views: {result['views']}, "
            f"likes: {result['likes']}, comments: {result['comments']}"
        )
        return result

    except Exception as exc:
        logger.error(
            f"[JOB {job_id}] YouTube Data API analytics pull failed: {exc}",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# YouTube Analytics API v2 — retention, avg duration, subscribers gained
# ---------------------------------------------------------------------------

def _pull_youtube_analytics_v2(
    job_id: str,
    video_id: str,
    config: dict | None = None,
) -> dict | None:
    """
    Fetch averageViewDuration, averageViewPercentage, and subscribersGained
    for a single YouTube video using the YouTube Analytics API v2.

    Requires the yt-analytics.readonly scope.  If the scope has not been
    consented yet the API returns HTTP 403 — we log a warning and return
    None so the rest of the pipeline continues unaffected.  The user will
    need to re-auth (the upload_engine now requests the scope; re-running
    the upload step triggers the consent screen).

    Args:
        job_id (str):    Job identifier (for log context).
        video_id (str):  YouTube video ID.
        config (dict):   Merged channel config.

    Returns:
        dict with avg_view_duration (seconds), avg_view_percentage (0-100),
        subscribers_gained — or None if unavailable.
    """
    from datetime import date

    channel_meta = (config or {}).get('_channel', {})
    secrets_file = (
        channel_meta.get('youtube_secrets_path')
        or os.getenv('YOUTUBE_CLIENT_SECRETS_FILE', 'client_secrets.json')
    )
    token_path = Path(
        channel_meta.get('youtube_token_path') or 'token.json'
    )

    if not Path(secrets_file).exists() or not token_path.exists():
        return None

    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError

        creds = _load_youtube_creds(job_id, token_path)
        if creds is None:
            return None

        # Analytics API v2 requires channel == MINE and video filter
        today       = date.today().isoformat()
        start_date  = '2020-01-01'

        logger.info(
            f"[JOB {job_id}] Calling YouTube Analytics API v2 "
            f"— video_id: {video_id}, range: {start_date} → {today}"
        )
        t0 = time.time()

        ya_service = build('youtubeAnalytics', 'v2', credentials=creds)
        response   = ya_service.reports().query(
            ids='channel==MINE',
            dimensions='video',
            metrics='views,likes,comments,subscribersGained,averageViewDuration,averageViewPercentage',
            filters=f'video=={video_id}',
            startDate=start_date,
            endDate=today,
        ).execute()

        elapsed = round(time.time() - t0, 2)
        logger.info(
            f"[JOB {job_id}] YouTube Analytics API v2 call succeeded "
            f"— response time: {elapsed:.2f}s"
        )

        # Parse column-indexed response
        headers = [h['name'] for h in response.get('columnHeaders', [])]
        rows    = response.get('rows', [])

        if not rows:
            logger.debug(
                f"[JOB {job_id}] YouTube Analytics API returned no rows for {video_id} "
                "— video may be too new or have < 1 hour of watch time"
            )
            return None

        row  = rows[0]
        data = dict(zip(headers, row))

        result = {
            'avg_view_duration':   data.get('averageViewDuration'),
            'avg_view_percentage': data.get('averageViewPercentage'),
            'subscribers_gained':  int(data.get('subscribersGained', 0) or 0),
        }
        logger.debug(
            f"[JOB {job_id}] YouTube Analytics v2 — "
            f"avg_duration: {result['avg_view_duration']}s, "
            f"avg_retention: {result['avg_view_percentage']}%, "
            f"subscribers_gained: {result['subscribers_gained']}"
        )
        return result

    except Exception as exc:
        exc_str = str(exc)
        if '403' in exc_str or 'insufficientPermissions' in exc_str:
            logger.warning(
                f"[JOB {job_id}] YouTube Analytics API v2 returned 403 — "
                "yt-analytics.readonly scope not yet granted. "
                "Re-auth via the upload flow to consent to the new scope."
            )
        else:
            logger.error(
                f"[JOB {job_id}] YouTube Analytics API v2 call failed: {exc}",
                exc_info=True,
            )
        return None


# ---------------------------------------------------------------------------
# TikTok
# ---------------------------------------------------------------------------

def _pull_tiktok_stats(job_id: str, video_id: str) -> dict | None:
    """
    Fetch view count, likes, comments, and shares for a TikTok video using
    the TikTok Content Posting API v2 video query endpoint.

    Args:
        job_id (str):   Job identifier (for log context).
        video_id (str): TikTok video ID.

    Returns:
        dict with keys views, likes, comments, shares — or None if skipped/failed.
    """
    access_token = os.getenv('TIKTOK_ACCESS_TOKEN', '').strip()
    client_key   = os.getenv('TIKTOK_CLIENT_KEY',   '').strip()

    if not access_token or not client_key:
        logger.warning(
            f"[JOB {job_id}] TIKTOK_ACCESS_TOKEN or TIKTOK_CLIENT_KEY not set "
            "— skipping TikTok analytics"
        )
        return None

    try:
        import requests

        url = 'https://open.tiktokapis.com/v2/video/query/'
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type':  'application/json; charset=UTF-8',
        }
        payload = {
            'filters': {'video_ids': [video_id]},
            'fields':  ['id', 'view_count', 'like_count', 'comment_count', 'share_count'],
        }

        logger.info(
            f"[JOB {job_id}] Calling TikTok API video.query — video_id: {video_id}"
        )
        t0 = time.time()

        response = requests.post(url, json=payload, headers=headers, timeout=15)
        elapsed  = round(time.time() - t0, 2)

        if response.status_code == 401:
            logger.warning(
                f"[JOB {job_id}] TikTok access token expired (HTTP 401) "
                "— re-auth required via /health page — skipping TikTok analytics"
            )
            return None

        if response.status_code != 200:
            logger.warning(
                f"[JOB {job_id}] TikTok API returned HTTP {response.status_code} "
                "— skipping TikTok analytics"
            )
            return None

        logger.info(
            f"[JOB {job_id}] TikTok API call succeeded — response time: {elapsed:.2f}s"
        )

        data   = response.json().get('data', {})
        videos = data.get('videos', [])

        if not videos:
            logger.warning(
                f"[JOB {job_id}] TikTok API returned no videos for video_id: {video_id}"
            )
            return None

        v = videos[0]
        result = {
            'views':    int(v.get('view_count',    0)),
            'likes':    int(v.get('like_count',    0)),
            'comments': int(v.get('comment_count', 0)),
            'shares':   int(v.get('share_count',   0)),
        }
        logger.debug(
            f"[JOB {job_id}] TikTok stats — views: {result['views']}, "
            f"likes: {result['likes']}, comments: {result['comments']}, "
            f"shares: {result['shares']}"
        )
        return result

    except Exception as exc:
        logger.error(
            f"[JOB {job_id}] TikTok analytics pull failed: {exc}",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Per-job entry point
# ---------------------------------------------------------------------------

def pull_analytics_for_job(
    job_id: str,
    youtube_video_id: str | None,
    tiktok_video_id:  str | None,
    config: dict | None = None,
    channel_id: str = 'engineering_brief',
) -> dict:
    """
    Pull and store analytics for a single job from both platforms.

    Combines YouTube Data API v3 (views/likes/comments) with
    YouTube Analytics API v2 (retention/duration/subscribers).  Both
    calls share the same token file.  Results are always inserted as a
    new snapshot row — never updated.

    Args:
        job_id (str):            Job identifier.
        youtube_video_id (str):  YouTube video ID, or None to skip.
        tiktok_video_id (str):   TikTok video ID, or None to skip.
        config (dict):           Merged channel config.
        channel_id (str):        Channel slug for the analytics row.

    Returns:
        dict: {'youtube': bool, 'tiktok': bool}
    """
    logger.info(
        f"[JOB {job_id}] Starting analytics_engine — "
        f"youtube_id: {youtube_video_id}, tiktok_id: {tiktok_video_id}, "
        f"channel: {channel_id}"
    )

    results = {'youtube': False, 'tiktok': False}

    # --- YouTube ---
    if youtube_video_id:
        yt    = _pull_youtube_stats(job_id, youtube_video_id, config)
        yt_v2 = _pull_youtube_analytics_v2(job_id, youtube_video_id, config)

        if yt:
            insert_analytics(
                job_id=job_id,
                platform='youtube',
                views=yt['views'],
                likes=yt['likes'],
                comments=yt['comments'],
                shares=yt['shares'],
                channel_id=channel_id,
                avg_view_duration=yt_v2.get('avg_view_duration') if yt_v2 else None,
                avg_view_percentage=yt_v2.get('avg_view_percentage') if yt_v2 else None,
                subscribers_gained=yt_v2.get('subscribers_gained') if yt_v2 else None,
                data_source='api',
            )
            logger.info(
                f"[JOB {job_id}] YouTube analytics stored — "
                f"views: {yt['views']}, likes: {yt['likes']}, "
                f"comments: {yt['comments']}"
                + (
                    f", retention: {yt_v2['avg_view_percentage']:.1f}%"
                    if yt_v2 and yt_v2.get('avg_view_percentage') is not None
                    else ''
                )
            )
            results['youtube'] = True
    else:
        logger.debug(
            f"[JOB {job_id}] No YouTube video ID on record — skipping YouTube analytics"
        )

    # --- TikTok ---
    if tiktok_video_id:
        tt = _pull_tiktok_stats(job_id, tiktok_video_id)
        if tt:
            insert_analytics(
                job_id=job_id,
                platform='tiktok',
                views=tt['views'],
                likes=tt['likes'],
                comments=tt['comments'],
                shares=tt['shares'],
                channel_id=channel_id,
                data_source='api',
            )
            logger.info(
                f"[JOB {job_id}] TikTok analytics stored — "
                f"views: {tt['views']}, likes: {tt['likes']}, "
                f"comments: {tt['comments']}, shares: {tt['shares']}"
            )
            results['tiktok'] = True
    else:
        logger.debug(
            f"[JOB {job_id}] No TikTok video ID on record — skipping TikTok analytics"
        )

    return results


# ---------------------------------------------------------------------------
# Bulk entry point — called by scheduler + /api/refresh-analytics
# ---------------------------------------------------------------------------

def pull_all_analytics(channel_id: str | None = None) -> dict:
    """
    Pull and store analytics for every posted job that has at least one
    platform video ID stored in the database.

    Snapshot-safe: always inserts new rows, never overwrites history.

    Called by:
      - APScheduler every Monday at 06:00 (per channel)
      - POST /api/refresh-analytics (manual trigger from dashboard)

    Args:
        channel_id (str | None): Limit to one channel, or None for all.

    Returns:
        dict: {
            'jobs_processed':  int,
            'youtube_updated': int,
            'tiktok_updated':  int,
            'errors':          int,
            'elapsed':         float,
        }
    """
    from database import init_db
    from utils.config_loader import load_channel_config

    init_db()

    t_start = time.time()
    label   = f"channel={channel_id}" if channel_id else "all channels"
    logger.info(f"analytics_engine: Starting full analytics pull — {label}")

    posted_jobs = get_all_jobs(status_filter='posted', channel_id=channel_id)

    if not posted_jobs:
        logger.info(f"analytics_engine: No posted jobs found ({label}) — nothing to pull")
        return {
            'jobs_processed':  0,
            'youtube_updated': 0,
            'tiktok_updated':  0,
            'errors':          0,
            'elapsed':         0.0,
        }

    logger.info(f"analytics_engine: Found {len(posted_jobs)} posted job(s) — {label}")

    jobs_processed  = 0
    youtube_updated = 0
    tiktok_updated  = 0
    errors          = 0

    for job in posted_jobs:
        job_id           = job['id']
        youtube_video_id = job.get('youtube_video_id')
        tiktok_video_id  = job.get('tiktok_video_id')
        job_channel      = job.get('channel_id', 'engineering_brief')

        if not youtube_video_id and not tiktok_video_id:
            logger.debug(f"[JOB {job_id}] No platform video IDs in DB — skipping")
            continue

        # Load per-channel config for token path resolution
        try:
            job_config = load_channel_config(job_channel)
        except Exception:
            job_config = {}

        try:
            r = pull_analytics_for_job(
                job_id=job_id,
                youtube_video_id=youtube_video_id,
                tiktok_video_id=tiktok_video_id,
                config=job_config,
                channel_id=job_channel,
            )
            jobs_processed += 1
            if r['youtube']:
                youtube_updated += 1
            if r['tiktok']:
                tiktok_updated += 1

        except Exception as exc:
            logger.error(
                f"[JOB {job_id}] Unexpected error during analytics pull: {exc}",
                exc_info=True,
            )
            errors += 1

    elapsed = round(time.time() - t_start, 1)
    logger.info(
        f"analytics_engine: Pull complete — "
        f"{jobs_processed} jobs processed, "
        f"{youtube_updated} YouTube updated, "
        f"{tiktok_updated} TikTok updated, "
        f"{errors} errors — {elapsed}s total"
    )

    return {
        'jobs_processed':  jobs_processed,
        'youtube_updated': youtube_updated,
        'tiktok_updated':  tiktok_updated,
        'errors':          errors,
        'elapsed':         elapsed,
    }
