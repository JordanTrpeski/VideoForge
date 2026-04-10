"""
database.py
===========
SQLite schema definition and all database operations for VideoForge.

Input:  Job parameters, status updates, analytics data
Output: Persistent SQLite database at videoforge.db
Logs:   logs/database.log

Dependencies:
    - sqlite3 (stdlib)
    - os (stdlib)
    - datetime (stdlib)

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import sqlite3
import os
from datetime import datetime
from typing import Optional

# 3. Local modules
from utils.logger import setup_logger

logger = setup_logger('database')

DB_PATH = 'videoforge.db'


def get_connection() -> sqlite3.Connection:
    """
    Open and return a connection to the SQLite database.

    Returns:
        sqlite3.Connection: Database connection with row_factory set to
                            sqlite3.Row for dict-style column access.
    """
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
                id               TEXT PRIMARY KEY,
                topic            TEXT NOT NULL,
                bucket           TEXT,
                hook_style       TEXT,
                status           TEXT DEFAULT 'queued',
                error_module     TEXT,
                error_message    TEXT,
                script_path      TEXT,
                audio_path       TEXT,
                images_dir       TEXT,
                raw_video_path   TEXT,
                final_video_path TEXT,
                thumbnail_path   TEXT,
                metadata_path    TEXT,
                tiktok_url       TEXT,
                youtube_url      TEXT,
                tiktok_video_id  TEXT,
                youtube_video_id TEXT,
                duration_seconds REAL,
                word_count       INTEGER,
                created_at       TEXT DEFAULT (datetime('now')),
                updated_at       TEXT DEFAULT (datetime('now'))
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
        """)
        conn.commit()
        logger.info("Database schema ready")
    finally:
        conn.close()


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


if __name__ == '__main__':
    init_db()
    print("Database initialised successfully at", DB_PATH)
