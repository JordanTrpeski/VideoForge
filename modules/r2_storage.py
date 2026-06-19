"""
r2_storage.py
=============
Phase 13 Block F — Cloudflare R2 client + upload helpers.

R2 is S3-compatible — we use boto3 with a custom endpoint URL. Credentials
come from .env (R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET, R2_BUCKET). The
caller (caption_engine or a hook in the pipeline) calls
upload_preview_for_job() after captioning completes. The function returns the
public URL and inserts an r2_objects row for the retention sweep.

On any failure the function returns {success: False, error: ...} — the caller
treats this as a non-blocking warning and the dashboard simply falls back to
the local file path.

Public URL: when config.r2.public_base_url is set, it is used as-is
(<base>/<key>). Otherwise the R2 default endpoint URL plus key is used —
useful for testing but not always public-readable without a custom domain.

Logs: logs/r2_storage.log
"""

# 1. Standard library
import json
import mimetypes
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# 2. Third-party
from dotenv import load_dotenv

# 3. Local modules
from database import insert_r2_object, update_job_field
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger('r2_storage')


def get_r2_client():
    """
    Return an S3-style boto3 client pointed at Cloudflare R2, or None if any
    of the required credentials are missing.

    Returns:
        boto3.client | None
    """
    account = (os.getenv('R2_ACCOUNT_ID') or '').strip()
    access  = (os.getenv('R2_ACCESS_KEY') or '').strip()
    secret  = (os.getenv('R2_SECRET') or '').strip()
    bucket  = (os.getenv('R2_BUCKET') or '').strip()
    if not all([account, access, secret, bucket]):
        return None
    try:
        import boto3
        from botocore.client import Config
    except ImportError:
        logger.warning("r2_storage: boto3 not installed — install via pip")
        return None
    try:
        return boto3.client(
            's3',
            endpoint_url=f'https://{account}.r2.cloudflarestorage.com',
            aws_access_key_id=access,
            aws_secret_access_key=secret,
            config=Config(signature_version='s3v4', region_name='auto'),
        )
    except Exception as exc:
        logger.warning(f"r2_storage: failed to build R2 client: {exc}")
        return None


def _bucket() -> str:
    """Return the configured R2 bucket name."""
    return (os.getenv('R2_BUCKET') or '').strip()


def _public_url(bucket: str, key: str, config: Optional[dict] = None) -> str:
    """
    Compose a public-facing URL for the uploaded object.

    Args:
        bucket (str):  R2 bucket name.
        key (str):     Object key.
        config (dict): Merged channel config (may carry r2.public_base_url).

    Returns:
        str: Public URL.
    """
    base = ((config or {}).get('r2', {}).get('public_base_url') or '').rstrip('/')
    if base:
        return f"{base}/{key}"
    account = (os.getenv('R2_ACCOUNT_ID') or '').strip()
    return f"https://{account}.r2.cloudflarestorage.com/{bucket}/{key}"


def _retention_expiry(config: Optional[dict] = None) -> str:
    """
    Compute the UTC ISO expires_at timestamp from r2.retention_days
    (default 7).

    Args:
        config (dict): Merged channel config.

    Returns:
        str: 'YYYY-MM-DD HH:MM:SS' UTC.
    """
    days = int((config or {}).get('r2', {}).get('retention_days', 7))
    when = datetime.utcnow() + timedelta(days=days)
    return when.strftime('%Y-%m-%d %H:%M:%S')


def upload_file(
    client,
    local_path: Path,
    key: str,
    bucket: Optional[str] = None,
) -> int:
    """
    Upload one file to R2 with auto content-type detection.

    Args:
        client:          boto3 S3 client.
        local_path:      Path to the local file.
        key (str):       Destination object key.
        bucket (str):    Bucket override; defaults to .env R2_BUCKET.

    Returns:
        int: Size in bytes (best-effort; 0 on stat failure).
    """
    bucket = bucket or _bucket()
    ctype, _ = mimetypes.guess_type(str(local_path))
    extra = {'ContentType': ctype or 'application/octet-stream'}
    client.upload_file(str(local_path), bucket, key, ExtraArgs=extra)
    try:
        return local_path.stat().st_size
    except Exception:
        return 0


def delete_object(client, bucket: str, key: str) -> None:
    """Delete one R2 object."""
    client.delete_object(Bucket=bucket, Key=key)


def upload_preview_for_job(job_id: str, config: dict) -> dict:
    """
    Upload the captioned video + chosen thumbnail to R2 and stamp preview URLs
    on the job row.

    Never raises — on any failure returns {success: False, error: ...} so the
    pipeline keeps moving and the dashboard falls back to local paths.

    Args:
        job_id (str):  Job identifier.
        config (dict): Merged channel config.

    Returns:
        dict: {success, skipped?, video_url?, thumbnail_url?, error?}.
    """
    client = get_r2_client()
    if client is None:
        logger.info(f"[JOB {job_id}] r2 upload skipped — credentials not set")
        return {'success': False, 'skipped': True,
                'error': 'R2 credentials not set in .env'}

    # Resolve local files
    final_video = Path(f'output/videos/{job_id}_captioned.mp4')
    if not final_video.exists():
        final_video = Path(f'output/videos/{job_id}_raw.mp4')
    if not final_video.exists():
        return {'success': False, 'error': f'No video file found for job {job_id}'}

    # Pick thumbnail: chosen variant if any, else default
    from database import get_job
    job = get_job(job_id) or {}
    variant = int(job.get('thumbnail_variant') or 0)
    thumb_candidates = []
    if variant:
        thumb_candidates.append(Path(f'output/thumbnails/{job_id}_v{variant}.jpg'))
    thumb_candidates.append(Path(f'output/thumbnails/{job_id}_v1.jpg'))
    thumb_candidates.append(Path(f'output/thumbnails/{job_id}.jpg'))
    thumb_path = next((t for t in thumb_candidates if t.exists()), None)

    bucket = _bucket()
    channel = job.get('channel_id') or config.get('default_channel') or 'unknown'
    expires_at = _retention_expiry(config)

    # Upload video
    video_key = f"previews/{channel}/{job_id}/{final_video.name}"
    try:
        size = upload_file(client, final_video, video_key, bucket=bucket)
        video_url = _public_url(bucket, video_key, config)
        insert_r2_object(
            job_id=job_id, kind='video', bucket=bucket, key=video_key,
            url=video_url, size_bytes=size,
            channel_id=channel, expires_at=expires_at,
        )
        update_job_field(job_id, 'preview_url', video_url)
        update_job_field(job_id, 'preview_uploaded_at',
                         datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'))
        try:
            from utils.usage_tracker import track as _usage_track
            _usage_track('r2', 'put_object', units=1,
                         channel_id=channel, job_id=job_id, config=config)
        except Exception:
            pass
        logger.info(
            f"[JOB {job_id}] r2 video uploaded — {video_url} "
            f"({size} bytes, expires {expires_at})"
        )
    except Exception as exc:
        logger.warning(f"[JOB {job_id}] r2 video upload FAILED: {exc}")
        return {'success': False, 'error': f'R2 video upload failed: {exc}'}

    # Upload thumbnail (non-fatal if it fails or is missing)
    thumb_url = None
    if thumb_path is not None:
        thumb_key = f"previews/{channel}/{job_id}/{thumb_path.name}"
        try:
            tsize = upload_file(client, thumb_path, thumb_key, bucket=bucket)
            thumb_url = _public_url(bucket, thumb_key, config)
            insert_r2_object(
                job_id=job_id, kind='thumbnail', bucket=bucket, key=thumb_key,
                url=thumb_url, size_bytes=tsize,
                channel_id=channel, expires_at=expires_at,
            )
            update_job_field(job_id, 'preview_thumb_url', thumb_url)
            try:
                from utils.usage_tracker import track as _usage_track
                _usage_track('r2', 'put_object', units=1,
                             channel_id=channel, job_id=job_id, config=config)
            except Exception:
                pass
            logger.info(
                f"[JOB {job_id}] r2 thumbnail uploaded — {thumb_url} ({tsize} bytes)"
            )
        except Exception as exc:
            logger.warning(f"[JOB {job_id}] r2 thumbnail upload FAILED: {exc}")

    return {'success': True, 'video_url': video_url, 'thumbnail_url': thumb_url}
