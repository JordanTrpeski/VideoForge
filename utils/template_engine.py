"""
template_engine.py
==================
Phase 13 Block A — Content templates lookup and selection.

A "content template" is a per-channel preset that defines visual_mode, length
window, hook style pool, music palette, thumbnail mode, caption mode, prompt
overrides and the dual_output flag. The variation engine picks one of a
channel's active templates per job, then picks length and hook within that
template.

This module wraps the database helpers in a stable API so script_engine and
other callers don't depend on schema details.

Input:  channel_id, optional template name override
Output: resolved template dict (or None if no templates configured)
"""

# 1. Standard library
import json
import random
from pathlib import Path
from typing import Optional

# 3. Local modules
from database import get_template, get_template_by_name, get_templates
from utils.logger import setup_logger

logger = setup_logger('template_engine')


def _channel_template_names(channel_slug: str) -> list:
    """
    Read the per-channel config.json for its 'templates' array, if any.

    The presence of a templates array on a channel's overlay marks that
    channel as opted in to the template system. Names listed there should
    correspond to content_templates rows. Templates referenced but missing
    from the DB are skipped silently (they may be in flight).

    Args:
        channel_slug (str): Channel identifier.

    Returns:
        list[str]: Template names declared in the channel's config overlay,
                   empty list if the overlay doesn't exist or has no array.
    """
    cfg_path = Path(f'channels/{channel_slug}/config.json')
    if not cfg_path.exists():
        return []
    try:
        with open(cfg_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return []
    names = data.get('templates', [])
    if not isinstance(names, list):
        return []
    return [str(n) for n in names if n]


def list_active_templates(channel_slug: str) -> list:
    """
    Return the active templates for a channel, intersected with the
    channel-config templates array if one is defined.

    Args:
        channel_slug (str): Channel identifier.

    Returns:
        list[dict]: Active template dicts.
    """
    allowed = set(_channel_template_names(channel_slug))
    all_active = get_templates(channel_id=channel_slug, active_only=True)
    if not allowed:
        # No allow-list — channel uses every active template it owns.
        return all_active
    return [t for t in all_active if t['name'] in allowed]


def resolve_template(
    channel_slug: str,
    template_name: Optional[str] = None,
) -> Optional[dict]:
    """
    Pick the template to use for one job.

    Lookup order:
      1. If template_name is given, return that template (active or not) so
         CLI overrides can still test inactive templates.
      2. Otherwise pick a random active template from the channel's pool.
      3. If the channel has no active templates configured, return None.
         Callers fall back to legacy variation.shorts / variation.reddit_long_form.

    Args:
        channel_slug (str):     Channel identifier.
        template_name (str):    Optional explicit template name (CLI --template
                                or test).

    Returns:
        dict | None: Resolved template, or None if no template is applicable.
    """
    if template_name:
        t = get_template_by_name(channel_slug, template_name)
        if t is None:
            logger.warning(
                f"template_engine: '{template_name}' not found for channel '{channel_slug}'"
            )
        else:
            logger.info(
                f"template_engine: explicit template — channel={channel_slug}, "
                f"template='{template_name}', id={t['id']}"
            )
        return t

    pool = list_active_templates(channel_slug)
    if not pool:
        logger.debug(
            f"template_engine: no active templates for channel '{channel_slug}' "
            "— falling back to legacy variation block"
        )
        return None
    chosen = random.choice(pool)
    logger.info(
        f"template_engine: random pick — channel={channel_slug}, "
        f"template='{chosen['name']}', id={chosen['id']} "
        f"(from {len(pool)} active)"
    )
    return chosen


def pick_length_and_hook(template: dict) -> tuple:
    """
    Choose length_seconds and hook_style for one job within a template's pools.

    Length is sampled uniformly between length_min_seconds and length_max_seconds
    inclusive, rounded to the nearest 5 seconds for clean targets. Hook is a
    random choice from hook_style_pool (or '' if the pool is empty).

    Args:
        template (dict): Resolved template dict.

    Returns:
        tuple: (length_seconds: int, hook_style: str)
    """
    lo = int(template.get('length_min_seconds') or 0)
    hi = int(template.get('length_max_seconds') or lo)
    if hi < lo:
        hi = lo
    raw = random.randint(lo, hi) if hi > lo else lo
    # Snap to 5-second grid so prompts and reports stay tidy
    length = max(lo, min(hi, 5 * round(raw / 5))) if raw else lo
    hooks = template.get('hook_style_pool') or []
    hook = random.choice(hooks) if hooks else ''
    return length, hook
