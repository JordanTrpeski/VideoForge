"""
prompt_exporter.py
==================
Phase 14 Block 7 — Export / import script flow.

Enables manual escalation of script generation to browser-only stronger models
(Claude.ai with Opus 4.7, ChatGPT with GPT-5 Pro) using the user's existing
subscriptions. The exporter resolves a job's full script-generation prompt
into a single self-contained string. The importer reads back a JSON response
from one of those chats and advances the job as if the API path had succeeded.

Input:  job_id
Output: rendered prompt string (export) or validated script JSON written to
        output/scripts/<job_id>.json (import)
Logs:   logs/prompt_exporter.log
"""

# 1. Standard library
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

# 2. Third-party
from dotenv import load_dotenv

# 3. Local
from database import get_job, update_job_field, update_job_status
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger('prompt_exporter')


REQUIRED_IMPORT_KEYS = {
    'topic', 'sections', 'narration', 'hooks', 'word_count',
    'primary_title', 'title_alternates',
    'primary_thumbnail_text', 'thumbnail_text_alternates',
    'first_30_seconds', 'symbolic_object',
}


def resolve_prompt(job_id: str, config: dict) -> str:
    """
    Build the fully resolved script-generation prompt for a job. Mirrors the
    rendering done inside script_engine.generate_script — variable
    substitution against config + job context.

    Args:
        job_id (str): Owning job id.
        config (dict): Merged channel config.

    Returns:
        str: Fully rendered prompt, ready to paste into a browser chat.
    """
    job = get_job(job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")

    mode = (job.get('mode') or 'standard').lower()
    topic = job.get('topic') or ''

    if mode == 'reddit':
        prompt_file = config['script'].get(
            'reddit_prompt_file', 'prompts/reddit_generation_prompt.txt'
        )
    else:
        prompt_file = config['script']['prompt_file']

    template_path = Path(prompt_file)
    if not template_path.exists():
        raise FileNotFoundError(
            f"Prompt template not found at {prompt_file}"
        )
    template = template_path.read_text(encoding='utf-8')

    # Pick length / hook from the job's variation pick if set, else config.
    target_length = (
        job.get('picked_length_seconds')
        or config.get('channel', {}).get('target_length_seconds')
        or 90
    )
    word_count_target = round(target_length * 2.5)

    content = config.get('content', {}) or {}
    emotional_lane = content.get('emotional_lane', 'betrayal_revenge')
    subgenre_weighting = content.get(
        'subgenre_weighting',
        {
            'family_inheritance_property': 0.50,
            'romantic_loyalty': 0.30,
            'legal_proof': 0.20,
        },
    )
    target_wpm = int((config.get('voice') or {}).get('target_wpm', 190))

    variables = {
        'title': topic,
        'topic': topic,
        'bucket': job.get('bucket') or '',
        'hook_style': job.get('picked_hook_style') or job.get('hook_style') or '',
        'selftext': job.get('source_selftext') or '',
        'word_count_target': word_count_target,
        'word_count_min': round(word_count_target * 0.92),
        'word_count_max': round(word_count_target * 1.08),
        'target_length_seconds': target_length,
        'emotional_lane': emotional_lane,
        'subgenre_weighting': json.dumps(subgenre_weighting, ensure_ascii=False),
        'target_wpm': target_wpm,
        'images_to_generate': config.get('script', {}).get('images_to_generate', 8),
    }
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace('{' + key + '}', str(value))
    return rendered


def import_script_from_payload(
    job_id: str,
    payload: dict,
    reported_model: str = 'external_manual',
) -> dict:
    """
    Validate a JSON payload returned by a browser chat and persist it as if
    the normal script engine had produced it. Raises ValueError on missing
    or malformed fields.

    Args:
        job_id (str): Owning job id.
        payload (dict): Parsed JSON returned by the model.
        reported_model (str): Free-form identifier of which external model
                              the user used (e.g. 'claude-opus-4.7-browser').

    Returns:
        dict: { 'success': True, 'script_path': str }
    """
    if not isinstance(payload, dict):
        raise ValueError("import payload must be a JSON object")

    missing = REQUIRED_IMPORT_KEYS - set(payload.keys())
    if missing:
        raise ValueError(
            f"Imported script JSON is missing required fields: "
            f"{sorted(missing)}"
        )

    # Light structural checks on the most fragile fields.
    if not isinstance(payload.get('sections'), dict):
        raise ValueError("'sections' must be a JSON object")
    for k in ('hook', 'body', 'cta'):
        if k not in payload['sections']:
            raise ValueError(f"sections.{k} is required")
    if not isinstance(payload.get('hooks'), list) or not payload['hooks']:
        raise ValueError("'hooks' must be a non-empty array")
    if not isinstance(payload.get('title_alternates'), list):
        raise ValueError("'title_alternates' must be an array")
    if not isinstance(payload.get('thumbnail_text_alternates'), list):
        raise ValueError("'thumbnail_text_alternates' must be an array")

    # Enrich + write to disk where script_engine would have.
    payload = dict(payload)
    payload['job_id'] = job_id
    payload.setdefault('mode', 'reddit')
    payload['generated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if 'estimated_duration_seconds' not in payload:
        payload['estimated_duration_seconds'] = round(
            int(payload.get('word_count') or 0) / 2.5
        )

    out_dir = Path('output/scripts')
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{job_id}.json"
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    # Record the external-model usage for production_evidence.json (Block 5).
    ext_dir = Path('output/external_model')
    ext_dir.mkdir(parents=True, exist_ok=True)
    with open(ext_dir / f"{job_id}.json", 'w', encoding='utf-8') as f:
        json.dump({
            'reported_model': reported_model,
            'imported_at':    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }, f, indent=2)

    update_job_field(job_id, 'script_path', str(out_path))
    update_job_field(job_id, 'word_count', int(payload.get('word_count') or 0))
    update_job_status(job_id, 'script_done')

    logger.info(
        f"[JOB {job_id}] External script imported from {reported_model} — "
        f"{out_path}"
    )
    return {'success': True, 'script_path': str(out_path)}
