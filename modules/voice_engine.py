"""
voice_engine.py
===============
Stage 2 of the VideoForge pipeline. Converts script narration into speech
and saves a final MP3 file.

Supports three TTS providers selected via config['voice']['provider']:
  - 'elevenlabs' (default) — cloud, requires ELEVENLABS_API_KEY + ELEVENLABS_VOICE_ID
  - 'openai'               — cloud, requires OPENAI_API_KEY; model tts-1
  - 'kokoro'               — local CPU inference; pip install kokoro soundfile

All providers chunk by section and concatenate via ffmpeg. Output is always
output/audio/NNN.mp3. The provider is read fresh from config on every call
so changing voice.provider in the Config editor applies on the next run
without restarting the app.

Input:  job_id, config dict — reads output/scripts/NNN.json
Output: output/audio/NNN.mp3
Logs:   logs/voice_engine.log

Dependencies:
    - requests            (ElevenLabs)
    - openai              (OpenAI TTS — pip install openai)
    - kokoro + soundfile  (Kokoro local TTS — pip install kokoro soundfile)
    - pydub               (WAV→MP3 conversion for Kokoro)
    - python-dotenv
"""

# 1. Standard library
import io
import json
import os
import shutil
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


# ---------------------------------------------------------------------------
# Shared audio utilities
# ---------------------------------------------------------------------------

def _get_audio_duration(path: Path, job_id: str) -> float:
    """Return audio file duration in seconds via ffprobe."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', str(path)],
            capture_output=True, text=True, check=True
        )
        return float(result.stdout.strip())
    except FileNotFoundError:
        raise RuntimeError(
            "ffprobe not found on PATH. Install ffmpeg from https://ffmpeg.org/download.html"
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffprobe failed for {path}: {e.stderr.strip()}")


def _load_script(job_id: str) -> dict:
    """Read and parse output/scripts/NNN.json."""
    script_path = Path(f'output/scripts/{job_id}.json')
    if not script_path.exists():
        raise FileNotFoundError(
            f"Script file not found: {script_path}. Run generate-script first."
        )
    with open(script_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if 'sections' not in data or 'narration' not in data:
        raise ValueError(f"Script JSON missing 'sections' or 'narration': {script_path}")
    return data


def _save_chunk(audio_bytes: bytes, output_path: Path, job_id: str, chunk_label: str) -> None:
    """Write raw audio bytes to disk."""
    output_path.write_bytes(audio_bytes)
    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(f"[JOB {job_id}] File created: {output_path} ({size_mb:.3f} MB) [chunk: {chunk_label}]")


def _concatenate_chunks(chunk_paths: list, output_path: Path, job_id: str) -> float:
    """
    Concatenate MP3 chunk files into one output file using ffmpeg.
    Returns total duration in seconds.
    """
    logger.info(f"[JOB {job_id}] Concatenating {len(chunk_paths)} audio chunk(s) via ffmpeg")

    if len(chunk_paths) == 1:
        shutil.copy2(str(chunk_paths[0]), str(output_path))
    else:
        concat_list_path = output_path.parent / f"{output_path.stem}_concat_list.txt"
        with open(concat_list_path, 'w', encoding='utf-8') as f:
            for cp in chunk_paths:
                f.write(f"file '{cp.resolve().as_posix()}'\n")
        try:
            subprocess.run(
                ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                 '-i', str(concat_list_path),
                 '-c:a', 'libmp3lame', '-b:a', '128k', str(output_path)],
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


# ---------------------------------------------------------------------------
# ElevenLabs provider
# ---------------------------------------------------------------------------

def _build_elevenlabs_payload(text: str, config: dict) -> dict:
    """Build the ElevenLabs TTS request body."""
    vc = config['voice']
    return {
        "text": text,
        "model_id": ELEVENLABS_MODELS['default'],
        "voice_settings": {
            "stability": vc['stability'],
            "similarity_boost": vc['similarity_boost'],
            "style": vc['style_exaggeration'],
            "use_speaker_boost": True,
        }
    }


def _tts_elevenlabs(
    text: str,
    chunk_label: str,
    config: dict,
    job_id: str,
    api_key: str,
    voice_id: str,
    max_retries: int = 3,
    backoff: float = 5.0,
) -> bytes:
    """Call ElevenLabs TTS and return raw MP3 bytes, with retry."""
    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id)
    output_format = config['voice'].get('output_format', 'mp3_44100_128')
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    params = {"output_format": output_format}
    payload = _build_elevenlabs_payload(text, config)

    logger.info(
        f"[JOB {job_id}] ElevenLabs TTS — chunk: {chunk_label}, "
        f"chars: {len(text)}, voice_id: {voice_id}"
    )

    for attempt in range(1, max_retries + 1):
        try:
            t0 = time.time()
            response = requests.post(url, headers=headers, params=params,
                                     json=payload, timeout=60)
            elapsed = time.time() - t0

            if response.status_code == 200:
                audio_bytes = response.content
                logger.info(
                    f"[JOB {job_id}] ElevenLabs OK — chunk: {chunk_label}, "
                    f"time: {elapsed:.2f}s, size: {len(audio_bytes)/1024:.1f}KB"
                )
                return audio_bytes

            elif response.status_code == 429:
                wait = int(response.headers.get('Retry-After', backoff * (2 ** (attempt - 1))))
                logger.warning(f"[JOB {job_id}] Rate limit — waiting {wait}s (attempt {attempt}/{max_retries})")
                if attempt < max_retries:
                    time.sleep(wait)

            elif response.status_code >= 500:
                wait = backoff * (2 ** (attempt - 1))
                logger.warning(
                    f"[JOB {job_id}] ElevenLabs {response.status_code} — "
                    f"waiting {wait:.0f}s (attempt {attempt}/{max_retries})"
                )
                if attempt < max_retries:
                    time.sleep(wait)

            else:
                raise Exception(f"ElevenLabs error {response.status_code}: {response.text[:200]}")

        except requests.exceptions.Timeout:
            wait = backoff * (2 ** (attempt - 1))
            logger.warning(f"[JOB {job_id}] ElevenLabs timeout — waiting {wait:.0f}s (attempt {attempt}/{max_retries})")
            if attempt < max_retries:
                time.sleep(wait)

        except requests.exceptions.ConnectionError as e:
            wait = backoff * (2 ** (attempt - 1))
            logger.warning(f"[JOB {job_id}] ElevenLabs connection error — waiting {wait:.0f}s: {e}")
            if attempt < max_retries:
                time.sleep(wait)

    raise Exception(f"ElevenLabs TTS failed after {max_retries} attempts for chunk '{chunk_label}'")


# ---------------------------------------------------------------------------
# OpenAI provider
# ---------------------------------------------------------------------------

def _tts_openai(
    text: str,
    chunk_label: str,
    config: dict,
    job_id: str,
    api_key: str,
    max_retries: int = 3,
    backoff: float = 5.0,
) -> bytes:
    """Call OpenAI TTS (tts-1) and return raw MP3 bytes, with retry."""
    from openai import OpenAI  # deferred — not a hard dependency

    voice = config['voice'].get('openai_voice', 'alloy')
    client = OpenAI(api_key=api_key)

    logger.info(
        f"[JOB {job_id}] OpenAI TTS — chunk: {chunk_label}, "
        f"voice: {voice}, chars: {len(text)}"
    )

    for attempt in range(1, max_retries + 1):
        try:
            t0 = time.time()
            response = client.audio.speech.create(
                model='tts-1',
                voice=voice,
                input=text,
                response_format='mp3',
            )
            elapsed = time.time() - t0
            audio_bytes = response.content
            logger.info(
                f"[JOB {job_id}] OpenAI TTS OK — chunk: {chunk_label}, "
                f"time: {elapsed:.2f}s, size: {len(audio_bytes)/1024:.1f}KB"
            )
            return audio_bytes

        except Exception as e:
            wait = backoff * (2 ** (attempt - 1))
            logger.warning(
                f"[JOB {job_id}] OpenAI TTS attempt {attempt}/{max_retries} failed — "
                f"retrying in {wait:.0f}s: {e}"
            )
            if attempt < max_retries:
                time.sleep(wait)

    raise Exception(f"OpenAI TTS failed after {max_retries} attempts for chunk '{chunk_label}'")


# ---------------------------------------------------------------------------
# Kokoro provider (local, CPU)
# ---------------------------------------------------------------------------

def _tts_kokoro(
    text: str,
    chunk_label: str,
    config: dict,
    job_id: str,
) -> bytes:
    """
    Run Kokoro local TTS and return MP3 bytes.
    Requires: pip install kokoro soundfile
    Kokoro generates audio as numpy arrays at 24 kHz; pydub converts to MP3.
    """
    import numpy as np
    from kokoro import KPipeline  # deferred — optional dependency
    import soundfile as sf
    from pydub import AudioSegment

    voice = config['voice'].get('kokoro_voice', 'af_heart')
    # Phase 14 Block 3 — derive Kokoro speed from target_wpm.
    # Kokoro at speed=1.0 produces ~165 WPM in typical English narration.
    # speed = target_wpm / 165, clamped to a safe playable range.
    target_wpm = int(config.get('voice', {}).get('target_wpm', 0) or 0)
    if target_wpm > 0:
        speed = target_wpm / 165.0
        speed = max(0.8, min(1.3, speed))
    else:
        speed = 1.0
    logger.info(
        f"[JOB {job_id}] Kokoro TTS (local CPU) — chunk: {chunk_label}, "
        f"voice: {voice}, target_wpm: {target_wpm or '(default)'}, "
        f"speed: {speed:.3f}, chars: {len(text)}"
    )

    t0 = time.time()

    # lang_code='a' = American English
    pipeline = KPipeline(lang_code='a')
    segments = []
    sample_rate = 24000

    for _, _, audio in pipeline(text, voice=voice, speed=speed):
        if audio is not None:
            segments.append(audio)

    if not segments:
        raise ValueError(f"Kokoro produced no audio for chunk '{chunk_label}'")

    combined = np.concatenate(segments)

    # Write PCM to an in-memory WAV buffer, then re-encode as MP3 via pydub
    wav_buf = io.BytesIO()
    sf.write(wav_buf, combined, sample_rate, format='WAV', subtype='PCM_16')
    wav_buf.seek(0)

    segment = AudioSegment.from_wav(wav_buf)
    mp3_buf = io.BytesIO()
    segment.export(mp3_buf, format='mp3', bitrate='128k')

    elapsed = time.time() - t0
    audio_bytes = mp3_buf.getvalue()
    logger.info(
        f"[JOB {job_id}] Kokoro TTS done — chunk: {chunk_label}, "
        f"time: {elapsed:.2f}s, size: {len(audio_bytes)/1024:.1f}KB"
    )
    return audio_bytes


# ---------------------------------------------------------------------------
# Provider routing
# ---------------------------------------------------------------------------

def _resolve_tts_fn(provider: str, config: dict, job_id: str):
    """
    Return (tts_callable, skip_result).

    tts_callable(text, chunk_label) -> bytes  — ready to use, or None on skip.
    skip_result dict — non-None when the provider is unavailable; pass through
                       as the generate_voice() return value.
    """
    if provider == 'elevenlabs':
        api_key = os.getenv('ELEVENLABS_API_KEY', '').strip()
        if not api_key:
            msg = ("ELEVENLABS_API_KEY is not set in .env. "
                   "Add your ElevenLabs key or switch voice.provider to 'openai' or 'kokoro'.")
            logger.warning(f"[JOB {job_id}] {msg}")
            return None, {'success': False, 'skipped': True, 'error': msg}

        voice_id = os.getenv('ELEVENLABS_VOICE_ID', '').strip()
        if not voice_id or voice_id == 'SET_IN_ENV':
            voice_id = config['voice'].get('voice_id', '')
        if not voice_id or voice_id in ('SET_IN_ENV', ''):
            msg = ("ELEVENLABS_VOICE_ID is not set. "
                   "Set it in .env after choosing a voice in the ElevenLabs Voice Library.")
            logger.warning(f"[JOB {job_id}] {msg}")
            return None, {'success': False, 'skipped': True, 'error': msg}

        def fn(text, label):
            return _tts_elevenlabs(text, label, config, job_id, api_key, voice_id)
        return fn, None

    elif provider == 'openai':
        api_key = os.getenv('OPENAI_API_KEY', '').strip()
        if not api_key:
            msg = ("OPENAI_API_KEY is not set in .env. "
                   "Add your OpenAI key or switch voice.provider to 'elevenlabs' or 'kokoro'.")
            logger.warning(f"[JOB {job_id}] {msg}")
            return None, {'success': False, 'skipped': True, 'error': msg}

        try:
            import openai  # noqa: F401 — just check it's installed
        except ImportError:
            msg = "openai package is not installed. Run: pip install openai"
            logger.warning(f"[JOB {job_id}] {msg}")
            return None, {'success': False, 'skipped': True, 'error': msg}

        def fn(text, label):
            return _tts_openai(text, label, config, job_id, api_key)
        return fn, None

    elif provider == 'kokoro':
        try:
            import kokoro  # noqa: F401
            import soundfile  # noqa: F401
        except ImportError as e:
            msg = f"Missing package for Kokoro provider: {e}. Run: pip install kokoro soundfile"
            logger.warning(f"[JOB {job_id}] {msg}")
            return None, {'success': False, 'skipped': True, 'error': msg}

        def fn(text, label):
            return _tts_kokoro(text, label, config, job_id)
        return fn, None

    else:
        msg = (f"Unknown voice.provider: '{provider}'. "
               "Valid values: 'elevenlabs', 'openai', 'kokoro'.")
        logger.error(f"[JOB {job_id}] {msg}")
        return None, {'success': False, 'error': msg}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_voice(job_id: str, config: dict) -> dict:
    """
    Convert script narration to speech and save as output/audio/NNN.mp3.

    The TTS provider is read fresh from config['voice']['provider'] on every
    call — changing it in the Config editor takes effect on the next run with
    no app restart needed.

    Chunking: when chunk_by_section=true, each script section (hook/body/cta)
    is synthesised separately and concatenated.  Downstream modules are
    untouched regardless of provider.

    Args:
        job_id (str):  Unique job identifier e.g. '001'.
        config (dict): Loaded config.json contents (read fresh by the caller).

    Returns:
        dict: {
            'success': bool,
            'output_path': str,        # path to NNN.mp3 if success
            'duration_seconds': float, # audio length if success
            'skipped': bool,           # True when key/package absent (not a hard failure)
            'error': str               # error message if failed or skipped
        }
    """
    stage_start = time.time()
    provider = config['voice'].get('provider', 'elevenlabs').lower().strip()
    logger.info(f"[JOB {job_id}] Starting voice_engine — provider: {provider}")

    # Resolve provider; get a callable or a skip result
    tts_fn, skip_result = _resolve_tts_fn(provider, config, job_id)
    if skip_result is not None:
        return skip_result

    # Block B — wrap TTS callable to record characters synthesized per chunk
    try:
        from utils.usage_tracker import track as _usage_track
        from database import get_job as _get_job
        _job_row = _get_job(job_id) or {}
        _ch_id = _job_row.get('channel_id')
        _provider_key = ('openai_tts' if provider == 'openai'
                         else 'kokoro' if provider == 'kokoro'
                         else 'elevenlabs')
        _raw_fn = tts_fn
        def _tracked_tts(text, label):
            audio = _raw_fn(text, label)
            _usage_track(
                _provider_key, f'tts:{label}', units=len(text or ''),
                channel_id=_ch_id, job_id=job_id, config=config,
            )
            return audio
        tts_fn = _tracked_tts
    except Exception as _exc:
        logger.debug(f"[JOB {job_id}] usage tracking wrap skipped: {_exc}")

    try:
        script = _load_script(job_id)
        logger.info(
            f"[JOB {job_id}] Script loaded — "
            f"word_count: {script.get('word_count')}, topic: '{script.get('topic')}'"
        )

        output_dir = Path('output/audio')
        output_dir.mkdir(parents=True, exist_ok=True)

        chunk_by_section = config['voice'].get('chunk_by_section', True)

        if chunk_by_section:
            sections = script['sections']
            chunk_paths = []

            for section_key in SECTION_ORDER:
                text = sections.get(section_key, '').strip()
                if not text:
                    logger.warning(f"[JOB {job_id}] Section '{section_key}' is empty — skipping chunk")
                    continue

                audio_bytes = tts_fn(text, section_key)
                chunk_path = output_dir / f"{job_id}_{section_key}.mp3"
                _save_chunk(audio_bytes, chunk_path, job_id, section_key)
                chunk_paths.append(chunk_path)

            if not chunk_paths:
                raise ValueError("No audio chunks produced — all sections were empty")

            final_path = output_dir / f"{job_id}.mp3"
            duration_seconds = _concatenate_chunks(chunk_paths, final_path, job_id)

        else:
            narration = script['narration'].strip()
            audio_bytes = tts_fn(narration, 'full')

            final_path = output_dir / f"{job_id}.mp3"
            _save_chunk(audio_bytes, final_path, job_id, 'full')
            duration_seconds = _get_audio_duration(final_path, job_id)
            logger.info(f"[JOB {job_id}] Audio duration: {duration_seconds:.2f}s")

        update_job_field(job_id, 'audio_path', str(final_path))
        update_job_field(job_id, 'duration_seconds', round(duration_seconds, 2))
        update_job_status(job_id, 'imaging')

        elapsed = time.time() - stage_start
        logger.info(f"[JOB {job_id}] voice_engine COMPLETED in {elapsed:.1f}s — provider: {provider}")

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
