"""
similarity_engine.py
====================
Phase 11.v1.C — Topic similarity detection for VideoForge.

Before a topic is added to the job queue, this module sends the new topic
alongside the last 50 job and topic_bank topics to Claude, which returns a
similarity score and the most similar existing title.

If the score is >= 70% the caller should surface a warning to the user before
proceeding.  The check is intentionally lightweight — one small Claude call —
so it never blocks fast-track or bulk add flows for more than a few seconds.

Input:  new topic string, config dict
Output: dict with similarity_score, similar_topic, similar_job_id, angle_suggestion

Logs:   logs/similarity_engine.log

Dependencies:
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

logger = setup_logger('similarity_engine')

# Topics with score >= this threshold trigger a warning
SIMILARITY_THRESHOLD = 70.0


def _get_existing_topics(limit: int = 50) -> list[dict]:
    """
    Pull the most recent topics from both the jobs table and topic_bank.

    Args:
        limit (int): Maximum number of topics to return.

    Returns:
        list[dict]: Each entry has 'id', 'topic', 'source' ('job' or 'bank').
    """
    from database import get_all_jobs, get_topics

    existing: list[dict] = []

    for job in get_all_jobs()[:limit]:
        if job.get('topic'):
            existing.append({
                'id':     job['id'],
                'topic':  job['topic'],
                'source': 'job',
            })

    for t in get_topics(include_archived=False, limit=limit):
        if t.get('topic'):
            existing.append({
                'id':     str(t['id']),
                'topic':  t['topic'],
                'source': 'bank',
            })

    return existing[:limit]


def check_similarity(new_topic: str, config: dict) -> dict:
    """
    Check a new topic against existing jobs and topic_bank entries for similarity.

    Sends the new topic and existing topic list to Claude. Claude returns a
    similarity score (0–100) and the closest matching title.

    Args:
        new_topic (str): The new topic text to evaluate.
        config (dict):   Loaded config.json.

    Returns:
        dict: {
            'checked':          bool,  # False if Claude unavailable
            'similarity_score': float, # 0-100
            'similar_topic':    str,   # matching topic text
            'similar_job_id':   str,   # job ID or bank ID of the match
            'similar_source':   str,   # 'job' or 'bank'
            'angle_suggestion': str,   # alternative angle (only when score >= threshold)
            'warning':          bool,  # True if score >= SIMILARITY_THRESHOLD
        }
    """
    api_key = os.getenv('ANTHROPIC_API_KEY', '').strip()
    if not api_key:
        logger.warning(
            "similarity_engine: ANTHROPIC_API_KEY not set — skipping similarity check"
        )
        return _no_check_result()

    existing = _get_existing_topics(limit=50)
    if not existing:
        logger.debug("similarity_engine: No existing topics — skipping similarity check")
        return _no_check_result()

    topics_list = "\n".join(
        f"- [{e['source']}:{e['id']}] {e['topic']}"
        for e in existing
    )

    prompt = f"""You are checking whether a new video topic is too similar to existing content.

New topic: "{new_topic}"

Existing topics (format: [source:id] topic):
{topics_list}

Respond with a JSON object containing exactly these fields:
- "similarity_score": integer 0-100 (0 = completely different, 100 = identical)
- "similar_topic": string (the most similar existing topic text, or "" if nothing is close)
- "similar_id": string (the id from the matching entry, e.g. "001" or "42", or "" if nothing is close)
- "similar_source": string ("job" or "bank" or "")
- "angle_suggestion": string (if similarity_score >= 70, suggest a fresh angle that is clearly distinct from the existing topic — one sentence. Otherwise return "")

Scoring guide:
90-100: Near-identical wording or concept
70-89:  Substantially overlapping — same core engineering concept, different words
50-69:  Related topic, meaningful overlap but distinct enough
0-49:   Different enough to proceed without concern

Respond with only valid JSON. No text outside the JSON."""

    try:
        import anthropic

        model = config.get('script', {}).get('model', 'claude-sonnet-4-6')
        logger.info(
            f"similarity_engine: Checking similarity for '{new_topic}' "
            f"against {len(existing)} existing topics — model: {model}"
        )
        t0 = time.time()

        client   = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=250,
            messages=[{'role': 'user', 'content': prompt}],
        )

        elapsed = round(time.time() - t0, 2)
        logger.info(
            f"similarity_engine: Claude response in {elapsed:.2f}s"
        )

        text = response.content[0].text.strip()
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]

        data = json.loads(text)

        score      = float(data.get('similarity_score', 0))
        sim_topic  = str(data.get('similar_topic', ''))
        sim_id     = str(data.get('similar_id', ''))
        sim_source = str(data.get('similar_source', ''))
        angle      = str(data.get('angle_suggestion', ''))
        warning    = score >= SIMILARITY_THRESHOLD

        logger.info(
            f"similarity_engine: '{new_topic}' → score: {score:.0f}%, "
            f"match: '{sim_topic}', warning: {warning}"
        )

        return {
            'checked':          True,
            'similarity_score': score,
            'similar_topic':    sim_topic,
            'similar_job_id':   sim_id,
            'similar_source':   sim_source,
            'angle_suggestion': angle,
            'warning':          warning,
        }

    except json.JSONDecodeError as exc:
        logger.error(f"similarity_engine: Could not parse Claude response: {exc}")
        return _no_check_result()
    except Exception as exc:
        logger.error(
            f"similarity_engine: Check failed for '{new_topic}': {exc}",
            exc_info=True,
        )
        return _no_check_result()


def _no_check_result() -> dict:
    """Return a safe default when the similarity check cannot run."""
    return {
        'checked':          False,
        'similarity_score': 0.0,
        'similar_topic':    '',
        'similar_job_id':   '',
        'similar_source':   '',
        'angle_suggestion': '',
        'warning':          False,
    }
