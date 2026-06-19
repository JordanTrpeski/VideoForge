"""
image_engine.py
===============
Stage 3 of the VideoForge pipeline. Generates portrait images from the
visual_brief prompts in the script JSON using the Leonardo.AI API.

For each prompt the engine:
  1. POSTs a generation request → receives a generationId
  2. Polls GET /generations/{id} until status is COMPLETE (or times out)
  3. Downloads the first generated image to output/images/NNN/img_NN.png
After all images are downloaded, verifies all 8 exist before marking done.

Input:  job_id, config dict — reads output/scripts/NNN.json
Output: output/images/NNN/img_01.png … img_08.png
Logs:   logs/image_engine.log

Dependencies:
    - requests (Leonardo.AI HTTP calls + image download)
    - Pillow  (image verification)
    - python-dotenv (env loading)

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import json
import os
import time
from pathlib import Path

# 2. Third-party libraries
import requests
from dotenv import load_dotenv

# 3. Local modules
from database import update_job_status, update_job_field
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger('image_engine')

# ---------------------------------------------------------------------------
# Leonardo.AI API constants
# ---------------------------------------------------------------------------

LEONARDO_BASE_URL = "https://cloud.leonardo.ai/api/rest/v1"

# Map human-readable model names (used in config.json) to Leonardo model UUIDs.
# Verify these at https://docs.leonardo.ai/reference/getmodels before first run.
# The user can also paste a UUID directly into config.json visuals.model.
LEONARDO_MODEL_IDS = {
    'leonardo-diffusion-xl':  '1e60896f-3c26-4296-8ecc-53e2afecc132',
    'leonardo-phoenix':       '6b645e3a-d64f-4341-a6d8-7a3690fbf042',
    'leonardo-creative':      'cd2b2a15-9760-4174-a5ff-4d2925057376',
    'leonardo-select':        'e71a1c2f-4f80-4800-934f-2c68979d1cc6',
    'leonardo-vision-xl':     'aa77f04e-3eec-4034-9c07-d0f619684628',
    'phoenix-1-0':            'de7d3faf-762f-48e0-b3b7-9d0ac3a3fcf3',
}

# Placeholder value written into config.json — means "not yet configured"
_PRESET_PLACEHOLDER = 'SET_AFTER_TESTING'

# Poll every N seconds while waiting for a generation to complete
POLL_INTERVAL_SECONDS = 4

# Maximum time to wait per image before giving up
POLL_TIMEOUT_SECONDS = 180


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
        ValueError: If visual_brief is missing or empty.
    """
    script_path = Path(f'output/scripts/{job_id}.json')
    logger.debug(f"[JOB {job_id}] Loading script: {script_path}")

    if not script_path.exists():
        raise FileNotFoundError(
            f"Script file not found: {script_path}. Run generate-script first."
        )

    with open(script_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    visual_brief = data.get('visual_brief', [])
    if not visual_brief:
        raise ValueError(f"Script JSON has no visual_brief prompts: {script_path}")

    return data


def _resolve_model_id(model_name: str) -> str:
    """
    Convert a model name or UUID string to a Leonardo.AI model UUID.

    If the value already looks like a UUID (contains hyphens and is ≥32 chars),
    it is returned as-is so users can paste UUIDs directly into config.json.

    Args:
        model_name (str): Name from config e.g. 'leonardo-diffusion-xl', or a UUID.

    Returns:
        str: Leonardo model UUID.

    Raises:
        ValueError: If the name is not in the known map and doesn't look like a UUID.
    """
    # If it already looks like a UUID, trust it
    if len(model_name) >= 32 and '-' in model_name:
        return model_name

    model_id = LEONARDO_MODEL_IDS.get(model_name.lower())
    if not model_id:
        raise ValueError(
            f"Unknown Leonardo model name: '{model_name}'. "
            f"Either use a known name {list(LEONARDO_MODEL_IDS.keys())} "
            f"or paste the UUID directly into config.json visuals.model."
        )
    return model_id


def _build_generation_payload(prompt: str, config: dict) -> dict:
    """
    Build the Leonardo.AI generation request payload for one image.

    Appends the negative_prompt from config to every request.
    Includes presetStyle only when style_preset_id is set to a real value.

    Args:
        prompt (str):   Visual prompt string from visual_brief.
        config (dict):  Loaded config.json.

    Returns:
        dict: JSON-serialisable payload for POST /generations.
    """
    vis = config['visuals']
    model_id = _resolve_model_id(vis['model'])

    payload = {
        "modelId":            model_id,
        "prompt":             prompt,
        "negative_prompt":    vis['negative_prompt'],
        "width":              vis['width'],
        "height":             vis['height'],
        "guidance_scale":     vis['guidance_scale'],
        "num_inference_steps": vis['num_inference_steps'],
        "num_images":         vis['num_images'],
    }

    # Only include presetStyle if it has been configured (not the placeholder)
    style = vis.get('style_preset_id', _PRESET_PLACEHOLDER)
    if style and style != _PRESET_PLACEHOLDER:
        payload['presetStyle'] = style.upper()

    return payload


def _create_generation(
    payload: dict,
    api_key: str,
    job_id: str,
    img_num: int,
    max_retries: int = 3,
    backoff_seconds: float = 5.0
) -> str:
    """
    POST a generation request to Leonardo.AI and return the generationId.

    Args:
        payload (dict):         Generation request body.
        api_key (str):          Leonardo.AI API key.
        job_id (str):           Job identifier for log context.
        img_num (int):          1-based image number for log context.
        max_retries (int):      Maximum attempts on transient errors.
        backoff_seconds (float): Base wait between retries.

    Returns:
        str: generationId UUID string.

    Raises:
        Exception: If the request fails after all retries.
    """
    url = f"{LEONARDO_BASE_URL}/generations"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(
                f"[JOB {job_id}] Calling Leonardo.AI API — "
                f"image: {img_num}/8, model: {payload['modelId']}, "
                f"attempt: {attempt}/{max_retries}"
            )
            call_start = time.time()
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            elapsed = time.time() - call_start

            if response.status_code == 200:
                data = response.json()
                generation_id = (
                    data.get('sdGenerationJob', {}).get('generationId')
                    or data.get('generationId')
                )
                if not generation_id:
                    raise ValueError(
                        f"Leonardo.AI response missing generationId: {data}"
                    )
                logger.info(
                    f"[JOB {job_id}] Generation created — "
                    f"image: {img_num}/8, id: {generation_id}, "
                    f"response time: {elapsed:.2f}s"
                )
                return generation_id

            elif response.status_code == 429:
                wait = int(response.headers.get('Retry-After', backoff_seconds * (2 ** (attempt - 1))))
                logger.warning(
                    f"[JOB {job_id}] Rate limit hit — waiting {wait}s "
                    f"(attempt {attempt}/{max_retries})"
                )
                if attempt < max_retries:
                    time.sleep(wait)

            elif response.status_code >= 500:
                wait = backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    f"[JOB {job_id}] Leonardo.AI server error {response.status_code} — "
                    f"waiting {wait:.0f}s (attempt {attempt}/{max_retries})"
                )
                if attempt < max_retries:
                    time.sleep(wait)

            else:
                raise Exception(
                    f"Leonardo.AI API error {response.status_code}: {response.text[:300]}"
                )

        except requests.exceptions.Timeout:
            wait = backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                f"[JOB {job_id}] Leonardo.AI request timed out — "
                f"waiting {wait:.0f}s (attempt {attempt}/{max_retries})"
            )
            if attempt < max_retries:
                time.sleep(wait)

        except requests.exceptions.ConnectionError as e:
            wait = backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                f"[JOB {job_id}] Leonardo.AI connection error — "
                f"waiting {wait:.0f}s (attempt {attempt}/{max_retries}): {e}"
            )
            if attempt < max_retries:
                time.sleep(wait)

    raise Exception(
        f"Leonardo.AI generation request failed after {max_retries} attempts "
        f"for image {img_num}"
    )


def _poll_generation(
    generation_id: str,
    api_key: str,
    job_id: str,
    img_num: int
) -> list:
    """
    Poll GET /generations/{id} until status is COMPLETE and return image URLs.

    Args:
        generation_id (str): Generation UUID from _create_generation.
        api_key (str):       Leonardo.AI API key.
        job_id (str):        Job identifier for log context.
        img_num (int):       1-based image number for log context.

    Returns:
        list[str]: List of image download URLs (at least one).

    Raises:
        TimeoutError: If the generation does not complete within POLL_TIMEOUT_SECONDS.
        RuntimeError: If the generation status is FAILED.
    """
    url = f"{LEONARDO_BASE_URL}/generations/{generation_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    poll_start = time.time()
    poll_count = 0

    while True:
        elapsed_total = time.time() - poll_start
        if elapsed_total > POLL_TIMEOUT_SECONDS:
            raise TimeoutError(
                f"Leonardo.AI generation {generation_id} did not complete "
                f"within {POLL_TIMEOUT_SECONDS}s (image {img_num}/8)"
            )

        time.sleep(POLL_INTERVAL_SECONDS)
        poll_count += 1

        try:
            response = requests.get(url, headers=headers, timeout=15)
        except requests.exceptions.RequestException as e:
            logger.warning(f"[JOB {job_id}] Poll request failed (will retry): {e}")
            continue

        if response.status_code != 200:
            logger.warning(
                f"[JOB {job_id}] Poll returned HTTP {response.status_code} — retrying"
            )
            continue

        data = response.json()
        gen = data.get('generations_by_pk') or data.get('generation', {})
        status = gen.get('status', 'PENDING')

        logger.debug(
            f"[JOB {job_id}] Poll #{poll_count} — "
            f"image: {img_num}/8, status: {status}, "
            f"elapsed: {elapsed_total:.0f}s"
        )

        if status == 'COMPLETE':
            images = gen.get('generated_images', [])
            if not images:
                raise RuntimeError(
                    f"Generation {generation_id} COMPLETE but no generated_images in response"
                )
            urls = [img['url'] for img in images if img.get('url')]
            logger.info(
                f"[JOB {job_id}] Generation COMPLETE — "
                f"image: {img_num}/8, "
                f"poll time: {elapsed_total:.1f}s"
            )
            if elapsed_total > 30:
                logger.warning(
                    f"[JOB {job_id}] Image {img_num} took {elapsed_total:.1f}s — "
                    "Leonardo.AI may be slow"
                )
            return urls

        elif status == 'FAILED':
            raise RuntimeError(
                f"Leonardo.AI generation {generation_id} FAILED (image {img_num}/8)"
            )

        # PENDING / PROCESSING — keep polling


def _download_image(url: str, output_path: Path, job_id: str, img_num: int) -> None:
    """
    Download an image from a URL and save it to disk.

    Args:
        url (str):          Direct image URL from Leonardo.AI.
        output_path (Path): Destination file path.
        job_id (str):       Job identifier for log context.
        img_num (int):      1-based image number for log context.

    Raises:
        requests.exceptions.RequestException: On network failure.
        ValueError: If the response body is empty.
    """
    logger.debug(f"[JOB {job_id}] Downloading image {img_num}/8 from Leonardo CDN")
    response = requests.get(url, timeout=60)
    response.raise_for_status()

    if not response.content:
        raise ValueError(f"Empty response body when downloading image {img_num}")

    output_path.write_bytes(response.content)
    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(f"[JOB {job_id}] File created: {output_path} ({size_mb:.2f} MB)")


def _verify_images(images_dir: Path, expected_count: int, job_id: str) -> None:
    """
    Confirm that all expected image files exist and are non-empty.

    Args:
        images_dir (Path):   Directory containing img_01.png … img_NN.png.
        expected_count (int): Number of images that must be present.
        job_id (str):         Job identifier for log context.

    Raises:
        FileNotFoundError: If any image file is missing.
        ValueError: If any image file is empty (0 bytes).
    """
    logger.info(f"[JOB {job_id}] Verifying {expected_count} images in {images_dir}")
    for i in range(1, expected_count + 1):
        path = images_dir / f"img_{i:02d}.png"
        if not path.exists():
            raise FileNotFoundError(
                f"Expected image not found: {path} (image {i}/{expected_count})"
            )
        if path.stat().st_size == 0:
            raise ValueError(f"Image file is empty (0 bytes): {path}")
    logger.info(f"[JOB {job_id}] All {expected_count} images verified OK")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_images(job_id: str, config: dict) -> dict:
    """
    Generate all visual_brief images for a job using Leonardo.AI.

    For each of the 8 prompts in the script JSON:
      - Appends negative_prompt and style_preset_id from config
      - Calls Leonardo.AI to create a generation
      - Polls until the generation is complete
      - Downloads the image to output/images/NNN/img_NN.png

    Verifies all images exist before updating the database.

    Args:
        job_id (str):  Unique job identifier e.g. '001'.
        config (dict): Loaded config.json contents.

    Returns:
        dict: {
            'success': bool,
            'images_dir': str,   # path to output/images/NNN/ if success
            'count': int,        # number of images downloaded
            'skipped': bool,     # True if key not configured
            'error': str         # error message if failed or skipped
        }
    """
    stage_start = time.time()
    logger.info(f"[JOB {job_id}] Starting image_engine")

    # ------------------------------------------------------------------ #
    # Guard: background-loop visual mode skips image generation entirely  #
    # (zero Leonardo.AI calls — visuals come from a background clip in     #
    # assembly_engine). Used by Reddit Stories and any clip-based content. #
    # ------------------------------------------------------------------ #
    visual_mode = config.get('pipeline', {}).get('visual_mode', 'images')
    if visual_mode == 'background_loop':
        msg = "visual_mode is 'background_loop' — skipping image generation (no Leonardo.AI calls)."
        logger.info(f"[JOB {job_id}] {msg}")
        update_job_status(job_id, 'assembling')
        return {'success': True, 'skipped': True, 'images_dir': None, 'count': 0, 'error': msg}

    # ------------------------------------------------------------------ #
    # Guard: Leonardo.AI API key must be present                          #
    # ------------------------------------------------------------------ #
    api_key = os.getenv('LEONARDO_API_KEY', '').strip()
    if not api_key:
        msg = (
            "LEONARDO_API_KEY is not set in .env. "
            "Add your Leonardo.AI key when you are ready for Phase 4. "
            "Skipping image generation."
        )
        logger.warning(f"[JOB {job_id}] {msg}")
        return {'success': False, 'skipped': True, 'error': msg}

    try:
        # Load script and extract prompts
        script = _load_script(job_id)
        visual_brief = script['visual_brief']
        expected_count = len(visual_brief)

        logger.info(
            f"[JOB {job_id}] Loaded {expected_count} visual prompts — "
            f"topic: '{script.get('topic')}'"
        )
        logger.debug(
            f"[JOB {job_id}] Config: model={config['visuals']['model']}, "
            f"size={config['visuals']['width']}x{config['visuals']['height']}, "
            f"guidance={config['visuals']['guidance_scale']}, "
            f"steps={config['visuals']['num_inference_steps']}, "
            f"style_preset={config['visuals']['style_preset_id']}"
        )

        # Prepare output directory
        images_dir = Path(f'output/images/{job_id}')
        images_dir.mkdir(parents=True, exist_ok=True)

        # ---------------------------------------------------------------- #
        # Generate each image sequentially                                  #
        # ---------------------------------------------------------------- #
        for i, prompt in enumerate(visual_brief, start=1):
            img_label = f"img_{i:02d}"
            output_path = images_dir / f"{img_label}.png"

            # Skip already-downloaded images (allows re-running after partial failure)
            if output_path.exists() and output_path.stat().st_size > 0:
                logger.info(
                    f"[JOB {job_id}] Image {i}/{expected_count} already exists — skipping"
                )
                continue

            logger.debug(
                f"[JOB {job_id}] Prompt {i}/{expected_count}: "
                f"{prompt[:100]}{'...' if len(prompt) > 100 else ''}"
            )

            # Build payload (negative_prompt and style appended here)
            payload = _build_generation_payload(prompt, config)

            # Step 1: Create generation
            generation_id = _create_generation(
                payload=payload,
                api_key=api_key,
                job_id=job_id,
                img_num=i
            )

            # Step 2: Poll until complete
            image_urls = _poll_generation(
                generation_id=generation_id,
                api_key=api_key,
                job_id=job_id,
                img_num=i
            )

            # Block B — record one Leonardo image request per prompt
            try:
                from utils.usage_tracker import track as _usage_track
                from database import get_job as _get_job
                _ch = (_get_job(job_id) or {}).get('channel_id')
                _usage_track('leonardo', 'generate', units=1,
                             channel_id=_ch, job_id=job_id, config=config)
            except Exception:
                pass

            # Step 3: Download first image
            _download_image(
                url=image_urls[0],
                output_path=output_path,
                job_id=job_id,
                img_num=i
            )

        # ---------------------------------------------------------------- #
        # Verify all images exist before marking done                       #
        # ---------------------------------------------------------------- #
        _verify_images(images_dir, expected_count, job_id)

        # Update database
        update_job_field(job_id, 'images_dir', str(images_dir))
        update_job_status(job_id, 'assembling')

        elapsed = time.time() - stage_start
        logger.info(f"[JOB {job_id}] image_engine COMPLETED in {elapsed:.1f}s")

        return {
            'success': True,
            'images_dir': str(images_dir),
            'count': expected_count,
        }

    except Exception as e:
        elapsed = time.time() - stage_start
        logger.error(f"[JOB {job_id}] image_engine FAILED: {str(e)}", exc_info=True)
        update_job_status(job_id, 'failed', error_module='image_engine', error_message=str(e))
        return {'success': False, 'error': str(e)}
