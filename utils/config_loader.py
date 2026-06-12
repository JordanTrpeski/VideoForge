"""
config_loader.py
================
Hot-reload-safe config loading with per-channel overlay support.

Global config.json is the base. Each channel can override any value via
channels/<slug>/config.json (deep-merged). Prompt paths, asset dirs, and
credential paths are injected transparently so pipeline modules receive a
fully resolved config without knowing about the multi-channel layer.

Input:  channel slug (str)
Output: merged config dict with _channel metadata injected
"""

# 1. Standard library
import json
from copy import deepcopy
from pathlib import Path
from typing import Optional


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge override into base.
    Dicts are merged key-by-key; all other types in override win outright.

    Args:
        base (dict):     Starting dict (not mutated).
        override (dict): Values to apply on top.

    Returns:
        dict: New merged dict.
    """
    result = deepcopy(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = deepcopy(val)
    return result


def load_global_config() -> dict:
    """
    Load and return the root config.json. Always reads fresh from disk.

    Returns:
        dict: Parsed global configuration.

    Raises:
        FileNotFoundError: If config.json does not exist.
    """
    path = Path('config.json')
    if not path.exists():
        raise FileNotFoundError(
            "config.json not found. Run from the VideoForge project root."
        )
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_channel_config(channel_slug: str) -> dict:
    """
    Load the fully merged config for a channel. Always reads fresh from disk
    (hot-reload safe — call this at the start of every pipeline run).

    Resolution order:
      1. Global config.json              (base)
      2. channels/<slug>/config.json     (overlay, deep-merged)
      3. channels/<slug>/prompts/        (inject paths for any prompt files present)
      4. channels/<slug>/assets/         (inject music_dir / backgrounds_dir if present)
      5. Inject _channel metadata block  (slug + credential paths for upload_engine)

    Voice ID is taken from the merged config['voice']['voice_id'] and is NEVER
    modified by the variation engine — only length_targets and hook_styles rotate.

    Args:
        channel_slug (str): Channel identifier e.g. 'engineering_brief'.

    Returns:
        dict: Fully merged channel config ready to pass to all pipeline modules.
    """
    merged = load_global_config()
    channel_dir = Path(f'channels/{channel_slug}')

    # 1. Deep-merge channel-specific overlay
    channel_cfg_path = channel_dir / 'config.json'
    if channel_cfg_path.exists():
        with open(channel_cfg_path, 'r', encoding='utf-8') as f:
            channel_override = json.load(f)
        merged = _deep_merge(merged, channel_override)

    # 2. Inject channel-specific prompt file paths
    prompts_dir = channel_dir / 'prompts'
    prompt_mappings = [
        ('script_prompt.txt',          ('script',   'prompt_file')),
        ('metadata_prompt.txt',        ('metadata', 'prompt_file')),
        ('reddit_rewrite_prompt.txt',  ('script',   'reddit_prompt_file')),
    ]
    for filename, (section, key) in prompt_mappings:
        candidate = prompts_dir / filename
        if candidate.exists():
            merged.setdefault(section, {})[key] = str(candidate)

    # 3. Inject channel-specific asset directories when non-empty
    music_dir = channel_dir / 'assets' / 'music'
    if music_dir.is_dir():
        mp3s = list(music_dir.glob('*.mp3'))
        if mp3s:
            merged.setdefault('video', {})['music_dir'] = str(music_dir)

    bg_dir = channel_dir / 'assets' / 'backgrounds'
    if bg_dir.is_dir():
        clips = [p for p in bg_dir.iterdir()
                 if p.suffix.lower() in ('.mp4', '.mov', '.mkv', '.webm')]
        if clips:
            merged.setdefault('video', {})['backgrounds_dir'] = str(bg_dir)

    # 4. Inject credential paths consumed by upload_engine
    merged['_channel'] = {
        'slug':                 channel_slug,
        'youtube_token_path':   str(channel_dir / 'youtube_token.json'),
        'youtube_secrets_path': str(channel_dir / 'client_secrets.json'),
        'tiktok_token_path':    str(channel_dir / 'tiktok_token.json'),
    }

    return merged


def get_default_channel(config: Optional[dict] = None) -> str:
    """
    Return the default channel slug from config, or 'engineering_brief'.

    Args:
        config (dict): Optional pre-loaded global config. Loads fresh if None.

    Returns:
        str: Default channel slug.
    """
    if config is None:
        try:
            config = load_global_config()
        except FileNotFoundError:
            return 'engineering_brief'
    return config.get('default_channel', 'engineering_brief')
