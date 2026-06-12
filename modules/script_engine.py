"""
script_engine.py
================
Stage 1 of the VideoForge pipeline. Generates a structured video script
using the Claude API and saves it as a JSON file.

Input:  job_id, topic string, bucket, hook_style, config dict
Output: output/scripts/NNN.json containing narration, sections, visual_brief
Logs:   logs/script_engine.log

Dependencies:
    - anthropic (Claude API)
    - python-dotenv (env loading)

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import json
import os
import random
import time
from datetime import datetime
from pathlib import Path

# 2. Third-party libraries
import anthropic
from dotenv import load_dotenv

# 3. Local modules
from database import (create_job, update_job_status, update_job_field,
                      get_job, get_next_job_id, get_last_job_variation)
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger('script_engine')

# Required keys that must be present in Claude's JSON response
REQUIRED_SCRIPT_KEYS = {'topic', 'bucket', 'hook_style', 'sections', 'narration', 'visual_brief', 'word_count'}
# Reddit Stories use a background-loop visual, so no visual_brief is needed —
# instead Claude returns 5 candidate opening hooks for the owner to pick from.
REQUIRED_REDDIT_KEYS = {'topic', 'bucket', 'hook_style', 'sections', 'narration', 'hooks', 'word_count'}
REQUIRED_SECTION_KEYS = {'hook', 'body', 'cta'}


def _load_prompt_template(prompt_file: str) -> str:
    """
    Read the script prompt template from disk.

    Args:
        prompt_file (str): Path to the prompt template file e.g. 'prompts/script_prompt.txt'.

    Returns:
        str: Raw template string with {placeholder} variables.

    Raises:
        FileNotFoundError: If the prompt file does not exist.
    """
    path = Path(prompt_file)
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_file}")
    return path.read_text(encoding='utf-8')


def _build_prompt(
    template: str,
    topic: str,
    bucket: str,
    hook_style: str,
    config: dict,
    target_length_override: int = None,
) -> str:
    """
    Inject runtime values into the prompt template.

    Args:
        template (str):             Raw prompt template with {placeholder} variables.
        topic (str):                Video topic.
        bucket (str):               Content bucket identifier.
        hook_style (str):           Hook style name.
        config (dict):              Loaded config.json contents.
        target_length_override (int): Variation-picked length in seconds; overrides
                                      config['channel']['target_length_seconds'] when set.

    Returns:
        str: Fully rendered prompt ready to send to Claude.
    """
    if target_length_override is not None:
        target_length = target_length_override
        word_count_target = round(target_length * 2.5)
    else:
        target_length = config['channel']['target_length_seconds']
        word_count_target = config['script']['word_count_target']

    variables = {
        'topic': topic,
        'bucket': bucket,
        'hook_style': hook_style,
        'word_count_target': word_count_target,
        'word_count_min': round(word_count_target * 0.92),
        'word_count_max': round(word_count_target * 1.08),
        'target_length_seconds': target_length,
        'images_to_generate': config['script']['images_to_generate'],
    }

    # Use explicit str.replace() so JSON literal braces in the template
    # are never misinterpreted as Python format() placeholders.
    result = template
    for key, value in variables.items():
        result = result.replace('{' + key + '}', str(value))
    return result


def _build_reddit_prompt(
    template: str,
    title: str,
    selftext: str,
    config: dict,
    target_length_override: int = None,
) -> str:
    """
    Inject the Reddit story and word-count targets into the rewrite template.

    Args:
        template (str):             Raw reddit_rewrite_prompt.txt with {placeholder} vars.
        title (str):                Original Reddit post title.
        selftext (str):             Original Reddit post body to be rewritten.
        config (dict):              Loaded config.json contents.
        target_length_override (int): Variation-picked length in seconds; overrides
                                      config['channel']['target_length_seconds'] when set.

    Returns:
        str: Fully rendered prompt ready to send to Claude.
    """
    if target_length_override is not None:
        target_length = target_length_override
        word_count_target = round(target_length * 2.5)
    else:
        target_length = config['channel']['target_length_seconds']
        word_count_target = config['script']['word_count_target']

    variables = {
        'title': title,
        'selftext': selftext,
        'word_count_target': word_count_target,
        'word_count_min': round(word_count_target * 0.92),
        'word_count_max': round(word_count_target * 1.08),
        'target_length_seconds': target_length,
    }

    # Explicit str.replace() so JSON literal braces in the template are never
    # misinterpreted as Python format() placeholders.
    result = template
    for key, value in variables.items():
        result = result.replace('{' + key + '}', str(value))
    return result


def _parse_script_response(raw_text: str, job_id: str, mode: str = 'standard') -> dict:
    """
    Parse and validate the JSON returned by Claude.

    Args:
        raw_text (str): Raw string from Claude's response content.
        job_id (str):   Job identifier for log context.

    Returns:
        dict: Validated script data dictionary.

    Raises:
        ValueError: If JSON is malformed or required keys are missing.
    """
    # Strip any accidental markdown fences Claude may have added
    text = raw_text.strip()
    if text.startswith('```'):
        lines = text.splitlines()
        # Remove opening fence line (```json or ```)
        lines = lines[1:]
        # Remove closing fence line
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        text = '\n'.join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"[JOB {job_id}] JSON decode error (may be truncated): {e}")
        logger.debug(f"[JOB {job_id}] Raw response was: {raw_text[:500]}")
        # Attempt to salvage a truncated response by finding the last valid
        # top-level key boundary and closing any open braces / brackets.
        # This handles the case where max_tokens cuts the response mid-string.
        salvaged = None
        # Walk backwards through the text to find the last complete key-value
        # pair — try progressively shorter slices.
        for cut in range(len(text), max(len(text) - 2000, 0), -1):
            candidate = text[:cut]
            # Count unmatched open braces/brackets
            depth = 0
            in_string = False
            escape = False
            for ch in candidate:
                if escape:
                    escape = False
                    continue
                if ch == '\\' and in_string:
                    escape = True
                    continue
                if ch == '"' and not escape:
                    in_string = not in_string
                    continue
                if not in_string:
                    if ch in ('{', '['):
                        depth += 1
                    elif ch in ('}', ']'):
                        depth -= 1
            if not in_string and depth >= 0:
                # Close all open structures
                close_map = {'{': '}', '[': ']'}
                # Re-derive open stack
                stack = []
                in_s = False
                esc = False
                for ch in candidate:
                    if esc:
                        esc = False
                        continue
                    if ch == '\\' and in_s:
                        esc = True
                        continue
                    if ch == '"':
                        in_s = not in_s
                        continue
                    if not in_s:
                        if ch in ('{', '['):
                            stack.append(ch)
                        elif ch in ('}', ']'):
                            if stack:
                                stack.pop()
                # Close any open string first, then close all open structures
                closing = ''
                if in_s:
                    closing += '"'
                for opener in reversed(stack):
                    closing += close_map[opener]
                patched = candidate + closing
                try:
                    salvaged = json.loads(patched)
                    logger.warning(
                        f"[JOB {job_id}] Salvaged truncated JSON by closing {len(closing)} char(s); "
                        f"cut at char {cut}/{len(text)}"
                    )
                    break
                except json.JSONDecodeError:
                    continue
        if salvaged is None:
            raise ValueError(f"Claude returned invalid JSON (truncated, not salvageable): {e}")
        data = salvaged

    # Validate top-level keys (mode-specific requirements)
    required_keys = REQUIRED_REDDIT_KEYS if mode == 'reddit' else REQUIRED_SCRIPT_KEYS
    missing = required_keys - set(data.keys())
    if missing:
        raise ValueError(f"Script JSON missing required keys: {missing}")

    # Validate sections sub-keys
    sections = data.get('sections', {})
    missing_sections = REQUIRED_SECTION_KEYS - set(sections.keys())
    if missing_sections:
        raise ValueError(f"Script JSON sections missing keys: {missing_sections}")

    if mode == 'reddit':
        # Reddit stories: require 5 candidate hooks; no image prompts needed
        # (visuals come from a background loop). Normalise visual_brief to [].
        hooks = data.get('hooks', [])
        if not isinstance(hooks, list) or len(hooks) < 1:
            raise ValueError(f"Reddit script JSON must contain a non-empty 'hooks' array, got: {hooks}")
        data.setdefault('visual_brief', [])
    else:
        # Validate visual_brief length for standard engineering scripts
        expected_images = 8
        actual_images = len(data.get('visual_brief', []))
        if actual_images != expected_images:
            raise ValueError(f"Expected {expected_images} visual_brief prompts, got {actual_images}")

    return data


def _call_claude_with_retry(
    client: anthropic.Anthropic,
    prompt: str,
    model: str,
    max_tokens: int,
    temperature: float,
    job_id: str,
    max_retries: int = 3,
    backoff_seconds: float = 5.0
) -> str:
    """
    Call the Claude Messages API with exponential backoff retry on transient errors.

    Args:
        client (anthropic.Anthropic): Authenticated Anthropic client.
        prompt (str):                 Fully rendered user prompt.
        model (str):                  Model ID from config.
        max_tokens (int):             Maximum tokens for the response.
        temperature (float):          Sampling temperature.
        job_id (str):                 Job ID for log context.
        max_retries (int):            Maximum number of attempts before raising.
        backoff_seconds (float):      Base wait time; doubles on each retry.

    Returns:
        str: Raw text content from Claude's first response block.

    Raises:
        Exception: If all retries are exhausted.
    """
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"[JOB {job_id}] Calling Claude API — model: {model}, attempt: {attempt}/{max_retries}")
            call_start = time.time()

            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=[{"role": "user", "content": prompt}]
            )

            elapsed = time.time() - call_start
            tokens_used = response.usage.input_tokens + response.usage.output_tokens
            logger.info(
                f"[JOB {job_id}] Claude API call succeeded — "
                f"response time: {elapsed:.2f}s, tokens: {tokens_used}"
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
            # 5xx errors are transient; 4xx (except 429) are not worth retrying
            if e.status_code >= 500:
                wait = backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    f"[JOB {job_id}] Claude API server error {e.status_code} — "
                    f"waiting {wait:.0f}s (attempt {attempt}/{max_retries})"
                )
                if attempt < max_retries:
                    time.sleep(wait)
            else:
                raise

    raise Exception(f"Claude API call failed after {max_retries} attempts")


def _pick_variation(mode: str, config: dict, job_id: str) -> tuple:
    """
    Randomly select a target length and hook style for this job from the
    variation config, enforcing the no-consecutive-identical-pair rule.

    Reads config fresh on every call (hot-reload safe — config is passed in).

    Args:
        mode (str):    'standard' or 'reddit' — selects the variation sub-block.
        config (dict): Loaded config.json contents.
        job_id (str):  Current job ID (excluded when fetching the previous pair).

    Returns:
        tuple: (picked_length_seconds: int, picked_hook_style: str)
    """
    var_key = 'reddit_long_form' if mode == 'reddit' else 'shorts'
    var_cfg = config['variation'][var_key]
    lengths: list = var_cfg['length_targets_seconds']
    hooks: list = var_cfg['hook_styles']

    last_length, last_hook = get_last_job_variation(exclude_job_id=job_id)

    # Try up to 10 times to avoid the exact previous pair.
    # With 3 lengths × 5 hooks = 15 combinations the chance of exhausting
    # all retries is negligible, but we cap to avoid an infinite loop when
    # the lists have only one entry each.
    picked_length = random.choice(lengths)
    picked_hook = random.choice(hooks)
    for _ in range(10):
        if (picked_length, picked_hook) != (last_length, last_hook):
            break
        picked_length = random.choice(lengths)
        picked_hook = random.choice(hooks)

    logger.info(
        f"[JOB {job_id}] Variation — mode: {var_key}, "
        f"length: {picked_length}s, hook: {picked_hook} "
        f"(previous: {last_length}s / {last_hook})"
    )
    return picked_length, picked_hook


def _create_teaser_job(
    long_job_id: str,
    script_data: dict,
    topic: str,
    story_id: str,
) -> str | None:
    """
    Create the paired short (teaser) job from the teaser_script embedded in
    the long job's Claude response.

    Writes the teaser's script JSON to disk, inserts a DB row, and sets
    story linking fields on the short job.  The long job's story fields are
    set by the caller (generate_script) after this returns.

    Args:
        long_job_id (str):  The long-form job that owns this story.
        script_data (dict): Full parsed Claude response with 'teaser_script'.
        topic (str):        Original story topic for naming the teaser job.
        story_id (str):     Shared identifier linking long + short jobs.

    Returns:
        str | None: The new short job's ID, or None if teaser data is absent.
    """
    teaser = script_data.get('teaser_script', {})
    full_script = (teaser.get('full_script') or '').strip()
    if not full_script:
        logger.warning(
            f"[JOB {long_job_id}] teaser_script missing or empty — skipping teaser job creation"
        )
        return None

    hook_options = teaser.get('hook_options') or []
    word_count   = teaser.get('word_count') or len(full_script.split())
    short_job_id = get_next_job_id()

    # Build the teaser's narration sections.
    # The first hook_option (or first sentence of the script) is the opening hook.
    opening = hook_options[0].strip() if hook_options else full_script.split('.')[0].strip()

    teaser_script_json = {
        'topic':    f"{topic} (Teaser)",
        'bucket':   'reddit',
        'hook_style': 'cliffhanger',
        'sections': {
            'hook': opening,
            'body': full_script,
            'cta':  'Watch the full story — link in bio.',
        },
        'narration':   full_script,
        'hooks':       hook_options,
        'word_count':  word_count,
        'estimated_duration_seconds': round(word_count / 2.5),
        'visual_brief': [],
        'mode':       'reddit',
        'story_role': 'short',
        'story_id':   story_id,
        'job_id':     short_job_id,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    # Write script to disk
    output_dir = Path('output/scripts')
    output_dir.mkdir(parents=True, exist_ok=True)
    short_script_path = output_dir / f"{short_job_id}.json"
    with open(short_script_path, 'w', encoding='utf-8') as f:
        json.dump(teaser_script_json, f, indent=2, ensure_ascii=False)

    file_size_mb = short_script_path.stat().st_size / (1024 * 1024)
    logger.info(
        f"[JOB {long_job_id}] Teaser script written: {short_script_path} "
        f"({file_size_mb:.4f} MB, {word_count} words)"
    )

    # Fetch long job for channel + source text
    long_job   = get_job(long_job_id)
    channel_id = (long_job or {}).get('channel_id', 'engineering_brief')
    source_selftext = (long_job or {}).get('source_selftext', '')

    # Create the short job row
    create_job(
        job_id=short_job_id,
        topic=f"{topic} (Teaser)",
        bucket='reddit',
        hook_style='cliffhanger',
        mode='reddit',
        source='reddit_teaser',
        source_selftext=source_selftext,
        channel_id=channel_id,
    )

    # Set story fields on the short job
    update_job_field(short_job_id, 'story_id',    story_id)
    update_job_field(short_job_id, 'story_role',  'short')
    update_job_field(short_job_id, 'linked_job_id', long_job_id)
    update_job_field(short_job_id, 'script_path', str(short_script_path))
    update_job_field(short_job_id, 'word_count',  word_count)
    update_job_status(short_job_id, 'script_done')

    logger.info(
        f"[JOB {long_job_id}] Teaser job {short_job_id} created "
        f"(story_id={story_id}, ~{round(word_count/2.5)}s)"
    )
    return short_job_id


def generate_script(
    job_id: str,
    topic: str,
    config: dict,
    bucket: str = 'elec',
    hook_style: str = None,
    mode: str = None,
) -> dict:
    """
    Generate a structured video script using the Claude API.

    Two modes:
      - 'standard' (default): the engineering explainer flow — loads
        prompts/script_prompt.txt and produces narration + 8 image prompts.
        On success, advances the job to 'voiced' so voice generation runs.
      - 'reddit': the story rewrite flow — loads prompts/reddit_rewrite_prompt.txt,
        injects the job's stored source story (jobs.source_selftext), and
        produces narration + 5 candidate hooks. On success, advances the job to
        'script_done' (a manual gate) so the owner can pick a hook in the
        dashboard before voice generation runs.

    If mode is None it is read from the job's `mode` column (default 'standard').

    Args:
        job_id (str):      Unique job identifier e.g. '001'.
        topic (str):       Video topic / story title.
        config (dict):     Loaded config.json contents.
        bucket (str):      Content bucket: elec / infra / vehicle / flaw / reddit.
        hook_style (str):  Hook style override. Defaults to config value if None.
        mode (str):        'standard' or 'reddit'. None → read from job record.

    Returns:
        dict: {
            'success': bool,
            'output_path': str,  # path to saved JSON if success
            'error': str         # error message if failed
        }

    Raises:
        ValueError: If topic is empty.
    """
    if not topic or not topic.strip():
        raise ValueError("Topic cannot be empty")

    # Resolve mode (and source story for reddit) from the job record if needed
    job = get_job(job_id)
    if mode is None:
        mode = (job or {}).get('mode') or 'standard'

    if hook_style is None:
        hook_style = 'reddit_story' if mode == 'reddit' else config['script']['hook_style']

    stage_start = time.time()
    logger.info(f"[JOB {job_id}] Starting script_engine (mode={mode}) for topic: '{topic}'")
    logger.debug(
        f"[JOB {job_id}] Config: model={config['script']['model']}, "
        f"temperature={config['script']['temperature']}, "
        f"word_count={config['script']['word_count_target']}, "
        f"bucket={bucket}, hook_style={hook_style}, mode={mode}"
    )

    update_job_status(job_id, 'scripting')

    try:
        # ------------------------------------------------------------------ #
        # Variation pick — choose length and hook style for this job          #
        # ------------------------------------------------------------------ #
        picked_length = None
        picked_hook = hook_style
        if config.get('variation'):
            picked_length, picked_hook = _pick_variation(mode, config, job_id)
            update_job_field(job_id, 'picked_length_seconds', picked_length)
            update_job_field(job_id, 'picked_hook_style', picked_hook)
            # Use the variation-picked hook as the effective hook for this run
            hook_style = picked_hook

        # Load and render the prompt for this mode
        if mode == 'reddit':
            prompt_file = config['script'].get('reddit_prompt_file', 'prompts/reddit_rewrite_prompt.txt')
            selftext = (job or {}).get('source_selftext') or ''
            if not selftext.strip():
                raise ValueError(
                    "Reddit-mode job has no source_selftext to rewrite. "
                    "Was this job created by approving a Reddit candidate?"
                )
            logger.debug(
                f"[JOB {job_id}] Loading reddit prompt: {prompt_file} "
                f"({len(selftext)} chars of source story)"
            )
            template = _load_prompt_template(prompt_file)
            prompt = _build_reddit_prompt(template, topic, selftext, config,
                                          target_length_override=picked_length)
        else:
            prompt_file = config['script']['prompt_file']
            logger.debug(f"[JOB {job_id}] Loading prompt template: {prompt_file}")
            template = _load_prompt_template(prompt_file)
            prompt = _build_prompt(template, topic, bucket, hook_style, config,
                                   target_length_override=picked_length)

        # Initialise Claude client
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY not set in environment")
        client = anthropic.Anthropic(api_key=api_key)

        # Call Claude with retry
        model = config['script']['model']
        max_tokens = config['script']['max_tokens']
        temperature = config['script']['temperature']

        raw_response = _call_claude_with_retry(
            client=client,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            job_id=job_id
        )

        # Parse and validate response
        script_data = _parse_script_response(raw_response, job_id, mode=mode)

        word_count = script_data['word_count']
        if mode == 'reddit':
            logger.info(
                f"[JOB {job_id}] Reddit script parsed — "
                f"word_count: {word_count}, hooks: {len(script_data.get('hooks', []))}"
            )
        else:
            logger.info(
                f"[JOB {job_id}] Script parsed — "
                f"word_count: {word_count}, visual_prompts: {len(script_data['visual_brief'])}"
            )

        # Enrich with pipeline metadata
        script_data['job_id'] = job_id
        script_data['mode'] = mode
        script_data['generated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if 'estimated_duration_seconds' not in script_data:
            script_data['estimated_duration_seconds'] = round(word_count / 2.5)

        # Save to disk
        output_dir = Path('output/scripts')
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{job_id}.json"

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(script_data, f, indent=2, ensure_ascii=False)

        file_size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info(f"[JOB {job_id}] File created: {output_path} ({file_size_mb:.4f} MB)")

        # Update database
        update_job_field(job_id, 'script_path', str(output_path))
        update_job_field(job_id, 'word_count', word_count)

        # Reddit jobs stop at a manual hook-selection gate; standard jobs flow on.
        if mode == 'reddit':
            update_job_status(job_id, 'script_done')
            logger.info(f"[JOB {job_id}] Reddit script ready — awaiting hook selection (status: script_done)")

            # Create the paired teaser (short) job if Claude included teaser_script
            teaser_job_id = None
            if 'teaser_script' in script_data:
                story_id      = f"story_{job_id}"
                teaser_job_id = _create_teaser_job(job_id, script_data, topic, story_id)
                if teaser_job_id:
                    # Link story fields on the long job
                    update_job_field(job_id, 'story_id',      story_id)
                    update_job_field(job_id, 'story_role',    'long')
                    update_job_field(job_id, 'linked_job_id', teaser_job_id)
        else:
            update_job_status(job_id, 'voiced')  # next stage is voice_engine

        elapsed = time.time() - stage_start
        logger.info(f"[JOB {job_id}] script_engine COMPLETED in {elapsed:.1f}s")

        return {'success': True, 'output_path': str(output_path)}

    except Exception as e:
        elapsed = time.time() - stage_start
        logger.error(f"[JOB {job_id}] script_engine FAILED: {str(e)}", exc_info=True)
        update_job_status(job_id, 'failed', error_module='script_engine', error_message=str(e))
        return {'success': False, 'error': str(e)}
