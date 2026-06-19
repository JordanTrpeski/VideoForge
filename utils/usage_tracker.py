"""
usage_tracker.py
================
Phase 13 Block B — Thin wrapper around database.record_api_usage.

Every module that hits an external API calls track() at completion. Cost is
estimated per-provider via a simple unit -> cents conversion table that lives
in config.json (pricing.* section). Missing config is treated as zero cost so
the call still records units for visibility.

Input:  provider, operation, units (tokens / characters / requests), context
Output: one api_usage row per call
Logs:   logs/usage_tracker.log (also via the parent module's logger)
"""

# 1. Standard library
import json
from pathlib import Path
from typing import Optional

# 3. Local modules
from database import record_api_usage
from utils.logger import setup_logger

logger = setup_logger('usage_tracker')


# Built-in fallback pricing (USD cents per 1,000 units) — overridable via
# config.json pricing.{provider} = {"unit": "...", "cents_per_1k": N}.
# Conservative public list rates; not authoritative — owner can edit in config.
_DEFAULT_PRICING = {
    'claude':      {'unit': 'tokens',     'cents_per_1k': 0.30},   # blended in/out
    'elevenlabs':  {'unit': 'characters', 'cents_per_1k': 3.00},
    'openai_tts':  {'unit': 'characters', 'cents_per_1k': 1.50},
    'kokoro':      {'unit': 'characters', 'cents_per_1k': 0.00},
    'leonardo':    {'unit': 'images',     'cents_per_1k': 0.00},
    'youtube':     {'unit': 'requests',   'cents_per_1k': 0.00},
    'reddit':      {'unit': 'requests',   'cents_per_1k': 0.00},
    'tiktok':      {'unit': 'requests',   'cents_per_1k': 0.00},
    'instagram':   {'unit': 'requests',   'cents_per_1k': 0.00},
    'r2':          {'unit': 'requests',   'cents_per_1k': 0.00},
}


def _load_pricing(config: Optional[dict] = None) -> dict:
    """Return the merged pricing table — config overlay on top of defaults."""
    out = dict(_DEFAULT_PRICING)
    cfg_pricing = (config or {}).get('pricing') or {}
    for k, v in cfg_pricing.items():
        if isinstance(v, dict):
            base = dict(out.get(k, {}))
            base.update(v)
            out[k] = base
        else:
            out[k] = v
    return out


def estimate_cost_cents(provider: str, units: int, config: Optional[dict] = None) -> int:
    """
    Convert unit count to estimated USD cents.

    Args:
        provider (str): One of the keys in the pricing table.
        units (int):    Provider-native unit count.
        config (dict):  Optional config dict for pricing overrides.

    Returns:
        int: Estimated cents (integer; rounds up by half).
    """
    pricing = _load_pricing(config)
    info = pricing.get(provider, {})
    rate = float(info.get('cents_per_1k') or 0)
    if rate <= 0 or units <= 0:
        return 0
    return int(round(units * rate / 1000.0))


def track(
    provider: str,
    operation: str,
    units: int = 0,
    *,
    channel_id: Optional[str] = None,
    job_id: Optional[str] = None,
    config: Optional[dict] = None,
    cost_cents_override: Optional[int] = None,
) -> int:
    """
    Record one external API call.

    Args:
        provider (str):  Provider key (claude / elevenlabs / kokoro / ...).
        operation (str): Short label of the operation (e.g. 'messages.create').
        units (int):     Units consumed by this call. Pass 0 for fixed-cost or
                         unmetered operations.
        channel_id (str):Owning channel slug, if known.
        job_id (str):    Owning job id, if known.
        config (dict):   Loaded config dict for cost estimation overrides.
        cost_cents_override (int): Use this exact cost in cents and skip
                         estimation (useful when the provider's response carries
                         a direct cost value).

    Returns:
        int: Inserted row id (or 0 on logging-only failure).
    """
    if cost_cents_override is not None:
        cents = int(cost_cents_override)
    else:
        cents = estimate_cost_cents(provider, units, config)
    try:
        row_id = record_api_usage(
            provider=provider, operation=operation,
            units_used=int(units or 0), cost_estimate_cents=cents,
            channel_id=channel_id, job_id=job_id,
        )
        logger.debug(
            f"usage_tracker: {provider}.{operation} — units={units}, "
            f"cents={cents}, channel={channel_id}, job={job_id}"
        )
        return row_id
    except Exception as exc:
        # Never crash a pipeline because of usage logging
        logger.warning(f"usage_tracker: failed to record {provider}.{operation}: {exc}")
        return 0
