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
                   request, send_from_directory, url_for)

load_dotenv()

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent))

# 3. Local modules
from database import (create_job, get_all_jobs, get_job, get_next_job_id,
                      init_db, update_job_field, update_job_status)
from utils.logger import setup_logger
from webhook import webhook_bp

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'videoforge-dev-secret-change-me')

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

# Register the webhook blueprint (provides /webhook/new-topic)
app.register_blueprint(webhook_bp)

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
                         start_from: str = 'generate-script') -> None:
    """Run the full pipeline for job_id in a background thread."""
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


def _run_upload_thread(job_id: str, config: dict) -> None:
    """Run upload_engine in a background thread after Approve."""
    logger.info(f"[JOB {job_id}] Upload thread started")
    with _pipeline_lock:
        _pipeline_state['running'] = True
        _pipeline_state['job_id'] = job_id

    try:
        update_job_status(job_id, 'uploading')
        from modules.upload_engine import upload_video
        result = upload_video(job_id=job_id, config=config)
        logger.info(f"[JOB {job_id}] Upload result: youtube={result.get('youtube',{}).get('success')}, "
                    f"tiktok={result.get('tiktok',{}).get('success')}")
    except Exception as exc:
        logger.error(f"[JOB {job_id}] Upload thread error: {exc}", exc_info=True)
        update_job_status(job_id, 'failed', error_module='upload_engine', error_message=str(exc))
    finally:
        with _pipeline_lock:
            _pipeline_state['running'] = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
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


def _get_stats() -> dict:
    jobs = get_all_jobs()
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


def _status_color(status: str) -> str:
    mapping = {
        'queued': 'gray', 'scripting': 'blue', 'voiced': 'blue',
        'imaging': 'blue', 'assembling': 'blue', 'captioning': 'blue',
        'metadata': 'blue', 'review': 'amber', 'uploading': 'purple',
        'posted': 'green', 'failed': 'red',
    }
    return mapping.get(status, 'gray')


app.jinja_env.globals['status_color'] = _status_color


# ---------------------------------------------------------------------------
# Main pages
# ---------------------------------------------------------------------------

@app.route('/')
def overview():
    from database import get_active_alerts
    stats = _get_stats()
    jobs = get_all_jobs()
    review_jobs = [j for j in jobs if j.get('status') == 'review']
    with _pipeline_lock:
        pipeline_running = _pipeline_state.get('running', False)
        pipeline_job_id  = _pipeline_state.get('job_id')
    return render_template('dashboard.html',
                           stats=stats,
                           review_jobs=review_jobs,
                           pipeline_running=pipeline_running,
                           pipeline_job_id=pipeline_job_id,
                           active_alerts=get_active_alerts(),
                           active='overview')


@app.route('/jobs')
def jobs_list():
    status_filter = request.args.get('status', 'all')
    all_jobs = get_all_jobs()
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
        created_ids = []
        for t in topics:
            jid = get_next_job_id()
            create_job(job_id=jid, topic=t, bucket=bucket, hook_style=hook)
            created_ids.append(jid)
            logger.info(f"[JOB {jid}] Created via dashboard — topic: '{t}'")

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

    video_url = None
    if job.get('final_video_path') and Path(job['final_video_path']).exists():
        video_url = f'/output/videos/{job_id}_captioned.mp4'
    elif job.get('raw_video_path') and Path(job['raw_video_path']).exists():
        video_url = f'/output/videos/{job_id}_raw.mp4'

    thumbnail_url = None
    if job.get('thumbnail_path') and Path(job['thumbnail_path']).exists():
        thumbnail_url = f'/output/thumbnails/{job_id}.jpg'

    log_lines = _read_log_lines(module='main', level='ALL',
                                job_filter=job_id, limit=150)

    return render_template('job_detail.html',
                           job=job,
                           script_data=script_data,
                           meta_data=meta_data,
                           log_lines=log_lines,
                           video_url=video_url,
                           thumbnail_url=thumbnail_url,
                           active='jobs')


@app.route('/jobs/<job_id>/approve', methods=['POST'])
def approve_job(job_id):
    job = get_job(job_id)
    if not job:
        flash('Job not found.', 'error')
        return redirect(url_for('overview'))
    config = _load_config()
    threading.Thread(
        target=_run_upload_thread,
        args=(job_id, config),
        daemon=True
    ).start()
    flash(f'Job {job_id} approved — uploading in background.', 'success')
    return redirect(request.referrer or url_for('overview'))


@app.route('/jobs/<job_id>/reject', methods=['POST'])
def reject_job(job_id):
    job = get_job(job_id)
    if not job:
        flash('Job not found.', 'error')
        return redirect(url_for('overview'))
    _clear_job_outputs(job_id)
    # update_job_status clears error_module + error_message when passed None
    update_job_status(job_id, 'queued', error_module=None, error_message=None)
    flash(f'Job {job_id} rejected — output cleared, returned to queue.', 'warning')
    return redirect(request.referrer or url_for('overview'))


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


@app.route('/config', methods=['GET', 'POST'])
def config_editor():
    if request.method == 'POST':
        try:
            new_config_str = request.form.get('config_json', '').strip()
            if not new_config_str:
                flash('No config data received.', 'error')
                return redirect(url_for('config_editor'))
            new_config = json.loads(new_config_str)
            with open('config.json', 'w', encoding='utf-8') as f:
                json.dump(new_config, f, indent=2, ensure_ascii=False)
            logger.info("config.json updated via dashboard")
            flash('Config saved. Changes apply to the next job run.', 'success')
        except json.JSONDecodeError as exc:
            flash(f'Invalid JSON: {exc}', 'error')
        except Exception as exc:
            flash(f'Save failed: {exc}', 'error')
        return redirect(url_for('config_editor'))

    config = _load_config()
    return render_template('config_editor.html', config=config, active='config')


@app.route('/prompts', methods=['GET'])
def prompts_editor():
    script_prompt = ''
    meta_prompt   = ''
    try:
        script_prompt = Path('prompts/script_prompt.txt').read_text(encoding='utf-8')
    except Exception:
        pass
    try:
        meta_prompt = Path('prompts/metadata_prompt.txt').read_text(encoding='utf-8')
    except Exception:
        pass
    config = _load_config()
    return render_template('prompts.html',
                           script_prompt=script_prompt,
                           meta_prompt=meta_prompt,
                           config=config,
                           active='prompts')


@app.route('/prompts/script', methods=['POST'])
def save_script_prompt():
    content = request.form.get('content', '')
    Path('prompts/script_prompt.txt').write_text(content, encoding='utf-8')
    logger.info("script_prompt.txt updated via dashboard")
    flash('Script prompt saved.', 'success')
    return redirect(url_for('prompts_editor'))


@app.route('/prompts/metadata', methods=['POST'])
def save_metadata_prompt():
    content = request.form.get('content', '')
    Path('prompts/metadata_prompt.txt').write_text(content, encoding='utf-8')
    logger.info("metadata_prompt.txt updated via dashboard")
    flash('Metadata prompt saved.', 'success')
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
    key = os.getenv('ELEVENLABS_API_KEY', '')
    if not key:
        results['elevenlabs'] = {'status': 'no_key', 'label': 'ELEVENLABS_API_KEY not set', 'latency_ms': None}
    else:
        try:
            t0 = time.time()
            r = req.get('https://api.elevenlabs.io/v1/user/subscription',
                        headers={'xi-api-key': key}, timeout=8)
            ms = round((time.time() - t0) * 1000)
            if r.status_code == 200:
                data = r.json()
                remaining = data.get('character_limit', 0) - data.get('character_count', 0)
                results['elevenlabs'] = {
                    'status': 'ok',
                    'label': f'OK — {remaining:,} chars remaining',
                    'latency_ms': ms
                }
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
            results['leonardo'] = {
                'status': 'ok' if r.status_code == 200 else 'error',
                'label': f'OK — {ms} ms' if r.status_code == 200 else f'HTTP {r.status_code}',
                'latency_ms': ms if r.status_code == 200 else None,
            }
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
            SELECT j.id, j.topic, j.bucket,
                   COALESCE(SUM(a.views),0)  AS total_views,
                   COALESCE(SUM(a.likes),0)  AS total_likes,
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
        summary = pull_all_analytics()
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
        'active_alerts':  get_active_alerts(),
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

    topic  = alert.get('reframed_angle') or alert['topic']
    bucket = alert.get('bucket', 'elec')
    jid    = get_next_job_id()

    create_job(job_id=jid, topic=topic, bucket=bucket, hook_style='shocking_fact')
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
    from database import get_topics
    show_archived = request.args.get('archived', '0') == '1'
    bucket_filter = request.args.get('bucket', '')
    sort_by       = request.args.get('sort', 'added_at')   # added_at | final_score | status

    topics = get_topics(include_archived=show_archived)

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
    create_job(
        job_id=jid,
        topic=t['topic'],
        bucket=t.get('bucket') or 'elec',
        hook_style='shocking_fact',
    )
    flash(f'Job {jid} added to queue from topic bank.', 'success')
    return redirect(url_for('research_topics'))


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
