"""
assembly_engine.py
==================
Stage 4 of the VideoForge pipeline. Assembles a raw MP4 video from still
images and an audio track, with crossfade transitions and optional background
music mixed at the configured dB level.

If the audio file does not exist yet (voice engine was skipped), a silent WAV
placeholder is created from the estimated_duration_seconds in the script JSON
so the assembly can be tested end-to-end without a real voiceover.

If images do not exist yet (image engine was skipped), placeholder solid-colour
images are generated at the correct 1080x1920 resolution.

Input:  job_id, config dict
        reads output/scripts/NNN.json, output/audio/NNN.mp3,
               output/images/NNN/img_01..NN.png, assets/music/*.mp3
Output: output/videos/NNN_raw.mp4
Logs:   logs/assembly_engine.log

Dependencies:
    - moviepy 2.x  (video assembly + export)
    - Pillow        (placeholder image generation)
    - wave (stdlib) (silent placeholder audio)

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import json
import math
import os
import random
import time
import wave
import struct
from pathlib import Path

# 2. Third-party libraries
from dotenv import load_dotenv

# 3. Local modules
from database import update_job_status, update_job_field
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger('assembly_engine')


# ---------------------------------------------------------------------------
# Placeholder generators (used when upstream stages were skipped)
# ---------------------------------------------------------------------------

# Distinct dark palette — one colour per placeholder image slot
_PLACEHOLDER_COLOURS = [
    (15, 20, 35),   # deep navy
    (25, 15, 35),   # deep purple
    (15, 30, 25),   # deep teal
    (35, 20, 15),   # deep rust
    (20, 30, 15),   # deep green
    (35, 15, 20),   # deep crimson
    (15, 25, 35),   # deep cerulean
    (30, 25, 15),   # deep amber
]


def _create_silent_audio(output_path: Path, duration_seconds: float, job_id: str) -> None:
    """
    Write a silent stereo WAV file using only stdlib (no external deps needed).

    Args:
        output_path (Path):       Destination path for the WAV file.
        duration_seconds (float): Length of silence in seconds.
        job_id (str):             Job identifier for log context.
    """
    sample_rate = 44100
    num_channels = 2
    sample_width = 2  # 16-bit
    num_frames = int(sample_rate * duration_seconds)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), 'w') as wf:
        wf.setnchannels(num_channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(b'\x00' * num_frames * num_channels * sample_width)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(
        f"[JOB {job_id}] Silent placeholder audio created: {output_path} "
        f"({duration_seconds:.1f}s, {size_mb:.3f} MB)"
    )


def _create_placeholder_images(
    images_dir: Path,
    count: int,
    width: int,
    height: int,
    job_id: str
) -> list:
    """
    Generate solid-colour placeholder PNG images using Pillow.
    One image per slot, each a different dark colour so transitions are visible.

    Args:
        images_dir (Path): Destination directory.
        count (int):       Number of images to create.
        width (int):       Image width in pixels.
        height (int):      Image height in pixels.
        job_id (str):      Job identifier for log context.

    Returns:
        list[Path]: Ordered list of created image paths.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise ImportError("Pillow is required for placeholder images. Run: pip install Pillow")

    images_dir.mkdir(parents=True, exist_ok=True)
    paths = []

    for i in range(1, count + 1):
        colour = _PLACEHOLDER_COLOURS[(i - 1) % len(_PLACEHOLDER_COLOURS)]
        img = Image.new('RGB', (width, height), color=colour)

        # Add subtle label so each frame is identifiable when reviewing
        draw = ImageDraw.Draw(img)
        label = f"PLACEHOLDER  Image {i}/{count}"
        draw.text((width // 2 - 180, height // 2 - 20), label, fill=(80, 80, 80))

        path = images_dir / f"img_{i:02d}.png"
        img.save(str(path))
        paths.append(path)

    logger.info(
        f"[JOB {job_id}] Created {count} placeholder images in {images_dir}"
    )
    return paths


# ---------------------------------------------------------------------------
# Asset discovery helpers
# ---------------------------------------------------------------------------

def _resolve_audio(job_id: str, script: dict, config: dict) -> Path:
    """
    Return the audio file path for the job. If the real MP3 does not exist,
    create a silent WAV placeholder and return that path instead.

    Args:
        job_id (str):   Job identifier.
        script (dict):  Parsed script JSON (used for estimated duration).
        config (dict):  Loaded config.json.

    Returns:
        Path: Path to the audio file that will be loaded by MoviePy.
    """
    mp3_path = Path(f'output/audio/{job_id}.mp3')
    if mp3_path.exists() and mp3_path.stat().st_size > 0:
        logger.info(f"[JOB {job_id}] Using real audio: {mp3_path}")
        return mp3_path

    # Fall back to silent placeholder
    duration = float(
        script.get('estimated_duration_seconds')
        or config['channel']['target_length_seconds']
    )
    logger.warning(
        f"[JOB {job_id}] Audio file not found at {mp3_path}. "
        f"Creating {duration:.1f}s silent placeholder."
    )
    silent_path = Path(f'output/audio/{job_id}_silent.wav')
    _create_silent_audio(silent_path, duration, job_id)
    return silent_path


def _resolve_images(job_id: str, config: dict) -> list:
    """
    Return an ordered list of image paths for the job. If fewer than expected
    images exist, placeholder images are created for missing slots.

    Args:
        job_id (str):   Job identifier.
        config (dict):  Loaded config.json (for dimensions and count).

    Returns:
        list[Path]: Ordered list of image paths img_01 … img_N.
    """
    images_dir = Path(f'output/images/{job_id}')
    expected = config['script']['images_to_generate']
    width = config['video']['width']
    height = config['video']['height']

    existing = sorted(images_dir.glob('img_*.png')) if images_dir.exists() else []
    if len(existing) == expected:
        logger.info(f"[JOB {job_id}] Using {len(existing)} real images from {images_dir}")
        return existing

    # Some or all images are missing — create placeholders for the missing slots
    if existing:
        logger.warning(
            f"[JOB {job_id}] Only {len(existing)}/{expected} images found. "
            "Creating placeholders for missing slots."
        )
    else:
        logger.warning(
            f"[JOB {job_id}] No images found in {images_dir}. "
            f"Creating {expected} placeholder images."
        )

    _create_placeholder_images(images_dir, expected, width, height, job_id)
    return sorted(images_dir.glob('img_*.png'))


def _find_music_file(job_id: str, config: dict = None) -> Path | None:
    """
    Return the first MP3 file found in the channel's music directory, or None.

    The directory is read from config['video']['music_dir'] when present;
    falls back to assets/music/ (global default).

    Args:
        job_id (str):  Job identifier for log context.
        config (dict): Merged channel config (optional).

    Returns:
        Path | None: Path to a music file, or None.
    """
    dir_str = (config or {}).get('video', {}).get('music_dir', 'assets/music')
    music_dir = Path(dir_str)
    if not music_dir.exists():
        logger.info(f"[JOB {job_id}] {music_dir}/ directory not found — no background music")
        return None

    mp3_files = sorted(music_dir.glob('*.mp3'))
    if not mp3_files:
        logger.info(f"[JOB {job_id}] No MP3 files in {music_dir}/ — no background music")
        return None

    logger.info(f"[JOB {job_id}] Music track: {mp3_files[0].name}")
    return mp3_files[0]


# Background video extensions searched in assets/backgrounds/ (order-independent)
_BACKGROUND_EXTENSIONS = ('*.mp4', '*.mov', '*.mkv', '*.webm')


def _resolve_background_clip(job_id: str, config: dict = None) -> Path | None:
    """
    Return a random background video clip from the channel's backgrounds dir,
    or None if the directory is missing or empty.

    The directory is read from config['video']['backgrounds_dir'] when present;
    falls back to assets/backgrounds/ (global default).

    Used by background_loop visual mode (e.g. Reddit Stories) where the video is
    a looping gameplay/ambient clip instead of an image slideshow.

    Args:
        job_id (str):  Job identifier for log context.
        config (dict): Merged channel config (optional).

    Returns:
        Path | None: Path to a randomly chosen background clip, or None.
    """
    cfg = config or {}
    dir_candidates = []
    if cfg.get('pipeline', {}).get('visual_mode') == 'long_form_ambient':
        # Prefer the channel's ambient dir for sleep/lore content.
        amb_dir = cfg.get('ambient', {}).get('backgrounds_dir', '') \
            or cfg.get('video', {}).get('ambient_dir', '')
        if amb_dir:
            dir_candidates.append(Path(amb_dir))
        ch_slug = cfg.get('_channel', {}).get('slug', '')
        if ch_slug:
            dir_candidates.append(Path(f'channels/{ch_slug}/assets/ambient'))
    dir_str = cfg.get('video', {}).get('backgrounds_dir', 'assets/backgrounds')
    dir_candidates.append(Path(dir_str))

    clips: list[Path] = []
    bg_dir = None
    for cand in dir_candidates:
        if cand and cand.exists():
            found = []
            for pattern in _BACKGROUND_EXTENSIONS:
                found.extend(cand.glob(pattern))
            if found:
                bg_dir = cand
                clips = found
                break

    if not clips:
        logger.warning(
            f"[JOB {job_id}] No video files found in: "
            f"{[str(c) for c in dir_candidates]} — cannot use background visual mode"
        )
        return None
    clips = sorted(set(clips))

    if not clips:
        logger.warning(
            f"[JOB {job_id}] No video files in {bg_dir}/ — "
            "cannot use background_loop visual mode"
        )
        return None

    chosen = random.choice(clips)
    logger.info(
        f"[JOB {job_id}] Background clip: {chosen.name} "
        f"(chosen at random from {len(clips)} clip(s))"
    )
    return chosen


# ---------------------------------------------------------------------------
# Video assembly
# ---------------------------------------------------------------------------

def _db_to_linear(db: float) -> float:
    """
    Convert a dB gain value to a linear amplitude multiplier.

    Args:
        db (float): Gain in decibels e.g. -18.0.

    Returns:
        float: Linear multiplier e.g. 0.126.
    """
    return 10.0 ** (db / 20.0)


def _build_video(
    image_paths: list,
    audio_path: Path,
    music_path: Path | None,
    config: dict,
    job_id: str
):
    """
    Assemble the raw video clip using MoviePy 2.x.

    Steps:
      1. Load audio → determine total duration
      2. Calculate per-image duration accounting for crossfade overlaps
      3. Build slideshow with CrossFadeIn transitions
      4. Mix voice audio + background music (if available)
      5. Return the final composite VideoClip (not yet written to disk)

    Args:
        image_paths (list[Path]): Ordered image file paths.
        audio_path (Path):        Voice audio file (MP3 or WAV).
        music_path (Path|None):   Background music file, or None.
        config (dict):            Loaded config.json.
        job_id (str):             Job identifier for log context.

    Returns:
        moviepy.VideoClip: Assembled clip ready for write_videofile.
    """
    from moviepy import (
        ImageClip,
        AudioFileClip,
        concatenate_videoclips,
        CompositeAudioClip,
    )
    from moviepy.video.fx import CrossFadeIn
    from moviepy.audio.fx import AudioLoop, MultiplyVolume

    vid_cfg = config['video']
    fps = vid_cfg['fps']
    transition_duration = vid_cfg['transition_duration']
    music_volume_db = vid_cfg['music_volume_db']
    num_images = len(image_paths)

    # ------------------------------------------------------------------
    # 1. Load voice audio and measure duration
    # ------------------------------------------------------------------
    logger.info(f"[JOB {job_id}] Loading audio: {audio_path}")
    voice_clip = AudioFileClip(str(audio_path))
    audio_duration = voice_clip.duration
    logger.info(f"[JOB {job_id}] Audio duration: {audio_duration:.2f}s")

    # ------------------------------------------------------------------
    # 2. Per-image duration — adjusted so total == audio_duration
    #    Formula: img_dur * N - transition * (N-1) = audio_duration
    # ------------------------------------------------------------------
    num_transitions = max(0, num_images - 1)
    img_duration = (audio_duration + transition_duration * num_transitions) / num_images
    logger.info(
        f"[JOB {job_id}] Image duration: {img_duration:.3f}s each "
        f"(transitions: {num_transitions} x {transition_duration}s)"
    )

    # ------------------------------------------------------------------
    # 3. Build slideshow clips
    # ------------------------------------------------------------------
    logger.info(f"[JOB {job_id}] Building {num_images}-image slideshow")
    clips = []
    for i, img_path in enumerate(image_paths):
        logger.debug(f"[JOB {job_id}] Loading image {i+1}/{num_images}: {img_path.name}")
        clip = ImageClip(str(img_path)).with_duration(img_duration)
        if i > 0:
            clip = clip.with_effects([CrossFadeIn(transition_duration)])
        clips.append(clip)

    video = concatenate_videoclips(clips, method='compose', padding=-transition_duration)
    video = video.with_fps(fps)

    # Trim to exact audio duration (handles any floating-point drift)
    video = video.with_duration(audio_duration)
    logger.info(
        f"[JOB {job_id}] Slideshow built — "
        f"final duration: {video.duration:.2f}s, fps: {fps}"
    )

    # ------------------------------------------------------------------
    # 4. Mix audio (voice + optional background music)
    # ------------------------------------------------------------------
    video = _mix_audio(video, voice_clip, audio_duration, music_path, config, job_id)
    return video


def _mix_audio(
    video,
    voice_clip,
    audio_duration: float,
    music_path: Path | None,
    config: dict,
    job_id: str,
):
    """
    Attach voice (and optional looped/trimmed background music) to a video clip.

    Shared by the image-slideshow path and the background-loop path so both use
    identical music mixing at the configured dB level.

    Args:
        video:                  MoviePy VideoClip to attach audio to.
        voice_clip:             Loaded voice AudioFileClip.
        audio_duration (float): Target audio length in seconds.
        music_path (Path|None): Background music file, or None.
        config (dict):          Loaded config.json.
        job_id (str):           Job identifier for log context.

    Returns:
        moviepy.VideoClip: The video with mixed audio attached.
    """
    from moviepy import AudioFileClip, CompositeAudioClip
    from moviepy.audio.fx import AudioLoop, MultiplyVolume

    music_volume_db = config['video']['music_volume_db']

    if music_path:
        music_linear = _db_to_linear(music_volume_db)
        logger.info(
            f"[JOB {job_id}] Loading music: {music_path.name} "
            f"({music_volume_db} dB = {music_linear:.3f}x)"
        )
        music_clip = AudioFileClip(str(music_path))

        # Loop or trim to match video duration
        if music_clip.duration < audio_duration:
            music_clip = music_clip.with_effects([AudioLoop(duration=audio_duration)])
        else:
            music_clip = music_clip.with_duration(audio_duration)

        music_clip = music_clip.with_effects([MultiplyVolume(music_linear)])
        mixed_audio = CompositeAudioClip([voice_clip, music_clip])
        logger.info(f"[JOB {job_id}] Voice + music mixed")
    else:
        mixed_audio = voice_clip
        logger.info(f"[JOB {job_id}] Audio: voice only (no music track found)")

    return video.with_audio(mixed_audio)


def _build_background_video(
    background_path: Path,
    audio_path: Path,
    music_path: Path | None,
    config: dict,
    job_id: str,
):
    """
    Assemble a video from a looping background clip instead of an image slideshow.

    Steps:
      1. Load voice audio → determine total duration
      2. Load the background clip, drop its own audio
      3. Pick a random start offset; trim if longer than the audio, loop if shorter
      4. Scale-to-cover and centre-crop to the configured portrait resolution
      5. Mix voice + background music (identical to the slideshow path)

    Args:
        background_path (Path): Background video clip from assets/backgrounds/.
        audio_path (Path):      Voice audio file (MP3 or WAV).
        music_path (Path|None): Background music file, or None.
        config (dict):          Loaded config.json.
        job_id (str):           Job identifier for log context.

    Returns:
        moviepy.VideoClip: Assembled clip ready for write_videofile.
    """
    from moviepy import AudioFileClip, VideoFileClip
    from moviepy.video.fx import Loop

    vid_cfg = config['video']
    fps = vid_cfg['fps']
    target_w = vid_cfg['width']
    target_h = vid_cfg['height']

    # ------------------------------------------------------------------
    # 1. Load voice audio and measure duration
    # ------------------------------------------------------------------
    logger.info(f"[JOB {job_id}] Loading audio: {audio_path}")
    voice_clip = AudioFileClip(str(audio_path))
    audio_duration = voice_clip.duration
    logger.info(f"[JOB {job_id}] Audio duration: {audio_duration:.2f}s")

    # ------------------------------------------------------------------
    # 2. Load background clip and drop its own audio track
    # ------------------------------------------------------------------
    logger.info(f"[JOB {job_id}] Loading background clip: {background_path.name}")
    bg = VideoFileClip(str(background_path)).without_audio()
    logger.info(
        f"[JOB {job_id}] Background clip: {bg.w}x{bg.h}, "
        f"duration {bg.duration:.1f}s"
    )

    # ------------------------------------------------------------------
    # 3. Trim (random offset) if long enough, otherwise loop to length
    # ------------------------------------------------------------------
    if bg.duration >= audio_duration:
        max_start = bg.duration - audio_duration
        start = random.uniform(0, max_start) if max_start > 0 else 0
        bg = bg.subclipped(start, start + audio_duration)
        logger.info(
            f"[JOB {job_id}] Trimmed background to {audio_duration:.2f}s "
            f"from random offset {start:.1f}s"
        )
    else:
        bg = bg.with_effects([Loop(duration=audio_duration)])
        logger.info(
            f"[JOB {job_id}] Background ({bg.duration:.1f}s) shorter than audio — "
            f"looped to {audio_duration:.2f}s"
        )

    # ------------------------------------------------------------------
    # 4. Scale-to-cover then centre-crop to portrait resolution
    # ------------------------------------------------------------------
    scale = max(target_w / bg.w, target_h / bg.h)
    bg = bg.resized(scale)
    bg = bg.cropped(
        width=target_w, height=target_h,
        x_center=bg.w / 2, y_center=bg.h / 2,
    )
    bg = bg.with_fps(fps)
    bg = bg.with_duration(audio_duration)
    logger.info(
        f"[JOB {job_id}] Background framed to {target_w}x{target_h} @ {fps}fps, "
        f"duration {bg.duration:.2f}s"
    )

    # ------------------------------------------------------------------
    # 5. Mix audio (voice + optional music)
    # ------------------------------------------------------------------
    return _mix_audio(bg, voice_clip, audio_duration, music_path, config, job_id)


def _build_long_form_ambient_video(
    background_path: Path,
    audio_path: Path,
    music_path: Path | None,
    config: dict,
    job_id: str,
):
    """
    Assemble a long-form (up to 3 hours) ambient video for sleep/lore content.

    Differences vs background_loop:
      - No random start offset; the source clip loops from t=0 to make the loop
        seamless when the same clip is reused for hours of audio.
      - Supports an optional static overlay image composited on top (e.g. dim
        castle illustration). Source: config.ambient.overlay_image (path).
      - Supports an optional ambient audio bed (rain, fire crackling) mixed in
        under the narration at config.ambient.ambient_audio_db (defaults -22).
        Source: config.ambient.ambient_audio_path.
      - Length cap is config.pipeline.long_form_max_seconds (default 10800).

    Voice + music mixing still goes through _mix_audio so the music_volume_db
    rules stay consistent across modes.

    Args:
        background_path (Path): Looped ambient background clip.
        audio_path (Path):      Voice narration.
        music_path (Path|None): Optional background music track.
        config (dict):          Loaded config dict.
        job_id (str):           Job id for log context.

    Returns:
        moviepy.VideoClip: Final clip ready to write.
    """
    from moviepy import (AudioFileClip, CompositeAudioClip, CompositeVideoClip,
                          ImageClip, VideoFileClip)
    from moviepy.video.fx import Loop
    from moviepy.audio.fx import AudioLoop, MultiplyVolume

    vid_cfg = config['video']
    fps = vid_cfg['fps']
    target_w = vid_cfg['width']
    target_h = vid_cfg['height']

    ambient_cfg = config.get('ambient', {}) or {}
    max_seconds = int(config.get('pipeline', {}).get('long_form_max_seconds', 10800))

    # --- 1. Load voice and clamp duration to max_seconds ---
    voice_clip = AudioFileClip(str(audio_path))
    audio_duration = voice_clip.duration
    if audio_duration > max_seconds:
        logger.warning(
            f"[JOB {job_id}] Ambient audio {audio_duration:.0f}s exceeds cap "
            f"{max_seconds}s — trimming"
        )
        voice_clip = voice_clip.with_duration(max_seconds)
        audio_duration = max_seconds
    logger.info(
        f"[JOB {job_id}] Long-form ambient — total duration: {audio_duration:.0f}s"
    )

    # --- 2. Load and loop background to full duration ---
    bg = VideoFileClip(str(background_path)).without_audio()
    logger.info(
        f"[JOB {job_id}] Background clip: {bg.w}x{bg.h}, src duration {bg.duration:.1f}s "
        f"— looping to {audio_duration:.0f}s"
    )
    bg = bg.with_effects([Loop(duration=audio_duration)])
    scale = max(target_w / bg.w, target_h / bg.h)
    bg = bg.resized(scale).cropped(
        width=target_w, height=target_h,
        x_center=bg.w * scale / 2, y_center=bg.h * scale / 2,
    )
    bg = bg.with_fps(fps).with_duration(audio_duration)

    # --- 3. Optional overlay image composited on top ---
    overlay_path = ambient_cfg.get('overlay_image') or ''
    if overlay_path and Path(overlay_path).exists():
        logger.info(f"[JOB {job_id}] Compositing overlay: {overlay_path}")
        overlay = (ImageClip(str(overlay_path))
                   .resized((target_w, target_h))
                   .with_duration(audio_duration))
        video = CompositeVideoClip([bg, overlay], size=(target_w, target_h))
    else:
        video = bg

    # --- 4. Optional ambient audio bed mixed under narration ---
    ambient_audio_path = ambient_cfg.get('ambient_audio_path') or ''
    if ambient_audio_path and Path(ambient_audio_path).exists():
        ambient_db = float(ambient_cfg.get('ambient_audio_db', -22))
        ambient_linear = _db_to_linear(ambient_db)
        logger.info(
            f"[JOB {job_id}] Mixing ambient bed: {ambient_audio_path} "
            f"@ {ambient_db}dB ({ambient_linear:.3f}x)"
        )
        amb = AudioFileClip(str(ambient_audio_path))
        if amb.duration < audio_duration:
            amb = amb.with_effects([AudioLoop(duration=audio_duration)])
        else:
            amb = amb.with_duration(audio_duration)
        amb = amb.with_effects([MultiplyVolume(ambient_linear)])
        # Mix voice + ambient bed first, then hand off to _mix_audio for music
        voice_clip = CompositeAudioClip([voice_clip, amb])

    # --- 5. Music + voice via shared mixer ---
    return _mix_audio(video, voice_clip, audio_duration, music_path, config, job_id)


# ---------------------------------------------------------------------------
# Phase 14 Block 1 — FFmpeg renderer dispatch helpers
# ---------------------------------------------------------------------------

def _resolve_ffmpeg_format_spec(config: dict, script: dict, job_id: str) -> str:
    """
    Decide which ffmpeg_renderer format_spec to use based on the channel's
    visual_mode and orientation. Mapping:

      - long_form_ambient            -> long_form_ambient_16_9_no_captions
      - background_loop / images:
          - portrait (h > w)          -> short_9_16_captions
          - landscape (w >= h)        -> long_form_16_9_captions
    """
    visual_mode = config.get('pipeline', {}).get('visual_mode', 'images')
    if visual_mode == 'long_form_ambient':
        return 'long_form_ambient_16_9_no_captions'

    vid_cfg = config.get('video', {})
    w = int(vid_cfg.get('width', 1080))
    h = int(vid_cfg.get('height', 1920))
    if h > w:
        return 'short_9_16_captions'
    return 'long_form_16_9_captions'


def _build_caption_srt(job_id: str, config: dict, audio_path: Path,
                       script: dict) -> Path | None:
    """
    Transcribe the audio (or fall back to script narration) and write an SRT
    file with word-by-word entries. Returns the SRT path, or None if the
    format spec doesn't use captions.
    """
    out_dir = Path('output/captions')
    out_dir.mkdir(parents=True, exist_ok=True)
    srt_path = out_dir / f"{job_id}.srt"

    # Try whisper first
    try:
        from modules.caption_engine import _run_whisper  # type: ignore
        words, reliable = _run_whisper(
            audio_path,
            config.get('captions', {}).get('whisper_model', 'base'),
            job_id,
        )
        if reliable and words:
            payload = [{
                'word': (w.word or '').strip() or ' ',
                'start': float(w.start or 0.0),
                'end': float(w.end or 0.0),
            } for w in words]
            from modules.ffmpeg_renderer import _write_srt as _ws
            _ws(payload, srt_path)
            logger.info(
                f"[JOB {job_id}] Word-by-word SRT written: {srt_path} "
                f"({len(payload)} words)"
            )
            return srt_path
    except Exception as e:
        logger.warning(f"[JOB {job_id}] Whisper transcription failed: {e}")

    # Fallback: evenly distribute script narration words across audio_duration
    try:
        narration = (script or {}).get('narration', '').strip()
        if not narration:
            return None
        from modules.ffmpeg_renderer import _probe_duration, _write_srt as _ws
        dur = _probe_duration(audio_path, job_id)
        tokens = narration.split()
        if not tokens or dur <= 0:
            return None
        step = dur / max(1, len(tokens))
        payload = []
        for i, tok in enumerate(tokens):
            payload.append({
                'word': tok,
                'start': i * step,
                'end': (i + 1) * step,
            })
        _ws(payload, srt_path)
        logger.info(
            f"[JOB {job_id}] Fallback SRT written from script narration "
            f"({len(payload)} words)"
        )
        return srt_path
    except Exception as e:
        logger.warning(f"[JOB {job_id}] Fallback SRT failed: {e}")
        return None


def _assemble_via_ffmpeg(job_id: str, config: dict, stage_start: float) -> dict:
    """
    FFmpeg-direct renderer entry point. Reads the same script/audio/visual
    inputs as the MoviePy path, but routes the actual encode through
    modules.ffmpeg_renderer.render_video. Captures the actual command used so
    the production_evidence log (Block 5) can record it verbatim.
    """
    from modules.ffmpeg_renderer import render_video

    script_path = Path(f'output/scripts/{job_id}.json')
    if not script_path.exists():
        raise FileNotFoundError(
            f"Script not found: {script_path}. Run generate-script first."
        )
    with open(script_path, 'r', encoding='utf-8') as f:
        script = json.load(f)

    logger.info(
        f"[JOB {job_id}] Assembling via FFmpeg for topic: '{script.get('topic')}'"
    )

    audio_path = _resolve_audio(job_id, script, config)
    music_path = _find_music_file(job_id, config)

    visual_mode = config.get('pipeline', {}).get('visual_mode', 'images')
    background_path = None
    if visual_mode in ('background_loop', 'long_form_ambient'):
        background_path = _resolve_background_clip(job_id, config)
    if background_path is None:
        # FFmpeg renderer always needs a background clip; fall back to a
        # solid-colour generated clip if none configured.
        background_path = _ensure_solid_background_clip(job_id, config)

    format_spec = _resolve_ffmpeg_format_spec(config, script, job_id)

    caption_spec = None
    if format_spec in ('long_form_16_9_captions', 'short_9_16_captions'):
        srt = _build_caption_srt(job_id, config, audio_path, script)
        if srt is not None:
            caption_spec = {'srt_path': str(srt)}

    ambient_cfg = config.get('ambient', {}) or {}
    input_paths = {
        'voice': str(audio_path),
        'background': str(background_path),
        'music': str(music_path) if music_path else None,
        'music_volume_db': config.get('video', {}).get('music_volume_db', -18),
        'overlay_image': ambient_cfg.get('overlay_image') or None,
        'ambient_audio_path': ambient_cfg.get('ambient_audio_path') or None,
        'ambient_audio_db': ambient_cfg.get('ambient_audio_db', -22),
        'max_seconds': config.get('pipeline', {}).get('long_form_max_seconds', 10800),
    }

    target_lufs = float(
        (config.get('audio') or {}).get('loudness_target_lufs', -14.0)
    )

    output_dir = Path('output/videos')
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{job_id}_raw.mp4"

    result = render_video(
        job_id=job_id,
        input_paths=input_paths,
        output_path=str(output_path),
        format_spec=format_spec,
        audio_spec={'loudness_target_lufs': target_lufs},
        caption_spec=caption_spec,
    )

    if not result.get('success'):
        raise RuntimeError(
            f"FFmpeg render failed: {result.get('stderr') or 'unknown error'}"
        )

    update_job_field(job_id, 'raw_video_path', str(output_path))
    # Stash the renderer used + command for the production_evidence log.
    try:
        meta_dir = Path('output/render_meta')
        meta_dir.mkdir(parents=True, exist_ok=True)
        with open(meta_dir / f'{job_id}.json', 'w', encoding='utf-8') as f:
            json.dump({
                'renderer_used': 'ffmpeg',
                'format_spec': format_spec,
                'loudness_target_lufs': result.get('loudness_target_lufs'),
                'render_time_seconds': result.get('render_time_seconds'),
                'duration_seconds': result.get('duration_seconds'),
                'command': result.get('command'),
            }, f, indent=2)
    except Exception:
        logger.debug(f"[JOB {job_id}] Could not write render_meta sidecar",
                     exc_info=True)
    update_job_status(job_id, 'captioning')

    elapsed = time.time() - stage_start
    logger.info(
        f"[JOB {job_id}] assembly_engine (ffmpeg) COMPLETED in {elapsed:.1f}s"
    )
    return {'success': True, 'output_path': str(output_path)}


def _ensure_solid_background_clip(job_id: str, config: dict) -> Path:
    """
    Generate a 1s solid-colour background MP4 using FFmpeg if no real clip is
    configured. Allows the FFmpeg path to render even when channel assets are
    not yet populated (used in test runs).
    """
    placeholders_dir = Path('output/placeholders')
    placeholders_dir.mkdir(parents=True, exist_ok=True)
    out = placeholders_dir / 'solid_bg.mp4'
    if out.exists() and out.stat().st_size > 0:
        return out

    import shutil as _shutil
    import subprocess as _sp
    ffmpeg = _shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH — cannot create placeholder bg")

    w = int(config.get('video', {}).get('width', 1080))
    h = int(config.get('video', {}).get('height', 1920))
    cmd = [
        ffmpeg, '-y', '-hide_banner', '-nostats',
        '-f', 'lavfi',
        '-i', f"color=c=0x0F1023:s={w}x{h}:d=1:r=30",
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-t', '1',
        str(out),
    ]
    logger.info(
        f"[JOB {job_id}] Generating solid placeholder background: {out}"
    )
    _sp.run(cmd, check=True, capture_output=True)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def assemble_video(job_id: str, config: dict) -> dict:
    """
    Assemble a raw MP4 from images and audio for the given job.

    Handles missing audio (creates silent placeholder) and missing images
    (creates placeholder solid-colour frames) so the stage can be tested
    independently of Phases 3 and 4.

    Args:
        job_id (str):  Unique job identifier e.g. '001'.
        config (dict): Loaded config.json contents.

    Returns:
        dict: {
            'success': bool,
            'output_path': str,  # path to NNN_raw.mp4 if success
            'error': str         # error message if failed
        }
    """
    stage_start = time.time()
    logger.info(f"[JOB {job_id}] Starting assembly_engine")

    # ------------------------------------------------------------------
    # Phase 14 Block 1 — FFmpeg renderer dispatch
    # ------------------------------------------------------------------
    # If config.pipeline.renderer is 'ffmpeg' the channel opts into the
    # direct-FFmpeg path; default 'moviepy' keeps the existing behaviour.
    renderer_choice = config.get('pipeline', {}).get('renderer', 'moviepy')
    if str(renderer_choice).lower() == 'ffmpeg':
        try:
            return _assemble_via_ffmpeg(job_id, config, stage_start)
        except Exception as e:
            logger.error(
                f"[JOB {job_id}] FFmpeg renderer dispatch FAILED: {e}",
                exc_info=True,
            )
            update_job_status(
                job_id, 'failed',
                error_module='assembly_engine',
                error_message=f"ffmpeg path failed: {e}",
            )
            return {'success': False, 'error': str(e)}

    try:
        # ----------------------------------------------------------------
        # Load script for metadata (topic, estimated duration)
        # ----------------------------------------------------------------
        script_path = Path(f'output/scripts/{job_id}.json')
        if not script_path.exists():
            raise FileNotFoundError(
                f"Script not found: {script_path}. Run generate-script first."
            )
        with open(script_path, 'r', encoding='utf-8') as f:
            script = json.load(f)

        logger.info(f"[JOB {job_id}] Assembling video for topic: '{script.get('topic')}'")
        logger.debug(
            f"[JOB {job_id}] Config: "
            f"size={config['video']['width']}x{config['video']['height']}, "
            f"fps={config['video']['fps']}, "
            f"codec={config['video']['codec']}, "
            f"bitrate={config['video']['bitrate']}, "
            f"transition={config['video']['transition_duration']}s, "
            f"music_vol={config['video']['music_volume_db']}dB"
        )

        # ----------------------------------------------------------------
        # Resolve inputs — create placeholders if upstream stages skipped
        # ----------------------------------------------------------------
        audio_path = _resolve_audio(job_id, script, config)
        music_path = _find_music_file(job_id, config)

        # Decide visual source: background-loop clip vs image slideshow vs ambient
        visual_mode = config.get('pipeline', {}).get('visual_mode', 'images')
        background_path = None
        if visual_mode in ('background_loop', 'long_form_ambient'):
            background_path = _resolve_background_clip(job_id, config)
            if background_path is None:
                raise RuntimeError(
                    f"{visual_mode} requires video clips in "
                    "channels/<slug>/assets/backgrounds/ or ambient/ — found 0 clips."
                )

        # ----------------------------------------------------------------
        # Build video
        # ----------------------------------------------------------------
        if visual_mode == 'long_form_ambient' and background_path is not None:
            logger.info(
                f"[JOB {job_id}] Long-form ambient — "
                f"audio: {audio_path.name}, "
                f"background: {background_path.name}, "
                f"music: {music_path.name if music_path else 'none'}"
            )
            video = _build_long_form_ambient_video(
                background_path=background_path,
                audio_path=audio_path,
                music_path=music_path,
                config=config,
                job_id=job_id,
            )
        elif background_path is not None:
            logger.info(
                f"[JOB {job_id}] Inputs ready — "
                f"audio: {audio_path.name}, "
                f"background: {background_path.name}, "
                f"music: {music_path.name if music_path else 'none'}"
            )
            video = _build_background_video(
                background_path=background_path,
                audio_path=audio_path,
                music_path=music_path,
                config=config,
                job_id=job_id,
            )
        else:
            image_paths = _resolve_images(job_id, config)
            logger.info(
                f"[JOB {job_id}] Inputs ready — "
                f"audio: {audio_path.name}, "
                f"images: {len(image_paths)}, "
                f"music: {music_path.name if music_path else 'none'}"
            )
            video = _build_video(
                image_paths=image_paths,
                audio_path=audio_path,
                music_path=music_path,
                config=config,
                job_id=job_id
            )

        # ----------------------------------------------------------------
        # Export
        # ----------------------------------------------------------------
        output_dir = Path('output/videos')
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{job_id}_raw.mp4"

        codec = config['video']['codec']
        bitrate = config['video']['bitrate']
        fps = config['video']['fps']

        logger.info(
            f"[JOB {job_id}] Exporting video: {output_path} "
            f"(codec: {codec}, bitrate: {bitrate}, fps: {fps})"
        )
        export_start = time.time()

        video.write_videofile(
            str(output_path),
            fps=fps,
            codec='libx264',
            audio_codec='aac',
            bitrate=bitrate,
            threads=4,
            logger=None,       # suppress moviepy progress bar output
        )
        video.close()

        export_elapsed = time.time() - export_start
        size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info(
            f"[JOB {job_id}] File created: {output_path} "
            f"({size_mb:.2f} MB, export took {export_elapsed:.1f}s)"
        )

        # ----------------------------------------------------------------
        # Update database
        # ----------------------------------------------------------------
        update_job_field(job_id, 'raw_video_path', str(output_path))
        update_job_status(job_id, 'captioning')

        elapsed = time.time() - stage_start
        logger.info(f"[JOB {job_id}] assembly_engine COMPLETED in {elapsed:.1f}s")

        return {'success': True, 'output_path': str(output_path)}

    except Exception as e:
        elapsed = time.time() - stage_start
        logger.error(f"[JOB {job_id}] assembly_engine FAILED: {str(e)}", exc_info=True)
        update_job_status(job_id, 'failed', error_module='assembly_engine', error_message=str(e))
        return {'success': False, 'error': str(e)}
