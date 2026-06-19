"""
tiktok_upload.py
================
Phase 13 Block D — Standalone TikTok uploader for teaser shorts.

Distinct from upload_engine.py (which handles the long-form path's combined
YouTube+TikTok+Instagram funnel). This module is used by the dual-output
scheduler to push the teaser short to TikTok 6 hours after its YouTube short
goes live, with the YouTube long-form URL injected into the description.

Credential file: channels/<slug>/tiktok_token.json
    Structure: {"access_token": "...", "open_id": "...", ...}
    Missing or malformed → skipped gracefully.

Per-channel enable flag: config.upload.tiktok = true (default true).

Returns the standard module result dict: {success, skipped?, url?, error?}.

Logs: logs/tiktok_upload.log
"""

# 1. Standard library
import json
import os
import time
from pathlib import Path
from typing import Optional

# 2. Third-party
import requests

# 3. Local modules
from database import update_job_field, update_job_status
from utils.logger import setup_logger

logger = setup_logger('tiktok_upload')


# TikTok Content Posting API v2 — direct file upload endpoint.
TIKTOK_INIT_URL = 'https://open.tiktokapis.com/v2/post/publish/inbox/video/init/'
TIKTOK_QUERY_URL = 'https://open.tiktokapis.com/v2/post/publish/status/fetch/'

# Max description chars TikTok will accept on direct post
MAX_DESCRIPTION_CHARS = 2200


def _load_token(channel_slug: str) -> Optional[dict]:
    """
    Load the channel's TikTok credentials file.

    Args:
        channel_slug (str): Channel slug.

    Returns:
        dict | None: Parsed token blob, or None if missing or unreadable.
    """
    path = Path(f'channels/{channel_slug}/tiktok_token.json')
    if not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not data.get('access_token'):
            return None
        return data
    except Exception as exc:
        logger.warning(f"tiktok_upload: failed to read {path}: {exc}")
        return None


def _build_description(metadata: dict, youtube_long_url: Optional[str]) -> str:
    """
    Compose the TikTok description with the YouTube long-form URL appended.

    Args:
        metadata (dict):          Job metadata (tiktok_title + youtube_description).
        youtube_long_url (str):   Link to the long-form video to drive viewers to.

    Returns:
        str: Description string, hard-trimmed to MAX_DESCRIPTION_CHARS.
    """
    base = metadata.get('tiktok_title') or metadata.get('youtube_title') or ''
    desc = (metadata.get('youtube_description') or base).strip()
    if youtube_long_url:
        desc = f"{desc}\n\nWatch the full story: {youtube_long_url}"
    if len(desc) > MAX_DESCRIPTION_CHARS:
        desc = desc[:MAX_DESCRIPTION_CHARS - 1].rstrip() + '…'
    return desc


def upload_to_tiktok(
    job_id: str,
    video_path: Path,
    metadata: dict,
    channel_slug: str,
    config: dict,
    youtube_long_url: Optional[str] = None,
) -> dict:
    """
    Push a teaser short to TikTok using the channel's stored token.

    Args:
        job_id (str):           Owning teaser-short job id.
        video_path (Path):      Path to the final captioned MP4.
        metadata (dict):        Loaded SEO metadata.
        channel_slug (str):     Channel slug — determines token + config overlay.
        config (dict):          Merged channel config dict.
        youtube_long_url (str): Long-form URL to inject into the description.

    Returns:
        dict: {success, skipped?, url?, video_id?, error?}.
    """
    stage_start = time.time()
    logger.info(f"[JOB {job_id}] tiktok_upload — starting (channel: {channel_slug})")

    # Per-channel enable flag (default true)
    enabled = config.get('upload', {}).get('tiktok', True)
    if not enabled:
        msg = f"upload.tiktok=false for channel {channel_slug} — skipping"
        logger.info(f"[JOB {job_id}] {msg}")
        return {'success': False, 'skipped': True, 'error': msg}

    token = _load_token(channel_slug)
    if token is None:
        msg = (
            f"channels/{channel_slug}/tiktok_token.json missing or invalid — "
            "skipping TikTok upload. Authorise via /health/reauth/tiktok."
        )
        logger.warning(f"[JOB {job_id}] {msg}")
        return {'success': False, 'skipped': True, 'error': msg}

    if not video_path.exists():
        return {'success': False, 'error': f'Video not found: {video_path}'}

    access_token = token['access_token']
    description = _build_description(metadata, youtube_long_url)

    try:
        # Step 1 — initialise upload session
        init_headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type':  'application/json; charset=UTF-8',
        }
        video_size = video_path.stat().st_size
        init_body = {
            'source_info': {
                'source':           'FILE_UPLOAD',
                'video_size':       video_size,
                'chunk_size':       video_size,  # single chunk for short videos
                'total_chunk_count': 1,
            },
            'post_info': {
                'title':            description,
                'privacy_level':    config.get('upload', {}).get('tiktok_privacy', 'SELF_ONLY'),
                'disable_duet':     False,
                'disable_stitch':   False,
                'disable_comment':  False,
            },
        }
        logger.info(f"[JOB {job_id}] tiktok init — size {video_size} bytes")
        init = requests.post(TIKTOK_INIT_URL, headers=init_headers,
                             json=init_body, timeout=30)
        if init.status_code != 200:
            return {'success': False,
                    'error': f'TikTok init failed: {init.status_code} {init.text[:300]}'}
        init_data = init.json().get('data', {})
        upload_url = init_data.get('upload_url')
        publish_id = init_data.get('publish_id')
        if not upload_url or not publish_id:
            return {'success': False, 'error': f'TikTok init missing upload_url: {init_data}'}

        # Step 2 — PUT the bytes to the upload URL
        with open(video_path, 'rb') as f:
            up = requests.put(
                upload_url,
                headers={
                    'Content-Type':   'video/mp4',
                    'Content-Range':  f'bytes 0-{video_size - 1}/{video_size}',
                },
                data=f, timeout=300,
            )
        if up.status_code not in (200, 201, 204):
            return {'success': False,
                    'error': f'TikTok PUT failed: {up.status_code} {up.text[:300]}'}

        # Step 3 — record usage and let TikTok process asynchronously
        try:
            from utils.usage_tracker import track as _usage_track
            _usage_track(
                'tiktok', 'post.publish', units=1,
                channel_id=channel_slug, job_id=job_id, config=config,
            )
        except Exception:
            pass

        tt_url = f"https://www.tiktok.com/@{token.get('open_id','')}/video/{publish_id}"
        update_job_field(job_id, 'tiktok_url', tt_url)
        update_job_field(job_id, 'tiktok_video_id', publish_id)
        elapsed = time.time() - stage_start
        logger.info(
            f"[JOB {job_id}] tiktok_upload COMPLETED in {elapsed:.1f}s — {tt_url}"
        )
        return {
            'success': True, 'skipped': False,
            'url': tt_url, 'video_id': publish_id,
        }

    except requests.exceptions.RequestException as exc:
        logger.error(f"[JOB {job_id}] tiktok_upload network error: {exc}", exc_info=True)
        return {'success': False, 'error': f'TikTok network error: {exc}'}
    except Exception as exc:
        logger.error(f"[JOB {job_id}] tiktok_upload FAILED: {exc}", exc_info=True)
        return {'success': False, 'error': str(exc)}
