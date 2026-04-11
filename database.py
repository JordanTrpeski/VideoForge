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
            CREATE TABLE IF NOT EXISTS jobs (
                id                  TEXT PRIMARY KEY,
                topic               TEXT NOT NULL,
                bucket              TEXT,
                hook_style          TEXT,
                status              TEXT DEFAULT 'queued',
                error_module        TEXT,
                error_message       TEXT,
                script_path         TEXT,
                audio_path          TEXT,
                images_dir          TEXT,
                raw_video_path      TEXT,
                final_video_path    TEXT,
                thumbnail_path      TEXT,
                metadata_path       TEXT,
                tiktok_url          TEXT,
                youtube_url         TEXT,
                tiktok_video_id     TEXT,
                youtube_video_id    TEXT,
                duration_seconds    REAL,
                word_count          INTEGER,
                similarity_checked  INTEGER DEFAULT 0,
                similar_to_job      TEXT,
                similarity_score    REAL,
                created_at          TEXT DEFAULT (datetime('now')),
                updated_at          TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS analytics (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id          TEXT REFERENCES jobs(id),
                platform        TEXT,
                views           INTEGER DEFAULT 0,
                likes           INTEGER DEFAULT 0,
                comments        INTEGER DEFAULT 0,
                shares          INTEGER DEFAULT 0,
                watch_time_avg  REAL,
                pulled_at       TEXT DEFAULT (datetime('now'))
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
    hook_style: Optional[str] = None
) -> None:
    """
    Insert a new job row with status='queued'.

    Args:
        job_id (str):     Unique identifier e.g. '001'.
        topic (str):      Video topic string.
        bucket (str):     Content bucket: elec / infra / vehicle / flaw.
        hook_style (str): Hook style: shocking_fact / wrong_assumption / nobody_talks.

    Returns:
        None
    """
    logger.info(f"[JOB {job_id}] Creating job — topic: '{topic}', bucket: {bucket}, hook: {hook_style}")
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO jobs (id, topic, bucket, hook_style, status)
               VALUES (?, ?, ?, ?, 'queued')""",
            (job_id, topic, bucket, hook_style)
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
        'duration_seconds', 'word_count', 'bucket', 'hook_style'
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


def get_all_jobs(status_filter: Optional[str] = None) -> list:
    """
    Fetch all job rows, optionally filtered by status.

    Args:
        status_filter (str): If provided, only return jobs with this status.

    Returns:
        list[dict]: List of job rows as dictionaries, newest first.
    """
    conn = get_connection()
    try:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC",
                (status_filter,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC"
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
    watch_time_avg: Optional[float] = None
) -> None:
    """
    Insert a new analytics snapshot for a job.

    Args:
        job_id (str):           Job identifier.
        platform (str):         'tiktok' or 'youtube'.
        views (int):            View count.
        likes (int):            Like count.
        comments (int):         Comment count.
        shares (int):           Share count.
        watch_time_avg (float): Average watch time in seconds.

    Returns:
        None
    """
    logger.debug(f"[JOB {job_id}] Inserting analytics — platform: {platform}, views: {views}")
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO analytics
               (job_id, platform, views, likes, comments, shares, watch_time_avg)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (job_id, platform, views, likes, comments, shares, watch_time_avg)
        )
        conn.commit()
    finally:
        conn.close()


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
) -> int:
    """
    Add a new topic to the topic_bank with status='pending'.

    Args:
        topic (str):           Topic text.
        bucket (str):          Content bucket.
        notes (str):           Free-text notes.
        hook_suggestion (str): Optional suggested hook.

    Returns:
        int: Rowid of the inserted topic.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO topic_bank (topic, bucket, notes, hook_suggestion, status)
               VALUES (?, ?, ?, ?, 'pending')""",
            (topic, bucket, notes, hook_suggestion),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_topics(include_archived: bool = False, limit: int = 500) -> list:
    """
    Fetch topics from the topic_bank.

    Args:
        include_archived (bool): If False, hide archived rows.
        limit (int):             Maximum rows to return.

    Returns:
        list[dict]: Topic rows.
    """
    conn = get_connection()
    try:
        if include_archived:
            rows = conn.execute(
                "SELECT * FROM topic_bank ORDER BY added_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM topic_bank WHERE archived = 0 ORDER BY added_at DESC LIMIT ?",
                (limit,),
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


if __name__ == '__main__':
    init_db()
    _run_migrations()
    print("Database initialised successfully at", DB_PATH)
