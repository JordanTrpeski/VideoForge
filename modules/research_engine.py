"""
research_engine.py
==================
Phase 11.v2.A — Full topic scoring engine for VideoForge.

Scores a topic 0-10 across four dimensions then combines them into a final
weighted composite score.  Each dimension degrades gracefully when its data
source is unavailable.

Dimensions
----------
1. Trend score       (weight 0.30) — Google Trends spike relative to 30-day baseline
2. Competition score (weight 0.30) — Claude-assessed competition level (inverted)
3. Channel-fit score (weight 0.25) — Claude channel relevance judgement
4. Performance score (weight 0.15) — channel's own analytics for this bucket

Input:  topic string, config dict
Output: dict with all four dimension scores, final_score, alt_angles,
        competition_level, hook_suggestion

Logs:   logs/research_engine.log

Dependencies:
    - anthropic
    - pytrends
    - database (get_analytics_summary, update_topic_score)
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

logger = setup_logger('research_engine')

# ---------------------------------------------------------------------------
# Weights — must sum to 1.0
# ---------------------------------------------------------------------------
WEIGHT_TREND       = 0.30
WEIGHT_COMPETITION = 0.30
WEIGHT_FIT         = 0.25
WEIGHT_PERFORMANCE = 0.15

SCORE_VERSION = 1   # bump when weight formula changes


def score_topic(topic: str, bucket: str, config: dict, topic_id: int = 0) -> dict:
    """
    Score a single topic across all four dimensions.

    Args:
        topic (str):     Topic text to score.
        bucket (str):    Content bucket: elec / infra / vehicle / flaw.
        config (dict):   Loaded config.json.
        topic_id (int):  If > 0, writes the result back to topic_bank via
                         update_topic_score().

    Returns:
        dict: {
            'success':           bool,
            'topic':             str,
            'trend_score':       float,  # 0-10
            'competition_score': float,  # 0-10
            'channel_fit_score': float,  # 0-10
            'performance_score': float,  # 0-10
            'final_score':       float,  # 0-10 weighted composite
            'competition_level': str,    # 'low' / 'medium' / 'high'
            'alt_angles':        list,   # 3 alternative angles
            'hook_suggestion':   str,
            'score_breakdown':   dict,
        }
    """
    t0 = time.time()
    logger.info(f"[SCORE] Scoring topic: '{topic}' (bucket={bucket})")

    trend_score       = _score_trend(topic, bucket, config)
    claude_result     = _score_claude(topic, bucket, config)
    performance_score = _score_performance(bucket)

    competition_score = claude_result.get('competition_score', 5.0)
    channel_fit_score = claude_result.get('channel_fit_score', 5.0)
    alt_angles        = claude_result.get('alt_angles', [])
    competition_level = claude_result.get('competition_level', 'medium')
    hook_suggestion   = claude_result.get('hook_suggestion', '')

    weights = config.get('research', {}).get('scoring_weights', {})
    w_trend = float(weights.get('trend',       WEIGHT_TREND))
    w_comp  = float(weights.get('competition', WEIGHT_COMPETITION))
    w_fit   = float(weights.get('channel_fit', WEIGHT_FIT))
    w_perf  = float(weights.get('performance', WEIGHT_PERFORMANCE))

    final_score = round(
        trend_score       * w_trend
        + competition_score * w_comp
        + channel_fit_score * w_fit
        + performance_score * w_perf,
        2,
    )

    elapsed = round(time.time() - t0, 2)
    logger.info(
        f"[SCORE] '{topic}' — final={final_score:.1f} "
        f"(trend={trend_score:.1f}, comp={competition_score:.1f}, "
        f"fit={channel_fit_score:.1f}, perf={performance_score:.1f}) "
        f"in {elapsed}s"
    )

    result = {
        'success':           True,
        'topic':             topic,
        'trend_score':       trend_score,
        'competition_score': competition_score,
        'channel_fit_score': channel_fit_score,
        'performance_score': performance_score,
        'final_score':       final_score,
        'competition_level': competition_level,
        'alt_angles':        alt_angles,
        'hook_suggestion':   hook_suggestion,
        'score_breakdown': {
            'trend':       {'score': trend_score,       'weight': w_trend},
            'competition': {'score': competition_score, 'weight': w_comp},
            'fit':         {'score': channel_fit_score, 'weight': w_fit},
            'performance': {'score': performance_score, 'weight': w_perf},
        },
    }

    if topic_id > 0:
        try:
            from database import update_topic_score
            update_topic_score(
                topic_id=topic_id,
                trend_score=trend_score,
                competition_score=competition_score,
                channel_fit_score=channel_fit_score,
                performance_score=performance_score,
                final_score=final_score,
                alt_angles=json.dumps(alt_angles),
                competition_level=competition_level,
                score_version=SCORE_VERSION,
            )
            logger.info(f"[SCORE] Saved scores to topic_bank id={topic_id}")
        except Exception as exc:
            logger.warning(f"[SCORE] Could not save scores to DB: {exc}")

    return result


def score_bulk(topics: list[dict], config: dict) -> list[dict]:
    """
    Score a list of topics sequentially with a short sleep between Claude calls.

    Args:
        topics (list[dict]):  Each entry must have 'topic', 'bucket', and
                              optionally 'id' (topic_bank primary key).
        config (dict):        Loaded config.json.

    Returns:
        list[dict]: Results in the same order as input, sorted by final_score desc.
    """
    results = []
    for i, t in enumerate(topics):
        if i > 0:
            time.sleep(1)   # gentle rate-limit between Claude calls
        r = score_topic(
            topic=t.get('topic', ''),
            bucket=t.get('bucket', 'elec'),
            config=config,
            topic_id=int(t.get('id', 0)),
        )
        results.append(r)

    results.sort(key=lambda x: x.get('final_score', 0), reverse=True)
    return results


# ---------------------------------------------------------------------------
# Dimension 1 — Google Trends spike score
# ---------------------------------------------------------------------------

def _score_trend(topic: str, bucket: str, config: dict) -> float:
    """
    Query Google Trends for the topic and convert spike % to a 0-10 score.

    Spike is calculated as: (mean of last 7 days) / (mean of prior 30 days).
    A 2× spike maps to ~10.  No data available -> neutral 5.0.

    Args:
        topic (str):   Topic to query.
        bucket (str):  Not used here but kept for future locale-per-bucket logic.
        config (dict): Loaded config.json.

    Returns:
        float: Trend score 0-10.
    """
    try:
        from pytrends.request import TrendReq

        pytrends = TrendReq(hl='en-US', tz=360, timeout=(10, 25))
        pytrends.build_payload([topic], timeframe='today 3-m', geo='')
        df = pytrends.interest_over_time()

        if df.empty or topic not in df.columns:
            logger.debug(f"[SCORE] No Trends data for '{topic}' — defaulting to 5.0")
            return 5.0

        series = df[topic]
        if len(series) < 8:
            return 5.0

        recent   = float(series.iloc[-7:].mean())
        baseline = float(series.iloc[:-7].mean())

        if baseline < 1:
            return 5.0 if recent < 1 else 8.0

        ratio = recent / baseline          # 1.0 = flat, 2.0 = doubled
        score = min(10.0, max(0.0, (ratio - 0.5) * 6.67))
        logger.debug(f"[SCORE] Trend '{topic}': recent={recent:.1f} baseline={baseline:.1f} ratio={ratio:.2f} -> {score:.1f}")
        return round(score, 1)

    except Exception as exc:
        logger.warning(f"[SCORE] Trends query failed for '{topic}': {exc}")
        return 5.0


# ---------------------------------------------------------------------------
# Dimension 2 + 3 + alt_angles — Claude assessment
# ---------------------------------------------------------------------------

def _score_claude(topic: str, bucket: str, config: dict) -> dict:
    """
    Ask Claude to assess competition level and channel fit simultaneously.

    Returns a single dict with competition_score, channel_fit_score,
    competition_level, alt_angles (list of 3), and hook_suggestion.
    Falls back to neutral values if Claude is unavailable.

    Args:
        topic (str):   Topic to assess.
        bucket (str):  Content bucket for channel context.
        config (dict): Loaded config.json.

    Returns:
        dict: {competition_score, channel_fit_score, competition_level,
               alt_angles, hook_suggestion}
    """
    api_key = os.getenv('ANTHROPIC_API_KEY', '').strip()
    if not api_key:
        logger.warning("[SCORE] ANTHROPIC_API_KEY not set — skipping Claude scoring")
        return _claude_neutral()

    bucket_labels = {
        'elec':    'Electrical engineering',
        'infra':   'Infrastructure / civil engineering',
        'vehicle': 'Vehicles and transport engineering',
        'flaw':    'Engineering failures and design flaws',
    }
    bucket_label = bucket_labels.get(bucket, bucket)

    prompt = f"""You are helping score a YouTube Shorts topic for "The Engineering Brief" —
a faceless educational channel about how everyday engineering and technology works.
Target audience: curious non-engineers, 18–35.  Video length: 60–90 seconds.
Content bucket: {bucket_label}.

Topic to score: "{topic}"

Return a JSON object with exactly these fields:
- "competition_score": integer 0-10
  (10 = almost no competing content, 0 = massively over-covered)
  Scoring guide: 9-10 niche/original, 7-8 moderate coverage, 5-6 well-covered, 3-4 saturated, 0-2 exhausted
- "channel_fit_score": integer 0-10
  (10 = perfect fit — explainable in 70 sec, non-engineer friendly, engineering hook)
  Scoring guide: 9-10 ideal, 7-8 good, 5-6 okay but needs angle work, 3-4 stretch, 0-2 wrong format
- "competition_level": string — exactly one of: "low", "medium", "high"
- "alt_angles": array of exactly 3 strings — each is a fresh reframing of this topic
  that is more specific, more curiosity-driven, or covers an underserved angle
- "hook_suggestion": string — one punchy hook line (under 15 words) for this topic

Respond with only valid JSON. No text outside the JSON."""

    try:
        import anthropic

        model = config.get('script', {}).get('model', 'claude-sonnet-4-6')
        client = anthropic.Anthropic(api_key=api_key)
        t0 = time.time()
        response = client.messages.create(
            model=model,
            max_tokens=400,
            messages=[{'role': 'user', 'content': prompt}],
        )
        elapsed = round(time.time() - t0, 2)
        logger.debug(f"[SCORE] Claude response in {elapsed}s for '{topic}'")

        text = response.content[0].text.strip()
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]

        data = json.loads(text)
        return {
            'competition_score': float(data.get('competition_score', 5)),
            'channel_fit_score': float(data.get('channel_fit_score', 5)),
            'competition_level': str(data.get('competition_level', 'medium')),
            'alt_angles':        list(data.get('alt_angles', [])),
            'hook_suggestion':   str(data.get('hook_suggestion', '')),
        }

    except json.JSONDecodeError as exc:
        logger.error(f"[SCORE] Claude JSON parse error: {exc}")
        return _claude_neutral()
    except Exception as exc:
        logger.error(f"[SCORE] Claude call failed: {exc}", exc_info=True)
        return _claude_neutral()


def _claude_neutral() -> dict:
    """Return neutral values when Claude is unavailable."""
    return {
        'competition_score': 5.0,
        'channel_fit_score': 5.0,
        'competition_level': 'medium',
        'alt_angles':        [],
        'hook_suggestion':   '',
    }


# ---------------------------------------------------------------------------
# Dimension 4 — Channel performance score
# ---------------------------------------------------------------------------

def _score_performance(bucket: str) -> float:
    """
    Use channel analytics to score how well this bucket performs historically.

    If fewer than 3 analytics rows exist for the bucket, returns a neutral 5.0
    so new channels aren't penalised for lack of data.

    Args:
        bucket (str): Content bucket key.

    Returns:
        float: Performance score 0-10.
    """
    try:
        from database import get_analytics_summary
        summary = get_analytics_summary()
        if not summary or bucket not in summary:
            return 5.0

        bdata = summary[bucket]
        count = bdata.get('cnt', 0)
        if count < 3:
            return 5.0

        # Find the best-performing bucket to normalise against
        max_views = max(
            (v.get('avg_views', 0) or 0) for v in summary.values()
        )
        if max_views < 1:
            return 5.0

        avg_views = bdata.get('avg_views', 0) or 0
        score = round(min(10.0, (avg_views / max_views) * 10), 1)
        logger.debug(
            f"[SCORE] Performance bucket={bucket}: avg_views={avg_views:.0f} "
            f"max={max_views:.0f} -> {score:.1f}"
        )
        return score

    except Exception as exc:
        logger.warning(f"[SCORE] Performance scoring failed: {exc}")
        return 5.0
