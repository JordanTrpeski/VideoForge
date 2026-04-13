"""
voice_engine.py
===============
Stage 2 of the VideoForge pipeline. Converts the script narration into speech
using the ElevenLabs API and saves a final MP3 file.

When chunk_by_section=true (default), the narration is split into three
sections (hook / body / cta) and each is sent as a separate API call. The
resulting chunks are concatenated into a single output file via pydub.

Input:  job_id, config dict — reads output/scripts/NNN.json
Output: output/audio/NNN.mp3  (+ NNN_hook.mp3, NNN_body.mp3, NNN_cta.mp3 as intermediates)
Logs:   logs/voice_engine.log

Dependencies:
    - requests (ElevenLabs HTTP calls)
    - pydub (MP3 concatenation + duration measurement — requires ffmpeg on PATH)
    - python-dotenv (env loading)

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import json
import os
import subprocess
import time
from pathlib import Path

# 2. Third-party libraries
import requests
from dotenv import load_dotenv

# 3. Local modules
from database import update_job_status, update_job_field, get_job
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger('voice_engine')

ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
ELEVENLABS_MODELS = {
    'default': 'eleven_turbo_v2_5',
    'multilingual': 'eleven_multilingual_v2',
    'turbo': 'eleven_turbo_v2_5',
    'flash': 'eleven_flash_v2_5',
}

# Section order must match the script JSON keys and the intended narration flow
SECTION_ORDER = ['hook', 'body', 'cta']


def _get_audio_duration(path: Path, job_id: str) -> float:
    """
    Return the duration of an audio file in seconds using ffprobe.

    Args:
        path (Path):   Path to the audio file.
        job_id (str):  Job identifier for log context.

    Returns:
        float: Duration in seconds.

    Raises:
        RuntimeError: If ffprobe is not on PATH or returns an error.
    """
    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'quiet',
                '-show_entries', 'format=duration',
                '-of', 'csv=p=0',
                str(path),
            ],
            capture_output=True, text=True, check=True
        )
        return float(result.stdout.strip())
    except FileNotFoundError:
        raise RuntimeError(
            "ffprobe not found on PATH. "
            "ffprobe ships with ffmpeg — install ffmpeg from https://ffmpeg.org/download.html"
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffprobe failed for {path}: {e.stderr.strip()}")


def _load_script(job_id: str) -> dict:
    """
    Read and parse the script JSON for the given job.

    Args:
        job_id (str): Job identifier e.g. '001'.

    Returns:
        dict: Parsed script data from output/scripts/NNN.json.

    Raises:
        FileNotFoundError: If the script file does not exist.
        ValueError: If the file cannot be parsed or is missing required keys.
    """
    script_path = Path(f'output/scripts/{job_id}.json')
    logger.debug(f"[JOB {job_id}] Loading script: {script_path}")

    if not script_path.exists():
        raise FileNotFoundError(
            f"Script file not found: {script_path}. "
            f"Run generate-script first."
        )

    with open(script_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if 'sections' not in data or 'narration' not in data:
        raise ValueError(f"Script JSON is missing 'sections' or 'narration' keys: {script_path}")

    return data


def _build_tts_payload(text: str, config: dict) -> dict:
    """
    Build the ElevenLabs TTS request body from config values.

    Args:
        text (str):   Text to synthesise.
        config (dict): Loaded config.json.

    Returns:
        dict: JSON-serialisable request body.
    """
    voice_cfg = config['voice']
    return {
        "text": text,
        "model_id": ELEVENLABS_MODELS['default'],
        "voice_settings": {
            "stability": voice_cfg['stability'],
            "similarity_boost": voice_cfg['similarity_boost'],
            "style": voice_cfg['style_exaggeration'],
            "use_speaker_boost": True,
        }
    }


def _call_elevenlabs_with_retry(
    text: str,
    voice_id: str,
    api_key: str,
    config: dict,
    job_id: str,
    chunk_label: str,
    max_retries: int = 3,
    backoff_seconds: float = 5.0
) -> bytes:
    """
    Call the ElevenLabs TTS endpoint and return raw MP3 bytes.
    Retries on rate-limit (429) and server errors (5xx) with exponential backoff.

    Args:
        text (str):           Text to synthesise.
        voice_id (str):       ElevenLabs voice ID.
        api_key (str):        ElevenLabs API key.
        config (dict):        Loaded config.json.
        job_id (str):         Job identifier for log context.
        chunk_label (str):    Chunk name for log context e.g. 'hook'.
        max_retries (int):    Maximum number of attempts.
        backoff_seconds (float): Base wait time between retries.

    Returns:
        bytes: Raw MP3 audio data.

    Raises:
        Exception: If all retries are exhausted or a non-retryable error occurs.
    """
    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id)
    output_format = config['voice'].get('output_format', 'mp3_44100_128')
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    params = {"output_format": output_format}
    payload = _build_tts_payload(text, config)

    logger.info(
        f"[JOB {job_id}] Calling ElevenLabs API — "
        f"chunk: {chunk_label}, chars: {len(text)}, voice_id: {voice_id}"
    )
    logger.debug(
        f"[JOB {job_id}] Config: stability={payload['voice_settings']['stability']}, "
        f"similarity={payload['voice_settings']['similarity_boost']}, "
        f"style={payload['voice_settings']['style']}, "
        f"output_format={output_format}"
    )

    for attempt in range(1, max_retries + 1):
        try:
            call_start = time.time()
            response = requests.post(
                url, headers=headers, params=params,
                json=payload, timeout=60
            )
            elapsed = time.time() - call_start

            if response.status_code == 200:
                audio_bytes = response.content
                size_kb = len(audio_bytes) / 1024
                logger.info(
                    f"[JOB {job_id}] ElevenLabs API call succeeded — "
                    f"chunk: {chunk_label}, response time: {elapsed:.2f}s, "
                    f"audio size: {size_kb:.1f} KB"
                )
                return audio_bytes

            elif response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', backoff_seconds * (2 ** (attempt - 1))))
                logger.warning(
                    f"[JOB {job_id}] Rate limit hit — waiting {retry_after}s "
                    f"(attempt {attempt}/{max_retries})"
                )
                if attempt < max_retries:
                    time.sleep(retry_after)

            elif response.status_code >= 500:
                wait = backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    f"[JOB {job_id}] ElevenLabs server error {response.status_code} — "
                    f"waiting {wait:.0f}s (attempt {attempt}/{max_retries})"
                )
                if attempt < max_retries:
                    time.sleep(wait)

            else:
                # 4xx errors (except 429) are not retryable
                raise Exception(
                    f"ElevenLabs API error {response.status_code}: {response.text[:200]}"
                )

        except requests.exceptions.Timeout:
            wait = backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                f"[JOB {job_id}] ElevenLabs request timed out — "
                f"waiting {wait:.0f}s (attempt {attempt}/{max_retries})"
            )
            if attempt < max_retries:
                time.sleep(wait)

        except requests.exceptions.ConnectionError as e:
            wait = backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                f"[JOB {job_id}] ElevenLabs connection error — "
                f"waiting {wait:.0f}s (attempt {attempt}/{max_retries}): {e}"
            )
            if attempt < max_retries:
                time.sleep(wait)

    raise Exception(f"ElevenLabs API call failed after {max_retries} attempts for chunk '{chunk_label}'")


def _save_chunk(audio_bytes: bytes, output_path: Path, job_id: str, chunk_label: str) -> None:
    """
    Write raw audio bytes to a file on disk.

    Args:
        audio_bytes (bytes): Raw MP3 data from ElevenLabs.
        output_path (Path):  Destination file path.
        job_id (str):        Job identifier for log context.
        chunk_label (str):   Chunk name for log context.
    """
    output_path.write_bytes(audio_bytes)
    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(f"[JOB {job_id}] File created: {output_path} ({size_mb:.3f} MB) [chunk: {chunk_label}]")


def _concatenate_chunks(chunk_paths: list, output_path: Path, job_id: str) -> float:
    """
    Concatenate a list of MP3 chunk files into one output file using ffmpeg.
    Returns the total duration in seconds measured via ffprobe.

    Args:
        chunk_paths (list[Path]): Ordered list of chunk MP3 paths.
        output_path (Path):       Destination path for the combined MP3.
        job_id (str):             Job identifier for log context.

    Returns:
        float: Total audio duration in seconds.

    Raises:
        RuntimeError: If ffmpeg or ffprobe is not available or fails.
    """
    logger.info(f"[JOB {job_id}] Concatenating {len(chunk_paths)} audio chunks via ffmpeg")

    if len(chunk_paths) == 1:
        # Nothing to concatenate — just copy
        import shutil
        shutil.copy2(str(chunk_paths[0]), str(output_path))
        logger.debug(f"[JOB {job_id}] Single chunk — copied directly to {output_path}")
    else:
        # Write a concat list file for ffmpeg
        concat_list_path = output_path.parent / f"{output_path.stem}_concat_list.txt"
        with open(concat_list_path, 'w', encoding='utf-8') as f:
            for cp in chunk_paths:
                # ffmpeg concat demuxer requires forward-slash paths in the list file
                f.write(f"file '{cp.resolve().as_posix()}'\n")

        try:
            subprocess.run(
                [
                    'ffmpeg', '-y',
                    '-f', 'concat', '-safe', '0',
                    '-i', str(concat_list_path),
                    '-c:a', 'libmp3lame', '-b:a', '128k',
                    str(output_path),
                ],
                capture_output=True, text=True, check=True
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"ffmpeg concat failed: {e.stderr[-500:]}")
        finally:
            concat_list_path.unlink(missing_ok=True)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(f"[JOB {job_id}] File created: {output_path} ({size_mb:.3f} MB)")

    duration_seconds = _get_audio_duration(output_path, job_id)
    logger.info(f"[JOB {job_id}] Audio duration: {duration_seconds:.2f}s")
    return duration_seconds


def generate_voice(job_id: str, config: dict) -> dict:
    """
    Convert the script narration to speech using ElevenLabs and save as MP3.

    Reads output/scripts/NNN.json, synthesises each section separately when
    chunk_by_section=true, concatenates the results, and saves the final file
    to output/audio/NNN.mp3. Duration is recorded in the database.

    Args:
        job_id (str):  Unique job identifier e.g. '001'.
        config (dict): Loaded config.json contents.

    Returns:
        dict: {
            'success': bool,
            'output_path': str,       # path to NNN.mp3 if success
            'duration_seconds': float,# audio length if success
            'skipped': bool,          # True if key not configured (not a hard failure)
            'error': str              # error message if failed or skipped
        }
    """
    stage_start = time.time()
    logger.info(f"[JOB {job_id}] Starting voice_engine")

    # ------------------------------------------------------------------ #
    # Guard: ElevenLabs API key must be present to proceed               #
    # ------------------------------------------------------------------ #
    api_key = os.getenv('ELEVENLABS_API_KEY', '').strip()
    if not api_key:
        msg = (
            "ELEVENLABS_API_KEY is not set in .env. "
            "Add your ElevenLabs key when you are ready for Phase 3. "
            "Skipping voice generation."
        )
        logger.warning(f"[JOB {job_id}] {msg}")
        return {'success': False, 'skipped': True, 'error': msg}

    # ------------------------------------------------------------------ #
    # Guard: voice ID must be configured                                  #
    # ------------------------------------------------------------------ #
    voice_id = os.getenv('ELEVENLABS_VOICE_ID', '').strip()
    if not voice_id or voice_id == 'SET_IN_ENV':
        voice_id = config['voice'].get('voice_id', '')
    if not voice_id or voice_id in ('SET_IN_ENV', ''):
        msg = (
            "ELEVENLABS_VOICE_ID is not set. "
            "Set it in .env after choosing a voice in the ElevenLabs Voice Library."
        )
        logger.warning(f"[JOB {job_id}] {msg}")
        return {'success': False, 'skipped': True, 'error': msg}

    try:
        # Load script
        script = _load_script(job_id)
        logger.info(
            f"[JOB {job_id}] Script loaded — "
            f"word_count: {script.get('word_count')}, "
            f"topic: '{script.get('topic')}'"
        )

        # Prepare output directory
        output_dir = Path('output/audio')
        output_dir.mkdir(parents=True, exist_ok=True)

        chunk_by_section = config['voice'].get('chunk_by_section', True)

        if chunk_by_section:
            # ---------------------------------------------------------- #
            # Chunked mode: one API call per section                       #
            # ---------------------------------------------------------- #
            sections = script['sections']
            chunk_paths = []

            for section_key in SECTION_ORDER:
                text = sections.get(section_key, '').strip()
                if not text:
                    logger.warning(f"[JOB {job_id}] Section '{section_key}' is empty — skipping chunk")
                    continue

                audio_bytes = _call_elevenlabs_with_retry(
                    text=text,
                    voice_id=voice_id,
                    api_key=api_key,
                    config=config,
                    job_id=job_id,
                    chunk_label=section_key
                )

                chunk_path = output_dir / f"{job_id}_{section_key}.mp3"
                _save_chunk(audio_bytes, chunk_path, job_id, section_key)
                chunk_paths.append(chunk_path)

            if not chunk_paths:
                raise ValueError("No audio chunks were produced — all sections were empty")

            # Concatenate chunks into final file
            final_path = output_dir / f"{job_id}.mp3"
            duration_seconds = _concatenate_chunks(chunk_paths, final_path, job_id)

        else:
            # ---------------------------------------------------------- #
            # Single-call mode: send full narration in one request        #
            # ---------------------------------------------------------- #
            narration = script['narration'].strip()
            audio_bytes = _call_elevenlabs_with_retry(
                text=narration,
                voice_id=voice_id,
                api_key=api_key,
                config=config,
                job_id=job_id,
                chunk_label='full'
            )

            final_path = output_dir / f"{job_id}.mp3"
            _save_chunk(audio_bytes, final_path, job_id, 'full')

            # Measure duration via ffprobe (no pydub needed)
            duration_seconds = _get_audio_duration(final_path, job_id)
            logger.info(f"[JOB {job_id}] Audio duration: {duration_seconds:.2f}s")

        # ------------------------------------------------------------------ #
        # Update database                                                      #
        # ------------------------------------------------------------------ #
        update_job_field(job_id, 'audio_path', str(final_path))
        update_job_field(job_id, 'duration_seconds', round(duration_seconds, 2))
        update_job_status(job_id, 'imaging')

        elapsed = time.time() - stage_start
        logger.info(f"[JOB {job_id}] voice_engine COMPLETED in {elapsed:.1f}s")

        return {
            'success': True,
            'output_path': str(final_path),
            'duration_seconds': round(duration_seconds, 2),
        }

    except Exception as e:
        elapsed = time.time() - stage_start
        logger.error(f"[JOB {job_id}] voice_engine FAILED: {str(e)}", exc_info=True)
        update_job_status(job_id, 'failed', error_module='voice_engine', error_message=str(e))
        return {'success': False, 'error': str(e)}
