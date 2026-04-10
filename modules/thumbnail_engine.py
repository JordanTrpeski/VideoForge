"""
thumbnail_engine.py
===================
Stage 6b of the VideoForge pipeline. Captures a still frame from the
captioned video at a configured timestamp, optionally composites a PNG
overlay, and saves the result as a JPEG thumbnail.

If the captioned video does not exist yet, falls back to the raw video.
If neither video exists, creates a solid dark placeholder image.

Input:  job_id, config dict
        reads output/videos/NNN_captioned.mp4 (or NNN_raw.mp4 fallback)
        reads assets/thumbnail_template/overlay.png (optional)
Output: output/thumbnails/NNN.jpg  (1080x1920 JPEG)
Logs:   logs/thumbnail_engine.log

Dependencies:
    - moviepy 2.x  (frame capture)
    - Pillow        (image composition + JPEG export)

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import time
from pathlib import Path

# 2. Third-party libraries
import numpy as np
from PIL import Image
from dotenv import load_dotenv

# 3. Local modules
from database import update_job_field
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger('thumbnail_engine')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_video(job_id: str, job_id_str: str) -> Path | None:
    """
    Return the best available video file for thumbnail capture.
    Prefers the captioned version, falls back to raw, then None.

    Args:
        job_id (str):     Job identifier for log context.
        job_id_str (str): Same value — kept separate for clarity.

    Returns:
        Path | None: Path to the video file, or None if neither exists.
    """
    captioned = Path(f'output/videos/{job_id_str}_captioned.mp4')
    raw = Path(f'output/videos/{job_id_str}_raw.mp4')

    if captioned.exists():
        logger.info(f"[JOB {job_id}] Using captioned video for thumbnail: {captioned}")
        return captioned
    if raw.exists():
        logger.warning(
            f"[JOB {job_id}] Captioned video not found — "
            f"falling back to raw video: {raw}"
        )
        return raw

    logger.warning(
        f"[JOB {job_id}] No video found — "
        "will create solid-colour placeholder thumbnail"
    )
    return None


def _capture_frame(video_path: Path, capture_seconds: float, job_id: str) -> np.ndarray:
    """
    Extract a single frame from the video at the given timestamp.

    Args:
        video_path (Path):      Video file to read.
        capture_seconds (float): Timestamp in seconds.
        job_id (str):           Job identifier for log context.

    Returns:
        np.ndarray: RGB frame array of shape (H, W, 3).
    """
    from moviepy import VideoFileClip

    logger.debug(f"[JOB {job_id}] Loading video for frame capture: {video_path}")
    clip = VideoFileClip(str(video_path))

    # Clamp to valid range — leave 0.1s margin before end
    t = min(float(capture_seconds), clip.duration - 0.1)
    if t < 0:
        t = 0.0

    logger.info(
        f"[JOB {job_id}] Capturing frame at t={t:.2f}s "
        f"(video duration: {clip.duration:.2f}s)"
    )
    frame = clip.get_frame(t)  # numpy (H, W, 3) uint8 RGB
    clip.close()
    return frame


def _make_placeholder_frame(width: int, height: int, job_id: str) -> np.ndarray:
    """
    Create a solid dark-gradient placeholder when no video is available.

    Args:
        width (int):  Frame width in pixels.
        height (int): Frame height in pixels.
        job_id (str): Job identifier for log context.

    Returns:
        np.ndarray: RGB array of shape (height, width, 3).
    """
    logger.warning(
        f"[JOB {job_id}] Creating placeholder thumbnail frame ({width}x{height})"
    )
    # Dark gradient: very dark navy at top, slightly lighter at bottom
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    for y in range(height):
        intensity = int(15 + (y / height) * 20)
        frame[y, :] = [intensity, intensity + 5, intensity + 15]
    return frame


def _apply_overlay(base_img: Image.Image, overlay_path: Path, job_id: str) -> Image.Image:
    """
    Composite a PNG overlay onto the base image using its alpha channel.
    The overlay is resized to match the base image if dimensions differ.

    Args:
        base_img (Image.Image): RGB base thumbnail image.
        overlay_path (Path):    Path to the RGBA PNG overlay file.
        job_id (str):           Job identifier for log context.

    Returns:
        Image.Image: Composited RGB image.
    """
    logger.info(f"[JOB {job_id}] Applying overlay: {overlay_path}")
    overlay = Image.open(str(overlay_path)).convert('RGBA')

    if overlay.size != base_img.size:
        logger.debug(
            f"[JOB {job_id}] Resizing overlay from {overlay.size} "
            f"to {base_img.size}"
        )
        overlay = overlay.resize(base_img.size, Image.LANCZOS)

    # Composite: paste overlay over base using overlay's alpha as mask
    base_rgba = base_img.convert('RGBA')
    composited = Image.alpha_composite(base_rgba, overlay)
    return composited.convert('RGB')


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_thumbnail(job_id: str, config: dict) -> dict:
    """
    Capture a video frame, apply any overlay, and save a JPEG thumbnail.

    Falls back gracefully if the video or overlay is not available:
    - No video -> solid dark placeholder image
    - No overlay -> skip compositing, save frame directly

    Args:
        job_id (str):  Unique job identifier e.g. '001'.
        config (dict): Loaded config.json contents.

    Returns:
        dict: {
            'success': bool,
            'output_path': str,  # path to NNN.jpg if success
            'error': str         # error message if failed
        }
    """
    stage_start = time.time()
    logger.info(f"[JOB {job_id}] Starting thumbnail_engine")

    try:
        thumb_cfg = config['thumbnail']
        capture_seconds = thumb_cfg['frame_capture_at_seconds']
        overlay_path_str = thumb_cfg['overlay_template']
        width = thumb_cfg['width']
        height = thumb_cfg['height']

        logger.debug(
            f"[JOB {job_id}] Config: capture_at={capture_seconds}s, "
            f"size={width}x{height}, overlay={overlay_path_str}"
        )

        # ----------------------------------------------------------------
        # Get frame
        # ----------------------------------------------------------------
        video_path = _resolve_video(job_id, job_id)

        if video_path:
            frame_array = _capture_frame(video_path, capture_seconds, job_id)
        else:
            frame_array = _make_placeholder_frame(width, height, job_id)

        # ----------------------------------------------------------------
        # Convert to PIL and resize to target dimensions
        # ----------------------------------------------------------------
        img = Image.fromarray(frame_array, mode='RGB')

        if img.size != (width, height):
            logger.debug(
                f"[JOB {job_id}] Resizing frame from {img.size} to ({width}, {height})"
            )
            img = img.resize((width, height), Image.LANCZOS)

        # ----------------------------------------------------------------
        # Apply overlay if it exists
        # ----------------------------------------------------------------
        overlay_path = Path(overlay_path_str)
        if overlay_path.exists():
            img = _apply_overlay(img, overlay_path, job_id)
        else:
            logger.info(
                f"[JOB {job_id}] Overlay not found at {overlay_path} — skipping"
            )

        # ----------------------------------------------------------------
        # Save as JPEG
        # ----------------------------------------------------------------
        output_dir = Path('output/thumbnails')
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{job_id}.jpg"

        img.save(str(output_path), format='JPEG', quality=95, optimize=True)

        size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info(f"[JOB {job_id}] File created: {output_path} ({size_mb:.3f} MB)")

        # ----------------------------------------------------------------
        # Update database (thumbnail_path only — status already set by
        # metadata_engine or stays as-is if running out of order)
        # ----------------------------------------------------------------
        update_job_field(job_id, 'thumbnail_path', str(output_path))

        elapsed = time.time() - stage_start
        logger.info(f"[JOB {job_id}] thumbnail_engine COMPLETED in {elapsed:.1f}s")

        return {'success': True, 'output_path': str(output_path)}

    except Exception as e:
        elapsed = time.time() - stage_start
        logger.error(f"[JOB {job_id}] thumbnail_engine FAILED: {str(e)}", exc_info=True)
        return {'success': False, 'error': str(e)}
