"""
webhook.py
==========
Flask Blueprint providing the /webhook/ namespace for external automation.
Designed for Make.com → Google Sheets → VideoForge automation flows.

Security:
    All requests must supply the WEBHOOK_SECRET value from .env.
    The secret is accepted in:
      - X-Webhook-Secret HTTP header  (preferred for Make.com)
      - 'secret' field in the JSON body (fallback)

    If WEBHOOK_SECRET is not set in .env the endpoint will still work but
    logs a WARNING — configure the secret before exposing to the internet.

Endpoint:
    POST /webhook/new-topic — add a topic to the queue (and optionally
                              trigger script generation immediately)

Input:  JSON body (see new_topic docstring for field reference)
Output: JSON {job_id, topic, status, message}  HTTP 201 on success
Logs:   logs/webhook.log

Dependencies:
    - flask (Blueprint)
    - python-dotenv

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import json
import os
import threading
from pathlib import Path

# 2. Third-party libraries
from dotenv import load_dotenv
from flask import Blueprint, jsonify, request

load_dotenv()

# 3. Local modules
from database import create_job, get_next_job_id, init_db
from utils.logger import setup_logger

logger = setup_logger('webhook')

webhook_bp = Blueprint('webhook', __name__, url_prefix='/webhook')

# Valid values for validation
_VALID_BUCKETS    = {'elec', 'infra', 'vehicle', 'flaw'}
_VALID_HOOK_STYLES = {'shocking_fact', 'wrong_assumption', 'nobody_talks'}


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _check_secret() -> bool:
    """
    Validate the webhook secret provided in the incoming request.

    Accepts the secret in (checked in this order):
      1. X-Webhook-Secret HTTP header
      2. 'secret' field in the JSON request body

    If WEBHOOK_SECRET is not configured in .env, any request is allowed
    (with a WARNING logged — fix before production use).

    Returns:
        bool: True if the request is authorised.
    """
    expected = os.getenv('WEBHOOK_SECRET', '').strip()

    if not expected:
        logger.warning(
            "WEBHOOK_SECRET not set in .env — /webhook/new-topic is unprotected! "
            "Set WEBHOOK_SECRET=<random-string> in .env before making it public."
        )
        return True   # allow but warn

    # Check header first (preferred — body may not be parsed yet)
    header_secret = request.headers.get('X-Webhook-Secret', '').strip()
    if header_secret and header_secret == expected:
        return True

    # Check JSON body fallback
    data = request.get_json(silent=True) or {}
    body_secret = str(data.get('secret', '')).strip()
    if body_secret and body_secret == expected:
        return True

    return False


# ---------------------------------------------------------------------------
# Script generation helper (runs in a daemon thread)
# ---------------------------------------------------------------------------

def _run_script_async(job_id: str, topic: str, bucket: str, hook_style: str) -> None:
    """
    Run script_engine for a single job in a background thread.
    Used when run_script=true is passed in the webhook payload.

    Args:
        job_id (str):    Job identifier.
        topic (str):     Video topic.
        bucket (str):    Content bucket.
        hook_style (str):Hook style.
    """
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
    except Exception as exc:
        logger.error(
            f"[JOB {job_id}] Webhook script thread: failed to load config.json: {exc}"
        )
        return

    logger.info(
        f"[JOB {job_id}] Webhook script thread started — topic: '{topic}'"
    )

    try:
        from modules.script_engine import generate_script
        result = generate_script(
            job_id=job_id,
            topic=topic,
            config=config,
            bucket=bucket,
            hook_style=hook_style,
        )
        if result.get('success'):
            logger.info(
                f"[JOB {job_id}] Webhook script generation complete "
                f"— saved to {result.get('output_path', '?')}"
            )
        else:
            logger.error(
                f"[JOB {job_id}] Webhook script generation failed: "
                f"{result.get('error', 'unknown')}"
            )
    except Exception as exc:
        logger.error(
            f"[JOB {job_id}] Webhook script thread raised exception: {exc}",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@webhook_bp.route('/new-topic', methods=['POST'])
def new_topic():
    """
    Add a new topic to the VideoForge job queue.

    Request body (JSON):
        topic      (str,  required) — video topic text
        bucket     (str,  optional) — elec / infra / vehicle / flaw  [default: elec]
        hook_style (str,  optional) — shocking_fact / wrong_assumption /
                                      nobody_talks  [default: shocking_fact]
        run_script (bool, optional) — if true, trigger script_engine in the
                                      background immediately after creating the
                                      job  [default: false]
        secret     (str,  optional) — webhook secret (can also be in the
                                      X-Webhook-Secret header)

    Returns:
        201 JSON: {job_id, topic, status, message}
        400 JSON: {error}  — missing or invalid fields
        401 JSON: {error}  — bad or missing webhook secret
        500 JSON: {error}  — server-side failure
    """
    # --- Auth ---
    if not _check_secret():
        logger.warning(
            f"Webhook /new-topic rejected — invalid secret "
            f"from {request.remote_addr}"
        )
        return jsonify({
            'error': 'Unauthorized — invalid or missing webhook secret'
        }), 401

    # --- Parse body ---
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body must be JSON'}), 400

    topic = str(data.get('topic', '')).strip()
    if not topic:
        return jsonify({'error': "'topic' is required and cannot be empty"}), 400

    bucket     = str(data.get('bucket',     'elec')).strip().lower()
    hook_style = str(data.get('hook_style', 'shocking_fact')).strip().lower()
    run_script = bool(data.get('run_script', False))

    if bucket not in _VALID_BUCKETS:
        return jsonify({
            'error': (
                f"Invalid bucket '{bucket}'. "
                f"Valid values: {', '.join(sorted(_VALID_BUCKETS))}"
            )
        }), 400

    if hook_style not in _VALID_HOOK_STYLES:
        return jsonify({
            'error': (
                f"Invalid hook_style '{hook_style}'. "
                f"Valid values: {', '.join(sorted(_VALID_HOOK_STYLES))}"
            )
        }), 400

    # --- Create job ---
    try:
        init_db()
        job_id = get_next_job_id()
        create_job(
            job_id=job_id,
            topic=topic,
            bucket=bucket,
            hook_style=hook_style,
        )
        logger.info(
            f"[JOB {job_id}] Created via webhook from {request.remote_addr} "
            f"— topic: '{topic}', bucket: {bucket}, "
            f"hook: {hook_style}, run_script: {run_script}"
        )

    except Exception as exc:
        logger.error(
            f"Webhook: failed to create job for topic '{topic}': {exc}",
            exc_info=True,
        )
        return jsonify({'error': f'Failed to create job: {str(exc)}'}), 500

    # --- Optional immediate script generation ---
    message = 'Job added to queue'
    if run_script:
        thread = threading.Thread(
            target=_run_script_async,
            args=(job_id, topic, bucket, hook_style),
            daemon=True,
            name=f'webhook-script-{job_id}',
        )
        thread.start()
        message = 'Job added to queue — script generation started in background'
        logger.info(
            f"[JOB {job_id}] Script generation thread launched "
            f"(thread id: {thread.ident})"
        )

    return jsonify({
        'job_id':  job_id,
        'topic':   topic,
        'status':  'queued',
        'message': message,
    }), 201
