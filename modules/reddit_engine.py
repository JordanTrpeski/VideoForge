"""
reddit_engine.py
================
Reddit Stories content source for VideoForge.

Scans configured subreddits for the top text posts of the week using PRAW and
inserts the strongest candidates into the topic bank with source='reddit' and
status='candidate'. Candidates stay out of the normal pipeline until the owner
approves them in the dashboard (see app.py /research/topics).

Like the Phase 3 (voice) and Phase 4 (image) engines, this module detects
missing API keys and skips gracefully instead of failing — so the rest of the
system keeps working before Reddit credentials are added.

Input:  subreddit list, min upvotes, post limit (from CLI / config)
Output: topic_bank rows (source='reddit', status='candidate')
Logs:   logs/reddit_engine.log

Required .env keys (skipped gracefully if any are missing):
    REDDIT_CLIENT_ID
    REDDIT_CLIENT_SECRET
    REDDIT_USER_AGENT

Dependencies:
    - praw (Reddit API wrapper)
    - python-dotenv (env loading)

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import os
import time
from pathlib import Path

# 2. Third-party libraries
from dotenv import load_dotenv

# 3. Local modules
from database import (
    get_existing_reddit_ids,
    insert_reddit_candidate,
)
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger('reddit_engine')

# Self-post body length bounds — too short = thin story, too long = won't fit a
# 60-90s rewrite. Read as defaults; callers may override via config later.
MIN_SELFTEXT_CHARS = 1500
MAX_SELFTEXT_CHARS = 6000


def _get_reddit_client():
    """
    Build an authenticated read-only PRAW Reddit client from .env credentials.

    Returns:
        praw.Reddit | None: Authenticated client, or None if praw is not
                            installed or credentials are missing.
    """
    client_id     = os.getenv('REDDIT_CLIENT_ID', '').strip()
    client_secret = os.getenv('REDDIT_CLIENT_SECRET', '').strip()
    user_agent    = os.getenv('REDDIT_USER_AGENT', '').strip()

    if not client_id or not client_secret or not user_agent:
        return None

    try:
        import praw
    except ImportError:
        logger.error(
            "reddit_engine: praw not installed. Run: pip install praw"
        )
        return None

    try:
        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=user_agent,
        )
        # Read-only mode — we never post or vote
        reddit.read_only = True
        return reddit
    except Exception as exc:
        logger.error(f"reddit_engine: Failed to build PRAW client: {exc}", exc_info=True)
        return None


def _is_eligible(post, min_upvotes: int, seen_ids: set) -> tuple[bool, str]:
    """
    Decide whether a Reddit submission qualifies as a story candidate.

    Filters applied:
      - text (self) posts only
      - selftext length between MIN_SELFTEXT_CHARS and MAX_SELFTEXT_CHARS
      - score >= min_upvotes
      - not pinned / stickied
      - not removed or deleted by mods/author
      - not already captured in the topic bank

    Args:
        post:             PRAW Submission object.
        min_upvotes (int): Minimum score to accept.
        seen_ids (set):   Reddit post IDs already in the DB plus this run.

    Returns:
        tuple[bool, str]: (eligible, reason_if_rejected)
    """
    if post.id in seen_ids:
        return False, 'already captured'

    if not getattr(post, 'is_self', False):
        return False, 'not a text post'

    if getattr(post, 'stickied', False) or getattr(post, 'pinned', False):
        return False, 'pinned/stickied'

    # Removed or deleted posts expose sentinel bodies or removal metadata
    selftext = (getattr(post, 'selftext', '') or '').strip()
    if selftext in ('[removed]', '[deleted]', ''):
        return False, 'removed/deleted/empty'
    if getattr(post, 'removed_by_category', None):
        return False, 'removed by mods'

    if (post.score or 0) < min_upvotes:
        return False, f'below min upvotes ({post.score} < {min_upvotes})'

    length = len(selftext)
    if length < MIN_SELFTEXT_CHARS:
        return False, f'too short ({length} < {MIN_SELFTEXT_CHARS} chars)'
    if length > MAX_SELFTEXT_CHARS:
        return False, f'too long ({length} > {MAX_SELFTEXT_CHARS} chars)'

    return True, ''


def scan_reddit(
    subs: list[str],
    min_upvotes: int = 2000,
    limit: int = 25,
) -> dict:
    """
    Scan subreddits for top weekly text posts and store candidates.

    For each subreddit, pulls the top posts of the past week, applies the
    eligibility filters, and inserts new candidates into the topic bank with
    source='reddit' and status='candidate'.

    Args:
        subs (list[str]):  Subreddit names without the r/ prefix.
        min_upvotes (int): Minimum post score to accept.
        limit (int):       Max posts to request per subreddit.

    Returns:
        dict: {
            'success':      bool,
            'skipped':      bool,   # True if Reddit credentials are not set
            'error':        str,    # populated on skip or failure
            'subs_scanned': list[str],
            'examined':     int,    # posts looked at
            'added':        int,    # new candidates inserted
            'candidates':   list[dict],  # {id, title, upvotes, subreddit}
        }
    """
    stage_start = time.time()
    logger.info(
        f"reddit_engine: Starting scan — subs: {subs}, "
        f"min_upvotes: {min_upvotes}, limit: {limit}"
    )

    # ------------------------------------------------------------------ #
    # Guard: Reddit credentials must be present                           #
    # ------------------------------------------------------------------ #
    reddit = _get_reddit_client()
    if reddit is None:
        msg = (
            "Reddit credentials are not set in .env "
            "(REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USER_AGENT), "
            "or praw is not installed. Skipping Reddit scan."
        )
        logger.warning(f"reddit_engine: {msg}")
        return {
            'success': False, 'skipped': True, 'error': msg,
            'subs_scanned': [], 'examined': 0, 'added': 0, 'candidates': [],
        }

    # Dedupe against what's already in the DB; extend within this run too so a
    # cross-posted story isn't inserted twice.
    seen_ids = set(get_existing_reddit_ids())

    examined = 0
    added = 0
    candidates: list[dict] = []
    subs_scanned: list[str] = []

    try:
        for sub_name in subs:
            sub_name = sub_name.strip().lstrip('r/').strip()
            if not sub_name:
                continue
            subs_scanned.append(sub_name)

            logger.info(f"reddit_engine: Scanning r/{sub_name} — top of week, limit {limit}")
            t0 = time.time()

            try:
                submissions = reddit.subreddit(sub_name).top(
                    time_filter='week', limit=limit
                )
                posts = list(submissions)
            except Exception as exc:
                logger.error(
                    f"reddit_engine: Failed to fetch r/{sub_name}: {exc}",
                    exc_info=True,
                )
                continue

            elapsed = round(time.time() - t0, 2)
            logger.info(
                f"reddit_engine: r/{sub_name} returned {len(posts)} posts "
                f"in {elapsed:.2f}s"
            )

            for post in posts:
                examined += 1
                eligible, reason = _is_eligible(post, min_upvotes, seen_ids)
                if not eligible:
                    logger.debug(
                        f"reddit_engine: skip {post.id} (r/{sub_name}) — {reason}"
                    )
                    continue

                selftext = post.selftext.strip()
                permalink = f"https://www.reddit.com{post.permalink}"

                topic_id = insert_reddit_candidate(
                    reddit_id=post.id,
                    title=post.title.strip(),
                    selftext=selftext,
                    upvotes=int(post.score or 0),
                    num_comments=int(getattr(post, 'num_comments', 0) or 0),
                    permalink=permalink,
                    bucket='reddit',
                )
                seen_ids.add(post.id)
                added += 1
                candidates.append({
                    'topic_id':  topic_id,
                    'reddit_id': post.id,
                    'title':     post.title.strip(),
                    'upvotes':   int(post.score or 0),
                    'subreddit': sub_name,
                    'chars':     len(selftext),
                })
                logger.info(
                    f"reddit_engine: CANDIDATE added — r/{sub_name} "
                    f"[{post.score} up] '{post.title[:70]}' "
                    f"({len(selftext)} chars, topic_id={topic_id})"
                )

        elapsed = round(time.time() - stage_start, 1)
        logger.info(
            f"reddit_engine: Scan complete — examined {examined} post(s), "
            f"added {added} candidate(s) across {len(subs_scanned)} sub(s), "
            f"{elapsed}s elapsed"
        )

        return {
            'success': True, 'skipped': False, 'error': '',
            'subs_scanned': subs_scanned,
            'examined': examined,
            'added': added,
            'candidates': candidates,
        }

    except Exception as exc:
        logger.error(f"reddit_engine: Scan failed: {exc}", exc_info=True)
        return {
            'success': False, 'skipped': False, 'error': str(exc),
            'subs_scanned': subs_scanned, 'examined': examined,
            'added': added, 'candidates': candidates,
        }
