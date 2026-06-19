"""
app.py
======
Flask web dashboard for VideoForge at localhost:5000.
Provides GUI for the full pipeline: job management, config editing,
log viewing, analytics, and API health checks.

Input:  Browser requests at localhost:5000
Output: HTML pages + JSON API responses
Logs:   logs/app.log

Dependencies:
    - flask
    - python-dotenv

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

# 2. Third-party libraries
from dotenv import load_dotenv
from flask import (Flask, flash, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)

load_dotenv()

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent))

# 3. Local modules
from database import (create_job, get_all_jobs, get_channels, get_job,
                      get_next_job_id, init_db, update_job_field, update_job_status,
                      insert_manual_analytics, get_latest_analytics_per_job,
                      get_all_analytics_for_job, get_linked_job)
from utils.config_loader import get_default_channel, load_channel_config
from utils.logger import setup_logger
from webhook import webhook_bp

app = Flask(__name__)
_flask_secret = os.getenv('FLASK_SECRET_KEY', '')
if not _flask_secret:
    raise RuntimeError(
        "FLASK_SECRET_KEY is not set in .env. "
        "Add a random string: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
app.secret_key = _flask_secret

logger = setup_logger('app')


@app.template_filter('fromjson')
def fromjson_filter(value):
    """Parse a JSON string inside a Jinja2 template. Returns [] on any error."""
    import json as _json
    if not value:
        return []
    try:
        return _json.loads(value)
    except Exception:
        return []


@app.template_filter('fmt_expires')
def fmt_expires_filter(dt_str: str) -> str:
    """
    Format a UTC ISO datetime string as a human-readable local time.

    Input:  '2026-04-13T11:10:43'  (UTC stored in DB)
    Output: 'Sun 13 Apr · 11:10'   (Europe/Skopje local time)
    """
    if not dt_str:
        return '—'
    try:
        from datetime import timezone
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
        dt_local = dt.astimezone(ZoneInfo('Europe/Skopje'))
        return dt_local.strftime('%a %d %b · %H:%M')
    except Exception:
        return dt_str[:16]


def _parse_alert_angles(alerts: list) -> list:
    """
    Parse the `angle_options` JSON string on each alert dict into a Python list
    so templates can iterate without filter gymnastics.
    """
    import json as _json
    for a in alerts:
        raw = a.get('angle_options')
        if isinstance(raw, str) and raw.strip():
            try:
                a['angle_options'] = _json.loads(raw)
            except (_json.JSONDecodeError, ValueError):
                a['angle_options'] = []
        elif not raw:
            a['angle_options'] = []
    return alerts

# Register the webhook blueprint (provides /webhook/new-topic)
app.register_blueprint(webhook_bp)


# ---------------------------------------------------------------------------
# Channel switcher — session-based selection persisted across requests
# ---------------------------------------------------------------------------

@app.context_processor
def _inject_channel_context():
    """Inject all_channels and selected_channel into every template."""
    try:
        all_channels = get_channels(active_only=True)
    except Exception:
        all_channels = []
    selected = session.get('selected_channel', 'all')
    return {'all_channels': all_channels, 'selected_channel': selected}


@app.route('/set-channel', methods=['POST'])
def set_channel():
    """Persist the selected channel in the browser session and redirect back."""
    slug = request.form.get('channel', 'all')
    session['selected_channel'] = slug
    return redirect(request.referrer or url_for('index'))

# ---------------------------------------------------------------------------
# Pipeline state  (shared across threads)
# ---------------------------------------------------------------------------

_pipeline_lock = threading.Lock()
_pipeline_state: dict = {
    'running': False,
    'job_id': None,
    'current_stage': None,
    'stages': {},
    'started_at': None,
    'error': None,
}

STAGE_NAMES = [
    'generate-script',
    'generate-voice',
    'generate-images',
    'assemble',
    'add-captions',
    'generate-metadata',
    'generate-thumbnail',
]

STAGE_LABELS = {
    'generate-script':    'Script',
    'generate-voice':     'Voice',
    'generate-images':    'Images',
    'assemble':           'Assemble',
    'add-captions':       'Captions',
    'generate-metadata':  'Metadata',
    'generate-thumbnail': 'Thumbnail',
}


def _set_stage(stage: str, status: str, message: str = '') -> None:
    with _pipeline_lock:
        now = time.time()
        if stage not in _pipeline_state['stages'] or _pipeline_state['stages'][stage].get('started_at') is None:
            _pipeline_state['stages'][stage] = {
                'status': status,
                'started_at': now,
                'elapsed': 0,
                'message': message,
            }
        else:
            s = _pipeline_state['stages'][stage]
            s['status'] = status
            s['message'] = message
            if status in ('done', 'failed', 'skipped'):
                started = s.get('started_at') or now
                s['elapsed'] = round(now - started, 1)
        _pipeline_state['current_stage'] = stage


def _run_pipeline_thread(job_id: str, config: dict,
                         start_from: str = 'generate-script',
                         stop_after: str = None) -> None:
    """Run the full pipeline for job_id in a background thread.

    If stop_after is set to a stage name, the thread runs through that stage and
    then stops (used by Reddit jobs to pause at the script gate for hook
    selection before voice generation).
    """
    with _pipeline_lock:
        _pipeline_state['running'] = True
        _pipeline_state['job_id'] = job_id
        _pipeline_state['started_at'] = time.time()
        _pipeline_state['error'] = None
        _pipeline_state['stages'] = {
            s: {'status': 'pending', 'started_at': None, 'elapsed': 0, 'message': ''}
            for s in STAGE_NAMES
        }

    try:
        start_idx = STAGE_NAMES.index(start_from)
    except ValueError:
        start_idx = 0

    stages_to_run = STAGE_NAMES[start_idx:]
    logger.info(f"[JOB {job_id}] Pipeline thread started — stages: {stages_to_run}")

    try:
        for stage in stages_to_run:
            _set_stage(stage, 'running')

            if stage == 'generate-script':
                from modules.script_engine import generate_script
                job = get_job(job_id)
                result = generate_script(
                    job_id=job_id, topic=job['topic'], config=config,
                    bucket=job['bucket'], hook_style=job['hook_style']
                )
            elif stage == 'generate-voice':
                from modules.voice_engine import generate_voice
                result = generate_voice(job_id=job_id, config=config)
            elif stage == 'generate-images':
                from modules.image_engine import generate_images
                result = generate_images(job_id=job_id, config=config)
            elif stage == 'assemble':
                from modules.assembly_engine import assemble_video
                result = assemble_video(job_id=job_id, config=config)
            elif stage == 'add-captions':
                from modules.caption_engine import add_captions
                result = add_captions(job_id=job_id, config=config)
            elif stage == 'generate-metadata':
                from modules.metadata_engine import generate_metadata
                result = generate_metadata(job_id=job_id, config=config)
            elif stage == 'generate-thumbnail':
                from modules.thumbnail_engine import generate_thumbnail
                result = generate_thumbnail(job_id=job_id, config=config)
            else:
                result = {'success': False, 'error': f'Unknown stage: {stage}'}

            if result.get('skipped'):
                _set_stage(stage, 'skipped', result.get('error', 'skipped'))
            elif result.get('success'):
                _set_stage(stage, 'done')
            else:
                _set_stage(stage, 'failed', result.get('error', 'unknown error'))
                with _pipeline_lock:
                    _pipeline_state['error'] = result.get('error', 'unknown error')
                    _pipeline_state['running'] = False
                return

            # Stop early when requested (e.g. Reddit jobs pause at the script gate)
            if stop_after and stage == stop_after:
                logger.info(f"[JOB {job_id}] Pipeline thread stopping after '{stage}' (gate)")
                break

        with _pipeline_lock:
            _pipeline_state['running'] = False
        logger.info(f"[JOB {job_id}] Pipeline thread completed successfully")

    except Exception as exc:
        logger.error(f"[JOB {job_id}] Pipeline thread error: {exc}", exc_info=True)
        current = _pipeline_state.get('current_stage')
        if current:
            _set_stage(current, 'failed', str(exc))
        update_job_status(job_id, 'failed', error_module='pipeline', error_message=str(exc))
        with _pipeline_lock:
            _pipeline_state['running'] = False
            _pipeline_state['error'] = str(exc)


def _inject_long_url_into_teaser(short_job_id: str, long_youtube_url: str) -> None:
    """
    Append the long video's YouTube URL to the teaser's metadata description.
    Called after the long job finishes uploading, before the teaser is scheduled.
    """
    import json as _json
    short_job = get_job(short_job_id)
    if not short_job:
        return
    meta_path = short_job.get('metadata_path')
    if not meta_path or not Path(meta_path).exists():
        logger.warning(
            f"[JOB {short_job_id}] Teaser metadata not found at '{meta_path}' "
            "— long URL will not be injected"
        )
        return
    try:
        with open(meta_path, 'r', encoding='utf-8') as f:
            meta = _json.load(f)
        desc = meta.get('youtube_description') or ''
        if long_youtube_url not in desc:
            meta['youtube_description'] = (
                f"{desc}\n\nWatch the full story: {long_youtube_url}".strip()
            )
        with open(meta_path, 'w', encoding='utf-8') as f:
            _json.dump(meta, f, indent=2, ensure_ascii=False)
        logger.info(
            f"[JOB {short_job_id}] Long video URL injected into teaser description: "
            f"{long_youtube_url}"
        )
    except Exception as exc:
        logger.error(
            f"[JOB {short_job_id}] Failed to inject long URL into teaser: {exc}",
            exc_info=True,
        )


def _run_upload_thread(job_id: str, config: dict) -> None:
    """
    Run upload_engine in a background thread after Approve.

    If this is a story long-form job (story_role='long'), after a successful
    YouTube upload the linked teaser's description is updated with the long
    video URL and the teaser is scheduled for upload >= 24 hours later.
    """
    from datetime import datetime as _dt, timedelta as _td

    logger.info(f"[JOB {job_id}] Upload thread started")
    with _pipeline_lock:
        _pipeline_state['running'] = True
        _pipeline_state['job_id'] = job_id

    try:
        update_job_status(job_id, 'uploading')
        from modules.upload_engine import upload_video
        result = upload_video(job_id=job_id, config=config)
        logger.info(
            f"[JOB {job_id}] Upload result: "
            f"youtube={result.get('youtube', {}).get('success')}, "
            f"tiktok={result.get('tiktok', {}).get('success')}"
        )

        # Story pair: after long upload succeeds, schedule the teaser
        job = get_job(job_id)
        if (
            job
            and job.get('story_role') == 'long'
            and job.get('linked_job_id')
            and result.get('youtube', {}).get('success')
        ):
            short_job_id   = job['linked_job_id']
            long_yt_url    = job.get('youtube_url') or ''
            short_job      = get_job(short_job_id)

            if short_job and short_job.get('status') == 'review':
                if long_yt_url:
                    _inject_long_url_into_teaser(short_job_id, long_yt_url)

                scheduled_at = (
                    _dt.utcnow() + _td(hours=24)
                ).strftime('%Y-%m-%d %H:%M:%S')
                update_job_field(short_job_id, 'scheduled_upload_at', scheduled_at)
                update_job_status(short_job_id, 'scheduled_upload')
                logger.info(
                    f"[JOB {short_job_id}] Teaser scheduled for upload at {scheduled_at} UTC"
                )

    except Exception as exc:
        logger.error(f"[JOB {job_id}] Upload thread error: {exc}", exc_info=True)
        update_job_status(job_id, 'failed', error_module='upload_engine', error_message=str(exc))
    finally:
        with _pipeline_lock:
            _pipeline_state['running'] = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config(channel_slug: str = None) -> dict:
    """
    Return the merged config for the given channel, or for the default channel
    when channel_slug is None. Falls back to raw global config on any error.
    """
    try:
        slug = channel_slug or session.get('selected_channel') or get_default_channel()
        if slug == 'all':
            slug = get_default_channel()
        return load_channel_config(slug)
    except Exception:
        try:
            with open('config.json', 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}


def _read_log_lines(module: str = 'main', level: str = 'ALL',
                    job_filter: str = '', limit: int = 200) -> list:
    """Parse and filter lines from a log file."""
    log_file = Path(f'logs/{module}.log')
    if not log_file.exists():
        return []

    LEVEL_COLORS = {
        'DEBUG': 'debug', 'INFO': 'info',
        'WARNING': 'warning', 'ERROR': 'error', 'CRITICAL': 'error',
    }

    try:
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            raw = f.readlines()
    except Exception:
        return []

    parsed = []
    for line in raw:
        line = line.rstrip()
        if not line:
            continue
        parts = line.split(' | ', 3)
        if len(parts) == 4:
            ts, mod, lvl, msg = parts
            lvl = lvl.strip()
        else:
            ts, mod, lvl, msg = '', '', 'INFO', line

        if level != 'ALL' and lvl != level:
            continue
        if job_filter and f'[JOB {job_filter}]' not in line:
            continue

        parsed.append({
            'ts': ts.strip(),
            'module': mod.strip(),
            'level': lvl,
            'message': msg.strip(),
            'color': LEVEL_COLORS.get(lvl, 'debug'),
        })

    return list(reversed(parsed[-limit:]))


def _get_stats(channel_id: str = None) -> dict:
    selected = channel_id or session.get('selected_channel', 'all')
    filter_ch = None if selected == 'all' else selected
    jobs = get_all_jobs(channel_id=filter_ch)
    today = datetime.now()
    monday = (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')
    return {
        'total':            len(jobs),
        'posted_this_week': sum(1 for j in jobs
                                if j.get('status') == 'posted'
                                and (j.get('created_at') or '') >= monday),
        'in_queue':         sum(1 for j in jobs if j.get('status') == 'queued'),
        'awaiting_review':  sum(1 for j in jobs if j.get('status') == 'review'),
    }


def _clear_job_outputs(job_id: str) -> None:
    """Delete all output files for a job so it can rerun from scratch."""
    patterns = [
        f'output/scripts/{job_id}.json',
        f'output/audio/{job_id}.mp3',
        f'output/audio/{job_id}_hook.mp3',
        f'output/audio/{job_id}_body.mp3',
        f'output/audio/{job_id}_cta.mp3',
        f'output/images/{job_id}',
        f'output/videos/{job_id}_raw.mp4',
        f'output/videos/{job_id}_captioned.mp4',
        f'output/thumbnails/{job_id}.jpg',
        f'output/metadata/{job_id}.json',
    ]
    for pattern in patterns:
        p = Path(pattern)
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except Exception as exc:
            logger.warning(f"Could not delete {pattern}: {exc}")

    clearable = [
        'script_path', 'audio_path', 'images_dir',
        'raw_video_path', 'final_video_path',
        'thumbnail_path', 'metadata_path',
    ]
    for field in clearable:
        try:
            update_job_field(job_id, field, None)
        except Exception:
            pass


# Files and DB fields to wipe when restarting FROM a given stage.
# Each entry clears that stage plus everything that comes after it.
_STAGE_CLEAR_MAP = {
    'generate-script': {
        'files':  [
            'output/scripts/{id}.json',
            'output/audio/{id}.mp3', 'output/audio/{id}_hook.mp3',
            'output/audio/{id}_body.mp3', 'output/audio/{id}_cta.mp3',
            'output/images/{id}',
            'output/videos/{id}_raw.mp4', 'output/videos/{id}_captioned.mp4',
            'output/thumbnails/{id}.jpg', 'output/metadata/{id}.json',
        ],
        'fields': ['script_path', 'audio_path', 'images_dir',
                   'raw_video_path', 'final_video_path', 'thumbnail_path', 'metadata_path'],
    },
    'generate-voice': {
        'files':  [
            'output/audio/{id}.mp3', 'output/audio/{id}_hook.mp3',
            'output/audio/{id}_body.mp3', 'output/audio/{id}_cta.mp3',
            'output/images/{id}',
            'output/videos/{id}_raw.mp4', 'output/videos/{id}_captioned.mp4',
            'output/thumbnails/{id}.jpg', 'output/metadata/{id}.json',
        ],
        'fields': ['audio_path', 'images_dir', 'raw_video_path',
                   'final_video_path', 'thumbnail_path', 'metadata_path'],
    },
    'generate-images': {
        'files':  [
            'output/images/{id}',
            'output/videos/{id}_raw.mp4', 'output/videos/{id}_captioned.mp4',
            'output/thumbnails/{id}.jpg', 'output/metadata/{id}.json',
        ],
        'fields': ['images_dir', 'raw_video_path', 'final_video_path',
                   'thumbnail_path', 'metadata_path'],
    },
    'assemble': {
        'files':  [
            'output/videos/{id}_raw.mp4', 'output/videos/{id}_captioned.mp4',
            'output/thumbnails/{id}.jpg', 'output/metadata/{id}.json',
        ],
        'fields': ['raw_video_path', 'final_video_path', 'thumbnail_path', 'metadata_path'],
    },
}


def _clear_from_stage(job_id: str, from_stage: str) -> None:
    """
    Delete output files and clear DB fields for all pipeline stages at or after
    `from_stage`.  Used for partial rejections so earlier stages are preserved.
    """
    spec = _STAGE_CLEAR_MAP.get(from_stage)
    if spec is None:
        _clear_job_outputs(job_id)
        return
    for pattern in spec['files']:
        p = Path(pattern.replace('{id}', job_id))
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except Exception as exc:
            logger.warning(f"Could not delete {p}: {exc}")
    for field in spec['fields']:
        try:
            update_job_field(job_id, field, None)
        except Exception:
            pass


def _status_color(status: str) -> str:
    mapping = {
        'queued': 'gray', 'scripting': 'blue', 'voiced': 'blue',
        'script_done': 'amber', 'imaging': 'blue', 'assembling': 'blue',
        'captioning': 'blue', 'metadata': 'blue', 'review': 'amber',
        'uploading': 'purple', 'posted': 'green', 'failed': 'red',
        'candidate': 'gray', 'archived': 'gray',
    }
    return mapping.get(status, 'gray')


app.jinja_env.globals['status_color'] = _status_color


# ---------------------------------------------------------------------------
# Main pages
# ---------------------------------------------------------------------------

@app.route('/')
def overview():
    from database import get_active_alerts
    selected = session.get('selected_channel', 'all')
    filter_ch = None if selected == 'all' else selected
    stats = _get_stats()
    jobs = get_all_jobs(channel_id=filter_ch)
    review_jobs = [j for j in jobs if j.get('status') == 'review']
    with _pipeline_lock:
        pipeline_running = _pipeline_state.get('running', False)
        pipeline_job_id  = _pipeline_state.get('job_id')
    return render_template('dashboard.html',
                           stats=stats,
                           review_jobs=review_jobs,
                           pipeline_running=pipeline_running,
                           pipeline_job_id=pipeline_job_id,
                           active_alerts=_parse_alert_angles(get_active_alerts()),
                           active='overview')


@app.route('/jobs')
def jobs_list():
    status_filter = request.args.get('status', 'all')
    selected = session.get('selected_channel', 'all')
    filter_ch = None if selected == 'all' else selected
    all_jobs = get_all_jobs(channel_id=filter_ch)
    filtered = [j for j in all_jobs if j.get('status') == status_filter] \
               if status_filter != 'all' else all_jobs
    counts: dict = {}
    for j in all_jobs:
        s = j.get('status', 'unknown')
        counts[s] = counts.get(s, 0) + 1
    return render_template('jobs.html',
                           jobs=filtered,
                           status_filter=status_filter,
                           counts=counts,
                           total=len(all_jobs),
                           active='jobs')


@app.route('/jobs/new', methods=['GET', 'POST'])
def new_job():
    config = _load_config()
    if request.method == 'POST':
        topic     = request.form.get('topic', '').strip()
        bucket    = request.form.get('bucket', 'elec')
        hook      = request.form.get('hook') or config.get('script', {}).get('hook_style', 'shocking_fact')
        run_mode  = request.form.get('run_mode', 'queue')
        bulk_raw  = request.form.get('bulk_topics', '').strip()

        topics = []
        if topic:
            topics.append(topic)
        if bulk_raw:
            topics.extend(t.strip() for t in bulk_raw.splitlines() if t.strip())

        if not topics:
            flash('Enter at least one topic.', 'error')
            return redirect(url_for('new_job'))

        init_db()
        selected = session.get('selected_channel', 'all')
        job_channel = get_default_channel() if selected == 'all' else selected
        created_ids = []
        for t in topics:
            jid = get_next_job_id()
            create_job(job_id=jid, topic=t, bucket=bucket, hook_style=hook,
                       channel_id=job_channel)
            created_ids.append(jid)
            logger.info(f"[JOB {jid}] Created via dashboard — topic: '{t}', channel: {job_channel}")

        if run_mode == 'now' and len(created_ids) == 1:
            jid = created_ids[0]
            threading.Thread(
                target=_run_pipeline_thread,
                args=(jid, config),
                daemon=True
            ).start()
            flash(f"Job {jid} started — pipeline running.", 'success')
            return redirect(url_for('overview'))

        if run_mode == 'script_only' and len(created_ids) == 1:
            jid = created_ids[0]
            threading.Thread(
                target=_run_pipeline_thread,
                args=(jid, config, 'generate-script'),
                daemon=True
            ).start()
            flash(f"Job {jid} started — script only.", 'success')
            return redirect(url_for('job_detail', job_id=jid))

        flash(f"{len(created_ids)} job(s) added to queue.", 'success')
        return redirect(url_for('jobs_list'))

    return render_template('new_job.html', active='new_job', config=config)


@app.route('/jobs/<job_id>')
def job_detail(job_id):
    job = get_job(job_id)
    if not job:
        flash(f'Job {job_id} not found.', 'error')
        return redirect(url_for('jobs_list'))

    script_data = None
    if job.get('script_path') and Path(job['script_path']).exists():
        try:
            with open(job['script_path'], 'r', encoding='utf-8') as f:
                script_data = json.load(f)
        except Exception:
            pass

    meta_data = None
    if job.get('metadata_path') and Path(job['metadata_path']).exists():
        try:
            with open(job['metadata_path'], 'r', encoding='utf-8') as f:
                meta_data = json.load(f)
        except Exception:
            pass

    # Block F — prefer R2 preview_url when present so the dashboard plays
    # from the cloud (works from any device, any network). Local file path
    # remains the fallback for when R2 is unavailable.
    video_url = None
    video_from_cloud = False
    if job.get('preview_url'):
        video_url = job['preview_url']
        video_from_cloud = True
    elif job.get('final_video_path') and Path(job['final_video_path']).exists():
        video_url = f'/output/videos/{job_id}_captioned.mp4'
    elif job.get('raw_video_path') and Path(job['raw_video_path']).exists():
        video_url = f'/output/videos/{job_id}_raw.mp4'

    # Primary thumbnail URL — R2 first, then chosen variant, then default
    thumbnail_url = None
    thumb_from_cloud = False
    if job.get('preview_thumb_url'):
        thumbnail_url = job['preview_thumb_url']
        thumb_from_cloud = True
    elif job.get('thumbnail_path') and Path(job['thumbnail_path']).exists():
        thumbnail_url = f'/output/thumbnails/{job_id}.jpg'

    # Detect text_template variants
    thumbnail_variants = []
    for v in [1, 2]:
        vp = Path(f'output/thumbnails/{job_id}_v{v}.jpg')
        if vp.exists():
            thumbnail_variants.append({'index': v, 'url': f'/output/thumbnails/{job_id}_v{v}.jpg'})
    chosen_variant = job.get('thumbnail_variant') or (1 if thumbnail_variants else 0)

    log_lines = _read_log_lines(module='main', level='ALL',
                                job_filter=job_id, limit=150)

    # Load the linked teaser/long job for story-pair review UI
    linked_job = get_linked_job(job_id) if job.get('linked_job_id') else None
    linked_script_data = None
    linked_video_url   = None
    if linked_job:
        lsp = linked_job.get('script_path')
        if lsp and Path(lsp).exists():
            try:
                with open(lsp, 'r', encoding='utf-8') as f:
                    linked_script_data = json.load(f)
            except Exception:
                pass
        lfp = linked_job.get('final_video_path')
        lrp = linked_job.get('raw_video_path')
        lid = linked_job['id']
        if linked_job.get('preview_url'):
            linked_video_url = linked_job['preview_url']
        elif lfp and Path(lfp).exists():
            linked_video_url = f'/output/videos/{lid}_captioned.mp4'
        elif lrp and Path(lrp).exists():
            linked_video_url = f'/output/videos/{lid}_raw.mp4'

    return render_template('job_detail.html',
                           job=job,
                           script_data=script_data,
                           meta_data=meta_data,
                           log_lines=log_lines,
                           video_url=video_url,
                           video_from_cloud=video_from_cloud,
                           thumbnail_url=thumbnail_url,
                           thumb_from_cloud=thumb_from_cloud,
                           thumbnail_variants=thumbnail_variants,
                           chosen_variant=chosen_variant,
                           linked_job=linked_job,
                           linked_script_data=linked_script_data,
                           linked_video_url=linked_video_url,
                           active='jobs')


@app.route('/jobs/<job_id>/set-thumbnail-variant', methods=['POST'])
def set_thumbnail_variant(job_id):
    """Store the owner's chosen thumbnail variant (1 or 2) for this job."""
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    variant = request.json.get('variant', 1) if request.is_json else int(request.form.get('variant', 1))
    try:
        update_job_field(job_id, 'thumbnail_variant', int(variant))
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, 'variant': variant})


@app.route('/jobs/<job_id>/approve', methods=['POST'])
def approve_job(job_id):
    job = get_job(job_id)
    if not job:
        flash('Job not found.', 'error')
        return redirect(url_for('overview'))

    # Disclosure checklist gate — if YouTube rejected containsSyntheticMedia,
    # the owner must confirm they've set it manually in Studio before we mark posted.
    if job.get('disclosure_checklist_required') and not request.form.get('disclosure_acknowledged'):
        flash(
            'Please check the disclosure checkbox confirming you have set '
            '"Contains synthetic/AI content" in YouTube Studio before approving.',
            'error',
        )
        return redirect(url_for('job_detail', job_id=job_id))

    config = _load_config(channel_slug=job.get('channel_id'))

    # Story pair gate — block approval if the linked teaser has not yet reached
    # a terminal or upload-ready status. The owner must either wait for the
    # teaser pipeline to finish, or explicitly check the abandon-teaser checkbox
    # to upload the long video alone without the paired short.
    linked_job = get_linked_job(job_id) if job.get('linked_job_id') else None
    _safe_teaser_statuses = {'review', 'scheduled_upload', 'uploading', 'posted', 'failed', 'archived'}
    teaser_not_ready = (
        linked_job
        and linked_job.get('story_role') == 'short'
        and linked_job.get('status') not in _safe_teaser_statuses
    )
    if teaser_not_ready:
        if not request.form.get('abandon_teaser'):
            flash(
                f'Teaser job {linked_job["id"]} is not ready yet '
                f'(status: {linked_job["status"]}) — '
                'wait for it to finish processing before approving.',
                'error',
            )
            return redirect(url_for('job_detail', job_id=job_id))
        # Owner explicitly chose to abandon — log and mark terminal
        logger.warning(
            f'[JOB {job_id}] Owner abandoned teaser {linked_job["id"]} '
            f'(was: {linked_job["status"]}) — long-job approval proceeding without paired short.'
        )
        update_job_status(
            linked_job['id'], 'failed',
            error_module='abandoned',
            error_message='Teaser abandoned by owner at long-job review — long video uploaded without paired short.',
        )

    threading.Thread(
        target=_run_upload_thread,
        args=(job_id, config),
        daemon=True
    ).start()
    flash(f'Job {job_id} approved — uploading in background.', 'success')
    return redirect(request.referrer or url_for('overview'))


@app.route('/jobs/<job_id>/reject', methods=['POST'])
def reject_job(job_id):
    """
    Reject a job at review with a targeted redo mode.

    Form params:
        mode  — full | script | voice | images | assembly  (default: full)
        note  — optional free-text review note saved to jobs.review_note
    """
    job = get_job(job_id)
    if not job:
        flash('Job not found.', 'error')
        return redirect(url_for('overview'))

    mode = request.form.get('mode', 'full')
    note = request.form.get('note', '').strip()

    if note:
        update_job_field(job_id, 'review_note', note)

    stage_map = {
        'full':     ('generate-script', 'full pipeline'),
        'script':   ('generate-script', 'script'),
        'voice':    ('generate-voice',  'voice'),
        'images':   ('generate-images', 'images'),
        'assembly': ('assemble',        'assembly'),
    }
    from_stage, label = stage_map.get(mode, ('generate-script', 'full pipeline'))

    _clear_from_stage(job_id, from_stage)
    update_job_status(job_id, 'queued', error_module=None, error_message=None)

    config = _load_config()
    threading.Thread(
        target=_run_pipeline_thread,
        args=(job_id, config, from_stage),
        daemon=True,
    ).start()

    flash(f'Job {job_id} — redoing from {label}.', 'warning')
    return redirect(url_for('job_detail', job_id=job_id))


@app.route('/jobs/<job_id>/archive', methods=['POST'])
def archive_job(job_id):
    """Mark a job as archived — removes it from the active queue, keeps script for reference."""
    job = get_job(job_id)
    if not job:
        flash('Job not found.', 'error')
        return redirect(url_for('jobs_list'))

    note = request.form.get('note', '').strip()
    if note:
        update_job_field(job_id, 'review_note', note)

    update_job_status(job_id, 'archived', error_module=None, error_message=None)
    flash(f'Job {job_id} archived.', 'success')
    return redirect(url_for('jobs_list'))


@app.route('/jobs/<job_id>/save-script', methods=['POST'])
def save_script_edit(job_id):
    """
    Save a manually edited script and restart the pipeline from voice engine.

    Form params:
        hook  — edited hook narration
        body  — edited body narration
        cta   — edited CTA line
        note  — optional review note
    """
    import json as _json

    job = get_job(job_id)
    if not job:
        flash('Job not found.', 'error')
        return redirect(url_for('jobs_list'))

    hook = request.form.get('hook', '').strip()
    body = request.form.get('body', '').strip()
    cta  = request.form.get('cta', 'Follow for more engineering explained simply.').strip()
    note = request.form.get('note', '').strip()

    if not hook or not body:
        flash('Hook and body cannot be empty.', 'error')
        return redirect(url_for('job_detail', job_id=job_id))

    script_path = job.get('script_path') or f'output/scripts/{job_id}.json'
    try:
        with open(script_path, 'r', encoding='utf-8') as f:
            script_data = _json.load(f)
    except Exception as exc:
        flash(f'Could not read script file: {exc}', 'error')
        return redirect(url_for('job_detail', job_id=job_id))

    # Update sections and rebuild narration
    script_data.setdefault('sections', {})
    script_data['sections']['hook'] = hook
    script_data['sections']['body'] = body
    script_data['sections']['cta']  = cta
    narration = f"{hook} {body} {cta}"
    script_data['narration'] = narration
    word_count = len(narration.split())
    script_data['word_count'] = word_count
    script_data['estimated_duration_seconds'] = round(word_count / 2.5)

    try:
        with open(script_path, 'w', encoding='utf-8') as f:
            _json.dump(script_data, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        flash(f'Could not write script file: {exc}', 'error')
        return redirect(url_for('job_detail', job_id=job_id))

    if note:
        update_job_field(job_id, 'review_note', note)

    # Clear everything from voice engine onward, keep the edited script
    _clear_from_stage(job_id, 'generate-voice')
    update_job_status(job_id, 'queued', error_module=None, error_message=None)

    config = _load_config()
    threading.Thread(
        target=_run_pipeline_thread,
        args=(job_id, config, 'generate-voice'),
        daemon=True,
    ).start()

    flash(f'Job {job_id} — script saved, pipeline restarting from voice.', 'success')
    return redirect(url_for('job_detail', job_id=job_id))


@app.route('/jobs/<job_id>/use-hook', methods=['POST'])
def use_hook(job_id):
    """
    Reddit hook gate: write the selected opening hook into the script JSON,
    then continue the pipeline from voice generation.

    Form params:
        hook_index — index into the script's "hooks" array (preferred), or
        hook_text  — explicit hook text fallback.
    """
    import json as _json

    job = get_job(job_id)
    if not job:
        flash('Job not found.', 'error')
        return redirect(url_for('jobs_list'))

    script_path = job.get('script_path') or f'output/scripts/{job_id}.json'
    try:
        with open(script_path, 'r', encoding='utf-8') as f:
            script_data = _json.load(f)
    except Exception as exc:
        flash(f'Could not read script file: {exc}', 'error')
        return redirect(url_for('job_detail', job_id=job_id))

    hooks = script_data.get('hooks', []) or []
    chosen = (request.form.get('hook_text') or '').strip()
    idx_raw = request.form.get('hook_index', '')
    if not chosen and idx_raw.isdigit():
        idx = int(idx_raw)
        if 0 <= idx < len(hooks):
            chosen = str(hooks[idx]).strip()

    if not chosen:
        flash('No hook selected.', 'error')
        return redirect(url_for('job_detail', job_id=job_id))

    # Replace the hook section and rebuild narration so voice picks up the change
    sections = script_data.setdefault('sections', {})
    sections['hook'] = chosen
    body = sections.get('body', '')
    cta  = sections.get('cta', 'Follow for part two and more stories like this.')
    narration = ' '.join(p for p in (chosen, body, cta) if p).strip()
    script_data['narration'] = narration
    word_count = len(narration.split())
    script_data['word_count'] = word_count
    script_data['estimated_duration_seconds'] = round(word_count / 2.5)
    script_data['selected_hook'] = chosen

    try:
        with open(script_path, 'w', encoding='utf-8') as f:
            _json.dump(script_data, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        flash(f'Could not write script file: {exc}', 'error')
        return redirect(url_for('job_detail', job_id=job_id))

    update_job_field(job_id, 'word_count', word_count)
    update_job_status(job_id, 'queued', error_module=None, error_message=None)

    config = _load_config(channel_slug=job.get('channel_id'))
    threading.Thread(
        target=_run_pipeline_thread,
        args=(job_id, config, 'generate-voice'),
        daemon=True,
    ).start()

    # Story pair: if there is a linked teaser also awaiting hook selection,
    # apply the teaser hook and start its pipeline too.
    linked_job = get_linked_job(job_id) if job.get('linked_job_id') else None
    if linked_job and linked_job.get('story_role') == 'short' and linked_job.get('status') == 'script_done':
        import json as _json2
        short_id     = linked_job['id']
        short_path   = linked_job.get('script_path') or f'output/scripts/{short_id}.json'
        teaser_idx   = request.form.get('teaser_hook_index', '')
        teaser_text  = (request.form.get('teaser_hook_text') or '').strip()

        try:
            with open(short_path, 'r', encoding='utf-8') as f:
                short_script = _json2.load(f)

            short_hooks   = short_script.get('hooks', []) or []
            chosen_teaser = teaser_text
            if not chosen_teaser and teaser_idx.isdigit():
                idx = int(teaser_idx)
                if 0 <= idx < len(short_hooks):
                    chosen_teaser = str(short_hooks[idx]).strip()
            if not chosen_teaser and short_hooks:
                chosen_teaser = str(short_hooks[0]).strip()

            if chosen_teaser:
                sections = short_script.setdefault('sections', {})
                sections['hook'] = chosen_teaser
                body  = sections.get('body', '')
                cta   = sections.get('cta', 'Watch the full story — link in bio.')
                short_narration = ' '.join(p for p in (chosen_teaser, body, cta) if p).strip()
                short_script['narration']  = short_narration
                short_wc = len(short_narration.split())
                short_script['word_count'] = short_wc
                short_script['estimated_duration_seconds'] = round(short_wc / 2.5)
                short_script['selected_hook'] = chosen_teaser

                with open(short_path, 'w', encoding='utf-8') as f:
                    _json2.dump(short_script, f, indent=2, ensure_ascii=False)

                update_job_field(short_id, 'word_count', short_wc)

            update_job_status(short_id, 'queued', error_module=None, error_message=None)
            short_config = _load_config(channel_slug=linked_job.get('channel_id'))
            threading.Thread(
                target=_run_pipeline_thread,
                args=(short_id, short_config, 'generate-voice'),
                daemon=True,
            ).start()
            logger.info(f"[JOB {short_id}] Teaser pipeline started from voice (linked to {job_id})")

        except Exception as exc:
            logger.error(
                f"[JOB {short_id}] Failed to start teaser pipeline: {exc}", exc_info=True
            )

    logger.info(f"[JOB {job_id}] Hook selected — continuing pipeline from voice")
    flash(f'Hook selected — job {job_id} continuing through voice, assembly and captions.', 'success')
    return redirect(url_for('job_detail', job_id=job_id))


@app.route('/jobs/<job_id>/retry', methods=['POST'])
def retry_job(job_id):
    job = get_job(job_id)
    if not job:
        flash('Job not found.', 'error')
        return redirect(url_for('jobs_list'))

    config = _load_config()

    # Find earliest missing stage
    start_from = 'generate-script'
    if job.get('script_path') and Path(job.get('script_path', '')).exists():
        start_from = 'generate-voice'
    if job.get('audio_path') and Path(job.get('audio_path', '')).exists():
        start_from = 'generate-images'
    if job.get('images_dir') and Path(job.get('images_dir', '')).exists():
        start_from = 'assemble'
    if job.get('raw_video_path') and Path(job.get('raw_video_path', '')).exists():
        start_from = 'add-captions'
    if job.get('final_video_path') and Path(job.get('final_video_path', '')).exists():
        start_from = 'generate-metadata'
    if job.get('metadata_path') and Path(job.get('metadata_path', '')).exists():
        start_from = 'generate-thumbnail'

    update_job_status(job_id, 'queued', error_module=None, error_message=None)
    threading.Thread(
        target=_run_pipeline_thread,
        args=(job_id, config, start_from),
        daemon=True
    ).start()
    flash(f'Job {job_id} retrying from {STAGE_LABELS.get(start_from, start_from)}.', 'success')
    return redirect(url_for('job_detail', job_id=job_id))


def _editor_scope() -> tuple:
    """
    Determine whether the editor writes to the global file or a channel overlay.

    Reads the currently-selected channel from the session and falls back to
    'global' when the channel selector is 'all'.

    Returns:
        tuple: (scope_kind: 'global' | 'channel', channel_slug: str | None)
    """
    selected = session.get('selected_channel', 'all')
    if not selected or selected == 'all':
        return ('global', None)
    return ('channel', selected)


@app.route('/config', methods=['GET', 'POST'])
def config_editor():
    scope_kind, channel_slug = _editor_scope()

    # Resolve the file path the editor reads/writes from
    if scope_kind == 'channel':
        cfg_path = Path(f'channels/{channel_slug}/config.json')
        scope_label = f'{channel_slug} overlay'
    else:
        cfg_path = Path('config.json')
        scope_label = 'global defaults'

    if request.method == 'POST':
        try:
            new_config_str = request.form.get('config_json', '').strip()
            if not new_config_str:
                flash('No config data received.', 'error')
                return redirect(url_for('config_editor'))
            new_config = json.loads(new_config_str)
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cfg_path, 'w', encoding='utf-8') as f:
                json.dump(new_config, f, indent=2, ensure_ascii=False)
            logger.info(f"{cfg_path} updated via dashboard ({scope_label})")
            flash(f'Config saved to {scope_label}. Changes apply to the next job run.',
                  'success')
        except json.JSONDecodeError as exc:
            flash(f'Invalid JSON: {exc}', 'error')
        except Exception as exc:
            flash(f'Save failed: {exc}', 'error')
        return redirect(url_for('config_editor'))

    # GET — show the file contents if the overlay exists, else an empty stub
    if cfg_path.exists():
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except Exception:
            config = {}
    else:
        config = {}
    return render_template(
        'config_editor.html',
        config=config,
        active='config',
        scope_kind=scope_kind,
        scope_label=scope_label,
        channel_slug=channel_slug,
    )


def _resolve_prompt_path(prompt_name: str) -> tuple:
    """
    Resolve the editor's read+write target for one prompt file.

    When a channel is selected: writes go to channels/<slug>/prompts/<name>.
    Reads fall back to the global file when the overlay doesn't exist yet so
    the user can start from the global text. When no channel is selected:
    everything goes to the global prompts/ directory.

    Args:
        prompt_name (str): Bare filename e.g. 'script_prompt.txt'.

    Returns:
        tuple: (write_path: Path, read_path: Path, scope_kind: str,
                scope_label: str, channel_slug: str | None)
    """
    scope_kind, channel_slug = _editor_scope()
    global_path = Path(f'prompts/{prompt_name}')
    if scope_kind == 'channel':
        overlay_path = Path(f'channels/{channel_slug}/prompts/{prompt_name}')
        # Read overlay if present, else fall back to global so the user starts
        # from a sane base. Always write to overlay.
        read_from = overlay_path if overlay_path.exists() else global_path
        return overlay_path, read_from, scope_kind, f'{channel_slug} overlay', channel_slug
    return global_path, global_path, scope_kind, 'global defaults', None


@app.route('/prompts', methods=['GET'])
def prompts_editor():
    scope_kind, channel_slug = _editor_scope()
    script_w, script_r, _, scope_label, _ = _resolve_prompt_path('script_prompt.txt')
    meta_w, meta_r, _, _, _ = _resolve_prompt_path('metadata_prompt.txt')

    def _safe_read(p):
        try:
            return p.read_text(encoding='utf-8')
        except Exception:
            return ''

    config = _load_config()
    return render_template(
        'prompts.html',
        script_prompt=_safe_read(script_r),
        meta_prompt=_safe_read(meta_r),
        config=config,
        active='prompts',
        scope_kind=scope_kind,
        scope_label=scope_label,
        channel_slug=channel_slug,
        script_target=str(script_w),
        meta_target=str(meta_w),
    )


@app.route('/prompts/script', methods=['POST'])
def save_script_prompt():
    write_path, _, scope_kind, scope_label, _ = _resolve_prompt_path('script_prompt.txt')
    content = request.form.get('content', '')
    write_path.parent.mkdir(parents=True, exist_ok=True)
    write_path.write_text(content, encoding='utf-8')
    logger.info(f"{write_path} updated via dashboard ({scope_label})")
    flash(f'Script prompt saved to {scope_label}.', 'success')
    return redirect(url_for('prompts_editor'))


@app.route('/prompts/metadata', methods=['POST'])
def save_metadata_prompt():
    write_path, _, scope_kind, scope_label, _ = _resolve_prompt_path('metadata_prompt.txt')
    content = request.form.get('content', '')
    write_path.parent.mkdir(parents=True, exist_ok=True)
    write_path.write_text(content, encoding='utf-8')
    logger.info(f"{write_path} updated via dashboard ({scope_label})")
    flash(f'Metadata prompt saved to {scope_label}.', 'success')
    return redirect(url_for('prompts_editor'))


@app.route('/logs')
def log_viewer():
    return render_template('logs.html', active='logs')


@app.route('/analytics')
def analytics():
    return render_template('stats.html', active='analytics')


@app.route('/health')
def health():
    return render_template('health.html', active='health')


# ---------------------------------------------------------------------------
# API endpoints  (called by JS)
# ---------------------------------------------------------------------------

@app.route('/api/pipeline-status')
def api_pipeline_status():
    with _pipeline_lock:
        running      = _pipeline_state.get('running', False)
        job_id       = _pipeline_state.get('job_id')
        current      = _pipeline_state.get('current_stage')
        stages_raw   = dict(_pipeline_state.get('stages', {}))
        error        = _pipeline_state.get('error')
        started_at   = _pipeline_state.get('started_at')

    elapsed_total = round(time.time() - started_at, 1) if started_at and running else 0

    stage_list = []
    for s in STAGE_NAMES:
        info = stages_raw.get(s, {})
        stage_list.append({
            'name':    s,
            'label':   STAGE_LABELS[s],
            'status':  info.get('status', 'pending'),
            'elapsed': info.get('elapsed', 0),
            'message': info.get('message', ''),
        })

    return jsonify({
        'running':       running,
        'job_id':        job_id,
        'current_stage': current,
        'stages':        stage_list,
        'elapsed_total': elapsed_total,
        'error':         error,
    })


@app.route('/api/logs')
def api_logs():
    module     = request.args.get('module', 'main')
    level      = request.args.get('level', 'ALL')
    job_filter = request.args.get('job_id', '').strip()
    limit      = min(int(request.args.get('limit', 200)), 500)
    lines = _read_log_lines(module=module, level=level,
                            job_filter=job_filter, limit=limit)
    return jsonify({'lines': lines, 'count': len(lines)})


@app.route('/api/health')
def api_health():
    """Live status check for all configured APIs."""
    import requests as req

    results: dict = {}

    # --- Claude (Anthropic) ---
    key = os.getenv('ANTHROPIC_API_KEY', '')
    if not key:
        results['claude'] = {'status': 'no_key', 'label': 'ANTHROPIC_API_KEY not set', 'latency_ms': None}
    else:
        try:
            import anthropic
            t0 = time.time()
            client = anthropic.Anthropic(api_key=key)
            client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=1,
                messages=[{'role': 'user', 'content': 'hi'}]
            )
            ms = round((time.time() - t0) * 1000)
            results['claude'] = {'status': 'ok', 'label': f'OK — {ms} ms', 'latency_ms': ms}
        except Exception as exc:
            results['claude'] = {'status': 'error', 'label': str(exc)[:100], 'latency_ms': None}

    # --- ElevenLabs ---
    # Uses /v1/voices (requires only the voices:read scope).
    # /v1/user/subscription requires user_read which most API keys lack.
    key = os.getenv('ELEVENLABS_API_KEY', '')
    if not key:
        results['elevenlabs'] = {'status': 'no_key', 'label': 'ELEVENLABS_API_KEY not set', 'latency_ms': None}
    else:
        try:
            t0 = time.time()
            r = req.get('https://api.elevenlabs.io/v1/voices',
                        headers={'xi-api-key': key}, timeout=8)
            ms = round((time.time() - t0) * 1000)
            if r.status_code == 200:
                voice_count = len(r.json().get('voices', []))
                voice_id    = os.getenv('ELEVENLABS_VOICE_ID', '').strip()
                detail      = f'{voice_count} voices available' + (f', voice ID set' if voice_id else ', voice ID not set yet')
                results['elevenlabs'] = {'status': 'ok', 'label': f'OK — {detail}', 'latency_ms': ms}
            else:
                results['elevenlabs'] = {'status': 'error', 'label': f'HTTP {r.status_code}', 'latency_ms': ms}
        except Exception as exc:
            results['elevenlabs'] = {'status': 'error', 'label': str(exc)[:100], 'latency_ms': None}

    # --- Leonardo.AI ---
    key = os.getenv('LEONARDO_API_KEY', '')
    if not key:
        results['leonardo'] = {'status': 'no_key', 'label': 'LEONARDO_API_KEY not set', 'latency_ms': None}
    else:
        try:
            t0 = time.time()
            r = req.get('https://cloud.leonardo.ai/api/rest/v1/me',
                        headers={'authorization': f'Bearer {key}'}, timeout=8)
            ms = round((time.time() - t0) * 1000)
            if r.status_code == 200:
                details  = r.json().get('user_details', [{}])[0]
                api_paid = details.get('apiPaidTokens', 0) or 0
                sub_tok  = details.get('subscriptionTokens', 0) or 0
                total    = api_paid + sub_tok
                slots    = details.get('apiConcurrencySlots', '?')
                results['leonardo'] = {
                    'status': 'ok',
                    'label': f'OK — {total:,} tokens ({api_paid:,} paid + {sub_tok} subscription), {slots} concurrency slots',
                    'latency_ms': ms,
                }
            else:
                results['leonardo'] = {'status': 'error', 'label': f'HTTP {r.status_code}', 'latency_ms': ms}
        except Exception as exc:
            results['leonardo'] = {'status': 'error', 'label': str(exc)[:100], 'latency_ms': None}

    # --- Pexels ---
    key = os.getenv('PEXELS_API_KEY', '')
    if not key:
        results['pexels'] = {'status': 'no_key', 'label': 'PEXELS_API_KEY not set', 'latency_ms': None}
    else:
        try:
            t0 = time.time()
            r = req.get('https://api.pexels.com/v1/search?query=test&per_page=1',
                        headers={'Authorization': key}, timeout=8)
            ms = round((time.time() - t0) * 1000)
            results['pexels'] = {
                'status': 'ok' if r.status_code == 200 else 'error',
                'label': f'OK — {ms} ms' if r.status_code == 200 else f'HTTP {r.status_code}',
                'latency_ms': ms if r.status_code == 200 else None,
            }
        except Exception as exc:
            results['pexels'] = {'status': 'error', 'label': str(exc)[:100], 'latency_ms': None}

    # --- YouTube ---
    secrets_file = os.getenv('YOUTUBE_CLIENT_SECRETS_FILE', 'client_secrets.json')
    if not Path(secrets_file).exists():
        # Check for auto-detected file
        found = next(Path('.').glob('client_secret_*.json'), None)
        if found:
            results['youtube'] = {
                'status': 'warning',
                'label': f'Found {found.name} — rename to client_secrets.json or update .env',
                'latency_ms': None,
            }
        else:
            results['youtube'] = {'status': 'no_key', 'label': 'client_secrets.json not found', 'latency_ms': None}
    elif Path('token.json').exists():
        results['youtube'] = {'status': 'ok', 'label': 'OAuth token present', 'latency_ms': 0}
    else:
        results['youtube'] = {
            'status': 'warning',
            'label': 'Secrets file found — run upload once to complete OAuth',
            'latency_ms': None,
        }

    # --- TikTok ---
    ck  = os.getenv('TIKTOK_CLIENT_KEY', '')
    cs  = os.getenv('TIKTOK_CLIENT_SECRET', '')
    tok = os.getenv('TIKTOK_ACCESS_TOKEN', '')
    if not ck or not cs:
        results['tiktok'] = {'status': 'no_key', 'label': 'Developer keys not set in .env', 'latency_ms': None}
    elif tok or Path('token_tiktok.json').exists():
        results['tiktok'] = {'status': 'ok', 'label': 'Access token present', 'latency_ms': 0}
    else:
        results['tiktok'] = {'status': 'warning', 'label': 'Keys set — click Re-auth to authorise', 'latency_ms': None}

    return jsonify(results)


@app.route('/api/analytics')
def api_analytics():
    import sqlite3 as _sqlite3
    try:
        conn = _sqlite3.connect('videoforge.db')
        conn.row_factory = _sqlite3.Row

        total_views = conn.execute(
            "SELECT COALESCE(SUM(views),0) FROM analytics"
        ).fetchone()[0]
        total_likes = conn.execute(
            "SELECT COALESCE(SUM(likes),0) FROM analytics"
        ).fetchone()[0]

        top_rows = conn.execute("""
            SELECT j.id, j.topic, j.bucket, j.channel_id,
                   COALESCE(SUM(a.views),0)  AS total_views,
                   COALESCE(SUM(a.likes),0)  AS total_likes,
                   AVG(a.avg_view_percentage) AS avg_retention_pct,
                   AVG(a.ctr)                 AS avg_ctr,
                   j.youtube_url, j.tiktok_url
            FROM jobs j
            LEFT JOIN analytics a ON j.id = a.job_id
            WHERE j.status = 'posted'
            GROUP BY j.id
            ORDER BY total_views DESC
            LIMIT 10
        """).fetchall()
        top_videos = [dict(r) for r in top_rows]

        bucket_rows = conn.execute("""
            SELECT j.bucket,
                   COUNT(DISTINCT j.id) AS video_count,
                   COALESCE(AVG(a.views), 0) AS avg_views
            FROM jobs j
            LEFT JOIN analytics a ON j.id = a.job_id
            WHERE j.status = 'posted' AND j.bucket IS NOT NULL
            GROUP BY j.bucket
        """).fetchall()
        by_bucket = [dict(r) for r in bucket_rows]

        conn.close()
        return jsonify({
            'total_views': total_views,
            'total_likes': total_likes,
            'top_videos':  top_videos,
            'by_bucket':   by_bucket,
        })
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/analytics/score-accuracy')
def api_score_accuracy():
    """
    Return score-accuracy correlation data for Phase 11.v2.E.

    Compares pre-production final_score from topic_bank (or similarity_score
    from jobs) against actual view performance. Only includes jobs that:
      - have status='posted'
      - have at least one analytics row

    Response JSON:
      {
        correlation: float | null,  # Pearson r (-1 to 1)
        data_points: int,
        points: [{topic, score, views, bucket}],
        summary: {high_score_avg_views, low_score_avg_views, lift_pct}
      }
    """
    try:
        from database import get_connection
        conn = get_connection()
        rows = conn.execute("""
            SELECT j.topic,
                   j.bucket,
                   j.similarity_score,
                   tb.final_score,
                   COALESCE(SUM(a.views), 0) AS total_views
            FROM jobs j
            LEFT JOIN analytics a   ON j.id = a.job_id
            LEFT JOIN topic_bank tb ON j.topic = tb.topic
            WHERE j.status = 'posted'
            GROUP BY j.id
            HAVING total_views > 0
            ORDER BY total_views DESC
        """).fetchall()
        conn.close()

        points = []
        for r in rows:
            score = r['final_score'] if r['final_score'] is not None else r['similarity_score']
            if score is None:
                continue
            points.append({
                'topic':  r['topic'],
                'bucket': r['bucket'],
                'score':  float(score),
                'views':  int(r['total_views']),
            })

        if len(points) < 2:
            return jsonify({
                'correlation': None,
                'data_points': len(points),
                'points': points,
                'summary': {},
                'message': 'Not enough data yet — need at least 2 posted videos with scores.',
            })

        # Pearson correlation (no numpy needed)
        scores = [p['score'] for p in points]
        views  = [p['views']  for p in points]
        n  = len(scores)
        sx = sum(scores);   sy = sum(views)
        sx2 = sum(x*x for x in scores)
        sy2 = sum(y*y for y in views)
        sxy = sum(x*y for x, y in zip(scores, views))
        denom = ((n*sx2 - sx*sx) * (n*sy2 - sy*sy)) ** 0.5
        corr  = round((n*sxy - sx*sy) / denom, 3) if denom > 0 else None

        # Simple summary: high-score (>=7) vs low-score (<7)
        high = [p['views'] for p in points if p['score'] >= 7]
        low  = [p['views'] for p in points if p['score'] < 7]
        high_avg = round(sum(high) / len(high)) if high else 0
        low_avg  = round(sum(low)  / len(low))  if low  else 0
        lift_pct = round((high_avg - low_avg) / low_avg * 100) if low_avg > 0 else None

        return jsonify({
            'correlation': corr,
            'data_points': len(points),
            'points': points,
            'summary': {
                'high_score_avg_views': high_avg,
                'low_score_avg_views':  low_avg,
                'lift_pct':             lift_pct,
            },
        })
    except Exception as exc:
        logger.error(f"api_score_accuracy error: {exc}", exc_info=True)
        return jsonify({'error': str(exc)}), 500


@app.route('/api/test-prompt', methods=['POST'])
def api_test_prompt():
    data        = request.get_json() or {}
    prompt_type = data.get('type', 'script')
    prompt_text = data.get('prompt', '')

    jobs = get_all_jobs()
    if not jobs:
        return jsonify({'error': 'No jobs exist yet — create a job first.'}), 400

    config = _load_config()
    job    = jobs[0]

    try:
        import anthropic
        api_key = os.getenv('ANTHROPIC_API_KEY', '')
        if not api_key:
            return jsonify({'error': 'ANTHROPIC_API_KEY not set in .env'}), 400

        if prompt_type == 'script':
            wc = config.get('script', {}).get('word_count_target', 175)
            variables = {
                'topic':                 job['topic'],
                'bucket':                job.get('bucket', 'elec'),
                'hook_style':            job.get('hook_style', 'shocking_fact'),
                'word_count_target':     str(wc),
                'word_count_min':        str(int(wc * 0.9)),
                'word_count_max':        str(int(wc * 1.1)),
                'target_length_seconds': str(config.get('channel', {}).get('target_length_seconds', 70)),
                'images_to_generate':    str(config.get('script', {}).get('images_to_generate', 8)),
            }
        else:
            mc = config.get('metadata', {})
            variables = {
                'topic':                         job['topic'],
                'bucket':                        job.get('bucket', 'elec'),
                'hook_style':                    job.get('hook_style', 'shocking_fact'),
                'narration':                     '[narration would appear here]',
                'hashtag_count':                 str(mc.get('hashtag_count', 10)),
                'description_max_chars':         str(mc.get('description_max_chars', 150)),
                'youtube_description_max_chars':  str(mc.get('youtube_description_max_chars', 500)),
                'default_hashtags':              ', '.join(mc.get('default_hashtags', [])),
            }

        rendered = prompt_text
        for k, v in variables.items():
            rendered = rendered.replace('{' + k + '}', v)

        client = anthropic.Anthropic(api_key=api_key)
        t0 = time.time()
        resp = client.messages.create(
            model=config.get('script', {}).get('model', 'claude-sonnet-4-6'),
            max_tokens=500,
            messages=[{'role': 'user', 'content': rendered}]
        )
        elapsed = round(time.time() - t0, 2)

        return jsonify({
            'preview':         resp.content[0].text,
            'topic':           job['topic'],
            'elapsed_seconds': elapsed,
        })
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/check-similarity', methods=['POST'])
def api_check_similarity():
    """
    Check a new topic for similarity against existing jobs and topic_bank.
    Called by the New Job form before submission.

    Request JSON: {topic: str}
    Response JSON: {checked, similarity_score, similar_topic, similar_job_id,
                    similar_source, angle_suggestion, warning}
    """
    data  = request.get_json(silent=True) or {}
    topic = str(data.get('topic', '')).strip()
    if not topic:
        return jsonify({'error': 'topic is required'}), 400
    try:
        from modules.similarity_engine import check_similarity
        config = _load_config()
        result = check_similarity(topic, config)
        return jsonify(result)
    except Exception as exc:
        logger.error(f"api_check_similarity error: {exc}", exc_info=True)
        return jsonify({'error': str(exc)}), 500


@app.route('/api/refresh-analytics', methods=['POST'])
def api_refresh_analytics():
    """Manually trigger an analytics pull for all posted jobs."""
    try:
        from modules.analytics_engine import pull_all_analytics
        selected = session.get('selected_channel', 'all')
        ch_filter = None if selected == 'all' else selected
        summary = pull_all_analytics(channel_id=ch_filter)
        return jsonify({
            'message':        'Analytics pull complete',
            'jobs_processed':  summary['jobs_processed'],
            'youtube_updated': summary['youtube_updated'],
            'tiktok_updated':  summary['tiktok_updated'],
            'errors':          summary['errors'],
        })
    except Exception as exc:
        logger.error(f"api_refresh_analytics error: {exc}", exc_info=True)
        return jsonify({'error': str(exc)}), 500


@app.route('/api/analytics/manual', methods=['POST'])
def api_analytics_manual():
    """
    Store a manual CTR/impressions entry for a posted video.

    JSON body:
      job_id            str   required
      impressions       int   required
      ctr               float required  (0.03 = 3%)
      avg_view_pct      float optional  (0-100)
      avg_view_duration float optional  (seconds)
      views             int   optional
      likes             int   optional
      platform          str   optional  default 'youtube'
    """
    try:
        body      = request.get_json(force=True)
        job_id    = body.get('job_id', '').strip()
        platform  = body.get('platform', 'youtube')

        if not job_id:
            return jsonify({'error': 'job_id is required'}), 400

        job = get_job(job_id)
        if not job:
            return jsonify({'error': f'Job {job_id} not found'}), 404

        channel_id = job.get('channel_id', 'engineering_brief')

        impressions = body.get('impressions')
        ctr         = body.get('ctr')

        if impressions is None or ctr is None:
            return jsonify({'error': 'impressions and ctr are required'}), 400

        insert_manual_analytics(
            job_id=job_id,
            platform=platform,
            impressions=int(impressions),
            ctr=float(ctr),
            avg_view_percentage=body.get('avg_view_pct'),
            avg_view_duration=body.get('avg_view_duration'),
            views=body.get('views'),
            likes=body.get('likes'),
            channel_id=channel_id,
            data_source='manual',
        )
        logger.info(
            f"Manual analytics entry stored — job: {job_id}, "
            f"impressions: {impressions}, ctr: {ctr}"
        )
        return jsonify({'success': True, 'job_id': job_id})

    except Exception as exc:
        logger.error(f"api_analytics_manual error: {exc}", exc_info=True)
        return jsonify({'error': str(exc)}), 500


@app.route('/api/analytics/csv-import', methods=['POST'])
def api_analytics_csv_import():
    """
    Import CTR and impressions from a YouTube Studio Content CSV export.

    Accepts multipart/form-data with a 'file' field containing the CSV.

    Expected CSV columns (YouTube Studio format):
      Video title, Video publish time, Views, Watch time (hours),
      Subscribers, Impressions, Impressions click-through rate (%),
      Average view duration, Average percentage viewed (%)

    Matching is done by job topic substring against video title.
    Each matched row is inserted as a new 'csv' data_source snapshot.
    """
    import csv
    import io

    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file in request — send multipart/form-data with field "file"'}), 400

        raw_bytes = request.files['file'].read()
        text      = raw_bytes.decode('utf-8-sig')  # handle BOM
        reader    = csv.DictReader(io.StringIO(text))

        rows_imported = 0
        rows_skipped  = 0
        errors_list   = []

        posted_jobs = get_all_jobs(status_filter='posted')
        topic_map   = {j['topic'].lower(): j for j in posted_jobs}

        for row in reader:
            title = (
                row.get('Video title') or
                row.get('Content') or
                row.get('Video')
                or ''
            ).strip().lower()

            if not title:
                rows_skipped += 1
                continue

            # Find the best matching job
            matched_job = None
            for topic_lower, job in topic_map.items():
                if topic_lower in title or title in topic_lower:
                    matched_job = job
                    break

            if not matched_job:
                rows_skipped += 1
                continue

            try:
                def _parse_float(s):
                    if s is None:
                        return None
                    cleaned = str(s).replace('%', '').replace(',', '').strip()
                    return float(cleaned) if cleaned else None

                def _parse_int(s):
                    if s is None:
                        return None
                    cleaned = str(s).replace(',', '').strip()
                    return int(float(cleaned)) if cleaned else None

                impressions = _parse_int(row.get('Impressions'))
                ctr_pct     = _parse_float(
                    row.get('Impressions click-through rate (%)') or
                    row.get('Impressions CTR (%)')
                )
                ctr         = (ctr_pct / 100.0) if ctr_pct is not None else None
                avg_pct     = _parse_float(row.get('Average percentage viewed (%)'))
                views       = _parse_int(row.get('Views'))

                avg_dur_str = (row.get('Average view duration') or '').strip()
                avg_dur = None
                if avg_dur_str and ':' in avg_dur_str:
                    parts = avg_dur_str.split(':')
                    try:
                        if len(parts) == 2:
                            avg_dur = int(parts[0]) * 60 + int(parts[1])
                        elif len(parts) == 3:
                            avg_dur = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    except ValueError:
                        avg_dur = None

                if impressions is None and ctr is None:
                    rows_skipped += 1
                    continue

                insert_manual_analytics(
                    job_id=matched_job['id'],
                    platform='youtube',
                    impressions=impressions or 0,
                    ctr=ctr or 0.0,
                    avg_view_percentage=avg_pct,
                    avg_view_duration=avg_dur,
                    views=views,
                    likes=None,
                    channel_id=matched_job.get('channel_id', 'engineering_brief'),
                    data_source='csv',
                )
                rows_imported += 1

            except Exception as row_exc:
                errors_list.append({'title': title, 'error': str(row_exc)})

        logger.info(
            f"CSV import complete — imported: {rows_imported}, "
            f"skipped: {rows_skipped}, errors: {len(errors_list)}"
        )
        return jsonify({
            'imported': rows_imported,
            'skipped':  rows_skipped,
            'errors':   errors_list,
        })

    except Exception as exc:
        logger.error(f"api_analytics_csv_import error: {exc}", exc_info=True)
        return jsonify({'error': str(exc)}), 500


@app.route('/api/analytics/channel-health')
def api_analytics_channel_health():
    """
    Return channel health summary card data for all active channels.

    Response:
      {
        channels: [{
          channel_id, channel_name,
          videos_posted, days_since_first_upload,
          avg_retention_pct, avg_ctr,
          trend_vs_prev5_views,   # % change vs 5 videos before latest 5
          latest_5_avg_views, prev_5_avg_views,
        }]
      }
    """
    try:
        from database import get_connection

        all_channels = get_channels(active_only=True)
        results      = []

        for ch in all_channels:
            ch_id   = ch['id']
            ch_name = ch['name']

            conn = get_connection()
            jobs = conn.execute("""
                SELECT j.id, j.created_at,
                       MAX(a.pulled_at) AS last_pull,
                       MAX(a.views)     AS views,
                       AVG(a.avg_view_percentage) AS avg_retention,
                       AVG(a.ctr)                 AS avg_ctr
                FROM jobs j
                LEFT JOIN analytics a ON j.id = a.job_id AND a.platform = 'youtube'
                WHERE j.status = 'posted' AND j.channel_id = ?
                GROUP BY j.id
                ORDER BY j.created_at ASC
            """, (ch_id,)).fetchall()
            conn.close()

            if not jobs:
                results.append({
                    'channel_id':             ch_id,
                    'channel_name':           ch_name,
                    'videos_posted':          0,
                    'days_since_first_upload': None,
                    'avg_retention_pct':      None,
                    'avg_ctr':                None,
                    'latest_5_avg_views':     None,
                    'prev_5_avg_views':       None,
                    'trend_pct':              None,
                })
                continue

            from datetime import datetime, timezone, timedelta

            def _parse_dt(s):
                if not s:
                    return None
                try:
                    dt = datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt
                except Exception:
                    return None

            first_dt = _parse_dt(jobs[0]['created_at'])
            days_live = None
            if first_dt:
                days_live = round((datetime.now(timezone.utc) - first_dt).total_seconds() / 86400, 1)

            all_views = [r['views'] or 0 for r in jobs]
            ret_vals  = [r['avg_retention'] for r in jobs if r['avg_retention'] is not None]
            ctr_vals  = [r['avg_ctr']       for r in jobs if r['avg_ctr']       is not None]

            avg_ret = round(sum(ret_vals) / len(ret_vals), 1) if ret_vals else None
            avg_ctr = round((sum(ctr_vals) / len(ctr_vals)) * 100, 2) if ctr_vals else None

            latest5  = all_views[-5:] if len(all_views) >= 5 else all_views
            prev5    = all_views[-10:-5] if len(all_views) >= 10 else (all_views[:-5] if len(all_views) > 5 else [])

            l5_avg = round(sum(latest5) / len(latest5), 1) if latest5 else None
            p5_avg = round(sum(prev5)   / len(prev5),   1) if prev5   else None

            trend_pct = None
            if l5_avg is not None and p5_avg and p5_avg > 0:
                trend_pct = round(((l5_avg - p5_avg) / p5_avg) * 100, 1)

            from database import get_archive_size_bytes
            archive_info = get_archive_size_bytes(channel_id=ch_id)
            archive_bytes = archive_info['channels'].get(ch_id, 0)

            results.append({
                'channel_id':              ch_id,
                'channel_name':            ch_name,
                'videos_posted':           len(jobs),
                'days_since_first_upload': days_live,
                'avg_retention_pct':       avg_ret,
                'avg_ctr':                 avg_ctr,
                'latest_5_avg_views':      l5_avg,
                'prev_5_avg_views':        p5_avg,
                'trend_pct':               trend_pct,
                'archive_size_bytes':      archive_bytes,
                'archive_size_gb':         round(archive_bytes / (1024 ** 3), 2),
            })

        return jsonify({'channels': results})

    except Exception as exc:
        logger.error(f"api_analytics_channel_health error: {exc}", exc_info=True)
        return jsonify({'error': str(exc)}), 500


@app.route('/api/analytics/kill-metrics')
def api_analytics_kill_metrics():
    """
    Return kill-metrics verdict for all active channels.

    Response:
      {
        verdicts: {
          channel_id: {
            verdict, rule_fired, checkpoint,
            video_count, days_live, metrics
          }
        }
      }
    """
    try:
        from modules.kill_metrics import compute_all_channel_verdicts

        all_channels = get_channels(active_only=True)
        channel_ids  = [ch['id'] for ch in all_channels]

        # Load kill_metrics config from global config
        global_cfg = _load_config()
        kill_cfg   = global_cfg.get('kill_metrics', {})

        # Fetch latest analytics per job across all channels
        jobs_with_analytics = get_latest_analytics_per_job(channel_id=None, platform='youtube')

        verdicts = compute_all_channel_verdicts(channel_ids, jobs_with_analytics, kill_cfg)

        return jsonify({'verdicts': verdicts})

    except Exception as exc:
        logger.error(f"api_analytics_kill_metrics error: {exc}", exc_info=True)
        return jsonify({'error': str(exc)}), 500


# ---------------------------------------------------------------------------
# Comment mining + calendar API endpoints (11.v2.C / 11.v2.D)
# ---------------------------------------------------------------------------

@app.route('/api/mine-comments', methods=['POST'])
def api_mine_comments():
    """Manually trigger YouTube comment mining."""
    try:
        from modules.comment_miner import mine_comments
        config = _load_config()
        result = mine_comments(config)
        return jsonify(result)
    except Exception as exc:
        logger.error(f"api_mine_comments error: {exc}", exc_info=True)
        return jsonify({'error': str(exc)}), 500


@app.route('/api/auto-fill-calendar', methods=['POST'])
def api_auto_fill_calendar():
    """Manually trigger the auto-fill weekly calendar job."""
    try:
        from scheduler import run_auto_fill_calendar
        n = int(request.get_json(silent=True).get('n', 5)) if request.get_json(silent=True) else 5
        result = run_auto_fill_calendar(n=n)
        return jsonify(result)
    except Exception as exc:
        logger.error(f"api_auto_fill_calendar error: {exc}", exc_info=True)
        return jsonify({'error': str(exc)}), 500


# ---------------------------------------------------------------------------
# Static files for output assets (video player + thumbnail preview)
# ---------------------------------------------------------------------------

@app.route('/output/videos/<path:filename>')
def serve_video(filename):
    return send_from_directory(Path('output/videos').resolve(), filename)


@app.route('/output/thumbnails/<path:filename>')
def serve_thumbnail(filename):
    return send_from_directory(Path('output/thumbnails').resolve(), filename)


# ---------------------------------------------------------------------------
# TikTok re-auth
# ---------------------------------------------------------------------------

@app.route('/health/reauth/tiktok', methods=['POST'])
def reauth_tiktok():
    ck = os.getenv('TIKTOK_CLIENT_KEY', '')
    cs = os.getenv('TIKTOK_CLIENT_SECRET', '')
    if not ck or not cs:
        flash('Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env first.', 'error')
        return redirect(url_for('health'))
    try:
        from modules.upload_engine import _run_tiktok_oauth
        _run_tiktok_oauth(ck, cs, 'reauth')
        flash('TikTok re-auth successful. Token saved.', 'success')
    except Exception as exc:
        flash(f'TikTok re-auth failed: {exc}', 'error')
    return redirect(url_for('health'))


# ---------------------------------------------------------------------------
# Research — Phase 11.v1.B/D
# ---------------------------------------------------------------------------

def _research_scan_context(config: dict) -> dict:
    """Build shared context for the trends page."""
    from database import get_active_alerts, get_trend_scans, count_scans_since
    from datetime import datetime, timedelta

    rc          = config.get('research', {})
    now         = datetime.utcnow()
    hour_ago    = (now - timedelta(hours=1)).isoformat()
    day_ago     = (now - timedelta(hours=24)).isoformat()
    history     = get_trend_scans(limit=50)

    return {
        'active_alerts':  _parse_alert_angles(get_active_alerts()),
        'scan_history':   history,
        'last_scan':      history[0] if history else None,
        'scans_today':    count_scans_since(day_ago),
        'scans_hour':     count_scans_since(hour_ago),
        'safe_per_hour':  rc.get('safe_scans_per_hour', 5),
        'safe_per_day':   rc.get('safe_scans_per_day', 15),
    }


@app.route('/research/trends')
def research_trends():
    config = _load_config()
    ctx    = _research_scan_context(config)
    return render_template('trends.html', active='trends', **ctx)


@app.route('/research/trends/scan', methods=['POST'])
def research_trends_scan():
    """Trigger an on-demand trend scan from the dashboard."""
    config = _load_config()
    try:
        from modules.trend_monitor import run_scan
        result = run_scan(config)
        if result.get('blocked'):
            flash(f"Scan blocked: {result['reason']}", 'warning')
        elif result['success']:
            flash(
                f"Scan complete — {result['topics_found']} spike(s) found, "
                f"{result['new_alerts']} alert(s) created.",
                'success' if result['new_alerts'] > 0 else 'info',
            )
        else:
            flash(f"Scan failed: {result.get('reason', 'unknown error')}", 'error')
    except Exception as exc:
        logger.error(f"research_trends_scan error: {exc}", exc_info=True)
        flash(f"Scan error: {exc}", 'error')
    return redirect(url_for('research_trends'))


@app.route('/research/alerts/<int:alert_id>/dismiss', methods=['POST'])
def dismiss_alert_route(alert_id):
    from database import dismiss_alert
    dismiss_alert(alert_id)
    flash('Alert dismissed.', 'success')
    return redirect(request.referrer or url_for('research_trends'))


@app.route('/research/alerts/<int:alert_id>/fast-track', methods=['POST'])
def fast_track_alert(alert_id):
    """Create a job from a priority alert and start the pipeline in background."""
    from database import get_active_alerts, link_alert_to_job

    config = _load_config()
    alerts = get_active_alerts()
    alert  = next((a for a in alerts if a['id'] == alert_id), None)

    if not alert:
        flash(f'Alert {alert_id} not found or no longer active.', 'error')
        return redirect(url_for('research_trends'))

    # selected_angle is set when the user clicks one of the three angle option cards
    selected_angle = request.form.get('selected_angle', '').strip()
    topic  = selected_angle or alert.get('reframed_angle') or alert['topic']
    bucket = alert.get('bucket', 'elec')
    jid    = get_next_job_id()

    selected = session.get('selected_channel', 'all')
    job_channel = get_default_channel() if selected == 'all' else selected
    create_job(job_id=jid, topic=topic, bucket=bucket, hook_style='shocking_fact',
               channel_id=job_channel)
    link_alert_to_job(alert_id, jid)

    threading.Thread(
        target=_run_pipeline_thread,
        args=(jid, config),
        daemon=True,
    ).start()

    flash(f'Job {jid} fast-tracked — pipeline running in background.', 'success')
    logger.info(f"[JOB {jid}] Fast-tracked from alert {alert_id} — topic: '{topic}'")
    return redirect(url_for('overview'))


@app.route('/research/topics')
def research_topics():
    from database import get_topics, get_reddit_candidates
    show_archived = request.args.get('archived', '0') == '1'
    bucket_filter = request.args.get('bucket', '')
    sort_by       = request.args.get('sort', 'added_at')   # added_at | final_score | status

    # Reddit story candidates awaiting approval — shown in their own section
    reddit_candidates = get_reddit_candidates(include_all=False)

    # Scored/manual topics table excludes Reddit-sourced rows (those live in
    # their own candidates section above).
    topics = [t for t in get_topics(include_archived=show_archived)
              if t.get('source') != 'reddit']

    # alt_angles is stored as a JSON string in the DB — parse it into a list here
    # so templates can iterate directly without filter gymnastics.
    import json as _json
    for t in topics:
        raw = t.get('alt_angles')
        if isinstance(raw, str) and raw.strip():
            try:
                t['alt_angles'] = _json.loads(raw)
            except (_json.JSONDecodeError, ValueError):
                t['alt_angles'] = []
        elif not raw:
            t['alt_angles'] = []

    if bucket_filter:
        topics = [t for t in topics if t.get('bucket') == bucket_filter]

    # Re-sort if requested
    if sort_by == 'final_score':
        topics = sorted(topics, key=lambda t: (t.get('final_score') or -1), reverse=True)
    elif sort_by == 'status':
        status_order = {'pending': 0, 'scored': 1, 'queued': 2, 'used': 3, 'archived': 9}
        topics = sorted(topics, key=lambda t: status_order.get(t.get('status', ''), 5))

    return render_template(
        'topics.html',
        active='topics',
        topics=topics,
        reddit_candidates=reddit_candidates,
        show_archived=show_archived,
        bucket_filter=bucket_filter,
        sort_by=sort_by,
    )


@app.route('/research/topics/add', methods=['POST'])
def research_topics_add():
    from database import insert_topic
    topic  = request.form.get('topic', '').strip()
    bucket = request.form.get('bucket', '').strip()
    notes  = request.form.get('notes', '').strip()
    if not topic:
        flash('Topic text is required.', 'error')
        return redirect(url_for('research_topics'))
    insert_topic(topic=topic, bucket=bucket, notes=notes)
    flash(f'Topic added: \u201c{topic}\u201d', 'success')
    return redirect(url_for('research_topics'))


@app.route('/research/topics/<int:topic_id>/archive', methods=['POST'])
def research_topics_archive(topic_id):
    from database import archive_topic
    reason = request.form.get('reason', '')
    archive_topic(topic_id=topic_id, reason=reason)
    flash('Topic archived.', 'success')
    return redirect(request.referrer or url_for('research_topics'))


@app.route('/research/topics/<int:topic_id>/unarchive', methods=['POST'])
def research_topics_unarchive(topic_id):
    from database import unarchive_topic
    unarchive_topic(topic_id=topic_id)
    flash('Topic restored.', 'success')
    return redirect(request.referrer or url_for('research_topics'))


@app.route('/research/topics/<int:topic_id>/delete', methods=['POST'])
def research_topics_delete(topic_id):
    from database import delete_topic
    delete_topic(topic_id=topic_id)
    flash('Topic deleted.', 'success')
    return redirect(request.referrer or url_for('research_topics'))


@app.route('/research/topics/<int:topic_id>/queue', methods=['POST'])
def research_topics_queue(topic_id):
    """Add a topic bank entry to the job queue."""
    from database import get_topics
    topics = get_topics(include_archived=True)
    t = next((x for x in topics if x['id'] == topic_id), None)
    if not t:
        flash('Topic not found.', 'error')
        return redirect(url_for('research_topics'))
    jid = get_next_job_id()
    selected = session.get('selected_channel', 'all')
    job_channel = t.get('channel_id') or (get_default_channel() if selected == 'all' else selected)
    create_job(
        job_id=jid,
        topic=t['topic'],
        bucket=t.get('bucket') or 'elec',
        hook_style='shocking_fact',
        channel_id=job_channel,
    )
    flash(f'Job {jid} added to queue from topic bank.', 'success')
    return redirect(url_for('research_topics'))


@app.route('/research/topics/<int:topic_id>/approve-reddit', methods=['POST'])
def approve_reddit_candidate(topic_id):
    """
    Approve a Reddit story candidate: create a reddit-mode job and run the
    script (rewrite) stage immediately, stopping at the hook-selection gate.

    Reddit candidates only enter the pipeline here — nothing happens to them
    until the owner clicks Approve.
    """
    from database import get_topic, update_topic_status
    t = get_topic(topic_id)
    if not t:
        flash('Candidate not found.', 'error')
        return redirect(url_for('research_topics'))
    if t.get('source') != 'reddit':
        flash('That topic is not a Reddit candidate.', 'error')
        return redirect(url_for('research_topics'))

    jid = get_next_job_id()
    selected = session.get('selected_channel', 'all')
    job_channel = t.get('channel_id') or (get_default_channel() if selected == 'all' else selected)
    create_job(
        job_id=jid,
        topic=t['topic'],
        bucket='reddit',
        hook_style='reddit_story',
        mode='reddit',
        source='reddit',
        source_selftext=t.get('selftext') or '',
        channel_id=job_channel,
    )
    if t.get('reddit_id'):
        update_job_field(jid, 'reddit_post_id', t['reddit_id'])
    update_topic_status(topic_id, 'queued')
    logger.info(f"[JOB {jid}] Created from Reddit candidate {topic_id} — running script stage")

    config = _load_config(channel_slug=job_channel)
    threading.Thread(
        target=_run_pipeline_thread,
        args=(jid, config, 'generate-script'),
        kwargs={'stop_after': 'generate-script'},
        daemon=True,
    ).start()

    flash(f'Reddit story approved — job {jid} is generating its script. '
          f'Pick a hook when it finishes.', 'success')
    return redirect(url_for('job_detail', job_id=jid))


# ---------------------------------------------------------------------------
# Topic scoring API routes (11.v2.A / 11.v2.B)
# ---------------------------------------------------------------------------

@app.route('/research/topics/<int:topic_id>/score', methods=['POST'])
def research_topics_score(topic_id):
    """Run the scoring engine for a single topic and refresh the page."""
    from database import get_topics
    from modules.research_engine import score_topic
    topics = get_topics(include_archived=True)
    t = next((x for x in topics if x['id'] == topic_id), None)
    if not t:
        flash('Topic not found.', 'error')
        return redirect(url_for('research_topics'))

    config = _load_config()
    result = score_topic(
        topic=t['topic'],
        bucket=t.get('bucket') or 'elec',
        config=config,
        topic_id=topic_id,
    )

    if result.get('success'):
        flash(
            f'\u201c{t["topic"]}\u201d scored: {result["final_score"]}/10',
            'success',
        )
    else:
        flash('Scoring failed — check logs for details.', 'error')

    return redirect(url_for('research_topics'))


@app.route('/api/score-topic', methods=['POST'])
def api_score_topic():
    """
    Score a topic via the research engine and return JSON.

    Request JSON: {topic, bucket, topic_id (optional)}
    Response JSON: full score result dict
    """
    data     = request.get_json(silent=True) or {}
    topic    = str(data.get('topic', '')).strip()
    bucket   = str(data.get('bucket', 'elec')).strip()
    topic_id = int(data.get('topic_id', 0))

    if not topic:
        return jsonify({'error': 'topic is required'}), 400

    try:
        from modules.research_engine import score_topic
        config = _load_config()
        result = score_topic(topic=topic, bucket=bucket, config=config, topic_id=topic_id)
        return jsonify(result)
    except Exception as exc:
        logger.error(f"api_score_topic error: {exc}", exc_info=True)
        return jsonify({'error': str(exc)}), 500


@app.route('/api/score-bulk', methods=['POST'])
def api_score_bulk():
    """
    Score up to 20 topics in one call.

    Request JSON: {topics: [{topic, bucket, id?}, ...]}
    Response JSON: {results: [...sorted by final_score desc...]}
    """
    data   = request.get_json(silent=True) or {}
    topics = data.get('topics', [])
    if not topics or not isinstance(topics, list):
        return jsonify({'error': 'topics array is required'}), 400
    if len(topics) > 20:
        topics = topics[:20]

    try:
        from modules.research_engine import score_bulk
        config  = _load_config()
        results = score_bulk(topics=topics, config=config)
        return jsonify({'results': results})
    except Exception as exc:
        logger.error(f"api_score_bulk error: {exc}", exc_info=True)
        return jsonify({'error': str(exc)}), 500


@app.route('/research/topics/score-top', methods=['POST'])
def research_score_top():
    """Score the top N unscored topics and redirect back."""
    n = int(request.form.get('n', 5))
    from database import get_topics
    from modules.research_engine import score_topic
    config   = _load_config()
    unscored = [t for t in get_topics() if not t.get('final_score')][:n]

    scored = 0
    for t in unscored:
        r = score_topic(
            topic=t['topic'],
            bucket=t.get('bucket') or 'elec',
            config=config,
            topic_id=t['id'],
        )
        if r.get('success'):
            scored += 1

    flash(f'Scored {scored} topic{"s" if scored != 1 else ""}.', 'success')
    return redirect(url_for('research_topics', sort='final_score'))


# ---------------------------------------------------------------------------
# Phase 13 Block B — API usage dashboard
# ---------------------------------------------------------------------------

def _month_start_iso() -> str:
    """Return the UTC ISO datetime for the first of the current month."""
    now = datetime.utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0,
                       microsecond=0).strftime('%Y-%m-%d %H:%M:%S')


def _read_channel_budgets() -> dict:
    """
    Return {channel_id: {provider: budget_cents}} read from each channel's
    config.json overlay (research/usage.monthly_budget_cents).
    """
    budgets: dict = {}
    ch_root = Path('channels')
    if not ch_root.exists():
        return budgets
    for ch_dir in ch_root.iterdir():
        cfg_path = ch_dir / 'config.json'
        if not cfg_path.exists():
            continue
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        usage_cfg = (data.get('usage') or {}).get('monthly_budget_cents') or {}
        if isinstance(usage_cfg, dict) and usage_cfg:
            budgets[ch_dir.name] = {k: int(v) for k, v in usage_cfg.items()}
    return budgets


@app.route('/api-usage')
def api_usage_page():
    """Render API usage breakdown for the current month with budget alerts."""
    from database import get_api_usage_summary
    selected = session.get('selected_channel', 'all')
    ch_filter = None if selected == 'all' else selected
    rows = get_api_usage_summary(
        since_iso=_month_start_iso(),
        channel_id=ch_filter,
    )
    budgets = _read_channel_budgets()

    # Decorate rows with budget % and alert flags
    decorated = []
    total_cents = 0
    for r in rows:
        ch = r['channel_id'] or 'unknown'
        prov = r['provider']
        budget_cents = budgets.get(ch, {}).get(prov, 0)
        spent_cents = int(r['cost_estimate_cents'] or 0)
        pct = (spent_cents / budget_cents * 100.0) if budget_cents > 0 else None
        alert = pct is not None and pct >= 80.0
        decorated.append({
            **r,
            'budget_cents': budget_cents,
            'pct_of_budget': pct,
            'alert_80pct': alert,
        })
        total_cents += spent_cents

    # Block G — R2 storage per channel + next-cleanup date
    from database import get_r2_storage_by_channel
    r2_storage = get_r2_storage_by_channel()
    if ch_filter:
        r2_storage = {k: v for k, v in r2_storage.items() if k == ch_filter}

    return render_template(
        'api_usage.html',
        active='api_usage',
        rows=decorated,
        total_cents=total_cents,
        channel_filter=ch_filter,
        month_start=_month_start_iso()[:10],
        r2_storage=r2_storage,
    )


@app.route('/api/api-usage.csv')
def api_usage_csv():
    """Stream the current month's usage breakdown as CSV."""
    import csv, io
    from database import get_api_usage_summary
    selected = session.get('selected_channel', 'all')
    ch_filter = None if selected == 'all' else selected
    rows = get_api_usage_summary(
        since_iso=_month_start_iso(),
        channel_id=ch_filter,
    )
    budgets = _read_channel_budgets()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['channel_id', 'provider', 'calls', 'units_used',
                'cost_estimate_cents', 'budget_cents', 'pct_of_budget'])
    for r in rows:
        ch = r['channel_id'] or 'unknown'
        prov = r['provider']
        budget_cents = budgets.get(ch, {}).get(prov, 0)
        spent = int(r['cost_estimate_cents'] or 0)
        pct = round((spent / budget_cents * 100.0), 1) if budget_cents > 0 else ''
        w.writerow([ch, prov, r['calls'], r['units_used'], spent, budget_cents, pct])

    from flask import Response
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=api_usage.csv'},
    )


# ---------------------------------------------------------------------------
# Phase 13 Block A — Content templates dashboard
# ---------------------------------------------------------------------------

@app.route('/templates')
def templates_page():
    """List content_templates rows, optionally filtered by selected channel."""
    from database import get_templates
    selected = session.get('selected_channel', 'all')
    ch_filter = None if selected == 'all' else selected
    rows = get_templates(channel_id=ch_filter)
    return render_template(
        'templates.html',
        active='templates',
        templates=rows,
        channel_filter=ch_filter,
    )


@app.route('/templates/new', methods=['POST'])
def templates_create():
    """Create a new content_templates row from the dashboard form."""
    from database import insert_template
    f = request.form
    pool = [s.strip() for s in (f.get('hook_pool', '') or '').split(',') if s.strip()]
    new_id = insert_template(
        channel_id=f.get('channel_id', '').strip(),
        name=f.get('name', '').strip(),
        visual_mode=f.get('visual_mode', 'images'),
        length_min_seconds=int(f.get('length_min_seconds') or 55),
        length_max_seconds=int(f.get('length_max_seconds') or 90),
        hook_style_pool=pool,
        music_palette=f.get('music_palette', ''),
        thumbnail_mode=f.get('thumbnail_mode', 'frame_capture'),
        caption_mode=f.get('caption_mode', 'on'),
        dual_output=(f.get('dual_output') == '1'),
    )
    flash(f'Template created (id={new_id}).', 'success')
    return redirect(url_for('templates_page'))


@app.route('/templates/<int:template_id>/edit', methods=['POST'])
def templates_edit(template_id):
    """Update an existing content_templates row."""
    from database import update_template
    f = request.form
    pool = [s.strip() for s in (f.get('hook_pool', '') or '').split(',') if s.strip()]
    update_template(
        template_id,
        name=f.get('name', '').strip(),
        visual_mode=f.get('visual_mode', 'images'),
        length_min_seconds=int(f.get('length_min_seconds') or 55),
        length_max_seconds=int(f.get('length_max_seconds') or 90),
        hook_style_pool=pool,
        music_palette=f.get('music_palette', ''),
        thumbnail_mode=f.get('thumbnail_mode', 'frame_capture'),
        caption_mode=f.get('caption_mode', 'on'),
        dual_output=(f.get('dual_output') == '1'),
    )
    flash(f'Template {template_id} updated.', 'success')
    return redirect(url_for('templates_page'))


@app.route('/templates/<int:template_id>/clone', methods=['POST'])
def templates_clone(template_id):
    """Clone a template under a new name (same channel by default)."""
    from database import get_template, insert_template
    src = get_template(template_id)
    if not src:
        flash(f'Template {template_id} not found.', 'error')
        return redirect(url_for('templates_page'))
    new_name = (request.form.get('name') or f"{src['name']}_copy").strip()
    new_id = insert_template(
        channel_id=src['channel_id'],
        name=new_name,
        visual_mode=src['visual_mode'],
        length_min_seconds=src['length_min_seconds'],
        length_max_seconds=src['length_max_seconds'],
        hook_style_pool=src['hook_style_pool'],
        music_palette=src['music_palette'],
        thumbnail_mode=src['thumbnail_mode'],
        caption_mode=src.get('caption_mode', 'on'),
        prompt_overrides=src['prompt_overrides'],
        dual_output=src['dual_output'],
    )
    flash(f'Template cloned as id {new_id}.', 'success')
    return redirect(url_for('templates_page'))


@app.route('/templates/<int:template_id>/toggle', methods=['POST'])
def templates_toggle(template_id):
    """Flip active=1/0 on a template."""
    from database import get_template, update_template
    t = get_template(template_id)
    if not t:
        flash(f'Template {template_id} not found.', 'error')
        return redirect(url_for('templates_page'))
    update_template(template_id, active=not t['active'])
    flash(
        f"Template {template_id} {'deactivated' if t['active'] else 'activated'}.",
        'success',
    )
    return redirect(url_for('templates_page'))


@app.route('/templates/<int:template_id>/delete', methods=['POST'])
def templates_delete(template_id):
    """Delete a template row."""
    from database import delete_template
    delete_template(template_id)
    flash(f'Template {template_id} deleted.', 'success')
    return redirect(url_for('templates_page'))


# ---------------------------------------------------------------------------
# Scheduler status API (Phase 10)
# ---------------------------------------------------------------------------

@app.route('/api/scheduler-status')
def api_scheduler_status():
    """Return the current scheduler state (running jobs and next fire times)."""
    try:
        from scheduler import get_scheduler_status
        return jsonify(get_scheduler_status())
    except Exception as exc:
        return jsonify({'running': False, 'error': str(exc)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    init_db()

    # Start APScheduler in the background (Sunday batch + Monday analytics)
    from scheduler import start_scheduler
    start_scheduler()

    port = int(os.getenv('FLASK_PORT', 5000))
    logger.info(f"Starting VideoForge dashboard at http://localhost:{port}")
    app.run(host='127.0.0.1', port=port, debug=False, threaded=True)
