"""
caption_engine.py
=================
Stage 5 of the VideoForge pipeline. Transcribes the voice audio using
faster-whisper to obtain word-level timestamps, groups the words into timed
caption blocks, renders each block as a transparent PIL image, and composites
them over the raw video. The result is saved as NNN_captioned.mp4.

If the audio is silent (voice engine skipped), placeholder captions are
generated from the script narration with evenly distributed timestamps so the
stage can be tested without a real voiceover.

Input:  job_id, config dict
        reads output/videos/NNN_raw.mp4, output/audio/NNN.mp3 (or .wav),
               output/scripts/NNN.json
Output: output/videos/NNN_captioned.mp4
Logs:   logs/caption_engine.log

Dependencies:
    - faster-whisper  (transcription + word timestamps)
    - Pillow          (caption image rendering with stroke)
    - moviepy 2.x     (video compositing + export)

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import json
import time
from pathlib import Path
from types import SimpleNamespace

# 2. Third-party libraries
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

# 3. Local modules
from database import update_job_status, update_job_field
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger('caption_engine')

# ---------------------------------------------------------------------------
# Font fallback chain — used when assets/fonts/ is not yet populated
# ---------------------------------------------------------------------------
_FONT_FALLBACKS = [
    'C:/Windows/Fonts/arialbd.ttf',
    'C:/Windows/Fonts/arial.ttf',
    'C:/Windows/Fonts/calibrib.ttf',
    'C:/Windows/Fonts/calibri.ttf',
    'C:/Windows/Fonts/segoeui.ttf',
]


# ---------------------------------------------------------------------------
# Helpers — font, audio, transcription
# ---------------------------------------------------------------------------

def _resolve_font(font_file: str, font_size: int, job_id: str) -> ImageFont.FreeTypeFont:
    """
    Load the configured font file, falling back to Windows system fonts if the
    configured path does not exist.

    Args:
        font_file (str): Path from config e.g. 'assets/fonts/Arial-Bold.ttf'.
        font_size (int): Point size.
        job_id (str):    Job identifier for log context.

    Returns:
        ImageFont.FreeTypeFont: Loaded font object.
    """
    candidates = [font_file] + _FONT_FALLBACKS
    for path in candidates:
        if Path(path).exists():
            font = ImageFont.truetype(str(path), font_size)
            if path != font_file:
                logger.warning(
                    f"[JOB {job_id}] Font '{font_file}' not found — "
                    f"using fallback: {path}"
                )
            else:
                logger.debug(f"[JOB {job_id}] Font loaded: {path} @ {font_size}pt")
            return font

    logger.warning(
        f"[JOB {job_id}] No TrueType font found — using PIL default (small/pixelated)"
    )
    return ImageFont.load_default()


def _resolve_audio(job_id: str) -> Path:
    """
    Return the audio file to transcribe: real MP3 first, silent WAV fallback.

    Args:
        job_id (str): Job identifier.

    Returns:
        Path: Path to the audio file.

    Raises:
        FileNotFoundError: If neither file exists.
    """
    mp3 = Path(f'output/audio/{job_id}.mp3')
    wav = Path(f'output/audio/{job_id}_silent.wav')

    if mp3.exists() and mp3.stat().st_size > 0:
        logger.info(f"[JOB {job_id}] Using real audio for transcription: {mp3}")
        return mp3
    if wav.exists():
        logger.warning(
            f"[JOB {job_id}] Real audio not found — "
            f"using silent placeholder WAV: {wav}"
        )
        return wav

    raise FileNotFoundError(
        f"No audio file found for job {job_id}. "
        "Run generate-voice or assemble first."
    )


def _run_whisper(
    audio_path: Path,
    model_name: str,
    job_id: str
) -> tuple:
    """
    Run faster-whisper on the audio file and return word objects plus
    transcription quality metadata.

    A transcription is considered unreliable when:
      - fewer than MIN_RELIABLE_WORDS words were found, OR
      - language_probability is below MIN_LANGUAGE_PROBABILITY
    In both cases the caller should fall back to placeholder captions.

    Args:
        audio_path (Path): Path to the audio file (MP3 or WAV).
        model_name (str):  Whisper model name e.g. 'base'.
        job_id (str):      Job identifier for log context.

    Returns:
        tuple: (words: list, reliable: bool)
               words — flat list of Word objects with .word/.start/.end
               reliable — False if the transcription looks like noise
    """
    MIN_RELIABLE_WORDS = 10
    MIN_LANGUAGE_PROBABILITY = 0.50

    from faster_whisper import WhisperModel

    logger.info(
        f"[JOB {job_id}] Loading Whisper model: {model_name} "
        "(first run downloads ~74 MB from HuggingFace)"
    )
    load_start = time.time()
    model = WhisperModel(model_name, device='cpu', compute_type='int8')
    logger.info(
        f"[JOB {job_id}] Whisper model loaded in {time.time() - load_start:.1f}s"
    )

    logger.info(f"[JOB {job_id}] Transcribing: {audio_path}")
    transcribe_start = time.time()
    segments, info = model.transcribe(str(audio_path), word_timestamps=True)

    words = []
    for segment in segments:
        if segment.words:
            for word in segment.words:
                words.append(word)

    elapsed = time.time() - transcribe_start
    logger.info(
        f"[JOB {job_id}] Transcription complete — "
        f"words: {len(words)}, "
        f"language: {info.language} ({info.language_probability:.0%}), "
        f"time: {elapsed:.1f}s"
    )

    # Reliability check
    reliable = (
        len(words) >= MIN_RELIABLE_WORDS
        and info.language_probability >= MIN_LANGUAGE_PROBABILITY
    )
    if not reliable:
        logger.warning(
            f"[JOB {job_id}] Transcription deemed unreliable — "
            f"words: {len(words)} (min {MIN_RELIABLE_WORDS}), "
            f"lang_prob: {info.language_probability:.0%} (min {MIN_LANGUAGE_PROBABILITY:.0%}). "
            "Will use placeholder captions."
        )

    return words, reliable


def _generate_placeholder_words(
    script: dict,
    audio_duration: float,
    job_id: str
) -> list:
    """
    Build synthetic word objects from the script narration with evenly
    distributed timestamps. Used when Whisper returns nothing (silent audio).

    Args:
        script (dict):          Parsed script JSON.
        audio_duration (float): Total audio length in seconds.
        job_id (str):           Job identifier for log context.

    Returns:
        list[SimpleNamespace]: Synthetic word objects with .word/.start/.end.
    """
    narration = script.get('narration', '').strip()
    if not narration:
        logger.warning(f"[JOB {job_id}] Script narration is empty — no placeholder captions")
        return []

    raw_words = narration.split()
    n = len(raw_words)
    if n == 0:
        return []

    # Distribute words evenly across the audio duration
    # Leave a small gap at the end so the last caption doesn't overshoot
    usable_duration = audio_duration * 0.97
    word_duration = usable_duration / n

    words = []
    for i, w in enumerate(raw_words):
        start = i * word_duration
        end = start + word_duration
        words.append(SimpleNamespace(word=w, start=start, end=end))

    logger.info(
        f"[JOB {job_id}] Placeholder captions: {n} words over {audio_duration:.1f}s "
        f"({word_duration:.2f}s/word)"
    )
    return words


# ---------------------------------------------------------------------------
# Caption grouping
# ---------------------------------------------------------------------------

def _group_into_captions(words: list, max_chars: int, max_words: int) -> list:
    """
    Group a flat list of word objects into timed caption blocks.

    A new block is started when adding the next word would exceed max_chars
    OR when the current block already has max_words words.

    Args:
        words (list):     Word objects with .word, .start, .end attributes.
        max_chars (int):  Maximum characters per caption line.
        max_words (int):  Maximum words per caption line.

    Returns:
        list[dict]: Caption blocks, each with keys:
                    'text' (str), 'start' (float), 'end' (float).
    """
    captions = []
    current = []
    current_chars = 0

    for word_obj in words:
        text = word_obj.word.strip()
        if not text:
            continue

        space = 1 if current else 0
        would_be_chars = current_chars + space + len(text)

        if current and (len(current) >= max_words or would_be_chars > max_chars):
            captions.append({
                'text':  ' '.join(w.word.strip() for w in current),
                'start': current[0].start,
                'end':   current[-1].end,
            })
            current = [word_obj]
            current_chars = len(text)
        else:
            current.append(word_obj)
            current_chars = would_be_chars

    if current:
        captions.append({
            'text':  ' '.join(w.word.strip() for w in current),
            'start': current[0].start,
            'end':   current[-1].end,
        })

    return captions


# ---------------------------------------------------------------------------
# Caption image rendering
# ---------------------------------------------------------------------------

def _render_caption_image(
    text: str,
    font: ImageFont.FreeTypeFont,
    text_color: tuple,
    stroke_color: tuple,
    stroke_width: int,
    video_width: int
) -> np.ndarray:
    """
    Render a single caption string as a transparent RGBA numpy array.
    The image is exactly as wide as needed (up to video_width) and tall
    enough for one line, with a small padding around the text.

    Args:
        text (str):         Caption text to render.
        font:               Loaded PIL font.
        text_color (tuple): RGBA fill colour e.g. (255, 255, 255, 255).
        stroke_color (tuple): RGBA stroke colour e.g. (0, 0, 0, 255).
        stroke_width (int): Stroke thickness in pixels.
        video_width (int):  Maximum image width (= video width).

    Returns:
        np.ndarray: RGBA array of shape (H, W, 4).
    """
    padding = stroke_width + 8

    # Measure text dimensions with a temporary draw surface
    dummy = Image.new('RGBA', (1, 1))
    draw = ImageDraw.Draw(dummy)
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    img_w = min(text_w + padding * 2, video_width)
    img_h = text_h + padding * 2

    img = Image.new('RGBA', (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    x = (img_w - text_w) // 2 - bbox[0]
    y = padding - bbox[1]

    draw.text(
        (x, y), text,
        font=font,
        fill=text_color,
        stroke_width=stroke_width,
        stroke_fill=stroke_color,
    )

    return np.array(img)


def _color_to_rgba(color_str: str, alpha: int = 255) -> tuple:
    """
    Convert a config color string to an RGBA tuple.

    Supports 'white', 'black', and '#RRGGBB' hex.

    Args:
        color_str (str): Color name or hex string from config.
        alpha (int):     Alpha channel value (0–255).

    Returns:
        tuple: (R, G, B, A) integers.
    """
    named = {
        'white': (255, 255, 255),
        'black': (0, 0, 0),
        'red':   (255, 0, 0),
        'yellow': (255, 255, 0),
    }
    name = color_str.strip().lower()
    if name in named:
        r, g, b = named[name]
    elif name.startswith('#') and len(name) == 7:
        r = int(name[1:3], 16)
        g = int(name[3:5], 16)
        b = int(name[5:7], 16)
    else:
        r, g, b = 255, 255, 255  # safe default
    return (r, g, b, alpha)


# ---------------------------------------------------------------------------
# Video compositing
# ---------------------------------------------------------------------------

def _burn_captions(
    raw_video_path: Path,
    captions: list,
    config: dict,
    job_id: str
):
    """
    Composite timed caption ImageClips over the raw video and return the
    combined VideoClip (not yet written to disk).

    Args:
        raw_video_path (Path): Input raw video file.
        captions (list[dict]): Caption blocks from _group_into_captions.
        config (dict):         Loaded config.json.
        job_id (str):          Job identifier for log context.

    Returns:
        moviepy.VideoClip: Composited clip ready for write_videofile.
    """
    from moviepy import VideoFileClip, CompositeVideoClip, ImageClip

    cap_cfg = config['captions']
    vid_cfg = config['video']

    font_size    = cap_cfg['font_size']
    stroke_width = cap_cfg['stroke_width']
    text_color   = _color_to_rgba(cap_cfg['color'])
    stroke_color = _color_to_rgba(cap_cfg['stroke_color'])
    y_percent    = cap_cfg['position_y_percent']
    video_width  = vid_cfg['width']
    video_height = vid_cfg['height']
    y_pixel      = int(y_percent * video_height)

    font = _resolve_font(cap_cfg['font_file'], font_size, job_id)

    logger.info(f"[JOB {job_id}] Loading raw video: {raw_video_path}")
    base = VideoFileClip(str(raw_video_path))
    video_duration = base.duration
    logger.info(f"[JOB {job_id}] Raw video duration: {video_duration:.2f}s")

    logger.info(f"[JOB {job_id}] Rendering {len(captions)} caption clips")
    caption_clips = []

    for i, cap in enumerate(captions):
        start = cap['start']
        end   = min(cap['end'], video_duration)
        duration = end - start
        if duration <= 0:
            continue

        img_array = _render_caption_image(
            text=cap['text'],
            font=font,
            text_color=text_color,
            stroke_color=stroke_color,
            stroke_width=stroke_width,
            video_width=video_width,
        )

        clip = (
            ImageClip(img_array)
            .with_duration(duration)
            .with_start(start)
            .with_position(('center', y_pixel))
        )
        caption_clips.append(clip)

        if (i + 1) % 10 == 0 or (i + 1) == len(captions):
            logger.debug(
                f"[JOB {job_id}] Rendered {i+1}/{len(captions)} captions"
            )

    if not caption_clips:
        logger.warning(f"[JOB {job_id}] No caption clips to composite — returning base video")
        return base

    composited = CompositeVideoClip([base] + caption_clips)
    logger.info(f"[JOB {job_id}] Composited {len(caption_clips)} caption clips")
    return composited


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def add_captions(job_id: str, config: dict) -> dict:
    """
    Transcribe the job's audio, group words into caption blocks, burn them
    into the raw video, and save the result as NNN_captioned.mp4.

    Args:
        job_id (str):  Unique job identifier e.g. '001'.
        config (dict): Loaded config.json contents.

    Returns:
        dict: {
            'success': bool,
            'output_path': str,    # path to NNN_captioned.mp4 if success
            'caption_count': int,  # number of caption blocks
            'error': str           # error message if failed
        }
    """
    stage_start = time.time()
    logger.info(f"[JOB {job_id}] Starting caption_engine")

    # Block C — caption_mode='off' (template-driven) skips this stage entirely.
    # Resolution order: explicit template caption_mode > config.captions.mode.
    caption_mode = 'on'
    try:
        from database import get_job as _get_job, get_template as _get_template
        _job = _get_job(job_id) or {}
        if _job.get('template_id'):
            _t = _get_template(int(_job['template_id']))
            if _t:
                caption_mode = (_t.get('caption_mode') or 'on').lower()
    except Exception:
        pass
    caption_mode = config.get('captions', {}).get('mode', caption_mode) or caption_mode
    if caption_mode == 'off':
        import shutil
        raw = Path(f'output/videos/{job_id}_raw.mp4')
        if not raw.exists():
            return {'success': False,
                    'error': f'Raw video not found: {raw}. Run assemble first.'}
        final_path = Path(f'output/videos/{job_id}_captioned.mp4')
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(raw), str(final_path))
        logger.info(
            f"[JOB {job_id}] caption_mode='off' — skipped burn-in, copied raw -> {final_path}"
        )
        try:
            from database import update_job_field, update_job_status
            update_job_field(job_id, 'final_video_path', str(final_path))
            update_job_status(job_id, 'metadata')
        except Exception:
            pass
        # Block F — same R2 hook applies even when caption burn-in is skipped
        try:
            from modules.r2_storage import upload_preview_for_job
            upload_preview_for_job(job_id, config)
        except Exception as exc:
            logger.warning(f"[JOB {job_id}] R2 preview hook crashed (non-fatal): {exc}")
        return {'success': True, 'output_path': str(final_path),
                'caption_count': 0, 'skipped': False}

    try:
        cap_cfg = config['captions']
        vid_cfg = config['video']

        # ----------------------------------------------------------------
        # Locate raw video
        # ----------------------------------------------------------------
        raw_video_path = Path(f'output/videos/{job_id}_raw.mp4')
        if not raw_video_path.exists():
            raise FileNotFoundError(
                f"Raw video not found: {raw_video_path}. Run assemble first."
            )

        # ----------------------------------------------------------------
        # Load script (needed for placeholder captions + topic logging)
        # ----------------------------------------------------------------
        script_path = Path(f'output/scripts/{job_id}.json')
        script = {}
        if script_path.exists():
            with open(script_path, 'r', encoding='utf-8') as f:
                script = json.load(f)
            logger.info(
                f"[JOB {job_id}] Captioning video for topic: '{script.get('topic')}'"
            )
        logger.debug(
            f"[JOB {job_id}] Config: model={cap_cfg['whisper_model']}, "
            f"font_size={cap_cfg['font_size']}, "
            f"max_chars={cap_cfg['max_chars_per_line']}, "
            f"max_words={cap_cfg['max_words_per_line']}, "
            f"position_y={cap_cfg['position_y_percent']}"
        )

        # ----------------------------------------------------------------
        # Transcribe
        # ----------------------------------------------------------------
        audio_path = _resolve_audio(job_id)
        words, reliable = _run_whisper(
            audio_path=audio_path,
            model_name=cap_cfg['whisper_model'],
            job_id=job_id,
        )

        # ----------------------------------------------------------------
        # Placeholder captions if transcript is absent or unreliable
        # ----------------------------------------------------------------
        if not words or not reliable:
            logger.warning(
                f"[JOB {job_id}] Whisper returned no words — "
                "generating placeholder captions from script narration"
            )
            # Estimate duration from script or default
            audio_duration = float(
                script.get('estimated_duration_seconds')
                or config['channel']['target_length_seconds']
            )
            words = _generate_placeholder_words(script, audio_duration, job_id)

        # ----------------------------------------------------------------
        # Group into caption blocks
        # ----------------------------------------------------------------
        max_chars = cap_cfg['max_chars_per_line']
        max_words = cap_cfg['max_words_per_line']
        captions = _group_into_captions(words, max_chars, max_words)
        logger.info(
            f"[JOB {job_id}] Caption blocks: {len(captions)} "
            f"(from {len(words)} words)"
        )

        if captions:
            logger.debug(
                f"[JOB {job_id}] First caption: '{captions[0]['text']}' "
                f"[{captions[0]['start']:.2f}s - {captions[0]['end']:.2f}s]"
            )
            logger.debug(
                f"[JOB {job_id}] Last caption:  '{captions[-1]['text']}' "
                f"[{captions[-1]['start']:.2f}s - {captions[-1]['end']:.2f}s]"
            )

        # ----------------------------------------------------------------
        # Composite captions over raw video
        # ----------------------------------------------------------------
        composited = _burn_captions(
            raw_video_path=raw_video_path,
            captions=captions,
            config=config,
            job_id=job_id,
        )

        # ----------------------------------------------------------------
        # Export
        # ----------------------------------------------------------------
        output_dir = Path('output/videos')
        output_path = output_dir / f"{job_id}_captioned.mp4"

        fps     = vid_cfg['fps']
        bitrate = vid_cfg['bitrate']
        logger.info(
            f"[JOB {job_id}] Exporting captioned video: {output_path} "
            f"(fps: {fps}, bitrate: {bitrate})"
        )
        export_start = time.time()

        composited.write_videofile(
            str(output_path),
            fps=fps,
            codec='libx264',
            audio_codec='aac',
            bitrate=bitrate,
            threads=4,
            logger=None,
        )
        composited.close()

        export_elapsed = time.time() - export_start
        size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info(
            f"[JOB {job_id}] File created: {output_path} "
            f"({size_mb:.2f} MB, export took {export_elapsed:.1f}s)"
        )

        # ----------------------------------------------------------------
        # Update database
        # ----------------------------------------------------------------
        update_job_field(job_id, 'final_video_path', str(output_path))
        update_job_status(job_id, 'metadata')

        # Block F — push video + thumbnail to R2 so the dashboard can stream
        # it from anywhere. Non-fatal: on any failure the local path stays the
        # source of truth and the dashboard falls back to it.
        try:
            from modules.r2_storage import upload_preview_for_job
            r2_result = upload_preview_for_job(job_id, config)
            if r2_result.get('success'):
                logger.info(
                    f"[JOB {job_id}] R2 preview ready — {r2_result.get('video_url')}"
                )
            elif r2_result.get('skipped'):
                logger.info(f"[JOB {job_id}] R2 preview skipped — {r2_result.get('error')}")
            else:
                logger.warning(f"[JOB {job_id}] R2 preview upload failed (non-fatal): "
                               f"{r2_result.get('error')}")
        except Exception as exc:
            logger.warning(f"[JOB {job_id}] R2 preview hook crashed (non-fatal): {exc}")

        elapsed = time.time() - stage_start
        logger.info(f"[JOB {job_id}] caption_engine COMPLETED in {elapsed:.1f}s")

        return {
            'success': True,
            'output_path': str(output_path),
            'caption_count': len(captions),
        }

    except Exception as e:
        elapsed = time.time() - stage_start
        logger.error(f"[JOB {job_id}] caption_engine FAILED: {str(e)}", exc_info=True)
        update_job_status(job_id, 'failed', error_module='caption_engine', error_message=str(e))
        return {'success': False, 'error': str(e)}
