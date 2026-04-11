"""
comment_miner.py
================
Phase 11.v2.C — YouTube comment mining for VideoForge.

Pulls comments from the channel's posted YouTube videos weekly, sends batches
to Claude to identify audience questions and topic suggestions, then adds
flagged suggestions to the topic_bank with status='pending' and a note
indicating they are audience-requested.

Input:  config dict
Output: topic_bank rows with notes='audience-requested'

Logs:   logs/comment_miner.log

Dependencies:
    - google-api-python-client (YouTube Data API v3)
    - anthropic (Claude API)

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import json
import os
import sys
import time
from pathlib import Path

# 2. Third-party libraries
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

# 3. Local modules
from utils.logger import setup_logger

logger = setup_logger('comment_miner')

# Maximum comments to fetch per video
MAX_COMMENTS_PER_VIDEO = 50

# Batch size sent to Claude for analysis
CLAUDE_BATCH_SIZE = 30


def mine_comments(config: dict) -> dict:
    """
    Pull recent comments from all posted YouTube videos and extract topic ideas.

    Skips gracefully if YouTube credentials or Claude API key are missing.

    Args:
        config (dict): Loaded config.json.

    Returns:
        dict: {
            'success':         bool,
            'videos_scanned':  int,
            'comments_pulled': int,
            'topics_added':    int,
            'topics':          list[str],
        }
    """
    logger.info("[COMMENT_MINER] Starting comment mining run")
    t0 = time.time()

    yt = _build_youtube_client()
    if yt is None:
        return {'success': False, 'error': 'YouTube client unavailable',
                'videos_scanned': 0, 'comments_pulled': 0, 'topics_added': 0, 'topics': []}

    api_key = os.getenv('ANTHROPIC_API_KEY', '').strip()
    if not api_key:
        logger.warning("[COMMENT_MINER] ANTHROPIC_API_KEY not set — skipping")
        return {'success': False, 'error': 'ANTHROPIC_API_KEY not set',
                'videos_scanned': 0, 'comments_pulled': 0, 'topics_added': 0, 'topics': []}

    # Fetch posted job video IDs from DB
    from database import get_all_jobs
    posted_jobs = [j for j in get_all_jobs() if j.get('youtube_video_id')]

    if not posted_jobs:
        logger.info("[COMMENT_MINER] No posted videos with YouTube IDs — nothing to mine")
        return {'success': True, 'videos_scanned': 0, 'comments_pulled': 0,
                'topics_added': 0, 'topics': []}

    all_comments: list[dict] = []
    videos_scanned = 0

    for job in posted_jobs[:20]:   # cap at 20 videos per run
        vid_id = job['youtube_video_id']
        comments = _fetch_comments(yt, vid_id, job.get('topic', ''))
        if comments:
            all_comments.extend(comments)
            videos_scanned += 1
        time.sleep(0.5)   # gentle rate limit

    logger.info(
        f"[COMMENT_MINER] Scanned {videos_scanned} videos, "
        f"pulled {len(all_comments)} comments"
    )

    if not all_comments:
        return {'success': True, 'videos_scanned': videos_scanned,
                'comments_pulled': 0, 'topics_added': 0, 'topics': []}

    # Process in batches
    new_topics: list[str] = []
    for i in range(0, len(all_comments), CLAUDE_BATCH_SIZE):
        batch = all_comments[i:i + CLAUDE_BATCH_SIZE]
        suggestions = _analyze_comments(batch, config)
        new_topics.extend(suggestions)
        if i + CLAUDE_BATCH_SIZE < len(all_comments):
            time.sleep(1)

    # Deduplicate and save to topic_bank
    topics_added = _save_topics(new_topics, config)

    elapsed = round(time.time() - t0, 2)
    logger.info(
        f"[COMMENT_MINER] Done in {elapsed}s — "
        f"{topics_added} topics added from {len(all_comments)} comments"
    )

    return {
        'success':         True,
        'videos_scanned':  videos_scanned,
        'comments_pulled': len(all_comments),
        'topics_added':    topics_added,
        'topics':          new_topics[:topics_added],
    }


# ---------------------------------------------------------------------------
# YouTube helpers
# ---------------------------------------------------------------------------

def _build_youtube_client():
    """
    Build an authenticated YouTube Data API v3 client.

    Returns:
        googleapiclient.discovery.Resource or None if credentials missing.
    """
    token_path = Path('token.json')
    secrets_path = Path(
        os.getenv('YOUTUBE_CLIENT_SECRETS_FILE', 'client_secrets.json')
    )

    if not token_path.exists() or not secrets_path.exists():
        logger.warning(
            "[COMMENT_MINER] YouTube credentials not found "
            f"(token.json={token_path.exists()}, "
            f"client_secrets.json={secrets_path.exists()}) — skipping"
        )
        return None

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(str(token_path))
        return build('youtube', 'v3', credentials=creds, cache_discovery=False)
    except Exception as exc:
        logger.error(f"[COMMENT_MINER] YouTube client build failed: {exc}")
        return None


def _fetch_comments(yt, video_id: str, video_topic: str) -> list[dict]:
    """
    Fetch top-level comments for a YouTube video.

    Args:
        yt:           Authenticated YouTube API client.
        video_id:     YouTube video ID string.
        video_topic:  Topic string for logging context.

    Returns:
        list[dict]: Each entry has 'text', 'likes', 'video_topic'.
    """
    try:
        response = yt.commentThreads().list(
            part='snippet',
            videoId=video_id,
            maxResults=MAX_COMMENTS_PER_VIDEO,
            order='relevance',
            textFormat='plainText',
        ).execute()

        comments = []
        for item in response.get('items', []):
            snippet = item.get('snippet', {}).get('topLevelComment', {}).get('snippet', {})
            text = snippet.get('textDisplay', '').strip()
            likes = snippet.get('likeCount', 0)
            if text and len(text) > 10:
                comments.append({
                    'text':        text[:300],   # truncate very long comments
                    'likes':       likes,
                    'video_topic': video_topic,
                })

        logger.debug(
            f"[COMMENT_MINER] {video_id} '{video_topic[:40]}': "
            f"{len(comments)} comments fetched"
        )
        return comments

    except Exception as exc:
        logger.warning(f"[COMMENT_MINER] Failed to fetch comments for {video_id}: {exc}")
        return []


# ---------------------------------------------------------------------------
# Claude analysis
# ---------------------------------------------------------------------------

def _analyze_comments(comments: list[dict], config: dict) -> list[str]:
    """
    Send a batch of comments to Claude and extract video topic suggestions.

    Args:
        comments (list[dict]): Comment batch (text, likes, video_topic).
        config (dict):         Loaded config.json.

    Returns:
        list[str]: Extracted topic suggestions (plain strings).
    """
    api_key = os.getenv('ANTHROPIC_API_KEY', '').strip()
    if not api_key:
        return []

    comment_block = '\n'.join(
        f'[from "{c["video_topic"][:40]}" video, {c["likes"]} likes] {c["text"]}'
        for c in comments
    )

    prompt = f"""You are analysing YouTube comments on "The Engineering Brief" — an educational
engineering channel that explains how everyday technology works in 60–90 second videos.

Channel niche: electrical engineering, infrastructure, vehicles, engineering failures.
Audience: curious non-engineers, aged 18–35.

Comments to analyse:
{comment_block}

Extract up to 5 video topic ideas that the audience is clearly curious about or asking for.
Focus on questions, "why does X happen", "how does Y work", "what causes Z" patterns.
Ignore generic praise, spam, or off-topic comments.

Return a JSON array of strings — each string is a topic idea suitable as a video title.
Only include topics that fit the channel niche.
If no good topic ideas exist in these comments, return an empty array [].

Respond with only valid JSON. No text outside the JSON."""

    try:
        import anthropic

        model = config.get('script', {}).get('model', 'claude-sonnet-4-6')
        client = anthropic.Anthropic(api_key=api_key)
        t0 = time.time()

        response = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[{'role': 'user', 'content': prompt}],
        )

        elapsed = round(time.time() - t0, 2)
        logger.debug(f"[COMMENT_MINER] Claude batch response in {elapsed}s")

        text = response.content[0].text.strip()
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]

        topics = json.loads(text)
        if isinstance(topics, list):
            return [str(t).strip() for t in topics if t and str(t).strip()]
        return []

    except json.JSONDecodeError as exc:
        logger.error(f"[COMMENT_MINER] Claude JSON parse error: {exc}")
        return []
    except Exception as exc:
        logger.error(f"[COMMENT_MINER] Claude call failed: {exc}", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# DB save
# ---------------------------------------------------------------------------

def _save_topics(topics: list[str], config: dict) -> int:
    """
    Deduplicate against existing topic_bank entries and save new topics.

    Args:
        topics (list[str]): Raw topic strings from Claude analysis.
        config (dict):      Loaded config.json (not used here, kept for future).

    Returns:
        int: Number of new topics actually saved.
    """
    if not topics:
        return 0

    from database import get_topics, insert_topic

    existing_texts = {t['topic'].lower().strip() for t in get_topics(include_archived=True)}
    saved = 0

    for topic in topics:
        if not topic:
            continue
        if topic.lower().strip() in existing_texts:
            logger.debug(f"[COMMENT_MINER] Skip duplicate: '{topic}'")
            continue
        insert_topic(topic=topic, notes='audience-requested')
        existing_texts.add(topic.lower().strip())
        saved += 1
        logger.info(f"[COMMENT_MINER] Added audience-requested topic: '{topic}'")

    return saved
