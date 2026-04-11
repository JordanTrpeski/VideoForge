"""
analytics_engine.py
===================
Stage 8 of the VideoForge pipeline. Pulls view counts, likes, comments,
and shares from YouTube Data API v3 and TikTok Content Posting API for
every posted job, then stores a snapshot in the analytics table.

Guard conditions (skip gracefully, no exception raised):
  YouTube — YOUTUBE_CLIENT_SECRETS_FILE missing OR token.json absent
  TikTok  — TIKTOK_ACCESS_TOKEN or TIKTOK_CLIENT_KEY not set in .env
             OR access token is expired (HTTP 401)

Input:  All jobs with status='posted'
        reads youtube_video_id and tiktok_video_id from DB
Output: Rows inserted into the analytics table in videoforge.db
Logs:   logs/analytics_engine.log

Dependencies:
    - google-api-python-client (YouTube Data API v3)
    - google-auth (credentials refresh)
    - requests (TikTok API)

Author: VideoForge
Version: 1.0
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


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

def _pull_youtube_stats(job_id: str, video_id: str) -> dict | None:
    """
    Fetch view count, like count, and comment count for a YouTube video
    using the YouTube Data API v3 videos.list endpoint.

    Args:
        job_id (str):   Job identifier (for log context).
        video_id (str): YouTube video ID e.g. 'dQw4w9WgXcQ'.

    Returns:
        dict with keys views, likes, comments, shares — or None if skipped/failed.
    """
    secrets_file = os.getenv('YOUTUBE_CLIENT_SECRETS_FILE', 'client_secrets.json')
    token_file   = Path('token.json')

    if not Path(secrets_file).exists():
        logger.warning(
            f"[JOB {job_id}] YouTube client secrets file not found "
            f"('{secrets_file}') — skipping YouTube analytics"
        )
        return None

    if not token_file.exists():
        logger.warning(
            f"[JOB {job_id}] token.json not found — run upload first to complete OAuth "
            "— skipping YouTube analytics"
        )
        return None

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GRequest
        from googleapiclient.discovery import build

        logger.debug(f"[JOB {job_id}] Loading YouTube credentials from {token_file}")
        creds = Credentials.from_authorized_user_file(
            str(token_file),
            scopes=[
                'https://www.googleapis.com/auth/youtube.readonly',
                'https://www.googleapis.com/auth/yt-analytics.readonly',
            ],
        )

        if creds.expired and creds.refresh_token:
            logger.info(f"[JOB {job_id}] YouTube credentials expired — refreshing token")
            creds.refresh(GRequest())
            token_file.write_text(creds.to_json(), encoding='utf-8')
            logger.info(f"[JOB {job_id}] YouTube token refreshed and saved")

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
            f"[JOB {job_id}] YouTube API call succeeded "
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
            'shares':   0,   # YouTube Data API does not expose share count
        }
        logger.debug(
            f"[JOB {job_id}] YouTube stats — views: {result['views']}, "
            f"likes: {result['likes']}, comments: {result['comments']}"
        )
        return result

    except Exception as exc:
        logger.error(
            f"[JOB {job_id}] YouTube analytics pull failed: {exc}",
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
            'fields':  [
                'id',
                'view_count',
                'like_count',
                'comment_count',
                'share_count',
            ],
        }

        logger.info(
            f"[JOB {job_id}] Calling TikTok API video.query "
            f"— video_id: {video_id}"
        )
        t0 = time.time()

        response = requests.post(
            url, json=payload, headers=headers, timeout=15
        )
        elapsed = round(time.time() - t0, 2)

        if response.status_code == 401:
            logger.warning(
                f"[JOB {job_id}] TikTok access token expired (HTTP 401) "
                "— re-auth required via /health page — skipping TikTok analytics"
            )
            return None

        if response.status_code != 200:
            logger.warning(
                f"[JOB {job_id}] TikTok API returned HTTP {response.status_code} "
                f"— skipping TikTok analytics"
            )
            return None

        logger.info(
            f"[JOB {job_id}] TikTok API call succeeded "
            f"— response time: {elapsed:.2f}s"
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
) -> dict:
    """
    Pull and store analytics for a single job from both platforms.

    Args:
        job_id (str):            Job identifier.
        youtube_video_id (str):  YouTube video ID, or None to skip YouTube.
        tiktok_video_id (str):   TikTok video ID, or None to skip TikTok.

    Returns:
        dict: {
            'youtube': bool,  # True if a row was stored
            'tiktok':  bool,
        }
    """
    logger.info(
        f"[JOB {job_id}] Starting analytics_engine — "
        f"youtube_id: {youtube_video_id}, tiktok_id: {tiktok_video_id}"
    )

    results = {'youtube': False, 'tiktok': False}

    # --- YouTube ---
    if youtube_video_id:
        yt = _pull_youtube_stats(job_id, youtube_video_id)
        if yt:
            insert_analytics(
                job_id=job_id,
                platform='youtube',
                views=yt['views'],
                likes=yt['likes'],
                comments=yt['comments'],
                shares=yt['shares'],
            )
            logger.info(
                f"[JOB {job_id}] YouTube analytics stored — "
                f"views: {yt['views']}, likes: {yt['likes']}, "
                f"comments: {yt['comments']}"
            )
            results['youtube'] = True
    else:
        logger.debug(f"[JOB {job_id}] No YouTube video ID on record — skipping YouTube analytics")

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
            )
            logger.info(
                f"[JOB {job_id}] TikTok analytics stored — "
                f"views: {tt['views']}, likes: {tt['likes']}, "
                f"comments: {tt['comments']}, shares: {tt['shares']}"
            )
            results['tiktok'] = True
    else:
        logger.debug(f"[JOB {job_id}] No TikTok video ID on record — skipping TikTok analytics")

    return results


# ---------------------------------------------------------------------------
# Bulk entry point (called by scheduler + /api/refresh-analytics)
# ---------------------------------------------------------------------------

def pull_all_analytics() -> dict:
    """
    Pull and store analytics for every posted job that has at least one
    platform video ID stored in the database.

    Called by:
      - APScheduler every Monday at 06:00
      - POST /api/refresh-analytics (manual trigger from dashboard)

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

    init_db()

    t_start = time.time()
    logger.info("analytics_engine: Starting full analytics pull for all posted jobs")

    posted_jobs = get_all_jobs(status_filter='posted')

    if not posted_jobs:
        logger.info("analytics_engine: No posted jobs found — nothing to pull")
        return {
            'jobs_processed':  0,
            'youtube_updated': 0,
            'tiktok_updated':  0,
            'errors':          0,
            'elapsed':         0.0,
        }

    logger.info(f"analytics_engine: Found {len(posted_jobs)} posted job(s)")

    jobs_processed  = 0
    youtube_updated = 0
    tiktok_updated  = 0
    errors          = 0

    for job in posted_jobs:
        job_id           = job['id']
        youtube_video_id = job.get('youtube_video_id')
        tiktok_video_id  = job.get('tiktok_video_id')

        if not youtube_video_id and not tiktok_video_id:
            logger.debug(
                f"[JOB {job_id}] No platform video IDs in DB — skipping"
            )
            continue

        try:
            r = pull_analytics_for_job(
                job_id=job_id,
                youtube_video_id=youtube_video_id,
                tiktok_video_id=tiktok_video_id,
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
