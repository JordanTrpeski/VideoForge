"""
upload_engine.py
================
Stage 7 of the VideoForge pipeline. Uploads the captioned video and thumbnail
to YouTube Shorts and TikTok using their respective APIs, then stores the
published URLs and video IDs in the database.

YouTube flow:
  1. Load client secrets from YOUTUBE_CLIENT_SECRETS_FILE (OAuth 2.0 app creds)
  2. First run opens a browser for user consent; saves token.json for reuse
  3. Subsequent runs load token.json, refreshing silently when expired
  4. Resumable video upload via videos().insert()
  5. Thumbnail set via thumbnails().set()

TikTok flow:
  1. Check TIKTOK_ACCESS_TOKEN; if absent run OAuth 2.0 code flow via local
     callback server using TIKTOK_CLIENT_KEY + TIKTOK_CLIENT_SECRET
  2. Initialize upload via Content Posting API v2 (FILE_UPLOAD source)
  3. Upload video in a single chunk via the signed upload URL
  4. Poll publish status until PUBLISH_COMPLETE or error

Guard conditions (skip gracefully, exit 0, DB untouched):
  YouTube  — YOUTUBE_CLIENT_SECRETS_FILE env var missing OR the file does
             not exist at that path
  TikTok   — TIKTOK_CLIENT_KEY or TIKTOK_CLIENT_SECRET env var is empty

Input:  job_id, config dict
        reads output/videos/NNN_captioned.mp4
        reads output/thumbnails/NNN.jpg
        reads output/metadata/NNN.json
Output: updates DB: youtube_url, youtube_video_id, tiktok_url, tiktok_video_id
        sets job status to 'posted' when at least one platform succeeds
Logs:   logs/upload_engine.log

Dependencies:
    - google-api-python-client
    - google-auth-oauthlib
    - google-auth-httplib2
    - requests (TikTok HTTP calls)
    - python-dotenv

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import json
import os
import shutil
import socket
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse, urlencode

# 2. Third-party libraries
import requests
from dotenv import load_dotenv

# 3. Local modules
from database import update_job_status, update_job_field, get_job
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger('upload_engine')

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

YOUTUBE_SCOPES = [
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/yt-analytics.readonly',
]
YOUTUBE_API_VERSION = 'v3'
YOUTUBE_UPLOAD_CHUNKSIZE = 10 * 1024 * 1024  # 10 MB per chunk
YOUTUBE_SHORTS_CATEGORY_ID = '28'            # Science & Technology
YOUTUBE_TOKEN_FILE = 'token.json'

TIKTOK_AUTH_URL = 'https://www.tiktok.com/v2/auth/authorize/'
TIKTOK_TOKEN_URL = 'https://open.tiktokapis.com/v2/oauth/token/'
TIKTOK_INIT_URL = 'https://open.tiktokapis.com/v2/post/publish/video/init/'
TIKTOK_STATUS_URL = 'https://open.tiktokapis.com/v2/post/publish/status/fetch/'
TIKTOK_TOKEN_FILE = 'token_tiktok.json'
TIKTOK_OAUTH_REDIRECT_PORT = 8765
TIKTOK_POLL_INTERVAL = 5       # seconds between status polls
TIKTOK_POLL_MAX_ATTEMPTS = 60  # 5 minutes total


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _load_metadata(job_id: str) -> dict:
    """
    Load and return the metadata JSON for the given job.

    Args:
        job_id (str): Job identifier e.g. '001'.

    Returns:
        dict: Metadata fields including titles, descriptions, hashtags.

    Raises:
        FileNotFoundError: If metadata file does not exist.
    """
    path = Path(f'output/metadata/{job_id}.json')
    if not path.exists():
        raise FileNotFoundError(
            f"Metadata not found: {path}. Run generate-metadata first."
        )
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _resolve_video_path(job_id: str) -> Path:
    """
    Return the captioned video path, falling back to raw if needed.

    Args:
        job_id (str): Job identifier.

    Returns:
        Path: Video file path.

    Raises:
        FileNotFoundError: If neither video file exists.
    """
    captioned = Path(f'output/videos/{job_id}_captioned.mp4')
    raw = Path(f'output/videos/{job_id}_raw.mp4')

    if captioned.exists():
        return captioned
    if raw.exists():
        logger.warning(
            f"[JOB {job_id}] Captioned video not found — uploading raw video instead"
        )
        return raw
    raise FileNotFoundError(
        f"No video file found for job {job_id}. Run assemble first."
    )


# ---------------------------------------------------------------------------
# YouTube — guards, auth, upload
# ---------------------------------------------------------------------------

def _check_youtube_ready(job_id: str) -> tuple:
    """
    Determine whether the YouTube upload can proceed.

    Checks that YOUTUBE_CLIENT_SECRETS_FILE is set and the file exists.
    Does NOT perform auto-discovery of client_secret_*.json files —
    the user must explicitly configure the path in .env.

    Args:
        job_id (str): Job identifier for log context.

    Returns:
        tuple: (ready: bool, message: str, secrets_path: str | None)
    """
    env_value = os.getenv('YOUTUBE_CLIENT_SECRETS_FILE', '').strip()

    if not env_value:
        msg = (
            "YOUTUBE_CLIENT_SECRETS_FILE is not set in .env. "
            "Set it to the path of your Google OAuth client secrets JSON file "
            "(download from Google Cloud Console → APIs & Services → Credentials)."
        )
        logger.warning(f"[JOB {job_id}] YouTube SKIP: {msg}")
        return False, msg, None

    secrets_path = Path(env_value)
    if not secrets_path.exists():
        # Give a helpful hint if the file exists under a different name
        discovered = sorted(Path('.').glob('client_secret_*.json'))
        hint = ''
        if discovered:
            hint = (
                f" Found '{discovered[0].name}' in project root — "
                f"rename it to '{env_value}' or update YOUTUBE_CLIENT_SECRETS_FILE in .env."
            )
        msg = (
            f"Client secrets file not found: '{env_value}'.{hint} "
            "Skipping YouTube upload."
        )
        logger.warning(f"[JOB {job_id}] YouTube SKIP: {msg}")
        return False, msg, None

    return True, 'OK', str(secrets_path)


def _get_youtube_service(secrets_file: str, job_id: str, token_path: str = None):
    """
    Build and return an authenticated YouTube API service object.

    On first run (no token.json), opens a browser for OAuth consent and
    saves the resulting credentials to token.json. On subsequent runs,
    loads token.json and refreshes silently when expired.

    Args:
        secrets_file (str): Path to the OAuth client secrets JSON.
        job_id (str):       Job identifier for log context.

    Returns:
        googleapiclient.discovery.Resource: Authenticated YouTube service.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    token_path = Path(token_path) if token_path else Path(YOUTUBE_TOKEN_FILE)

    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(
                str(token_path), YOUTUBE_SCOPES
            )
            logger.debug(
                f"[JOB {job_id}] Loaded YouTube token from {token_path}"
            )
        except Exception as e:
            logger.warning(
                f"[JOB {job_id}] Failed to load token.json ({e}) — "
                "will re-authenticate"
            )
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info(f"[JOB {job_id}] Refreshing expired YouTube token")
            creds.refresh(Request())
        else:
            logger.info(
                f"[JOB {job_id}] No valid YouTube token — "
                "opening browser for OAuth consent"
            )
            flow = InstalledAppFlow.from_client_secrets_file(
                secrets_file, YOUTUBE_SCOPES
            )
            creds = flow.run_local_server(port=0, open_browser=True)
            logger.info(f"[JOB {job_id}] YouTube OAuth consent granted")

        # Persist for future runs
        token_path.write_text(creds.to_json(), encoding='utf-8')
        logger.info(f"[JOB {job_id}] YouTube token saved to {token_path}")

    service = build('youtube', YOUTUBE_API_VERSION, credentials=creds)
    return service


def _upload_youtube_video(
    service,
    video_path: Path,
    thumbnail_path: Path,
    metadata: dict,
    job_id: str
) -> dict:
    """
    Upload a video to YouTube Shorts with resumable upload and set thumbnail.

    Args:
        service:            Authenticated YouTube API service.
        video_path (Path):  Video file to upload.
        thumbnail_path (Path): Thumbnail JPEG to attach.
        metadata (dict):    Metadata JSON with titles, description, tags.
        job_id (str):       Job identifier for log context.

    Returns:
        dict: {'video_id': str, 'url': str}

    Raises:
        Exception: On non-retryable API errors.
    """
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError

    # Add #Shorts to description to help YouTube classify as a Short
    description = metadata['youtube_description']
    if '#Shorts' not in description:
        description = description + '\n\n#Shorts'

    body = {
        'snippet': {
            'title':       metadata['youtube_title'],
            'description': description,
            'tags':        metadata['youtube_tags'] + ['Shorts'],
            'categoryId':  YOUTUBE_SHORTS_CATEGORY_ID,
        },
        'status': {
            'privacyStatus':           'public',
            'selfDeclaredMadeForKids': False,
            'madeForKids':             False,
            'containsSyntheticMedia':  True,
        },
    }

    video_size_mb = video_path.stat().st_size / (1024 * 1024)
    logger.info(
        f"[JOB {job_id}] Starting YouTube upload: {video_path.name} "
        f"({video_size_mb:.2f} MB)"
    )

    media = MediaFileUpload(
        str(video_path),
        mimetype='video/mp4',
        resumable=True,
        chunksize=YOUTUBE_UPLOAD_CHUNKSIZE,
    )

    disclosure_accepted = True

    def _do_insert(upload_body):
        return service.videos().insert(
            part='snippet,status',
            body=upload_body,
            media_body=media,
        )

    insert_request = _do_insert(body)

    response = None
    retry_count = 0
    upload_start = time.time()

    while response is None:
        try:
            status_chunk, response = insert_request.next_chunk()
            if status_chunk:
                progress = int(status_chunk.progress() * 100)
                elapsed = time.time() - upload_start
                logger.info(
                    f"[JOB {job_id}] YouTube upload progress: {progress}% "
                    f"({elapsed:.0f}s elapsed)"
                )
        except HttpError as e:
            # YouTube may reject containsSyntheticMedia on older API versions or
            # accounts that haven't enabled the altered-content feature.
            # Fall back without the field and mark the disclosure checklist.
            if e.resp.status == 400 and 'containsSyntheticMedia' in str(e.content):
                logger.warning(
                    f"[JOB {job_id}] YouTube rejected containsSyntheticMedia — "
                    f"retrying without field. Owner MUST set disclosure manually in Studio."
                )
                disclosure_accepted = False
                body['status'].pop('containsSyntheticMedia', None)
                # Reset media upload — must create a fresh MediaFileUpload
                media2 = MediaFileUpload(
                    str(video_path),
                    mimetype='video/mp4',
                    resumable=True,
                    chunksize=YOUTUBE_UPLOAD_CHUNKSIZE,
                )
                insert_request = _do_insert(body)
                insert_request._media = media2
                response = None
                continue
            elif e.resp.status in (500, 502, 503, 504):
                retry_count += 1
                if retry_count > 5:
                    raise Exception(
                        f"YouTube upload failed after {retry_count} retries: {e}"
                    )
                wait = 5 * retry_count
                logger.warning(
                    f"[JOB {job_id}] YouTube server error {e.resp.status} — "
                    f"waiting {wait}s (retry {retry_count}/5)"
                )
                time.sleep(wait)
            else:
                raise

    if not disclosure_accepted:
        update_job_field(job_id, 'disclosure_checklist_required', 1)
        logger.warning(
            f"[JOB {job_id}] disclosure_checklist_required set — "
            f"review gate will enforce manual Studio disclosure."
        )

    video_id = response['id']
    video_url = f'https://www.youtube.com/shorts/{video_id}'
    elapsed = time.time() - upload_start
    logger.info(
        f"[JOB {job_id}] YouTube upload complete — "
        f"video_id: {video_id}, url: {video_url}, time: {elapsed:.0f}s"
    )

    # Set thumbnail
    if thumbnail_path.exists():
        try:
            logger.info(f"[JOB {job_id}] Setting YouTube thumbnail")
            service.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail_path), mimetype='image/jpeg'),
            ).execute()
            logger.info(f"[JOB {job_id}] YouTube thumbnail set successfully")
        except Exception as e:
            logger.warning(
                f"[JOB {job_id}] Failed to set YouTube thumbnail (non-fatal): {e}"
            )
    else:
        logger.warning(
            f"[JOB {job_id}] Thumbnail not found at {thumbnail_path} — skipping"
        )

    return {'video_id': video_id, 'url': video_url}


# ---------------------------------------------------------------------------
# TikTok — guards, OAuth, upload
# ---------------------------------------------------------------------------

def _check_tiktok_ready(job_id: str) -> tuple:
    """
    Determine whether the TikTok upload can proceed.

    Requires TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET to be non-empty.
    An access token (TIKTOK_ACCESS_TOKEN) is optional — the OAuth flow
    is run automatically if it is missing.

    Args:
        job_id (str): Job identifier for log context.

    Returns:
        tuple: (ready: bool, message: str)
    """
    client_key = os.getenv('TIKTOK_CLIENT_KEY', '').strip()
    client_secret = os.getenv('TIKTOK_CLIENT_SECRET', '').strip()

    if not client_key or not client_secret:
        msg = (
            "TIKTOK_CLIENT_KEY and/or TIKTOK_CLIENT_SECRET are not set in .env. "
            "Apply for TikTok developer access at developers.tiktok.com, "
            "create an app, and add the keys when approved. "
            "Skipping TikTok upload."
        )
        logger.warning(f"[JOB {job_id}] TikTok SKIP: {msg}")
        return False, msg

    return True, 'OK'


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler to capture the TikTok OAuth redirect code."""

    auth_code = None

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        if 'code' in params:
            _OAuthCallbackHandler.auth_code = params['code'][0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(
                b'<html><body><h2>TikTok authorisation successful.</h2>'
                b'<p>You can close this tab and return to the terminal.</p>'
                b'</body></html>'
            )
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'Missing code parameter')

    def log_message(self, format, *args):  # suppress default access log noise
        pass


def _run_tiktok_oauth(
    client_key: str,
    client_secret: str,
    job_id: str,
    token_path: str = None,
) -> str:
    """
    Run TikTok OAuth 2.0 authorization code flow via a local callback server.

    Opens the browser for user consent and listens on localhost for the
    redirect. Exchanges the code for an access token, saves it to
    token_tiktok.json (or the channel-specific path), and returns the token string.

    Args:
        client_key (str):    TikTok app client key.
        client_secret (str): TikTok app client secret.
        job_id (str):        Job identifier for log context.
        token_path (str):    Override path for the saved token file.

    Returns:
        str: Access token.

    Raises:
        TimeoutError: If the user does not complete auth within 120 seconds.
        Exception: On token exchange failure.
    """
    redirect_uri = f'http://localhost:{TIKTOK_OAUTH_REDIRECT_PORT}/'
    _OAuthCallbackHandler.auth_code = None

    auth_params = {
        'client_key':     client_key,
        'response_type':  'code',
        'scope':          'video.publish,video.upload',
        'redirect_uri':   redirect_uri,
        'state':          'videoforge',
    }
    auth_url = TIKTOK_AUTH_URL + '?' + urlencode(auth_params)

    # Start local callback server in a thread
    server = HTTPServer(('localhost', TIKTOK_OAUTH_REDIRECT_PORT), _OAuthCallbackHandler)

    def _serve():
        server.handle_request()

    t = Thread(target=_serve, daemon=True)
    t.start()

    logger.info(
        f"[JOB {job_id}] Opening browser for TikTok OAuth consent: {auth_url}"
    )
    webbrowser.open(auth_url)

    # Wait for callback
    deadline = time.time() + 120
    while _OAuthCallbackHandler.auth_code is None:
        if time.time() > deadline:
            raise TimeoutError(
                "TikTok OAuth timed out after 120s. "
                "Complete the browser authorisation and retry."
            )
        time.sleep(0.5)

    auth_code = _OAuthCallbackHandler.auth_code
    logger.info(f"[JOB {job_id}] TikTok auth code received — exchanging for token")

    # Exchange code for token
    token_response = requests.post(
        TIKTOK_TOKEN_URL,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        data={
            'client_key':     client_key,
            'client_secret':  client_secret,
            'code':           auth_code,
            'grant_type':     'authorization_code',
            'redirect_uri':   redirect_uri,
        },
        timeout=30,
    )
    token_response.raise_for_status()
    token_data = token_response.json()

    if token_data.get('error'):
        raise Exception(
            f"TikTok token exchange failed: {token_data.get('error_description', token_data)}"
        )

    access_token = token_data['data']['access_token']
    open_id = token_data['data']['open_id']

    # Persist
    token_payload = {
        'access_token':  access_token,
        'refresh_token': token_data['data'].get('refresh_token', ''),
        'open_id':       open_id,
        'expires_in':    token_data['data'].get('expires_in', 86400),
        'obtained_at':   time.time(),
    }
    _tt_path = Path(token_path) if token_path else Path(TIKTOK_TOKEN_FILE)
    _tt_path.parent.mkdir(parents=True, exist_ok=True)
    _tt_path.write_text(json.dumps(token_payload, indent=2), encoding='utf-8')
    logger.info(
        f"[JOB {job_id}] TikTok token saved to {_tt_path} "
        f"(open_id: {open_id})"
    )
    return access_token


def _get_tiktok_access_token(
    client_key: str,
    client_secret: str,
    job_id: str,
    token_path: str = None,
) -> str:
    """
    Return a valid TikTok access token.

    Priority:
      1. TIKTOK_ACCESS_TOKEN env var (manually set)
      2. Channel-specific token file (or token_tiktok.json) from a previous OAuth run
      3. Run the OAuth flow (opens browser)

    Args:
        client_key (str):    TikTok app client key.
        client_secret (str): TikTok app client secret.
        job_id (str):        Job identifier for log context.
        token_path (str):    Override path for the token file (channel-specific).

    Returns:
        str: Valid access token.
    """
    # 1. Manual env var (takes precedence — simplest for daily use)
    env_token = os.getenv('TIKTOK_ACCESS_TOKEN', '').strip()
    if env_token:
        logger.debug(f"[JOB {job_id}] Using TIKTOK_ACCESS_TOKEN from .env")
        return env_token

    # 2. Saved token from previous OAuth run
    token_file = Path(token_path) if token_path else Path(TIKTOK_TOKEN_FILE)
    if token_file.exists():
        try:
            saved = json.loads(token_file.read_text(encoding='utf-8'))
            obtained_at = saved.get('obtained_at', 0)
            expires_in = saved.get('expires_in', 86400)
            # Consider valid if not yet expired (with 5-minute buffer)
            if time.time() < obtained_at + expires_in - 300:
                logger.debug(
                    f"[JOB {job_id}] Using saved TikTok token from {token_file}"
                )
                return saved['access_token']
            else:
                logger.info(
                    f"[JOB {job_id}] Saved TikTok token is expired — re-authorising"
                )
        except Exception as e:
            logger.warning(
                f"[JOB {job_id}] Could not read {token_file}: {e} — re-authorising"
            )

    # 3. Full OAuth flow
    return _run_tiktok_oauth(client_key, client_secret, job_id, token_path=str(token_file))


def _upload_tiktok_video(
    access_token: str,
    video_path: Path,
    metadata: dict,
    job_id: str
) -> dict:
    """
    Upload a video to TikTok using the Content Posting API v2 FILE_UPLOAD flow.

    Steps:
      1. Initialize upload session (get publish_id + upload_url)
      2. PUT video bytes to the signed upload URL
      3. Poll publish status until PUBLISH_COMPLETE

    Args:
        access_token (str): Valid TikTok access token.
        video_path (Path):  Video file to upload.
        metadata (dict):    Metadata with tiktok_title and tiktok_hashtags.
        job_id (str):       Job identifier for log context.

    Returns:
        dict: {'video_id': str, 'url': str}

    Raises:
        Exception: On API errors or publish failure.
    """
    video_size = video_path.stat().st_size
    # TikTok allows up to 64 MB per chunk; upload in one chunk if possible
    chunk_size = min(video_size, 64 * 1024 * 1024)
    total_chunk_count = -(-video_size // chunk_size)  # ceiling division

    # Build caption: title + hashtags
    hashtags_str = ' '.join(metadata.get('tiktok_hashtags', []))
    caption = f"{metadata['tiktok_title']}\n\n{hashtags_str}"
    caption = caption[:2200]  # TikTok caption limit

    auth_headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type':  'application/json; charset=UTF-8',
    }

    # ----------------------------------------------------------------
    # Step 1: Initialize upload
    # ----------------------------------------------------------------
    logger.info(
        f"[JOB {job_id}] Initialising TikTok upload — "
        f"size: {video_size / (1024*1024):.2f} MB, chunks: {total_chunk_count}"
    )
    init_payload = {
        'post_info': {
            'title':                    caption,
            'privacy_level':            'PUBLIC_TO_EVERYONE',
            'disable_duet':             False,
            'disable_comment':          False,
            'disable_stitch':           False,
            'video_cover_timestamp_ms': 5000,
        },
        'source_info': {
            'source':             'FILE_UPLOAD',
            'video_size':         video_size,
            'chunk_size':         chunk_size,
            'total_chunk_count':  total_chunk_count,
        },
    }

    init_response = requests.post(
        TIKTOK_INIT_URL,
        headers=auth_headers,
        json=init_payload,
        timeout=30,
    )
    init_response.raise_for_status()
    init_data = init_response.json()

    if init_data.get('error', {}).get('code', 'ok') != 'ok':
        raise Exception(
            f"TikTok init failed: {init_data.get('error', {}).get('message', init_data)}"
        )

    publish_id = init_data['data']['publish_id']
    upload_url = init_data['data']['upload_url']
    logger.info(
        f"[JOB {job_id}] TikTok upload initialised — publish_id: {publish_id}"
    )

    # ----------------------------------------------------------------
    # Step 2: Upload video chunks
    # ----------------------------------------------------------------
    upload_start = time.time()
    with open(video_path, 'rb') as f:
        for chunk_index in range(total_chunk_count):
            chunk_data = f.read(chunk_size)
            actual_chunk_size = len(chunk_data)
            byte_start = chunk_index * chunk_size
            byte_end = byte_start + actual_chunk_size - 1

            logger.info(
                f"[JOB {job_id}] TikTok uploading chunk "
                f"{chunk_index + 1}/{total_chunk_count} "
                f"(bytes {byte_start}-{byte_end}/{video_size})"
            )
            upload_response = requests.put(
                upload_url,
                headers={
                    'Content-Type':   'video/mp4',
                    'Content-Range':  f'bytes {byte_start}-{byte_end}/{video_size}',
                    'Content-Length': str(actual_chunk_size),
                },
                data=chunk_data,
                timeout=120,
            )
            if upload_response.status_code not in (200, 206):
                raise Exception(
                    f"TikTok chunk upload failed: HTTP {upload_response.status_code} "
                    f"— {upload_response.text[:200]}"
                )

    upload_elapsed = time.time() - upload_start
    logger.info(
        f"[JOB {job_id}] TikTok video uploaded in {upload_elapsed:.1f}s"
    )

    # ----------------------------------------------------------------
    # Step 3: Poll publish status
    # ----------------------------------------------------------------
    logger.info(f"[JOB {job_id}] Polling TikTok publish status")
    video_id = None

    for attempt in range(1, TIKTOK_POLL_MAX_ATTEMPTS + 1):
        time.sleep(TIKTOK_POLL_INTERVAL)

        status_response = requests.post(
            TIKTOK_STATUS_URL,
            headers=auth_headers,
            json={'publish_id': publish_id},
            timeout=15,
        )
        if status_response.status_code != 200:
            logger.warning(
                f"[JOB {job_id}] TikTok status poll HTTP {status_response.status_code} "
                f"(attempt {attempt}) — retrying"
            )
            continue

        status_data = status_response.json()
        publish_status = (
            status_data.get('data', {}).get('status', 'UNKNOWN')
        )
        logger.debug(
            f"[JOB {job_id}] TikTok status poll #{attempt}: {publish_status}"
        )

        if publish_status == 'PUBLISH_COMPLETE':
            video_id = status_data['data'].get('publicaly_available_post_id', [''])[0]
            break
        elif publish_status in ('FAILED', 'SEND_TO_USER_INBOX_FAILED'):
            raise Exception(
                f"TikTok publish failed: {status_data.get('data', {}).get('fail_reason', 'unknown')}"
            )

    if not video_id:
        raise TimeoutError(
            f"TikTok publish did not complete within "
            f"{TIKTOK_POLL_MAX_ATTEMPTS * TIKTOK_POLL_INTERVAL}s "
            f"(publish_id: {publish_id})"
        )

    video_url = f'https://www.tiktok.com/@HowThingsWorkEng/video/{video_id}'
    logger.info(
        f"[JOB {job_id}] TikTok publish complete — "
        f"video_id: {video_id}, url: {video_url}"
    )
    return {'video_id': video_id, 'url': video_url}


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Permanent archive
# ---------------------------------------------------------------------------

def archive_job_files(job_id: str, channel_id: str) -> None:
    """
    Copy the four permanent artefacts to archive/<channel>/<job>/ after a
    successful upload.  The archive directory is never cleaned up.

    Copies:
      - Final (captioned) video
      - Chosen thumbnail
      - Script JSON
      - Metadata JSON

    Args:
        job_id (str):     Job identifier.
        channel_id (str): Channel identifier (used as directory component).
    """
    archive_dir = Path(f'archive/{channel_id}/{job_id}')
    archive_dir.mkdir(parents=True, exist_ok=True)

    job = get_job(job_id)
    if not job:
        logger.warning(f"[JOB {job_id}] archive_job_files: job not found in DB — skipping")
        return

    candidates = {
        'final_video': job.get('final_video_path'),
        'thumbnail':   job.get('thumbnail_path'),
        'script':      f'output/scripts/{job_id}.json',
        'metadata':    f'output/metadata/{job_id}.json',
    }

    for name, src in candidates.items():
        if not src:
            logger.debug(f"[JOB {job_id}] Archive: {name} path is empty — skipping")
            continue
        src_path = Path(src)
        if not src_path.exists():
            logger.warning(f"[JOB {job_id}] Archive: {name} not found at {src_path} — skipping")
            continue
        dest = archive_dir / src_path.name
        try:
            shutil.copy2(str(src_path), str(dest))
            size_mb = dest.stat().st_size / (1024 * 1024)
            logger.info(f"[JOB {job_id}] Archived {name}: {dest} ({size_mb:.3f} MB)")
        except Exception as e:
            logger.warning(f"[JOB {job_id}] Archive copy failed for {name}: {e}")

    logger.info(f"[JOB {job_id}] Archive complete: {archive_dir}")


# ---------------------------------------------------------------------------
# Phase 14 Block 5 — Production evidence metadata log
# ---------------------------------------------------------------------------

def _set_review_due_at(job_id: str) -> None:
    """
    Phase 14 Block 6 — stamp jobs.review_due_at = now + 48h on the row.
    Wrapped so the upload flow can call it without import side effects.
    """
    from datetime import datetime, timedelta, timezone
    from database import set_review_due_at as _db_set
    try:
        when = (datetime.now(timezone.utc) + timedelta(hours=48)).strftime(
            '%Y-%m-%d %H:%M:%S'
        )
        _db_set(job_id, when)
    except Exception as e:
        logger.warning(
            f"[JOB {job_id}] set_review_due_at failed (non-fatal): {e}"
        )


def write_production_evidence(
    job_id: str,
    channel_id: str,
    config: dict,
    youtube_url: str | None,
    tiktok_url: str | None,
    instagram_url: str | None,
) -> Path | None:
    """
    Write archive/<channel>/<job_id>/production_evidence.json capturing every
    production decision needed for YPP review preparedness. Failures are
    logged but never raised — the upload itself must not be blocked.

    Returns:
        Path | None: Path to the written file, or None on failure.
    """
    from datetime import datetime, timezone
    try:
        archive_dir = Path(f'archive/{channel_id}/{job_id}')
        archive_dir.mkdir(parents=True, exist_ok=True)

        job = get_job(job_id) or {}

        # Pull script + metadata sidecars if present
        script_data = {}
        sp = Path(f'output/scripts/{job_id}.json')
        if sp.exists():
            try:
                script_data = json.loads(sp.read_text(encoding='utf-8'))
            except Exception:
                pass
        meta_data = {}
        mp = Path(f'output/metadata/{job_id}.json')
        if mp.exists():
            try:
                meta_data = json.loads(mp.read_text(encoding='utf-8'))
            except Exception:
                pass

        # Renderer sidecar (Block 1 emits this when FFmpeg is used)
        render_meta = {}
        rmp = Path(f'output/render_meta/{job_id}.json')
        if rmp.exists():
            try:
                render_meta = json.loads(rmp.read_text(encoding='utf-8'))
            except Exception:
                pass
        # Determine renderer (ffmpeg vs moviepy) from the channel config or sidecar
        renderer_used = (
            render_meta.get('renderer_used')
            or config.get('pipeline', {}).get('renderer', 'moviepy')
        )

        # Determine video_kind: 'short' if duration < 90s or story_role == short
        duration = job.get('duration_seconds') or render_meta.get('duration_seconds')
        story_role = (job.get('story_role') or '').lower()
        if story_role == 'short':
            video_kind = 'short'
        elif duration and duration < 90:
            video_kind = 'short'
        else:
            video_kind = 'long'

        # External-manual model record (Block 7 sets this sidecar)
        ext_model = None
        ep = Path(f'output/external_model/{job_id}.json')
        if ep.exists():
            try:
                ext_model = json.loads(ep.read_text(encoding='utf-8'))
            except Exception:
                ext_model = None

        # Visual mode + background clip used (FFmpeg sidecar carries format spec
        # too, which is informative)
        visual_mode = config.get('pipeline', {}).get('visual_mode', 'images')
        bg_clip_used = None
        # Best-effort: scan render_meta sidecar for the background path
        cmd = render_meta.get('command') or ''

        title_alternates = (
            script_data.get('title_alternates')
            or [meta_data.get('youtube_title')]
            or []
        )
        primary_title = (
            script_data.get('primary_title')
            or meta_data.get('youtube_title')
            or job.get('topic')
            or ''
        )
        primary_thumb_text = (
            script_data.get('primary_thumbnail_text')
            or meta_data.get('thumbnail_text')
            or ''
        )
        thumb_text_alternates = (
            script_data.get('thumbnail_text_alternates')
            or []
        )

        voice_cfg = config.get('voice', {}) or {}
        narration_provider = voice_cfg.get('provider')
        if narration_provider == 'kokoro':
            narration_voice_id = voice_cfg.get('kokoro_voice')
        elif narration_provider == 'openai':
            narration_voice_id = voice_cfg.get('openai_voice')
        else:
            narration_voice_id = voice_cfg.get('voice_id')

        payload = {
            'job_id':                       job_id,
            'channel':                      channel_id,
            'video_kind':                   video_kind,
            'story_premise':                job.get('topic') or '',
            'emotional_lane':               config.get('content', {}).get('emotional_lane'),
            'subgenre_chosen':              script_data.get('subgenre_chosen'),
            'script_origin':                (
                'external_manual' if ext_model
                else 'ai_generated_original'
            ),
            'claude_model_used':            (
                ext_model.get('reported_model') if ext_model
                else config.get('script', {}).get('model')
            ),
            'claude_prompt_version':        'phase_14_v1',
            'narration_voice_id':           narration_voice_id,
            'narration_provider':           narration_provider,
            'narration_target_wpm':         voice_cfg.get('target_wpm'),
            'visual_mode':                  visual_mode,
            'background_clip_used':         bg_clip_used,
            'primary_title':                primary_title,
            'title_alternates':             list(title_alternates),
            'primary_thumbnail_text':       primary_thumb_text,
            'thumbnail_text_alternates':    list(thumb_text_alternates),
            'primary_thumbnail_path':       job.get('thumbnail_path'),
            'primary_thumbnail_variant_id': job.get('thumbnail_variant'),
            'symbolic_object':              script_data.get('symbolic_object'),
            'hook_chosen':                  job.get('picked_hook_style') or job.get('hook_style'),
            'length_seconds':               duration,
            'loudness_lufs':                render_meta.get('loudness_target_lufs')
                                            or (config.get('audio') or {}).get('loudness_target_lufs'),
            'ai_disclosure_set':            True,
            'renderer_used':                renderer_used,
            'render_command':               cmd or None,
            'upload_url_youtube':           youtube_url,
            'upload_url_tiktok':            tiktok_url,
            'upload_url_instagram':         instagram_url,
            'upload_timestamp':             datetime.now(timezone.utc).strftime(
                '%Y-%m-%dT%H:%M:%SZ'
            ),
        }

        out_path = archive_dir / 'production_evidence.json'
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        logger.info(
            f"[JOB {job_id}] Production evidence written: {out_path}"
        )
        return out_path
    except Exception as e:
        logger.warning(
            f"[JOB {job_id}] write_production_evidence failed (non-fatal): {e}",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Phase 13 Block D — cross-platform fan-out for teaser shorts
# ---------------------------------------------------------------------------

def _schedule_cross_platform_followups(youtube_job_id: str, youtube_long_url: str) -> None:
    """
    After a successful YouTube upload, schedule TikTok + Instagram uploads of
    the paired teaser short 6 hours later. The schedule field used is the
    teaser short job's `scheduled_upload_at` — the existing scheduler picks it
    up via run_scheduled_uploads. The YouTube long-form URL is stashed in the
    teaser job's review_note so the platform-specific uploaders can inject it
    into descriptions.

    No-op for jobs without a linked short.
    """
    from datetime import datetime, timedelta
    from database import get_job
    job = get_job(youtube_job_id) or {}
    # Only the LONG job triggers fan-out — its linked_job_id points at the short.
    if (job.get('story_role') or '').lower() != 'long':
        return
    short_id = job.get('linked_job_id')
    if not short_id:
        return
    when = (datetime.utcnow() + timedelta(hours=6)).strftime('%Y-%m-%d %H:%M:%S')
    update_job_field(short_id, 'scheduled_upload_at', when)
    update_job_field(short_id, 'review_note', f'yt_long_url:{youtube_long_url}')
    logger.info(
        f"[JOB {youtube_job_id}] Block D — scheduled short {short_id} for "
        f"TikTok+Instagram at {when} (YouTube URL: {youtube_long_url})"
    )


def _maybe_shorten_r2_expiry(job_id: str, config: dict) -> None:
    """
    Block G — after a successful YouTube upload, optionally compress the R2
    expiry to +24h so the cloud preview frees up the day after publish.

    Behaviour gated by config.r2.keep_after_youtube_upload (default False):
      - False: shorten every active r2_objects row for this job to now+24h.
      - True:  leave expires_at alone; the standard r2.retention_days applies.

    The linked teaser short keeps its preview intact through this window so
    Instagram (which needs a public URL) can still pull from R2 6 hours later.
    """
    from datetime import datetime, timedelta
    from database import (get_r2_objects_for_job, set_r2_expiry, get_job)

    keep = bool((config.get('r2') or {}).get('keep_after_youtube_upload', False))
    if keep:
        logger.info(f"[JOB {job_id}] r2.keep_after_youtube_upload=true — leaving expiry untouched")
        return
    new_expiry = (datetime.utcnow() + timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
    rows = get_r2_objects_for_job(job_id)
    changed = 0
    for row in rows:
        if row.get('deleted'):
            continue
        set_r2_expiry(row['id'], new_expiry)
        changed += 1
    logger.info(
        f"[JOB {job_id}] Block G — R2 expiry shortened to {new_expiry} "
        f"for {changed} object(s) (post-YouTube override)"
    )

    # Also apply the override to the linked teaser short so its own R2 objects
    # get cleaned up on the same schedule (Block D scheduling already covers
    # the +6h Instagram window before then).
    job = get_job(job_id) or {}
    linked_id = job.get('linked_job_id')
    if linked_id and (job.get('story_role') or '').lower() == 'long':
        linked_rows = get_r2_objects_for_job(linked_id)
        for row in linked_rows:
            if row.get('deleted'):
                continue
            set_r2_expiry(row['id'], new_expiry)


def upload_short_cross_platform(short_job_id: str, config: dict) -> dict:
    """
    Drive the teaser short to TikTok and Instagram (Block D).

    Reads the long-form YouTube URL from the short's review_note (set by
    _schedule_cross_platform_followups). Per-platform enable flags come from
    config.upload.tiktok and config.upload.instagram. Missing credential files
    are treated as skipped (not failures).

    Args:
        short_job_id (str): Teaser short job id (status: scheduled_upload).
        config (dict):      Merged channel config.

    Returns:
        dict: {success, tiktok: {...}, instagram: {...}, error?}.
    """
    from database import get_job
    job = get_job(short_job_id) or {}
    channel_slug = job.get('channel_id') or config.get('default_channel', 'engineering_brief')

    # Recover the YouTube long URL stashed in review_note
    note = job.get('review_note') or ''
    yt_url = ''
    for line in note.splitlines():
        if line.startswith('yt_long_url:'):
            yt_url = line.split(':', 1)[1].strip()
            break

    # Load this short's metadata for descriptions
    meta_path = Path(f'output/metadata/{short_job_id}.json')
    metadata = {}
    if meta_path.exists():
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
        except Exception:
            pass

    video_path = Path(f'output/videos/{short_job_id}_captioned.mp4')
    if not video_path.exists():
        video_path = Path(f'output/videos/{short_job_id}_raw.mp4')

    results = {'tiktok': {}, 'instagram': {}}

    # TikTok
    try:
        from modules.tiktok_upload import upload_to_tiktok
        results['tiktok'] = upload_to_tiktok(
            job_id=short_job_id, video_path=video_path, metadata=metadata,
            channel_slug=channel_slug, config=config, youtube_long_url=yt_url,
        )
    except Exception as exc:
        results['tiktok'] = {'success': False, 'error': str(exc)}

    # Instagram — needs a public URL; prefer R2 preview_url if Block F set it.
    public_url = job.get('preview_url') or ''
    try:
        from modules.instagram_upload import upload_to_instagram
        results['instagram'] = upload_to_instagram(
            job_id=short_job_id, video_url=public_url, metadata=metadata,
            channel_slug=channel_slug, config=config, youtube_long_url=yt_url,
        )
    except Exception as exc:
        results['instagram'] = {'success': False, 'error': str(exc)}

    return {'success': True, **results}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def upload_video(job_id: str, config: dict) -> dict:
    """
    Upload the captioned video and thumbnail to YouTube Shorts and TikTok.

    Skips a platform gracefully (exit 0, DB untouched for that platform) if
    its credentials are not yet configured. Sets job status to 'posted' when
    at least one platform upload succeeds with no failures.

    Args:
        job_id (str):  Unique job identifier e.g. '001'.
        config (dict): Loaded config.json contents.

    Returns:
        dict: {
            'success': bool,
            'youtube': dict,   # {'skipped', 'success', 'url', 'video_id', 'error'}
            'tiktok': dict,    # {'skipped', 'success', 'url', 'video_id', 'error'}
            'error': str       # top-level error if pre-flight checks fail
        }
    """
    stage_start = time.time()
    logger.info(f"[JOB {job_id}] Starting upload_engine")

    youtube_result = {'skipped': False, 'success': False, 'url': None,
                      'video_id': None, 'error': None}
    tiktok_result  = {'skipped': False, 'success': False, 'url': None,
                      'video_id': None, 'error': None}

    try:
        # ----------------------------------------------------------------
        # Load shared inputs
        # ----------------------------------------------------------------
        metadata = _load_metadata(job_id)
        video_path = _resolve_video_path(job_id)

        # Resolve thumbnail: use the chosen variant if stored in DB, else default
        job_row = get_job(job_id)
        chosen_variant = (job_row.get('thumbnail_variant') or 0) if job_row else 0
        if chosen_variant and chosen_variant > 0:
            thumbnail_path = Path(f'output/thumbnails/{job_id}_v{chosen_variant}.jpg')
            if not thumbnail_path.exists():
                logger.warning(
                    f"[JOB {job_id}] Chosen thumbnail variant {chosen_variant} not found "
                    f"— falling back to default"
                )
                thumbnail_path = Path(f'output/thumbnails/{job_id}.jpg')
        else:
            # Check if text_template variants exist and default to v1
            v1 = Path(f'output/thumbnails/{job_id}_v1.jpg')
            thumbnail_path = v1 if v1.exists() else Path(f'output/thumbnails/{job_id}.jpg')

        logger.info(
            f"[JOB {job_id}] Uploading '{metadata['topic']}' — "
            f"video: {video_path.name}, "
            f"thumbnail: {'found' if thumbnail_path.exists() else 'missing'}"
        )

        # ----------------------------------------------------------------
        # Resolve channel credential paths
        # ----------------------------------------------------------------
        channel_meta = config.get('_channel', {})
        yt_token_path  = channel_meta.get('youtube_token_path')
        yt_secrets_override = channel_meta.get('youtube_secrets_path')
        tt_token_path  = channel_meta.get('tiktok_token_path')

        # ----------------------------------------------------------------
        # Check platform guards before doing anything
        # ----------------------------------------------------------------
        yt_ready, yt_msg, yt_secrets = _check_youtube_ready(job_id)
        # Prefer channel-specific secrets file when it exists
        if yt_secrets_override and Path(yt_secrets_override).exists():
            yt_secrets = yt_secrets_override
        tt_ready, tt_msg = _check_tiktok_ready(job_id)

        if not yt_ready:
            youtube_result['skipped'] = True
            youtube_result['error'] = yt_msg

        if not tt_ready:
            tiktok_result['skipped'] = True
            tiktok_result['error'] = tt_msg

        # ----------------------------------------------------------------
        # YouTube upload
        # ----------------------------------------------------------------
        if yt_ready:
            try:
                logger.info(f"[JOB {job_id}] Starting YouTube upload")
                service = _get_youtube_service(yt_secrets, job_id, token_path=yt_token_path)
                yt = _upload_youtube_video(
                    service, video_path, thumbnail_path, metadata, job_id
                )
                youtube_result['success'] = True
                youtube_result['url'] = yt['url']
                youtube_result['video_id'] = yt['video_id']
                update_job_field(job_id, 'youtube_url', yt['url'])
                update_job_field(job_id, 'youtube_video_id', yt['video_id'])
                # Block B — usage tracking
                try:
                    from utils.usage_tracker import track as _usage_track
                    _usage_track(
                        'youtube', 'videos.insert', units=1,
                        channel_id=(get_job(job_id) or {}).get('channel_id'),
                        job_id=job_id, config=config,
                    )
                except Exception:
                    pass
                # Block D — schedule TikTok + Instagram for the teaser short
                # 6 hours after the YouTube short publishes (avoids platform
                # duplicate-detection penalties).  Long-form jobs and jobs that
                # have no story link are left alone.
                try:
                    _schedule_cross_platform_followups(job_id, yt['url'])
                except Exception as _e:
                    logger.warning(
                        f"[JOB {job_id}] Could not schedule cross-platform followups: {_e}"
                    )
                # Block G — when keep_after_youtube_upload=false (default),
                # shorten R2 expiry to +24h so the cloud preview frees up the
                # day after publish. Long-form jobs that need their teasers to
                # use R2 (Instagram) get a 24h grace window.
                try:
                    _maybe_shorten_r2_expiry(job_id, config)
                except Exception as _e:
                    logger.warning(
                        f"[JOB {job_id}] Could not adjust R2 expiry: {_e}"
                    )
                logger.info(
                    f"[JOB {job_id}] YouTube upload done — {yt['url']}"
                )
            except Exception as e:
                youtube_result['success'] = False
                youtube_result['error'] = str(e)
                logger.error(
                    f"[JOB {job_id}] YouTube upload FAILED: {e}", exc_info=True
                )

        # ----------------------------------------------------------------
        # TikTok upload
        # ----------------------------------------------------------------
        if tt_ready:
            try:
                logger.info(f"[JOB {job_id}] Starting TikTok upload")
                client_key    = os.getenv('TIKTOK_CLIENT_KEY', '').strip()
                client_secret = os.getenv('TIKTOK_CLIENT_SECRET', '').strip()
                access_token  = _get_tiktok_access_token(
                    client_key, client_secret, job_id, token_path=tt_token_path
                )
                tt = _upload_tiktok_video(
                    access_token, video_path, metadata, job_id
                )
                tiktok_result['success'] = True
                tiktok_result['url'] = tt['url']
                tiktok_result['video_id'] = tt['video_id']
                update_job_field(job_id, 'tiktok_url', tt['url'])
                update_job_field(job_id, 'tiktok_video_id', tt['video_id'])
                # Block B — usage tracking
                try:
                    from utils.usage_tracker import track as _usage_track
                    _usage_track(
                        'tiktok', 'video.upload', units=1,
                        channel_id=(get_job(job_id) or {}).get('channel_id'),
                        job_id=job_id, config=config,
                    )
                except Exception:
                    pass
                logger.info(
                    f"[JOB {job_id}] TikTok upload done — {tt['url']}"
                )
            except Exception as e:
                tiktok_result['success'] = False
                tiktok_result['error'] = str(e)
                logger.error(
                    f"[JOB {job_id}] TikTok upload FAILED: {e}", exc_info=True
                )

        # ----------------------------------------------------------------
        # Determine overall outcome and set final status
        # ----------------------------------------------------------------
        any_failure = (
            (yt_ready and not youtube_result['success']) or
            (tt_ready and not tiktok_result['success'])
        )
        any_success = youtube_result['success'] or tiktok_result['success']
        all_skipped = youtube_result['skipped'] and tiktok_result['skipped']

        if any_failure:
            update_job_status(
                job_id, 'failed',
                error_module='upload_engine',
                error_message=(
                    f"YouTube: {youtube_result['error'] or 'OK'} | "
                    f"TikTok: {tiktok_result['error'] or 'OK'}"
                ),
            )
        elif any_success:
            update_job_status(job_id, 'posted')
            logger.info(f"[JOB {job_id}] Job status set to POSTED")
            # Permanent archive — copy artefacts after every successful upload
            try:
                channel_id = config.get('_channel', {}).get('id', config.get('default_channel', 'engineering_brief'))
                archive_job_files(job_id, channel_id)
            except Exception as e:
                logger.warning(f"[JOB {job_id}] Archive failed (non-fatal): {e}")
            # Phase 14 Block 5 — production_evidence.json (must never raise)
            try:
                channel_id = config.get('_channel', {}).get(
                    'id',
                    config.get('default_channel', 'engineering_brief'),
                )
                yt_url = youtube_result.get('url') if youtube_result.get('success') else None
                tt_url = tiktok_result.get('url') if tiktok_result.get('success') else None
                ig_url = None  # Instagram path may set this via cross-platform fan-out later
                # Phase 14 Block 5 — Phase 14 Block 6 review window
                _set_review_due_at(job_id)
                write_production_evidence(
                    job_id=job_id,
                    channel_id=channel_id,
                    config=config,
                    youtube_url=yt_url,
                    tiktok_url=tt_url,
                    instagram_url=ig_url,
                )
            except Exception as e:
                logger.warning(
                    f"[JOB {job_id}] production_evidence write failed "
                    f"(non-fatal): {e}"
                )
        else:
            # All platforms skipped — leave status unchanged (still at review)
            logger.info(
                f"[JOB {job_id}] All platforms skipped — "
                "job status unchanged (still at review)"
            )

        elapsed = time.time() - stage_start
        overall_success = not any_failure
        logger.info(
            f"[JOB {job_id}] upload_engine COMPLETED in {elapsed:.1f}s — "
            f"YouTube: {'skip' if youtube_result['skipped'] else ('OK' if youtube_result['success'] else 'FAIL')}, "
            f"TikTok: {'skip' if tiktok_result['skipped'] else ('OK' if tiktok_result['success'] else 'FAIL')}"
        )

        return {
            'success':  overall_success,
            'youtube':  youtube_result,
            'tiktok':   tiktok_result,
        }

    except Exception as e:
        elapsed = time.time() - stage_start
        logger.error(
            f"[JOB {job_id}] upload_engine FAILED (pre-flight): {e}", exc_info=True
        )
        update_job_status(
            job_id, 'failed', error_module='upload_engine', error_message=str(e)
        )
        return {'success': False, 'error': str(e),
                'youtube': youtube_result, 'tiktok': tiktok_result}
