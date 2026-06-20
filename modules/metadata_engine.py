"""
metadata_engine.py
==================
Stage 6 of the VideoForge pipeline. Generates platform-optimised SEO
metadata for TikTok and YouTube by calling the Claude API with the metadata
prompt template, then saves the result as a JSON file.

Input:  job_id, config dict — reads output/scripts/NNN.json
Output: output/metadata/NNN.json  (tiktok_title, youtube_title,
        youtube_description, tiktok_hashtags, youtube_tags, thumbnail_text)
Logs:   logs/metadata_engine.log

Dependencies:
    - anthropic (Claude API)
    - python-dotenv

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import json
import os
import time
from datetime import datetime
from pathlib import Path

# 2. Third-party libraries
import anthropic
from dotenv import load_dotenv

# 3. Local modules
from database import (
    update_job_status, update_job_field,
    get_last_description_skeleton_index, get_recent_youtube_titles,
)
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger('metadata_engine')

REQUIRED_METADATA_KEYS = {
    'tiktok_title', 'youtube_title', 'youtube_description',
    'tiktok_hashtags', 'youtube_tags', 'thumbnail_text',
}

# Phase 14 Block 2 — banned terms in titles + thumbnail text.
BANNED_METADATA_TERMS = (
    'rape', 'murdered', 'suicide', 'minor', 'pregnant teen',
)


def _enforce_metadata_guardrails(data: dict, job_id: str) -> None:
    """
    Raise ValueError if any title / thumbnail text field contains a banned
    term. Caller catches and triggers regeneration.
    """
    fields = (
        'tiktok_title', 'youtube_title', 'thumbnail_text',
    )
    for f in fields:
        value = (data.get(f) or '').lower()
        for banned in BANNED_METADATA_TERMS:
            if banned in value:
                raise ValueError(
                    f"Banned term '{banned}' in metadata field '{f}' "
                    f"({data.get(f)!r}) — regenerate"
                )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_script(job_id: str) -> dict:
    """
    Read and return the script JSON for the given job.

    Args:
        job_id (str): Job identifier e.g. '001'.

    Returns:
        dict: Parsed script data.

    Raises:
        FileNotFoundError: If the script file does not exist.
    """
    path = Path(f'output/scripts/{job_id}.json')
    if not path.exists():
        raise FileNotFoundError(
            f"Script not found: {path}. Run generate-script first."
        )
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _load_prompt_template(prompt_file: str) -> str:
    """
    Read the metadata prompt template from disk.

    Args:
        prompt_file (str): Path from config e.g. 'prompts/metadata_prompt.txt'.

    Returns:
        str: Raw template string.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    path = Path(prompt_file)
    if not path.exists():
        raise FileNotFoundError(f"Metadata prompt not found: {prompt_file}")
    return path.read_text(encoding='utf-8')


def _build_prompt(template: str, script: dict, config: dict) -> str:
    """
    Inject script and config values into the metadata prompt template.
    Uses explicit str.replace() so JSON literal braces in the template
    are never misinterpreted as Python format() placeholders.

    Args:
        template (str):  Raw template with {placeholder} variables.
        script (dict):   Parsed script JSON.
        config (dict):   Loaded config.json.

    Returns:
        str: Fully rendered prompt.
    """
    meta_cfg = config['metadata']
    default_hashtags_str = ', '.join(meta_cfg['default_hashtags'])

    variables = {
        'topic':                        script.get('topic', ''),
        'bucket':                       script.get('bucket', ''),
        'hook_style':                   script.get('hook_style', ''),
        'narration':                    script.get('narration', ''),
        'hashtag_count':                str(meta_cfg['hashtag_count']),
        'description_max_chars':        str(meta_cfg['description_max_chars']),
        'youtube_description_max_chars': str(meta_cfg['youtube_description_max_chars']),
        'default_hashtags':             default_hashtags_str,
    }

    result = template
    for key, value in variables.items():
        result = result.replace('{' + key + '}', value)
    return result


def _parse_metadata_response(raw_text: str, job_id: str) -> dict:
    """
    Parse and validate the JSON returned by Claude.

    Args:
        raw_text (str): Raw response text from Claude.
        job_id (str):   Job identifier for log context.

    Returns:
        dict: Validated metadata dictionary.

    Raises:
        ValueError: If JSON is malformed or required keys are missing.
    """
    text = raw_text.strip()
    if text.startswith('```'):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        text = '\n'.join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"[JOB {job_id}] JSON decode error: {e}")
        logger.debug(f"[JOB {job_id}] Raw response: {raw_text[:500]}")
        raise ValueError(f"Claude returned invalid JSON: {e}")

    missing = REQUIRED_METADATA_KEYS - set(data.keys())
    if missing:
        raise ValueError(f"Metadata JSON missing required keys: {missing}")

    if not isinstance(data['tiktok_hashtags'], list):
        raise ValueError("tiktok_hashtags must be a JSON array")
    if not isinstance(data['youtube_tags'], list):
        raise ValueError("youtube_tags must be a JSON array")

    # Phase 14 Block 2 — banned-term guardrails on titles / thumbnail text
    _enforce_metadata_guardrails(data, job_id)

    return data


def _call_claude_with_retry(
    client: anthropic.Anthropic,
    prompt: str,
    model: str,
    max_tokens: int,
    temperature: float,
    job_id: str,
    max_retries: int = 3,
    backoff_seconds: float = 5.0,
) -> str:
    """
    Call the Claude Messages API with exponential backoff on transient errors.

    Args:
        client:           Authenticated Anthropic client.
        prompt (str):     Fully rendered prompt.
        model (str):      Model ID from config.
        max_tokens (int): Maximum response tokens.
        temperature (float): Sampling temperature.
        job_id (str):     Job identifier for log context.
        max_retries (int): Maximum attempts.
        backoff_seconds (float): Base wait time between retries.

    Returns:
        str: Raw text content from Claude's first response block.

    Raises:
        Exception: If all retries are exhausted.
    """
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(
                f"[JOB {job_id}] Calling Claude API — "
                f"model: {model}, attempt: {attempt}/{max_retries}"
            )
            call_start = time.time()
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=[{"role": "user", "content": prompt}],
            )
            elapsed = time.time() - call_start
            tokens = response.usage.input_tokens + response.usage.output_tokens
            logger.info(
                f"[JOB {job_id}] Claude API call succeeded — "
                f"response time: {elapsed:.2f}s, tokens: {tokens}"
            )
            return response.content[0].text

        except anthropic.RateLimitError as e:
            wait = backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                f"[JOB {job_id}] Rate limit hit — waiting {wait:.0f}s "
                f"(attempt {attempt}/{max_retries}): {e}"
            )
            if attempt < max_retries:
                time.sleep(wait)

        except anthropic.APIConnectionError as e:
            wait = backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                f"[JOB {job_id}] API connection error — waiting {wait:.0f}s "
                f"(attempt {attempt}/{max_retries}): {e}"
            )
            if attempt < max_retries:
                time.sleep(wait)

        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                wait = backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    f"[JOB {job_id}] Claude server error {e.status_code} — "
                    f"waiting {wait:.0f}s (attempt {attempt}/{max_retries})"
                )
                if attempt < max_retries:
                    time.sleep(wait)
            else:
                raise

    raise Exception(f"Claude API failed after {max_retries} attempts")


# ---------------------------------------------------------------------------
# Description skeleton rotation + title uniqueness
# ---------------------------------------------------------------------------

def _pick_description_skeleton(channel_id: str, config: dict) -> tuple:
    """
    Pick the next description skeleton index for this channel, avoiding
    repeating the same skeleton used for the previous video.

    Args:
        channel_id (str): Channel identifier (used for DB lookup).
        config (dict):    Loaded (merged) config.

    Returns:
        tuple: (skeleton_index: int, skeleton_template: str)
    """
    skeletons = config.get('metadata', {}).get('description_skeletons', [])
    if not skeletons:
        return -1, '{youtube_description}'

    last_idx = get_last_description_skeleton_index(channel_id)
    n = len(skeletons)

    if n == 1:
        return 0, skeletons[0]

    # Rotate to next — ensure we don't repeat the last one
    next_idx = (last_idx + 1) % n
    logger.debug(
        f"[METADATA] Skeleton rotation: last={last_idx}, next={next_idx} "
        f"(total skeletons: {n})"
    )
    return next_idx, skeletons[next_idx]


def _apply_description_skeleton(
    template: str,
    youtube_description: str,
    topic: str,
    hook: str,
) -> str:
    """
    Substitute placeholders in the description skeleton template.

    Supported placeholders: {youtube_description}, {topic}, {hook}

    Args:
        template (str):             Skeleton template string.
        youtube_description (str):  Claude-generated description body.
        topic (str):                Video topic.
        hook (str):                 Hook line from script.

    Returns:
        str: Fully rendered description.
    """
    result = template
    result = result.replace('{youtube_description}', youtube_description)
    result = result.replace('{topic}', topic)
    result = result.replace('{hook}', hook)
    return result


def _check_title_uniqueness(
    proposed_title: str,
    channel_id: str,
    job_id: str,
) -> str:
    """
    Ensure the first 4 words of proposed_title don't match any of the
    channel's last 10 video titles.  If they do, append a disambiguator.

    Args:
        proposed_title (str): Claude's suggested youtube_title.
        channel_id (str):     Channel identifier for DB lookup.
        job_id (str):         For logging.

    Returns:
        str: Original or lightly amended title.
    """
    recent_titles = get_recent_youtube_titles(channel_id, limit=10)

    def _first4(title: str) -> str:
        words = title.strip().split()
        return ' '.join(words[:4]).lower()

    proposed_start = _first4(proposed_title)

    for existing in recent_titles:
        if _first4(existing) == proposed_start:
            logger.warning(
                f"[JOB {job_id}] Title first-4-words clash detected: "
                f"'{proposed_start}' matches existing '{existing[:60]}' — "
                f"appending disambiguation"
            )
            # Append a short disambiguator — Claude already picked a good title,
            # we just need to break the 4-word prefix match
            proposed_title = proposed_title.rstrip() + ' — Explained'
            break

    return proposed_title


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_metadata(job_id: str, config: dict) -> dict:
    """
    Generate SEO metadata for a job using the Claude API and save as JSON.

    Reads the script JSON, renders the metadata prompt, calls Claude,
    validates the response, and saves output/metadata/NNN.json.

    Args:
        job_id (str):  Unique job identifier e.g. '001'.
        config (dict): Loaded config.json contents.

    Returns:
        dict: {
            'success': bool,
            'output_path': str,  # path to NNN.json if success
            'error': str         # error message if failed
        }
    """
    stage_start = time.time()
    logger.info(f"[JOB {job_id}] Starting metadata_engine")

    try:
        # ----------------------------------------------------------------
        # Load inputs
        # ----------------------------------------------------------------
        script = _load_script(job_id)
        logger.info(
            f"[JOB {job_id}] Generating metadata for topic: '{script.get('topic')}'"
        )
        logger.debug(
            f"[JOB {job_id}] Config: model={config['script']['model']}, "
            f"hashtag_count={config['metadata']['hashtag_count']}, "
            f"tiktok_desc_max={config['metadata']['description_max_chars']}, "
            f"youtube_desc_max={config['metadata']['youtube_description_max_chars']}"
        )

        # ----------------------------------------------------------------
        # Build prompt
        # ----------------------------------------------------------------
        template = _load_prompt_template(config['metadata']['prompt_file'])
        prompt = _build_prompt(template, script, config)

        # ----------------------------------------------------------------
        # Call Claude
        # ----------------------------------------------------------------
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY not set in environment")

        client = anthropic.Anthropic(api_key=api_key)
        raw_response = _call_claude_with_retry(
            client=client,
            prompt=prompt,
            model=config['script']['model'],
            max_tokens=800,
            temperature=0.6,
            job_id=job_id,
        )

        # ----------------------------------------------------------------
        # Parse and validate
        # ----------------------------------------------------------------
        metadata = _parse_metadata_response(raw_response, job_id)
        logger.info(
            f"[JOB {job_id}] Metadata parsed — "
            f"tiktok_title: '{metadata['tiktok_title'][:60]}...', "
            f"hashtags: {len(metadata['tiktok_hashtags'])}, "
            f"youtube_tags: {len(metadata['youtube_tags'])}"
        )

        # ----------------------------------------------------------------
        # Title uniqueness — first-4-words check against last 10 channel titles
        # ----------------------------------------------------------------
        channel_id = config.get('_channel', {}).get('id', config.get('default_channel', 'engineering_brief'))
        metadata['youtube_title'] = _check_title_uniqueness(
            metadata['youtube_title'], channel_id, job_id
        )

        # ----------------------------------------------------------------
        # Description skeleton rotation
        # ----------------------------------------------------------------
        skeleton_idx, skeleton_template = _pick_description_skeleton(channel_id, config)
        if skeleton_template and skeleton_idx >= 0:
            hook_text = script.get('hook', '')
            topic_text = script.get('topic', '')
            metadata['youtube_description'] = _apply_description_skeleton(
                skeleton_template,
                metadata['youtube_description'],
                topic_text,
                hook_text,
            )
            logger.info(f"[JOB {job_id}] Applied description skeleton #{skeleton_idx}")
        else:
            skeleton_idx = -1
            logger.info(f"[JOB {job_id}] No description skeletons configured — using Claude description as-is")

        # Enrich with pipeline metadata
        metadata['job_id'] = job_id
        metadata['topic'] = script.get('topic', '')
        metadata['generated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        metadata['description_skeleton_index'] = skeleton_idx

        # ----------------------------------------------------------------
        # Save
        # ----------------------------------------------------------------
        output_dir = Path('output/metadata')
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{job_id}.json"

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

        size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info(f"[JOB {job_id}] File created: {output_path} ({size_mb:.4f} MB)")

        # ----------------------------------------------------------------
        # Update database
        # ----------------------------------------------------------------
        update_job_field(job_id, 'metadata_path', str(output_path))
        if skeleton_idx >= 0:
            update_job_field(job_id, 'description_skeleton_index', skeleton_idx)
        update_job_status(job_id, 'review')

        elapsed = time.time() - stage_start
        logger.info(f"[JOB {job_id}] metadata_engine COMPLETED in {elapsed:.1f}s")

        return {'success': True, 'output_path': str(output_path)}

    except Exception as e:
        elapsed = time.time() - stage_start
        logger.error(f"[JOB {job_id}] metadata_engine FAILED: {str(e)}", exc_info=True)
        update_job_status(job_id, 'failed', error_module='metadata_engine', error_message=str(e))
        return {'success': False, 'error': str(e)}
