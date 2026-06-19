"""
instagram_upload.py
===================
Phase 13 Block D — Instagram Graph API uploader for teaser Reels.

Reads channels/<slug>/instagram_token.json:
    {"access_token": "...", "ig_user_id": "...", ...}

The flow is the standard Instagram Graph API media + media_publish two-step:
  1. POST /<ig_user_id>/media with media_type=REELS, video_url, caption.
  2. Poll GET /<container_id>?fields=status_code until FINISHED.
  3. POST /<ig_user_id>/media_publish with creation_id=container_id.

Because Instagram needs a public URL for the video, the dual-output scheduler
should pass the R2-hosted preview_url (Block F) as the video_url. If R2 is
unavailable, this module returns skipped=True with a clear message.

Per-channel enable flag: config.upload.instagram = true (default true).

Logs: logs/instagram_upload.log
"""

# 1. Standard library
import json
import time
from pathlib import Path
from typing import Optional

# 2. Third-party
import requests

# 3. Local modules
from database import update_job_field
from utils.logger import setup_logger

logger = setup_logger('instagram_upload')


GRAPH_BASE = 'https://graph.facebook.com/v19.0'
MAX_CAPTION_CHARS = 2200


def _load_token(channel_slug: str) -> Optional[dict]:
    """Load and validate the channel's Instagram credential file."""
    path = Path(f'channels/{channel_slug}/instagram_token.json')
    if not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not (data.get('access_token') and data.get('ig_user_id')):
            return None
        return data
    except Exception as exc:
        logger.warning(f"instagram_upload: failed to read {path}: {exc}")
        return None


def _build_caption(metadata: dict, youtube_long_url: Optional[str]) -> str:
    """Compose the Reel caption with the long-form YouTube URL appended."""
    base = metadata.get('youtube_title') or metadata.get('tiktok_title') or ''
    cap = (metadata.get('youtube_description') or base).strip()
    if youtube_long_url:
        cap = f"{cap}\n\nWatch the full story: {youtube_long_url}"
    if len(cap) > MAX_CAPTION_CHARS:
        cap = cap[:MAX_CAPTION_CHARS - 1].rstrip() + '…'
    return cap


def upload_to_instagram(
    job_id: str,
    video_url: str,
    metadata: dict,
    channel_slug: str,
    config: dict,
    youtube_long_url: Optional[str] = None,
) -> dict:
    """
    Publish a Reels container to Instagram.

    Args:
        job_id (str):           Owning teaser-short job id.
        video_url (str):        Public URL of the video file (R2 preview_url).
        metadata (dict):        Loaded metadata.json blob.
        channel_slug (str):     Channel slug for the credentials file + config.
        config (dict):          Merged channel config dict.
        youtube_long_url (str): Long-form YouTube URL to inject.

    Returns:
        dict: {success, skipped?, url?, container_id?, error?}.
    """
    stage_start = time.time()
    logger.info(f"[JOB {job_id}] instagram_upload — starting (channel: {channel_slug})")

    enabled = config.get('upload', {}).get('instagram', True)
    if not enabled:
        msg = f"upload.instagram=false for channel {channel_slug} — skipping"
        logger.info(f"[JOB {job_id}] {msg}")
        return {'success': False, 'skipped': True, 'error': msg}

    token = _load_token(channel_slug)
    if token is None:
        msg = (
            f"channels/{channel_slug}/instagram_token.json missing or invalid — "
            "skipping Instagram upload."
        )
        logger.warning(f"[JOB {job_id}] {msg}")
        return {'success': False, 'skipped': True, 'error': msg}

    if not video_url:
        return {'success': False, 'skipped': True,
                'error': 'No public video_url available (R2 preview missing).'}

    caption = _build_caption(metadata, youtube_long_url)
    access_token = token['access_token']
    ig_user_id = token['ig_user_id']

    try:
        # Step 1 — create media container
        create_resp = requests.post(
            f'{GRAPH_BASE}/{ig_user_id}/media',
            data={
                'media_type':  'REELS',
                'video_url':   video_url,
                'caption':     caption,
                'access_token': access_token,
            },
            timeout=30,
        )
        if create_resp.status_code != 200:
            return {'success': False,
                    'error': f'IG create failed: {create_resp.status_code} '
                             f'{create_resp.text[:300]}'}
        container_id = create_resp.json().get('id')
        if not container_id:
            return {'success': False,
                    'error': f'IG create missing container id: {create_resp.text[:300]}'}

        # Step 2 — poll container until FINISHED (cap at ~3 minutes)
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(5)
            stat = requests.get(
                f'{GRAPH_BASE}/{container_id}',
                params={'fields': 'status_code', 'access_token': access_token},
                timeout=20,
            )
            code = stat.json().get('status_code', 'UNKNOWN')
            logger.debug(f"[JOB {job_id}] IG container {container_id} status: {code}")
            if code == 'FINISHED':
                break
            if code == 'ERROR':
                return {'success': False,
                        'error': f'IG container processing ERROR: {stat.text[:300]}'}
        else:
            return {'success': False, 'error': 'IG container did not FINISH in 180s'}

        # Step 3 — publish container
        publish = requests.post(
            f'{GRAPH_BASE}/{ig_user_id}/media_publish',
            data={'creation_id': container_id, 'access_token': access_token},
            timeout=30,
        )
        if publish.status_code != 200:
            return {'success': False,
                    'error': f'IG publish failed: {publish.status_code} '
                             f'{publish.text[:300]}'}
        media_id = publish.json().get('id')
        ig_url = f'https://www.instagram.com/reel/{media_id}/'

        try:
            from utils.usage_tracker import track as _usage_track
            _usage_track(
                'instagram', 'media.publish', units=1,
                channel_id=channel_slug, job_id=job_id, config=config,
            )
        except Exception:
            pass

        elapsed = time.time() - stage_start
        logger.info(f"[JOB {job_id}] instagram_upload COMPLETED in {elapsed:.1f}s — {ig_url}")
        update_job_field(job_id, 'review_note',
                         f'instagram:{ig_url}')  # best-effort log
        return {
            'success': True, 'skipped': False,
            'url': ig_url, 'container_id': container_id,
        }

    except requests.exceptions.RequestException as exc:
        logger.error(f"[JOB {job_id}] instagram_upload network error: {exc}", exc_info=True)
        return {'success': False, 'error': f'IG network error: {exc}'}
    except Exception as exc:
        logger.error(f"[JOB {job_id}] instagram_upload FAILED: {exc}", exc_info=True)
        return {'success': False, 'error': str(exc)}
