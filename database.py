"""
database.py
===========
SQLite schema definition and all database operations for VideoForge.

Input:  Job parameters, status updates, analytics data
Output: Persistent SQLite database — path set by VIDEOFORGE_DB_PATH in .env,
        falling back to videoforge.db in the project root.
Logs:   logs/database.log

Device sync:
    Set VIDEOFORGE_DB_PATH to a Dropbox or Google Drive path on each machine.
    Both machines share the same database automatically.
    Leave blank to use the default local path.

Dependencies:
    - sqlite3 (stdlib)
    - os (stdlib)
    - datetime (stdlib)

Author: VideoForge
Version: 1.1
"""

# 1. Standard library
import sqlite3
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

# 2. Third-party libraries
from dotenv import load_dotenv

load_dotenv()

# 3. Local modules
from utils.logger import setup_logger

logger = setup_logger('database')


def _resolve_db_path() -> str:
    """
    Resolve the database file path.

    Reads VIDEOFORGE_DB_PATH from .env. If set, uses that path (enables
    Dropbox / Google Drive sync across devices). If blank or unset, falls
    back to videoforge.db in the project root.

    Returns:
        str: Absolute path to the SQLite database file.
    """
    env_path = os.getenv('VIDEOFORGE_DB_PATH', '').strip()
    if env_path:
        resolved = str(Path(env_path).expanduser().resolve())
        logger.debug(f"Database path from VIDEOFORGE_DB_PATH: {resolved}")
        return resolved
    default = str(Path('videoforge.db').resolve())
    logger.debug(f"Database path: default ({default})")
    return default


# Resolved once at import time — consistent within a process
DB_PATH: str = _resolve_db_path()


def get_connection() -> sqlite3.Connection:
    """
    Open and return a connection to the SQLite database.

    The path is determined once at import time by _resolve_db_path() using
    the VIDEOFORGE_DB_PATH environment variable (or the local default).

    Returns:
        sqlite3.Connection: Database connection with row_factory set to
                            sqlite3.Row for dict-style column access.
    """
    # Ensure the parent directory exists (important when VIDEOFORGE_DB_PATH
    # points to a Dropbox / Google Drive subfolder that hasn't been created yet)
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Create all tables if they do not already exist.
    Safe to call on every startup — uses CREATE TABLE IF NOT EXISTS.

    Returns:
        None
    """
    logger.info("Initialising database schema")
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.executescript("""
            -- Phase 12: channels registry
            CREATE TABLE IF NOT EXISTS channels (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                handle_yt   TEXT DEFAULT '',
                handle_tt   TEXT DEFAULT '',
                niche       TEXT DEFAULT '',
                format      TEXT DEFAULT 'single_narrator',
                active      INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            -- Seed the default channel if it doesn't already exist
            INSERT OR IGNORE INTO channels (id, name, handle_yt, handle_tt, niche, format)
            VALUES (
                'engineering_brief',
                'The Engineering Brief',
                '@HowThingsWorkEng',
                '@HowThingsWorkEng',
                'engineering',
                'single_narrator'
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id                    TEXT PRIMARY KEY,
                topic                 TEXT NOT NULL,
                bucket                TEXT,
                hook_style            TEXT,
                status                TEXT DEFAULT 'queued',
                error_module          TEXT,
                error_message         TEXT,
                script_path           TEXT,
                audio_path            TEXT,
                images_dir            TEXT,
                raw_video_path        TEXT,
                final_video_path      TEXT,
                thumbnail_path        TEXT,
                metadata_path         TEXT,
                tiktok_url            TEXT,
                youtube_url           TEXT,
                tiktok_video_id       TEXT,
                youtube_video_id      TEXT,
                duration_seconds      REAL,
                word_count            INTEGER,
                similarity_checked    INTEGER DEFAULT 0,
                similar_to_job        TEXT,
                similarity_score      REAL,
                picked_length_seconds INTEGER,
                picked_hook_style     TEXT,
                channel_id            TEXT DEFAULT 'engineering_brief',
                created_at            TEXT DEFAULT (datetime('now')),
                updated_at            TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS analytics (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id              TEXT REFERENCES jobs(id),
                platform            TEXT,
                views               INTEGER DEFAULT 0,
                likes               INTEGER DEFAULT 0,
                comments            INTEGER DEFAULT 0,
                shares              INTEGER DEFAULT 0,
                watch_time_avg      REAL,
                channel_id          TEXT DEFAULT 'engineering_brief',
                -- YouTube Analytics API v2 fields (Phase 13)
                avg_view_duration   REAL,       -- averageViewDuration (seconds)
                avg_view_percentage REAL,       -- averageViewPercentage (0-100)
                subscribers_gained  INTEGER,    -- subscribersGained
                impressions         INTEGER,    -- impressions (manual/CSV only — not public API)
                ctr                 REAL,       -- CTR as decimal 0.03 = 3% (manual/CSV only)
                data_source         TEXT DEFAULT 'api',  -- 'api' | 'manual' | 'csv'
                pulled_at           TEXT DEFAULT (datetime('now'))
            );

            -- Phase 11.v1.B: Priority Alert system
            CREATE TABLE IF NOT EXISTS trend_scans (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at      TEXT DEFAULT (datetime('now')),
                topics_found    INTEGER DEFAULT 0,
                new_alerts      INTEGER DEFAULT 0,
                buckets_scanned TEXT,
                status          TEXT DEFAULT 'complete'
            );

            CREATE TABLE IF NOT EXISTS priority_alerts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                topic           TEXT NOT NULL,
                bucket          TEXT,
                spike_percent   REAL,
                channel_fit     REAL,
                hook_suggestion TEXT,
                reframed_angle  TEXT,
                window_hours    INTEGER DEFAULT 48,
                triggered_at    TEXT DEFAULT (datetime('now')),
                expires_at      TEXT,
                status          TEXT DEFAULT 'active',
                job_id          TEXT,
                dismissed_at    TEXT
            );

            -- Phase 13 Block A: Content templates (per-channel variation pools)
            CREATE TABLE IF NOT EXISTS content_templates (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id           TEXT NOT NULL,
                name                 TEXT NOT NULL,
                visual_mode          TEXT DEFAULT 'images',
                length_min_seconds   INTEGER DEFAULT 55,
                length_max_seconds   INTEGER DEFAULT 90,
                hook_style_pool      TEXT DEFAULT '[]',  -- JSON array
                music_palette        TEXT DEFAULT '',
                thumbnail_mode       TEXT DEFAULT 'frame_capture',
                caption_mode         TEXT DEFAULT 'on',  -- 'on' | 'off' (Block C)
                prompt_overrides     TEXT DEFAULT '{}',  -- JSON object
                dual_output          INTEGER DEFAULT 0,
                active               INTEGER DEFAULT 1,
                created_at           TEXT DEFAULT (datetime('now')),
                updated_at           TEXT DEFAULT (datetime('now')),
                UNIQUE (channel_id, name)
            );

            -- Phase 13 Block B: Per-call API usage tracking
            CREATE TABLE IF NOT EXISTS api_usage (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id           TEXT,
                job_id               TEXT,
                provider             TEXT NOT NULL,
                operation            TEXT NOT NULL,
                units_used           INTEGER DEFAULT 0,
                cost_estimate_cents  INTEGER DEFAULT 0,
                timestamp            TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_api_usage_lookup
                ON api_usage (channel_id, provider, timestamp);

            CREATE TABLE IF NOT EXISTS api_usage_daily (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                day                  TEXT NOT NULL,            -- YYYY-MM-DD
                channel_id           TEXT,
                provider             TEXT,
                units_used           INTEGER DEFAULT 0,
                cost_estimate_cents  INTEGER DEFAULT 0,
                rolled_at            TEXT DEFAULT (datetime('now')),
                UNIQUE (day, channel_id, provider)
            );

            -- Phase 13 Block F: R2 cloud assets (preview URLs + lifecycle)
            CREATE TABLE IF NOT EXISTS r2_objects (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id          TEXT NOT NULL,
                channel_id      TEXT,
                kind            TEXT,                 -- 'video' | 'thumbnail'
                bucket          TEXT,
                key             TEXT NOT NULL,
                url             TEXT,
                size_bytes      INTEGER DEFAULT 0,
                uploaded_at     TEXT DEFAULT (datetime('now')),
                expires_at      TEXT,
                deleted         INTEGER DEFAULT 0,
                deleted_at      TEXT
            );

            -- Phase 11.v1.D: Topic bank
            CREATE TABLE IF NOT EXISTS topic_bank (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                topic           TEXT NOT NULL,
                bucket          TEXT,
                score           REAL,
                status          TEXT DEFAULT 'pending',
                hook_suggestion TEXT,
                notes           TEXT,
                archived        INTEGER DEFAULT 0,
                archived_at     TEXT,
                archive_reason  TEXT,
                channel_id      TEXT DEFAULT 'engineering_brief',
                added_at        TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now'))
            );
        """)
        conn.commit()
        logger.info("Database schema ready")
    finally:
        conn.close()

    _run_migrations()


def create_job(
    job_id: str,
    topic: str,
    bucket: Optional[str] = None,
    hook_style: Optional[str] = None,
    mode: str = 'standard',
    source: str = 'manual',
    source_selftext: Optional[str] = None,
    channel_id: str = 'engineering_brief',
) -> None:
    """
    Insert a new job row with status='queued'.

    Args:
        job_id (str):          Unique identifier e.g. '001'.
        topic (str):           Video topic string.
        bucket (str):          Content bucket: elec / infra / vehicle / flaw.
        hook_style (str):      Hook style: shocking_fact / wrong_assumption / nobody_talks.
        mode (str):            Content mode: 'standard' (engineering) or 'reddit' (story).
        source (str):          Provenance: 'manual' / 'reddit' / 'topic_bank' etc.
        source_selftext (str): Raw source story text the script engine rewrites
                               (Reddit mode only — None for standard jobs).
        channel_id (str):      Channel this job belongs to (default 'engineering_brief').

    Returns:
        None
    """
    logger.info(
        f"[JOB {job_id}] Creating job — topic: '{topic}', bucket: {bucket}, "
        f"hook: {hook_style}, mode: {mode}, source: {source}, channel: {channel_id}"
    )
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO jobs (id, topic, bucket, hook_style, status,
                                 mode, source, source_selftext, channel_id)
               VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
            (job_id, topic, bucket, hook_style, mode, source, source_selftext, channel_id)
        )
        conn.commit()
        logger.info(f"[JOB {job_id}] Job created successfully")
    finally:
        conn.close()


def update_job_status(
    job_id: str,
    status: str,
    error_module: Optional[str] = None,
    error_message: Optional[str] = None
) -> None:
    """
    Update the status field for a job row.

    Args:
        job_id (str):       Job identifier.
        status (str):       New status from the status flow defined in CLAUDE.md.
        error_module (str): Module name if status='failed'.
        error_message (str):Error text if status='failed'.

    Returns:
        None
    """
    logger.debug(f"[JOB {job_id}] Status -> {status}")
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE jobs
               SET status = ?, error_module = ?, error_message = ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (status, error_module, error_message, job_id)
        )
        conn.commit()
    finally:
        conn.close()


def update_job_field(job_id: str, field: str, value) -> None:
    """
    Update a single field on a job row.

    Args:
        job_id (str): Job identifier.
        field (str):  Column name to update.
        value:        New value for the column.

    Returns:
        None

    Raises:
        ValueError: If field name is not in the allowed column list (SQL injection guard).
    """
    allowed_fields = {
        'script_path', 'audio_path', 'images_dir', 'raw_video_path',
        'final_video_path', 'thumbnail_path', 'metadata_path',
        'tiktok_url', 'youtube_url', 'tiktok_video_id', 'youtube_video_id',
        'duration_seconds', 'word_count', 'bucket', 'hook_style',
        'mode', 'source', 'source_selftext', 'review_note',
        'picked_length_seconds', 'picked_hook_style', 'channel_id',
        # Reddit dual output
        'story_id', 'story_role', 'linked_job_id', 'scheduled_upload_at',
        # Compliance & odds pack
        'thumbnail_variant', 'disclosure_checklist_required', 'description_skeleton_index',
        # Reddit dedup — FIX 3
        'reddit_post_id',
        # Phase 13 — templates / cloud preview
        'template_id', 'template_name', 'preview_url', 'preview_thumb_url',
        'preview_uploaded_at', 'preview_deleted_at',
        # Phase 14 Block 6 — 48h review tracking
        'review_due_at', 'review_completed_at', 'iteration_note',
    }
    if field not in allowed_fields:
        raise ValueError(f"Field '{field}' is not an allowed job column")

    logger.debug(f"[JOB {job_id}] Field update — {field} = {value}")
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE jobs SET {field} = ?, updated_at = datetime('now') WHERE id = ?",
            (value, job_id)
        )
        conn.commit()
    finally:
        conn.close()


def get_job(job_id: str) -> Optional[dict]:
    """
    Fetch a single job row by ID.

    Args:
        job_id (str): Job identifier.

    Returns:
        dict: Job row as a dictionary, or None if not found.
    """
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_last_job_variation(
    exclude_job_id: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> tuple:
    """
    Return (picked_length_seconds, picked_hook_style) from the most recently
    created job that has both variation fields set.  Used by the variation
    picker to enforce the no-consecutive-identical-pair rule.

    Args:
        exclude_job_id (str): Exclude this job ID (the current job being created).
        channel_id (str):     If provided, restrict to jobs on this channel.

    Returns:
        tuple: (int|None, str|None) — (length_seconds, hook_style).
    """
    conn = get_connection()
    try:
        conditions = ["picked_length_seconds IS NOT NULL"]
        params: list = []
        if exclude_job_id:
            conditions.append("id != ?")
            params.append(exclude_job_id)
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        where = "WHERE " + " AND ".join(conditions)
        row = conn.execute(
            f"SELECT picked_length_seconds, picked_hook_style FROM jobs {where} "
            "ORDER BY created_at DESC LIMIT 1",
            params,
        ).fetchone()
        if row:
            return (row['picked_length_seconds'], row['picked_hook_style'])
        return (None, None)
    finally:
        conn.close()


def get_all_jobs(
    status_filter: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> list:
    """
    Fetch all job rows, optionally filtered by status and/or channel.

    Args:
        status_filter (str): If provided, only return jobs with this status.
        channel_id (str):    If provided, only return jobs for this channel.

    Returns:
        list[dict]: List of job rows as dictionaries, newest first.
    """
    conn = get_connection()
    try:
        conditions = []
        params: list = []
        if status_filter:
            conditions.append("status = ?")
            params.append(status_filter)
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC",
            params
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_next_job_id() -> str:
    """
    Calculate the next sequential 3-digit job ID.

    Returns:
        str: Next job ID as zero-padded string e.g. '001', '002'.
    """
    conn = get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) as cnt FROM jobs").fetchone()
        next_num = (row['cnt'] or 0) + 1
        return str(next_num).zfill(3)
    finally:
        conn.close()


def insert_analytics(
    job_id: str,
    platform: str,
    views: int = 0,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    watch_time_avg: Optional[float] = None,
    channel_id: str = 'engineering_brief',
    avg_view_duration: Optional[float] = None,
    avg_view_percentage: Optional[float] = None,
    subscribers_gained: Optional[int] = None,
    impressions: Optional[int] = None,
    ctr: Optional[float] = None,
    data_source: str = 'api',
) -> None:
    """
    Insert a new analytics snapshot for a job. Snapshots always INSERT — never
    overwrite history — so callers can accumulate time-series data safely.

    Args:
        job_id (str):                  Job identifier.
        platform (str):                'youtube' or 'tiktok'.
        views (int):                   View count.
        likes (int):                   Like count.
        comments (int):                Comment count.
        shares (int):                  Share count.
        watch_time_avg (float):        Average watch time in seconds (legacy field).
        channel_id (str):              Channel this row belongs to.
        avg_view_duration (float):     averageViewDuration in seconds (YouTube Analytics API v2).
        avg_view_percentage (float):   averageViewPercentage 0-100 (YouTube Analytics API v2).
        subscribers_gained (int):      subscribersGained (YouTube Analytics API v2).
        impressions (int):             Impression count (manual/CSV — not exposed by public API).
        ctr (float):                   CTR as decimal 0.03=3% (manual/CSV).
        data_source (str):             'api' | 'manual' | 'csv'.

    Returns:
        None
    """
    logger.debug(
        f"[JOB {job_id}] Inserting analytics — platform: {platform}, "
        f"views: {views}, source: {data_source}"
    )
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO analytics
               (job_id, platform, views, likes, comments, shares, watch_time_avg,
                channel_id, avg_view_duration, avg_view_percentage, subscribers_gained,
                impressions, ctr, data_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, platform, views, likes, comments, shares, watch_time_avg,
             channel_id, avg_view_duration, avg_view_percentage, subscribers_gained,
             impressions, ctr, data_source)
        )
        conn.commit()
    finally:
        conn.close()


def insert_manual_analytics(
    job_id: str,
    platform: str,
    impressions: Optional[int] = None,
    ctr: Optional[float] = None,
    avg_view_percentage: Optional[float] = None,
    avg_view_duration: Optional[float] = None,
    views: int = 0,
    likes: int = 0,
    channel_id: str = 'engineering_brief',
    data_source: str = 'manual',
) -> int:
    """
    Insert a manual analytics row (from the dashboard form or CSV import).
    Always appends a new snapshot row — never overwrites history.

    Args:
        job_id (str):                  Job identifier.
        platform (str):                'youtube' or 'tiktok'.
        impressions (int):             Impression count from YouTube Studio.
        ctr (float):                   CTR as decimal (0.03 = 3%).
        avg_view_percentage (float):   Retention % (0-100).
        avg_view_duration (float):     Average view duration in seconds.
        views (int):                   View count from manual entry.
        likes (int):                   Like count from manual entry.
        channel_id (str):              Channel this row belongs to.
        data_source (str):             'manual' or 'csv'.

    Returns:
        int: Rowid of the inserted row.
    """
    logger.info(
        f"[JOB {job_id}] Manual analytics entry — platform: {platform}, "
        f"impressions: {impressions}, ctr: {ctr}, retention: {avg_view_percentage}%, "
        f"source: {data_source}"
    )
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO analytics
               (job_id, platform, views, likes, comments, shares,
                channel_id, avg_view_duration, avg_view_percentage,
                impressions, ctr, data_source)
               VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)""",
            (job_id, platform, views, likes, channel_id,
             avg_view_duration, avg_view_percentage, impressions, ctr, data_source)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_latest_analytics_per_job(
    channel_id: Optional[str] = None,
    platform: str = 'youtube',
    status_filter: str = 'posted',
) -> list:
    """
    Return the most-recent analytics snapshot per posted job, optionally
    filtered to a specific channel.  Used by the kill-metrics engine and
    channel health card.

    Args:
        channel_id (str): Channel to filter to. None = all channels.
        platform (str):   Platform to query ('youtube' or 'tiktok').
        status_filter (str): Job status to include (default 'posted').

    Returns:
        list[dict]: One row per job with latest snapshot values plus job metadata.
    """
    conn = get_connection()
    try:
        params: list = [platform, status_filter]
        ch_clause = ""
        if channel_id:
            ch_clause = "AND j.channel_id = ?"
            params.append(channel_id)

        rows = conn.execute(
            f"""SELECT j.id AS job_id, j.topic, j.bucket, j.channel_id,
                       j.created_at AS job_created_at,
                       a.views, a.likes, a.comments,
                       a.avg_view_duration, a.avg_view_percentage,
                       a.subscribers_gained, a.impressions, a.ctr,
                       a.data_source, a.pulled_at
                FROM jobs j
                LEFT JOIN analytics a ON a.id = (
                    SELECT id FROM analytics
                    WHERE job_id = j.id AND platform = ?
                    ORDER BY pulled_at DESC LIMIT 1
                )
                WHERE j.status = ? {ch_clause}
                ORDER BY j.created_at ASC""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_analytics_for_job(job_id: str, platform: str = 'youtube') -> list:
    """
    Return all analytics snapshots for a single job, newest first.
    Used to show accumulation history and never-overwrite guarantee.

    Args:
        job_id (str):   Job identifier.
        platform (str): Platform filter.

    Returns:
        list[dict]: All snapshot rows for this job+platform.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM analytics
               WHERE job_id = ? AND platform = ?
               ORDER BY pulled_at DESC""",
            (job_id, platform),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 12 — Channel management helpers
# ---------------------------------------------------------------------------

def create_channel(
    slug: str,
    name: str,
    handle_yt: str = '',
    handle_tt: str = '',
    niche: str = '',
    fmt: str = 'single_narrator',
) -> bool:
    """
    Register a new channel in the channels table.

    Args:
        slug (str):       Short identifier used in file paths e.g. 'reddit_stories'.
        name (str):       Display name.
        handle_yt (str):  YouTube handle e.g. '@MyChannel'.
        handle_tt (str):  TikTok handle.
        niche (str):      Short niche description.
        fmt (str):        'single_narrator' or 'dialogue'.

    Returns:
        bool: True if created, False if a channel with this slug already existed.
    """
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM channels WHERE id = ?", (slug,)
        ).fetchone()
        if existing:
            logger.warning(f"Channel '{slug}' already exists — skipping create")
            return False
        conn.execute(
            """INSERT INTO channels (id, name, handle_yt, handle_tt, niche, format)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (slug, name, handle_yt, handle_tt, niche, fmt),
        )
        conn.commit()
        logger.info(f"Channel '{slug}' created — {name}")
        return True
    finally:
        conn.close()


def get_channels(active_only: bool = True) -> list:
    """
    Fetch all registered channels.

    Args:
        active_only (bool): If True, only return channels where active = 1.

    Returns:
        list[dict]: Channel rows.
    """
    conn = get_connection()
    try:
        if active_only:
            rows = conn.execute(
                "SELECT * FROM channels WHERE active = 1 ORDER BY created_at ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM channels ORDER BY created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_channel(slug: str) -> Optional[dict]:
    """
    Fetch a single channel row by its slug ID.

    Args:
        slug (str): Channel identifier.

    Returns:
        dict | None: Channel row, or None if not found.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM channels WHERE id = ?", (slug,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reddit dual output — story link helpers
# ---------------------------------------------------------------------------

def get_linked_job(job_id: str) -> Optional[dict]:
    """
    Return the partner job linked via linked_job_id (long→short or short→long).

    Args:
        job_id (str): Either the long or the short job ID.

    Returns:
        dict | None: The partner job row, or None if not linked or not found.
    """
    conn = get_connection()
    try:
        row = conn.execute("SELECT linked_job_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row or not row['linked_job_id']:
            return None
        linked = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (row['linked_job_id'],)
        ).fetchone()
        return dict(linked) if linked else None
    finally:
        conn.close()


def get_scheduled_upload_jobs() -> list:
    """
    Return jobs that have passed their scheduled upload time and are ready to upload.

    These are teaser (story_role='short') jobs approved alongside a long-form video
    but delayed by at least 24 hours to give the long video a head-start.

    Returns:
        list[dict]: Jobs with status='scheduled_upload' where scheduled_upload_at <= now.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE status = 'scheduled_upload'
               AND scheduled_upload_at IS NOT NULL
               AND scheduled_upload_at <= datetime('now')
               ORDER BY scheduled_upload_at ASC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Compliance & odds pack helpers
# ---------------------------------------------------------------------------

def get_last_description_skeleton_index(channel_id: str) -> int:
    """
    Return the description_skeleton_index used by the most recently completed
    job on this channel, or -1 if no jobs exist yet.

    Args:
        channel_id (str): Channel identifier.

    Returns:
        int: Last skeleton index (0, 1, or 2), or -1 if none.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT description_skeleton_index FROM jobs
               WHERE channel_id = ?
               AND description_skeleton_index >= 0
               ORDER BY created_at DESC LIMIT 1""",
            (channel_id,),
        ).fetchone()
        return row['description_skeleton_index'] if row else -1
    finally:
        conn.close()


def get_recent_youtube_titles(channel_id: str, limit: int = 10) -> list:
    """
    Return the youtube_title values from the most recent posted jobs on this
    channel, for title first-4-words uniqueness checking.

    Args:
        channel_id (str): Channel identifier.
        limit (int):      Number of recent titles to return.

    Returns:
        list[str]: Most recent youtube_title strings (may be empty).
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT youtube_url, id FROM jobs
               WHERE channel_id = ?
               AND status IN ('posted', 'review', 'uploading')
               ORDER BY created_at DESC LIMIT ?""",
            (channel_id, limit),
        ).fetchall()
        # Pull titles from metadata JSON files since youtube_title isn't in jobs table
        import json as _json
        from pathlib import Path as _Path
        titles = []
        for row in rows:
            meta_path = _Path(f'output/metadata/{row["id"]}.json')
            if meta_path.exists():
                try:
                    data = _json.loads(meta_path.read_text(encoding='utf-8'))
                    t = data.get('youtube_title', '')
                    if t:
                        titles.append(t)
                except Exception:
                    pass
        return titles
    finally:
        conn.close()


def get_archive_size_bytes(channel_id: str = None) -> dict:
    """
    Return archive folder sizes.

    Args:
        channel_id (str | None): If given, return size for that channel only;
                                 otherwise return total and per-channel breakdown.

    Returns:
        dict: {'total_bytes': int, 'channels': {channel_id: int}}
    """
    from pathlib import Path as _Path
    archive_root = _Path('archive')
    result = {'total_bytes': 0, 'channels': {}}

    if not archive_root.exists():
        return result

    for ch_dir in archive_root.iterdir():
        if not ch_dir.is_dir():
            continue
        if channel_id and ch_dir.name != channel_id:
            continue
        ch_bytes = sum(f.stat().st_size for f in ch_dir.rglob('*') if f.is_file())
        result['channels'][ch_dir.name] = ch_bytes
        result['total_bytes'] += ch_bytes

    return result


# ---------------------------------------------------------------------------
# Live migrations — add columns to existing databases safely
# ---------------------------------------------------------------------------

def _run_migrations() -> None:
    """
    Apply any ALTER TABLE migrations needed for Phase 11+ on databases that
    were created before these columns existed.  Each statement is wrapped in
    its own try/except so a column-already-exists error does not abort the rest.
    """
    migrations = [
        # 11.v1.C — similarity detection columns on jobs
        "ALTER TABLE jobs ADD COLUMN similarity_checked INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN similar_to_job TEXT",
        "ALTER TABLE jobs ADD COLUMN similarity_score REAL",
        # 11.v2.A — full scoring columns on topic_bank
        "ALTER TABLE topic_bank ADD COLUMN trend_score REAL",
        "ALTER TABLE topic_bank ADD COLUMN competition_score REAL",
        "ALTER TABLE topic_bank ADD COLUMN channel_fit_score REAL",
        "ALTER TABLE topic_bank ADD COLUMN performance_score REAL",
        "ALTER TABLE topic_bank ADD COLUMN final_score REAL",
        "ALTER TABLE topic_bank ADD COLUMN alt_angles TEXT",
        "ALTER TABLE topic_bank ADD COLUMN competition_level TEXT",
        "ALTER TABLE topic_bank ADD COLUMN scored_at TEXT",
        "ALTER TABLE topic_bank ADD COLUMN score_version INTEGER DEFAULT 0",
        # review gate — rejection notes
        "ALTER TABLE jobs ADD COLUMN review_note TEXT",
        # 11.v1.B v2 — enriched priority alert fields
        "ALTER TABLE priority_alerts ADD COLUMN why_trending TEXT",
        "ALTER TABLE priority_alerts ADD COLUMN why_relevant TEXT",
        "ALTER TABLE priority_alerts ADD COLUMN angle_options TEXT",
        "ALTER TABLE priority_alerts ADD COLUMN urgency TEXT DEFAULT 'medium'",
        # Reddit Stories — topic_bank provenance + raw story payload
        "ALTER TABLE topic_bank ADD COLUMN source TEXT DEFAULT 'manual'",
        "ALTER TABLE topic_bank ADD COLUMN reddit_id TEXT",
        "ALTER TABLE topic_bank ADD COLUMN selftext TEXT",
        "ALTER TABLE topic_bank ADD COLUMN upvotes INTEGER",
        "ALTER TABLE topic_bank ADD COLUMN num_comments INTEGER",
        "ALTER TABLE topic_bank ADD COLUMN permalink TEXT",
        # Reddit Stories — jobs carry content mode + the source story text so
        # the script engine can rewrite it. mode: 'standard' | 'reddit'
        "ALTER TABLE jobs ADD COLUMN mode TEXT DEFAULT 'standard'",
        "ALTER TABLE jobs ADD COLUMN source TEXT DEFAULT 'manual'",
        "ALTER TABLE jobs ADD COLUMN source_selftext TEXT",
        # Variation system — per-job randomly chosen length and hook style
        "ALTER TABLE jobs ADD COLUMN picked_length_seconds INTEGER",
        "ALTER TABLE jobs ADD COLUMN picked_hook_style TEXT",
        # Phase 12 — multi-channel: channel_id FK on all tables
        "ALTER TABLE jobs ADD COLUMN channel_id TEXT DEFAULT 'engineering_brief'",
        "ALTER TABLE analytics ADD COLUMN channel_id TEXT DEFAULT 'engineering_brief'",
        "ALTER TABLE topic_bank ADD COLUMN channel_id TEXT DEFAULT 'engineering_brief'",
        # Phase 13 — YouTube Analytics API v2 + manual CTR entry
        "ALTER TABLE analytics ADD COLUMN avg_view_duration REAL",
        "ALTER TABLE analytics ADD COLUMN avg_view_percentage REAL",
        "ALTER TABLE analytics ADD COLUMN subscribers_gained INTEGER",
        "ALTER TABLE analytics ADD COLUMN impressions INTEGER",
        "ALTER TABLE analytics ADD COLUMN ctr REAL",
        "ALTER TABLE analytics ADD COLUMN data_source TEXT DEFAULT 'api'",
        # Reddit dual output — story linking
        "ALTER TABLE jobs ADD COLUMN story_id TEXT",
        "ALTER TABLE jobs ADD COLUMN story_role TEXT",
        "ALTER TABLE jobs ADD COLUMN linked_job_id TEXT",
        "ALTER TABLE jobs ADD COLUMN scheduled_upload_at TEXT",
        # Compliance & odds pack
        "ALTER TABLE jobs ADD COLUMN thumbnail_variant INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN disclosure_checklist_required INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN description_skeleton_index INTEGER DEFAULT -1",
        # FIX 3 — track originating Reddit post ID on the job so dedup covers
        # posts that were approved+queued even if the topic_bank row is deleted
        "ALTER TABLE jobs ADD COLUMN reddit_post_id TEXT",
        # Phase 13 Block A — template chosen for this job
        "ALTER TABLE jobs ADD COLUMN template_id INTEGER",
        "ALTER TABLE jobs ADD COLUMN template_name TEXT",
        # Phase 13 Block F — cloud preview URLs and lifecycle timestamps
        "ALTER TABLE jobs ADD COLUMN preview_url TEXT",
        "ALTER TABLE jobs ADD COLUMN preview_thumb_url TEXT",
        "ALTER TABLE jobs ADD COLUMN preview_uploaded_at TEXT",
        "ALTER TABLE jobs ADD COLUMN preview_deleted_at TEXT",
        # Phase 14 Block 6 — 48h post-upload review tracking
        "ALTER TABLE jobs ADD COLUMN review_due_at TEXT",
        "ALTER TABLE jobs ADD COLUMN review_completed_at TEXT",
        "ALTER TABLE jobs ADD COLUMN iteration_note TEXT",
    ]
    conn = get_connection()
    try:
        for sql in migrations:
            try:
                conn.execute(sql)
                conn.commit()
            except Exception:
                pass   # column already exists — safe to ignore
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Priority Alert helpers (11.v1.B)
# ---------------------------------------------------------------------------

def insert_trend_scan(
    topics_found: int = 0,
    new_alerts: int = 0,
    buckets_scanned: str = '',
    status: str = 'complete',
) -> int:
    """
    Record a completed trend scan in trend_scans.

    Args:
        topics_found (int):    Number of trending topics examined.
        new_alerts (int):      Number of priority_alerts created.
        buckets_scanned (str): Comma-separated bucket names scanned.
        status (str):          'complete' or 'error'.

    Returns:
        int: Rowid of the inserted scan record.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO trend_scans (topics_found, new_alerts, buckets_scanned, status)
               VALUES (?, ?, ?, ?)""",
            (topics_found, new_alerts, buckets_scanned, status),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def insert_priority_alert(
    topic: str,
    bucket: str,
    spike_percent: float,
    channel_fit: float,
    hook_suggestion: str,
    reframed_angle: str,
    window_hours: int = 48,
    expires_at: str = '',
    why_trending: str = '',
    why_relevant: str = '',
    angle_options: str = '[]',
    urgency: str = 'medium',
) -> int:
    """
    Create a new priority alert record.

    Args:
        topic (str):            Original trending topic string.
        bucket (str):           Content bucket.
        spike_percent (float):  Percentage spike vs prior 30-day average.
        channel_fit (float):    Claude channel-fit score 1–10.
        hook_suggestion (str):  Claude-suggested hook line.
        reframed_angle (str):   Claude-suggested everyday engineering angle.
        window_hours (int):     Hours until this alert expires.
        expires_at (str):       ISO datetime string for expiry.
        why_trending (str):     1-2 sentences on what is causing the spike.
        why_relevant (str):     1 sentence on channel fit reason.
        angle_options (str):    JSON array of [{title, hook}, ...] angle options.
        urgency (str):          'high' | 'medium' | 'low'.

    Returns:
        int: Rowid of the inserted alert record.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO priority_alerts
               (topic, bucket, spike_percent, channel_fit, hook_suggestion,
                reframed_angle, window_hours, expires_at,
                why_trending, why_relevant, angle_options, urgency)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (topic, bucket, spike_percent, channel_fit, hook_suggestion,
             reframed_angle, window_hours, expires_at,
             why_trending, why_relevant, angle_options, urgency),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_active_alerts() -> list:
    """
    Fetch all non-expired, non-dismissed priority alerts ordered by channel_fit desc.

    Returns:
        list[dict]: Active alert rows.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM priority_alerts
               WHERE status = 'active'
                 AND (expires_at = '' OR expires_at > datetime('now'))
               ORDER BY channel_fit DESC, triggered_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_alerts(limit: int = 100) -> list:
    """
    Fetch all alerts (active, dismissed, expired) for history display.

    Args:
        limit (int): Maximum rows to return.

    Returns:
        list[dict]: Alert rows newest first.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM priority_alerts ORDER BY triggered_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def dismiss_alert(alert_id: int) -> None:
    """
    Mark a priority alert as dismissed.

    Args:
        alert_id (int): Alert primary key.
    """
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE priority_alerts
               SET status = 'dismissed', dismissed_at = datetime('now')
               WHERE id = ?""",
            (alert_id,),
        )
        conn.commit()
    finally:
        conn.close()


def link_alert_to_job(alert_id: int, job_id: str) -> None:
    """
    Record which job was created from a fast-tracked priority alert.

    Args:
        alert_id (int): Alert primary key.
        job_id (str):   Job identifier.
    """
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE priority_alerts SET job_id = ?, status = 'fast_tracked' WHERE id = ?",
            (job_id, alert_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_trend_scans(limit: int = 50) -> list:
    """
    Fetch recent trend scan history records.

    Args:
        limit (int): Maximum rows to return.

    Returns:
        list[dict]: Scan rows newest first.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM trend_scans ORDER BY scanned_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_last_scan_time() -> str | None:
    """
    Return the timestamp of the most recent trend scan, or None if no scans exist.

    Returns:
        str | None: ISO datetime string or None.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT scanned_at FROM trend_scans ORDER BY scanned_at DESC LIMIT 1"
        ).fetchone()
        return row['scanned_at'] if row else None
    finally:
        conn.close()


def count_scans_since(since_iso: str) -> int:
    """
    Count how many trend scans have occurred since a given datetime.

    Args:
        since_iso (str): ISO datetime string lower bound.

    Returns:
        int: Number of scans since that time.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM trend_scans WHERE scanned_at >= ?",
            (since_iso,),
        ).fetchone()
        return row['cnt'] or 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Topic bank helpers (11.v1.D)
# ---------------------------------------------------------------------------

def insert_topic(
    topic: str,
    bucket: str = '',
    notes: str = '',
    hook_suggestion: str = '',
    channel_id: str = 'engineering_brief',
) -> int:
    """
    Add a new topic to the topic_bank with status='pending'.

    Args:
        topic (str):           Topic text.
        bucket (str):          Content bucket.
        notes (str):           Free-text notes.
        hook_suggestion (str): Optional suggested hook.
        channel_id (str):      Channel this topic belongs to.

    Returns:
        int: Rowid of the inserted topic.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO topic_bank (topic, bucket, notes, hook_suggestion, status, channel_id)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (topic, bucket, notes, hook_suggestion, channel_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_topics(
    include_archived: bool = False,
    limit: int = 500,
    channel_id: Optional[str] = None,
) -> list:
    """
    Fetch topics from the topic_bank.

    Args:
        include_archived (bool): If False, hide archived rows.
        limit (int):             Maximum rows to return.
        channel_id (str):        If provided, filter to this channel.

    Returns:
        list[dict]: Topic rows.
    """
    conn = get_connection()
    try:
        conditions = []
        params: list = []
        if not include_archived:
            conditions.append("archived = 0")
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM topic_bank {where} ORDER BY added_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def archive_topic(topic_id: int, reason: str = '') -> None:
    """
    Mark a topic_bank entry as archived.

    Args:
        topic_id (int): Topic primary key.
        reason (str):   Optional reason for archiving.
    """
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE topic_bank
               SET archived = 1, archived_at = datetime('now'), archive_reason = ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (reason, topic_id),
        )
        conn.commit()
    finally:
        conn.close()


def unarchive_topic(topic_id: int) -> None:
    """
    Restore an archived topic_bank entry.

    Args:
        topic_id (int): Topic primary key.
    """
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE topic_bank
               SET archived = 0, archived_at = NULL, archive_reason = NULL,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (topic_id,),
        )
        conn.commit()
    finally:
        conn.close()


def delete_topic(topic_id: int) -> None:
    """
    Permanently delete a topic_bank entry.

    Args:
        topic_id (int): Topic primary key.
    """
    conn = get_connection()
    try:
        conn.execute("DELETE FROM topic_bank WHERE id = ?", (topic_id,))
        conn.commit()
    finally:
        conn.close()


def get_topic(topic_id: int) -> Optional[dict]:
    """
    Fetch a single topic_bank row by ID.

    Args:
        topic_id (int): Topic primary key.

    Returns:
        dict | None: Topic row as a dictionary, or None if not found.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM topic_bank WHERE id = ?", (topic_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_topic_status(topic_id: int, status: str) -> None:
    """
    Update only the status field of a topic_bank row.

    Args:
        topic_id (int): Topic primary key.
        status (str):   New status e.g. 'candidate' / 'queued' / 'used'.
    """
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE topic_bank SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, topic_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reddit Stories helpers
# ---------------------------------------------------------------------------

def insert_reddit_candidate(
    reddit_id: str,
    title: str,
    selftext: str,
    upvotes: int,
    num_comments: int,
    permalink: str,
    bucket: str = 'reddit',
) -> int:
    """
    Insert a Reddit post into topic_bank as a story candidate awaiting approval.

    The row is created with source='reddit' and status='candidate' so it stays
    out of the normal scored-topic flow until the owner approves it.

    Args:
        reddit_id (str):    Reddit post ID (e.g. '1abc2de') — used for dedupe.
        title (str):        Post title — becomes the topic text.
        selftext (str):     Full self-post body the script engine will rewrite.
        upvotes (int):      Post score at scan time.
        num_comments (int): Comment count at scan time.
        permalink (str):    Reddit permalink path.
        bucket (str):       Content bucket label (default 'reddit').

    Returns:
        int: Rowid of the inserted candidate.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO topic_bank
                   (topic, bucket, status, source, reddit_id, selftext,
                    upvotes, num_comments, permalink)
               VALUES (?, ?, 'candidate', 'reddit', ?, ?, ?, ?, ?)""",
            (title, bucket, reddit_id, selftext, upvotes, num_comments, permalink),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_existing_reddit_ids() -> set:
    """
    Return the set of Reddit post IDs already seen — either as topic_bank
    candidates or as approved pipeline jobs (via jobs.reddit_post_id).

    Covers the gap where a topic_bank row is deleted after approval: the job
    still carries the reddit_post_id so the post won't be re-surfaced.

    Returns:
        set[str]: Reddit post IDs to skip on the next scan.
    """
    conn = get_connection()
    try:
        tb_rows = conn.execute(
            "SELECT reddit_id FROM topic_bank WHERE reddit_id IS NOT NULL"
        ).fetchall()
        job_rows = conn.execute(
            "SELECT reddit_post_id FROM jobs WHERE reddit_post_id IS NOT NULL"
        ).fetchall()
        ids = {r['reddit_id'] for r in tb_rows if r['reddit_id']}
        ids |= {r['reddit_post_id'] for r in job_rows if r['reddit_post_id']}
        return ids
    finally:
        conn.close()


def get_reddit_candidates(include_all: bool = False, limit: int = 200) -> list:
    """
    Fetch Reddit-sourced topics.

    Args:
        include_all (bool): If False, only status='candidate' rows (awaiting
                            approval). If True, all reddit-sourced rows.
        limit (int):        Maximum rows to return.

    Returns:
        list[dict]: Reddit topic rows, newest first.
    """
    conn = get_connection()
    try:
        if include_all:
            rows = conn.execute(
                """SELECT * FROM topic_bank
                   WHERE source = 'reddit' AND archived = 0
                   ORDER BY added_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM topic_bank
                   WHERE source = 'reddit' AND status = 'candidate' AND archived = 0
                   ORDER BY upvotes DESC, added_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Topic scoring helpers (11.v2.A)
# ---------------------------------------------------------------------------

def update_topic_score(
    topic_id: int,
    trend_score: float,
    competition_score: float,
    channel_fit_score: float,
    performance_score: float,
    final_score: float,
    alt_angles: str = '',
    competition_level: str = '',
    score_version: int = 1,
) -> None:
    """
    Write scoring results back to a topic_bank row.

    Args:
        topic_id (int):           Primary key of the topic_bank row.
        trend_score (float):      0-10 Google Trends spike score.
        competition_score (float): 0-10 (inverted — low competition = high score).
        channel_fit_score (float): 0-10 Claude channel relevance score.
        performance_score (float): 0-10 channel analytics performance score.
        final_score (float):       0-10 weighted composite.
        alt_angles (str):          JSON-encoded list of 3 alternative angles.
        competition_level (str):   'low' / 'medium' / 'high'.
        score_version (int):       Monotonically increasing version counter.
    """
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE topic_bank
               SET trend_score       = ?,
                   competition_score = ?,
                   channel_fit_score = ?,
                   performance_score = ?,
                   final_score       = ?,
                   alt_angles        = ?,
                   competition_level = ?,
                   score_version     = ?,
                   scored_at         = datetime('now'),
                   status            = 'scored',
                   updated_at        = datetime('now')
               WHERE id = ?""",
            (trend_score, competition_score, channel_fit_score, performance_score,
             final_score, alt_angles, competition_level, score_version, topic_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_top_topics(limit: int = 20, bucket: str = '') -> list:
    """
    Return scored topics ordered by final_score descending.

    Args:
        limit (int):  Maximum rows to return.
        bucket (str): If set, filter to this content bucket.

    Returns:
        list[dict]: Topic rows with score fields populated.
    """
    conn = get_connection()
    try:
        if bucket:
            rows = conn.execute(
                """SELECT * FROM topic_bank
                   WHERE archived = 0 AND final_score IS NOT NULL AND bucket = ?
                   ORDER BY final_score DESC LIMIT ?""",
                (bucket, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM topic_bank
                   WHERE archived = 0 AND final_score IS NOT NULL
                   ORDER BY final_score DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_analytics_summary() -> dict:
    """
    Compute average views/likes/watch_time per bucket from the analytics table.
    Used by the scoring engine to weight topics by proven channel performance.

    Returns:
        dict: {bucket: {avg_views, avg_likes, avg_watch_time, count}}
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT j.bucket,
                      AVG(a.views)          AS avg_views,
                      AVG(a.likes)          AS avg_likes,
                      AVG(a.watch_time_avg) AS avg_watch_time,
                      COUNT(*)              AS cnt
               FROM analytics a
               JOIN jobs j ON a.job_id = j.id
               WHERE j.bucket IS NOT NULL
               GROUP BY j.bucket"""
        ).fetchall()
        return {r['bucket']: dict(r) for r in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 13 Block A — Content templates
# ---------------------------------------------------------------------------

def insert_template(
    channel_id: str,
    name: str,
    visual_mode: str = 'images',
    length_min_seconds: int = 55,
    length_max_seconds: int = 90,
    hook_style_pool: Optional[list] = None,
    music_palette: str = '',
    thumbnail_mode: str = 'frame_capture',
    caption_mode: str = 'on',
    prompt_overrides: Optional[dict] = None,
    dual_output: bool = False,
    active: bool = True,
) -> int:
    """
    Insert a new content_templates row. JSON fields are serialised to text.

    Args:
        channel_id (str):           Owning channel slug.
        name (str):                 Unique template name within the channel.
        visual_mode (str):          'images' | 'background_loop' | 'long_form_ambient'.
        length_min_seconds (int):   Lower bound for variation length pick.
        length_max_seconds (int):   Upper bound for variation length pick.
        hook_style_pool (list):     Allowed hook style strings.
        music_palette (str):        Free-form name pointing at a music asset folder.
        thumbnail_mode (str):       'frame_capture' | 'text_template' | 'off'.
        caption_mode (str):         'on' | 'off' (Block C: sleep content skips captions).
        prompt_overrides (dict):    Per-template prompt patches.
        dual_output (bool):         Whether to also produce a teaser short.
        active (bool):              Whether this template is in the rotation pool.

    Returns:
        int: New template id.
    """
    import json as _json
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO content_templates
                   (channel_id, name, visual_mode,
                    length_min_seconds, length_max_seconds,
                    hook_style_pool, music_palette, thumbnail_mode, caption_mode,
                    prompt_overrides, dual_output, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (channel_id, name, visual_mode,
             int(length_min_seconds), int(length_max_seconds),
             _json.dumps(hook_style_pool or []),
             music_palette, thumbnail_mode, caption_mode,
             _json.dumps(prompt_overrides or {}),
             1 if dual_output else 0,
             1 if active else 0),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_templates(channel_id: Optional[str] = None, active_only: bool = False) -> list:
    """
    Fetch content_templates rows, with JSON fields decoded.

    Args:
        channel_id (str | None): Filter to one channel, or None for all.
        active_only (bool):      If True, only return active=1 rows.

    Returns:
        list[dict]: Templates with hook_style_pool and prompt_overrides decoded.
    """
    import json as _json
    conn = get_connection()
    try:
        sql = "SELECT * FROM content_templates"
        conds, params = [], []
        if channel_id:
            conds.append("channel_id = ?"); params.append(channel_id)
        if active_only:
            conds.append("active = 1")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY channel_id, name"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for r in rows:
            try:
                r['hook_style_pool'] = _json.loads(r.get('hook_style_pool') or '[]')
            except Exception:
                r['hook_style_pool'] = []
            try:
                r['prompt_overrides'] = _json.loads(r.get('prompt_overrides') or '{}')
            except Exception:
                r['prompt_overrides'] = {}
            r['dual_output'] = bool(r.get('dual_output'))
            r['active'] = bool(r.get('active'))
        return rows
    finally:
        conn.close()


def get_template(template_id: int) -> Optional[dict]:
    """Fetch one template by id, with JSON decoded. Returns None if missing."""
    import json as _json
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM content_templates WHERE id = ?", (template_id,)
        ).fetchone()
        if not row:
            return None
        r = dict(row)
        try:
            r['hook_style_pool'] = _json.loads(r.get('hook_style_pool') or '[]')
        except Exception:
            r['hook_style_pool'] = []
        try:
            r['prompt_overrides'] = _json.loads(r.get('prompt_overrides') or '{}')
        except Exception:
            r['prompt_overrides'] = {}
        r['dual_output'] = bool(r.get('dual_output'))
        r['active'] = bool(r.get('active'))
        return r
    finally:
        conn.close()


def get_template_by_name(channel_id: str, name: str) -> Optional[dict]:
    """Lookup a template by (channel_id, name). Returns None if missing."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM content_templates WHERE channel_id = ? AND name = ?",
            (channel_id, name),
        ).fetchone()
    finally:
        conn.close()
    return get_template(row['id']) if row else None


def update_template(template_id: int, **fields) -> None:
    """
    Update arbitrary fields on a template row. JSON fields auto-encoded.

    Args:
        template_id (int): Row id.
        **fields:          Any of the column names from content_templates.
    """
    import json as _json
    allowed = {
        'name', 'visual_mode', 'length_min_seconds', 'length_max_seconds',
        'hook_style_pool', 'music_palette', 'thumbnail_mode', 'caption_mode',
        'prompt_overrides', 'dual_output', 'active',
    }
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ('hook_style_pool', 'prompt_overrides') and not isinstance(v, str):
            v = _json.dumps(v)
        if k in ('dual_output', 'active') and isinstance(v, bool):
            v = 1 if v else 0
        sets.append(f"{k} = ?"); params.append(v)
    if not sets:
        return
    sets.append("updated_at = datetime('now')")
    params.append(template_id)
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE content_templates SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()


def delete_template(template_id: int) -> None:
    """Permanently delete a template row."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM content_templates WHERE id = ?", (template_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 13 Block B — API usage tracking
# ---------------------------------------------------------------------------

def record_api_usage(
    provider: str,
    operation: str,
    units_used: int = 0,
    cost_estimate_cents: int = 0,
    channel_id: Optional[str] = None,
    job_id: Optional[str] = None,
) -> int:
    """
    Insert one row into api_usage for a single external API call.

    Cost is stored in integer cents so totals never drift on floating point.
    Pass cost_estimate_cents=0 for zero-cost providers (e.g. Kokoro local).

    Args:
        provider (str):           e.g. 'claude', 'elevenlabs', 'kokoro', 'leonardo'.
        operation (str):          Short label e.g. 'messages.create', 'tts'.
        units_used (int):         Provider-native units (tokens, chars, frames).
        cost_estimate_cents (int): Estimated USD cents.
        channel_id (str):         Owning channel slug, if known.
        job_id (str):             Owning job id, if known.

    Returns:
        int: Inserted row id.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO api_usage
                   (channel_id, job_id, provider, operation, units_used, cost_estimate_cents)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (channel_id, job_id, provider, operation, int(units_used or 0),
             int(cost_estimate_cents or 0)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_api_usage_summary(
    since_iso: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> list:
    """
    Aggregate api_usage rows by (channel_id, provider).

    Args:
        since_iso (str | None): If provided, only count rows from this UTC time.
        channel_id (str|None):  Optional channel filter.

    Returns:
        list[dict]: [{channel_id, provider, units_used, cost_estimate_cents, calls}]
    """
    conn = get_connection()
    try:
        sql = """
            SELECT channel_id, provider,
                   SUM(units_used)           AS units_used,
                   SUM(cost_estimate_cents)  AS cost_estimate_cents,
                   COUNT(*)                  AS calls
            FROM api_usage
        """
        conds, params = [], []
        if since_iso:
            conds.append("timestamp >= ?"); params.append(since_iso)
        if channel_id:
            conds.append("channel_id = ?"); params.append(channel_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " GROUP BY channel_id, provider ORDER BY channel_id, provider"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def rollup_api_usage_daily(day_iso: str) -> int:
    """
    Roll up all api_usage rows from `day_iso` into api_usage_daily and return
    the number of (channel, provider) buckets written.

    Args:
        day_iso (str): YYYY-MM-DD UTC day to roll up.

    Returns:
        int: Number of bucket rows upserted.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT channel_id, provider,
                      SUM(units_used)          AS units_used,
                      SUM(cost_estimate_cents) AS cost_estimate_cents
               FROM api_usage
               WHERE substr(timestamp, 1, 10) = ?
               GROUP BY channel_id, provider""",
            (day_iso,),
        ).fetchall()
        for r in rows:
            conn.execute(
                """INSERT INTO api_usage_daily
                       (day, channel_id, provider, units_used, cost_estimate_cents)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(day, channel_id, provider) DO UPDATE SET
                       units_used          = excluded.units_used,
                       cost_estimate_cents = excluded.cost_estimate_cents,
                       rolled_at           = datetime('now')""",
                (day_iso, r['channel_id'], r['provider'],
                 int(r['units_used'] or 0), int(r['cost_estimate_cents'] or 0)),
            )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 13 Block F/G — R2 object tracking
# ---------------------------------------------------------------------------

def insert_r2_object(
    job_id: str,
    kind: str,
    bucket: str,
    key: str,
    url: str,
    size_bytes: int = 0,
    channel_id: Optional[str] = None,
    expires_at: Optional[str] = None,
) -> int:
    """
    Record a successful R2 upload so the nightly retention job can clean it up.

    Args:
        job_id (str):     Owning job id.
        kind (str):       'video' | 'thumbnail'.
        bucket (str):     R2 bucket name.
        key (str):        Object key.
        url (str):        Stored URL returned to the dashboard.
        size_bytes (int): Size as reported by R2 (best-effort).
        channel_id (str): Owning channel.
        expires_at (str): UTC ISO datetime when this object becomes eligible
                          for deletion (used by retention sweep).

    Returns:
        int: Inserted row id.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO r2_objects
                   (job_id, channel_id, kind, bucket, key, url, size_bytes, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, channel_id, kind, bucket, key, url, int(size_bytes or 0), expires_at),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_active_r2_objects(channel_id: Optional[str] = None) -> list:
    """Return r2_objects rows where deleted=0, optionally filtered by channel."""
    conn = get_connection()
    try:
        sql = "SELECT * FROM r2_objects WHERE deleted = 0"
        params = []
        if channel_id:
            sql += " AND channel_id = ?"; params.append(channel_id)
        sql += " ORDER BY uploaded_at DESC"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def get_expired_r2_objects(now_iso: str) -> list:
    """
    Return r2_objects rows whose expires_at <= now_iso and not yet deleted.

    Args:
        now_iso (str): Current UTC ISO datetime string.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM r2_objects
               WHERE deleted = 0
                 AND expires_at IS NOT NULL
                 AND expires_at <= ?
               ORDER BY expires_at""",
            (now_iso,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_r2_deleted(r2_id: int) -> None:
    """Mark an r2_objects row as deleted."""
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE r2_objects
               SET deleted = 1, deleted_at = datetime('now')
               WHERE id = ?""",
            (r2_id,),
        )
        conn.commit()
    finally:
        conn.close()


def get_r2_storage_by_channel() -> dict:
    """
    Return per-channel R2 bytes-in-use and object counts.

    Returns:
        dict: {channel_id: {'bytes': int, 'count': int, 'next_expiry': str|None}}
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT channel_id,
                      SUM(size_bytes)  AS bytes,
                      COUNT(*)         AS count,
                      MIN(expires_at)  AS next_expiry
               FROM r2_objects
               WHERE deleted = 0
               GROUP BY channel_id"""
        ).fetchall()
        return {
            (r['channel_id'] or 'unknown'): {
                'bytes':       int(r['bytes'] or 0),
                'count':       int(r['count'] or 0),
                'next_expiry': r['next_expiry'],
            }
            for r in rows
        }
    finally:
        conn.close()


def set_r2_expiry(r2_id: int, expires_at: str) -> None:
    """Override an r2_objects row's expires_at (used for post-YouTube override)."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE r2_objects SET expires_at = ? WHERE id = ?",
            (expires_at, r2_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_r2_objects_for_job(job_id: str) -> list:
    """Return all r2_objects rows tied to a job (any state)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM r2_objects WHERE job_id = ? ORDER BY uploaded_at DESC",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 14 Block 6 — Post-upload review tracking
# ---------------------------------------------------------------------------

def set_review_due_at(job_id: str, when_iso: str) -> None:
    """Stamp jobs.review_due_at on a row (called on successful upload)."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE jobs SET review_due_at = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (when_iso, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_review_completed(job_id: str, iteration_note: str = '') -> None:
    """
    Set review_completed_at = now and store the optional iteration_note.
    """
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE jobs
                  SET review_completed_at = datetime('now'),
                      iteration_note = ?,
                      updated_at = datetime('now')
                WHERE id = ?""",
            (iteration_note or '', job_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_reviews_due(channel_id: Optional[str] = None) -> list:
    """
    Return jobs where review_due_at <= now and review_completed_at IS NULL.

    Args:
        channel_id (str): Optional channel filter.

    Returns:
        list[dict]: Job rows, oldest review_due_at first.
    """
    conn = get_connection()
    try:
        params: list = []
        ch_clause = ""
        if channel_id:
            ch_clause = " AND channel_id = ?"
            params.append(channel_id)
        rows = conn.execute(
            f"""SELECT * FROM jobs
                  WHERE review_due_at IS NOT NULL
                    AND review_due_at <= datetime('now')
                    AND review_completed_at IS NULL
                    {ch_clause}
               ORDER BY review_due_at ASC""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


if __name__ == '__main__':
    init_db()
    _run_migrations()
    print("Database initialised successfully at", DB_PATH)
