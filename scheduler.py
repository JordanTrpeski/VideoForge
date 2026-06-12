"""
scheduler.py
============
APScheduler-based automation for VideoForge.

Two recurring schedules:
  - Sunday 22:00 Europe/Skopje  → batch production run (N queued jobs)
  - Monday 06:00 Europe/Skopje  → analytics pull for all posted jobs

Also exposes run_batch() and run_pipeline_sync() for direct use by
main.py (CLI batch command) and app.py (background task triggering).

Input:  config.json (batch_size_per_week, timezone), jobs DB
Output: Pipeline stages executed per job; jobs reach 'review' status
Logs:   logs/scheduler.log

Dependencies:
    - apscheduler>=3.10.0 (scheduling)

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import json
import os
import signal
import sys
import time
from pathlib import Path

# 2. Third-party libraries
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

# Ensure project root is importable regardless of working directory
sys.path.insert(0, str(Path(__file__).parent))

# 3. Local modules
from utils.logger import setup_logger

logger = setup_logger('scheduler')

# Module-level scheduler instance — created once, shared with app.py
_scheduler: BackgroundScheduler | None = None


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """
    Load config.json from the project root.

    Returns:
        dict: Parsed configuration dictionary, or empty dict on failure.
    """
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as exc:
        logger.error(f"Failed to load config.json: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Synchronous pipeline runner
# ---------------------------------------------------------------------------

def run_pipeline_sync(job_id: str, config: dict) -> bool:
    """
    Run the full pipeline for a single job synchronously (blocking call).
    Stops at the review gate — does NOT upload. Each module is called in
    sequence; if any module returns success=False the pipeline stops and
    the job status is set to 'failed'.

    Args:
        job_id (str):  Job identifier e.g. '001'.
        config (dict): Loaded config.json contents.

    Returns:
        bool: True if all stages completed and job reached 'review' status.
              False if any stage failed.
    """
    from database import get_job, update_job_status

    STAGES = [
        'generate-script',
        'generate-voice',
        'generate-images',
        'assemble',
        'add-captions',
        'generate-metadata',
        'generate-thumbnail',
    ]

    logger.info(f"[JOB {job_id}] Pipeline starting (scheduler / batch run)")
    pipeline_start = time.time()

    for stage in STAGES:
        t0 = time.time()
        logger.info(f"[JOB {job_id}] Stage starting: {stage}")

        try:
            if stage == 'generate-script':
                from modules.script_engine import generate_script
                job = get_job(job_id)
                result = generate_script(
                    job_id=job_id,
                    topic=job['topic'],
                    config=config,
                    bucket=job.get('bucket', 'elec'),
                    hook_style=job.get('hook_style', 'shocking_fact'),
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

        except Exception as exc:
            elapsed = round(time.time() - t0, 1)
            logger.error(
                f"[JOB {job_id}] Stage {stage} raised exception after {elapsed}s: {exc}",
                exc_info=True,
            )
            update_job_status(job_id, 'failed', error_module=stage, error_message=str(exc))
            return False

        elapsed = round(time.time() - t0, 1)

        if result.get('skipped'):
            logger.warning(
                f"[JOB {job_id}] Stage {stage} SKIPPED in {elapsed}s "
                f"— {result.get('error', 'no reason given')}"
            )
            # Skipped stages (missing API key) are treated as non-fatal
            continue

        if result.get('success'):
            logger.info(f"[JOB {job_id}] Stage {stage} DONE in {elapsed}s")
            # Reddit-mode jobs pause at the hook-selection gate after the script
            # stage — the owner picks a hook in the dashboard before voice runs.
            if stage == 'generate-script':
                job = get_job(job_id)
                if job and job.get('mode') == 'reddit':
                    logger.info(
                        f"[JOB {job_id}] Reddit job — stopping at hook-selection gate "
                        "(status: script_done)"
                    )
                    return True
        else:
            err = result.get('error', 'unknown error')
            logger.error(f"[JOB {job_id}] Stage {stage} FAILED in {elapsed}s — {err}")
            # update_job_status is already called inside each module on failure,
            # but set it again here to be safe and capture the module name.
            update_job_status(job_id, 'failed', error_module=stage, error_message=err)
            return False

    total = round(time.time() - pipeline_start, 1)
    logger.info(
        f"[JOB {job_id}] Pipeline COMPLETE in {total}s — job status: review (awaiting approval)"
    )
    return True


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def run_batch(count: int | None = None) -> dict:
    """
    Pick the oldest N queued jobs and run the full pipeline for each in sequence.
    N comes from config.posting.batch_size_per_week unless overridden by count.

    Called by:
      - APScheduler every Sunday 22:00
      - main.py `batch --count N` CLI command

    Args:
        count (int | None): Number of jobs to process.
                            None → use config value.

    Returns:
        dict: {
            'total':     int,  # jobs attempted
            'succeeded': int,
            'failed':    int,
            'elapsed':   float,
        }
    """
    from database import init_db, get_all_jobs

    logger.info("=" * 60)
    logger.info("BATCH RUN STARTING")
    logger.info("=" * 60)

    init_db()
    config = _load_config()

    if count is None:
        count = config.get('posting', {}).get('batch_size_per_week', 5)

    logger.info(f"BATCH: Requested {count} jobs")

    # get_all_jobs returns newest-first; reverse for FIFO (oldest job first)
    all_queued = list(reversed(get_all_jobs(status_filter='queued')))
    to_run = all_queued[:count]

    if not to_run:
        logger.info("BATCH: No queued jobs to process — nothing to do")
        return {'total': 0, 'succeeded': 0, 'failed': 0, 'elapsed': 0.0}

    logger.info(
        f"BATCH: Processing {len(to_run)} job(s) "
        f"({len(all_queued)} total queued, taking oldest {count})"
    )

    batch_start = time.time()
    succeeded = 0
    failed = 0

    for i, job in enumerate(to_run, 1):
        job_id = job['id']
        logger.info(
            f"BATCH: [{i}/{len(to_run)}] Starting job {job_id} — '{job['topic']}'"
        )
        t0 = time.time()

        ok = run_pipeline_sync(job_id, config)
        elapsed = round(time.time() - t0, 1)

        if ok:
            succeeded += 1
            logger.info(
                f"BATCH: [{i}/{len(to_run)}] Job {job_id} DONE in {elapsed}s"
            )
        else:
            failed += 1
            logger.error(
                f"BATCH: [{i}/{len(to_run)}] Job {job_id} FAILED after {elapsed}s"
            )

    total_elapsed = round(time.time() - batch_start, 1)
    logger.info("=" * 60)
    logger.info(
        f"BATCH COMPLETE — {succeeded} succeeded, {failed} failed, "
        f"total time: {total_elapsed}s"
    )
    logger.info("=" * 60)

    return {
        'total':     len(to_run),
        'succeeded': succeeded,
        'failed':    failed,
        'elapsed':   total_elapsed,
    }


# ---------------------------------------------------------------------------
# Analytics trigger
# ---------------------------------------------------------------------------

def run_analytics() -> None:
    """
    Trigger analytics pull for all posted jobs.
    Called by the Monday 06:00 APScheduler job and /api/refresh-analytics.
    """
    logger.info("SCHEDULER: Analytics pull starting")
    try:
        from modules.analytics_engine import pull_all_analytics
        summary = pull_all_analytics()
        logger.info(
            f"SCHEDULER: Analytics pull done — "
            f"{summary.get('jobs_processed', 0)} jobs processed, "
            f"{summary.get('youtube_updated', 0)} YouTube, "
            f"{summary.get('tiktok_updated', 0)} TikTok updated"
        )
    except Exception as exc:
        logger.error(f"SCHEDULER: Analytics pull failed: {exc}", exc_info=True)


# ---------------------------------------------------------------------------
# Comment mining trigger (11.v2.C)
# ---------------------------------------------------------------------------

def run_comment_mining() -> None:
    """
    Trigger weekly YouTube comment mining.
    Pulls comments from posted videos, extracts topic suggestions, adds to
    topic_bank with notes='audience-requested'.
    Called Monday 09:00 by APScheduler.
    """
    logger.info("SCHEDULER: Comment mining starting")
    try:
        from modules.comment_miner import mine_comments
        config = _load_config()
        result = mine_comments(config)
        logger.info(
            f"SCHEDULER: Comment mining done — "
            f"scanned {result.get('videos_scanned', 0)} videos, "
            f"pulled {result.get('comments_pulled', 0)} comments, "
            f"added {result.get('topics_added', 0)} topics"
        )
    except Exception as exc:
        logger.error(f"SCHEDULER: Comment mining failed: {exc}", exc_info=True)


# ---------------------------------------------------------------------------
# Auto-fill weekly calendar (11.v2.D)
# ---------------------------------------------------------------------------

def run_auto_fill_calendar(n: int = 5) -> dict:
    """
    Select the top N scored topics from the topic_bank and queue them for
    the week's production batch. Runs Sunday 09:00, before the 22:00 batch.

    Balances across buckets — at most ceil(n/4) topics per bucket.
    Skips topics already in the queue or already used.
    Records the selection in the log so it appears in the dashboard.

    Args:
        n (int): Number of topics to queue (default: batch_size_per_week from config).

    Returns:
        dict: {queued: int, topics: list[str]}
    """
    logger.info("SCHEDULER: Auto-fill calendar starting")
    try:
        from database import init_db, get_topics, get_all_jobs, create_job, get_next_job_id
        import math

        init_db()
        config = _load_config()
        if n is None or n <= 0:
            n = config.get('posting', {}).get('batch_size_per_week', 5)

        # Topics already in the pipeline (queued, in-progress, review, etc.)
        active_topics = {
            j['topic'].lower().strip()
            for j in get_all_jobs()
            if j.get('status') not in ('posted', 'failed')
        }

        scored = [
            t for t in get_topics()
            if t.get('final_score') is not None
            and t.get('status') not in ('queued', 'used', 'archived')
            and t['topic'].lower().strip() not in active_topics
        ]

        if not scored:
            logger.info("SCHEDULER: Auto-fill — no scored topics available")
            return {'queued': 0, 'topics': []}

        # Sort by final_score desc
        scored.sort(key=lambda t: t.get('final_score', 0), reverse=True)

        # Balance across buckets (at most ceil(n/4) per bucket)
        per_bucket_limit = max(1, math.ceil(n / 4))
        bucket_counts: dict[str, int] = {}
        selected = []

        for t in scored:
            if len(selected) >= n:
                break
            bucket = t.get('bucket', 'elec')
            if bucket_counts.get(bucket, 0) >= per_bucket_limit:
                continue
            selected.append(t)
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1

        # If we haven't filled n slots, top up without bucket balancing
        if len(selected) < n:
            remaining = [t for t in scored if t not in selected]
            selected.extend(remaining[:n - len(selected)])

        queued_topics = []
        for t in selected:
            jid = get_next_job_id()
            create_job(
                job_id=jid,
                topic=t['topic'],
                bucket=t.get('bucket') or 'elec',
                hook_style=config.get('script', {}).get('hook_style', 'shocking_fact'),
            )
            queued_topics.append(t['topic'])
            logger.info(
                f"SCHEDULER: Auto-fill queued JOB {jid} — "
                f"'{t['topic']}' (score={t.get('final_score', 0):.1f}, bucket={t.get('bucket')})"
            )

        logger.info(
            f"SCHEDULER: Auto-fill calendar done — "
            f"{len(queued_topics)} topic(s) queued for this week"
        )
        return {'queued': len(queued_topics), 'topics': queued_topics}

    except Exception as exc:
        logger.error(f"SCHEDULER: Auto-fill calendar failed: {exc}", exc_info=True)
        return {'queued': 0, 'topics': [], 'error': str(exc)}


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------

def start_scheduler() -> BackgroundScheduler:
    """
    Start the APScheduler BackgroundScheduler with VideoForge job schedules.
    Safe to call multiple times — returns the running instance if already started.

    Schedules added:
      - weekly_batch     : Sunday 22:00 <timezone> → run_batch()
      - weekly_analytics : Monday 06:00 <timezone> → run_analytics()

    Returns:
        BackgroundScheduler: The live scheduler instance.
    """
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        logger.debug("Scheduler already running — returning existing instance")
        return _scheduler

    config   = _load_config()
    timezone = config.get('posting', {}).get('timezone', 'Europe/Skopje')

    try:
        _scheduler = BackgroundScheduler(timezone=timezone)
    except Exception:
        # Fallback if timezone string is unrecognised
        logger.warning(f"Unknown timezone '{timezone}' — falling back to UTC")
        _scheduler = BackgroundScheduler(timezone='UTC')

    # Sunday 22:00 — weekly production batch
    _scheduler.add_job(
        func=run_batch,
        trigger=CronTrigger(
            day_of_week='sun', hour=22, minute=0, timezone=timezone
        ),
        id='weekly_batch',
        name='Weekly batch production run (Sunday 22:00)',
        replace_existing=True,
        misfire_grace_time=3600,      # run even if app was offline at 22:00
        coalesce=True,                # collapse multiple missed fires into one
    )
    logger.info(f"Scheduled: weekly_batch — Sunday 22:00 {timezone}")

    # Monday 06:00 — analytics pull
    _scheduler.add_job(
        func=run_analytics,
        trigger=CronTrigger(
            day_of_week='mon', hour=6, minute=0, timezone=timezone
        ),
        id='weekly_analytics',
        name='Weekly analytics pull (Monday 06:00)',
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    logger.info(f"Scheduled: weekly_analytics — Monday 06:00 {timezone}")

    # Monday 09:00 — comment mining (11.v2.C)
    _scheduler.add_job(
        func=run_comment_mining,
        trigger=CronTrigger(
            day_of_week='mon', hour=9, minute=0, timezone=timezone
        ),
        id='weekly_comment_mining',
        name='Weekly comment mining (Monday 09:00)',
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    logger.info(f"Scheduled: weekly_comment_mining — Monday 09:00 {timezone}")

    # Sunday 09:00 — auto-fill weekly calendar (11.v2.D)
    _scheduler.add_job(
        func=run_auto_fill_calendar,
        trigger=CronTrigger(
            day_of_week='sun', hour=9, minute=0, timezone=timezone
        ),
        id='weekly_calendar_fill',
        name='Weekly calendar auto-fill (Sunday 09:00)',
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    logger.info(f"Scheduled: weekly_calendar_fill — Sunday 09:00 {timezone}")

    _scheduler.start()
    logger.info("APScheduler started successfully")

    return _scheduler


def stop_scheduler() -> None:
    """
    Shut down the scheduler gracefully if it is running.
    Called on process exit when running in standalone mode.
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")
        _scheduler = None


def get_scheduler_status() -> dict:
    """
    Return a snapshot of the scheduler state for the dashboard API.

    Returns:
        dict: {
            'running': bool,
            'jobs': list of {id, name, next_run_time}
        }
    """
    if _scheduler is None or not _scheduler.running:
        return {'running': False, 'jobs': []}

    jobs = []
    for j in _scheduler.get_jobs():
        nrt = j.next_run_time
        jobs.append({
            'id':            j.id,
            'name':          j.name,
            'next_run_time': nrt.isoformat() if nrt else None,
        })

    return {'running': True, 'jobs': jobs}


# ---------------------------------------------------------------------------
# Standalone entry point — python scheduler.py
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    print("VideoForge Scheduler — standalone mode")
    print("Schedules: calendar-fill Sunday 09:00, batch Sunday 22:00, "
          "analytics Monday 06:00, comment-mining Monday 09:00")
    print("Press Ctrl+C to stop.\n")

    scheduler = start_scheduler()

    for j in scheduler.get_jobs():
        print(f"  {j.id}: {j.name}")
        print(f"    next run: {j.next_run_time}\n")

    def _handle_signal(sig, frame):
        print("\nShutting down scheduler...")
        stop_scheduler()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Keep the main thread alive
    while True:
        time.sleep(60)
