"""
thumbnail_engine.py
===================
Stage 6b of the VideoForge pipeline. Generates a thumbnail in one of two modes:

  frame_capture   — Captures a still from the video, composites an optional PNG
                    overlay, and saves a portrait JPEG (1080×1920) for Shorts/TikTok.

  text_template   — Composes a landscape YouTube thumbnail (1280×720) using PIL:
                    bold 3–5 word text (from metadata thumbnail_text) on a dark
                    background with a per-channel accent colour, optional frame-
                    capture strip at the bottom.  Generates 2 colour variants so the
                    owner can pick the best one at the review gate.

Input:  job_id, config dict
        reads output/videos/NNN_captioned.mp4 (or NNN_raw.mp4 fallback)
        reads output/metadata/NNN.json  (for thumbnail_text in text_template mode)
        reads assets/thumbnail_template/overlay.png (optional, frame_capture mode)
Output: frame_capture  → output/thumbnails/NNN.jpg         (1080×1920 JPEG)
        text_template  → output/thumbnails/NNN_v1.jpg      (1280×720 JPEG)
                         output/thumbnails/NNN_v2.jpg      (1280×720 JPEG)
Logs:   logs/thumbnail_engine.log
"""

# 1. Standard library
import json
import textwrap
import time
from pathlib import Path

# 2. Third-party libraries
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

# 3. Local modules
from database import update_job_field
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger('thumbnail_engine')


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve_video(job_id: str) -> Path | None:
    """Return best available video file for frame capture, or None."""
    captioned = Path(f'output/videos/{job_id}_captioned.mp4')
    raw = Path(f'output/videos/{job_id}_raw.mp4')

    if captioned.exists():
        logger.info(f"[JOB {job_id}] Using captioned video for thumbnail: {captioned}")
        return captioned
    if raw.exists():
        logger.warning(f"[JOB {job_id}] Captioned video not found — falling back: {raw}")
        return raw
    logger.warning(f"[JOB {job_id}] No video found — will use placeholder")
    return None


def _capture_frame(video_path: Path, capture_seconds: float, job_id: str) -> np.ndarray:
    """Extract an RGB numpy frame at capture_seconds from video_path."""
    from moviepy import VideoFileClip

    logger.debug(f"[JOB {job_id}] Loading video for frame capture: {video_path}")
    clip = VideoFileClip(str(video_path))
    t = min(float(capture_seconds), clip.duration - 0.1)
    t = max(t, 0.0)
    logger.info(
        f"[JOB {job_id}] Capturing frame at t={t:.2f}s "
        f"(duration: {clip.duration:.2f}s)"
    )
    frame = clip.get_frame(t)
    clip.close()
    return frame


def _make_placeholder_frame(width: int, height: int, job_id: str) -> np.ndarray:
    """Dark gradient placeholder when no video is available."""
    logger.warning(f"[JOB {job_id}] Creating placeholder frame ({width}x{height})")
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    for y in range(height):
        intensity = int(15 + (y / height) * 20)
        frame[y, :] = [intensity, intensity + 5, intensity + 15]
    return frame


def _apply_overlay(base_img: Image.Image, overlay_path: Path, job_id: str) -> Image.Image:
    """Composite a RGBA PNG overlay onto the base image."""
    logger.info(f"[JOB {job_id}] Applying overlay: {overlay_path}")
    overlay = Image.open(str(overlay_path)).convert('RGBA')
    if overlay.size != base_img.size:
        overlay = overlay.resize(base_img.size, Image.LANCZOS)
    base_rgba = base_img.convert('RGBA')
    return Image.alpha_composite(base_rgba, overlay).convert('RGB')


def _load_font(font_path: str, size: int, job_id: str) -> ImageFont.FreeTypeFont:
    """Load a TTF font, falling back to PIL default if not found."""
    p = Path(font_path)
    if p.exists():
        try:
            return ImageFont.truetype(str(p), size)
        except Exception as e:
            logger.warning(f"[JOB {job_id}] Font load failed ({e}), using default")
    else:
        logger.warning(f"[JOB {job_id}] Font not found at {p}, using default")
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# text_template mode
# ---------------------------------------------------------------------------

def _generate_text_template_thumbnails(
    job_id: str,
    thumbnail_text: str,
    config: dict,
    video_path: Path | None,
) -> list:
    """
    Compose two landscape 1280×720 thumbnail variants using PIL.

    Each variant uses a different accent colour from config.thumbnail.text_template
    .variant_accents.  The layout is:
      - Full background in background_color
      - Thin accent bar at the very top (4% of height)
      - Bold, stroke-outlined text centred in the upper ~72% of the image
      - Optional frame-capture strip at the bottom (frame_strip_height_pct)

    Args:
        job_id (str):           Job identifier for logging.
        thumbnail_text (str):   3–5 word text from metadata.
        config (dict):          Loaded (merged) config.
        video_path (Path|None): Video file for the bottom strip, or None.

    Returns:
        list[str]: Paths to the two generated JPEG files.
    """
    tt_cfg = config['thumbnail']['text_template']
    W = tt_cfg.get('output_width', 1280)
    H = tt_cfg.get('output_height', 720)
    bg_color = tuple(tt_cfg.get('background_color', [15, 15, 25]))
    txt_color = tuple(tt_cfg.get('text_color', [255, 255, 255]))
    stroke_color = tuple(tt_cfg.get('stroke_color', [0, 0, 0]))
    stroke_width = tt_cfg.get('stroke_width', 5)
    font_size = tt_cfg.get('font_size', 110)
    font_file = tt_cfg.get('font_file', config['thumbnail'].get('font_file', 'assets/fonts/Arial-Bold.ttf'))
    strip_pct = tt_cfg.get('frame_strip_height_pct', 0.28)
    variant_accents = tt_cfg.get('variant_accents', [[59, 130, 246], [239, 68, 68]])

    accent_bar_h = max(8, int(H * 0.04))
    strip_h = int(H * strip_pct) if video_path else 0
    text_area_h = H - accent_bar_h - strip_h

    font = _load_font(font_file, font_size, job_id)

    # Wrap text to fit width — rough char-per-line estimate
    chars_per_line = max(8, int(W / (font_size * 0.55)))
    lines = textwrap.wrap(thumbnail_text.upper(), width=chars_per_line)

    # Capture frame strip once (same for both variants)
    strip_img = None
    if video_path and strip_h > 0:
        try:
            frame = _capture_frame(video_path, config['thumbnail'].get('frame_capture_at_seconds', 5), job_id)
            strip_img = Image.fromarray(frame, mode='RGB').resize((W, strip_h), Image.LANCZOS)
        except Exception as e:
            logger.warning(f"[JOB {job_id}] Frame strip capture failed: {e} — omitting strip")
            strip_h = 0
            text_area_h = H - accent_bar_h

    output_dir = Path('output/thumbnails')
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = []

    for variant_idx, accent_raw in enumerate(variant_accents[:2], start=1):
        accent = tuple(accent_raw)
        img = Image.new('RGB', (W, H), color=bg_color)
        draw = ImageDraw.Draw(img)

        # Accent bar at top
        draw.rectangle([(0, 0), (W, accent_bar_h)], fill=accent)

        # Draw text centred in the text area
        # Measure total text block height
        line_bboxes = [draw.textbbox((0, 0), ln, font=font) for ln in lines]
        line_heights = [bb[3] - bb[1] for bb in line_bboxes]
        line_gap = int(font_size * 0.15)
        total_text_h = sum(line_heights) + line_gap * (len(lines) - 1)

        y_start = accent_bar_h + (text_area_h - total_text_h) // 2
        y = y_start

        for ln, lh in zip(lines, line_heights):
            bbox = draw.textbbox((0, 0), ln, font=font)
            text_w = bbox[2] - bbox[0]
            x = (W - text_w) // 2

            # Stroke
            for dx in range(-stroke_width, stroke_width + 1):
                for dy in range(-stroke_width, stroke_width + 1):
                    if dx != 0 or dy != 0:
                        draw.text((x + dx, y + dy), ln, font=font, fill=stroke_color)
            draw.text((x, y), ln, font=font, fill=txt_color)
            y += lh + line_gap

        # Accent line separating text from strip
        if strip_img:
            sep_y = H - strip_h
            draw.rectangle([(0, sep_y - 3), (W, sep_y)], fill=accent)
            img.paste(strip_img, (0, sep_y))

        out_path = output_dir / f"{job_id}_v{variant_idx}.jpg"
        img.save(str(out_path), format='JPEG', quality=95, optimize=True)
        size_mb = out_path.stat().st_size / (1024 * 1024)
        logger.info(
            f"[JOB {job_id}] text_template variant {variant_idx} saved: "
            f"{out_path} ({size_mb:.3f} MB)"
        )
        paths.append(str(out_path))

    return paths


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_thumbnail(job_id: str, config: dict) -> dict:
    """
    Generate a thumbnail for the job.

    Mode is determined by config['thumbnail']['mode']:
      'text_template'  → 2 landscape 1280×720 variants; stores v1 path in DB by default.
      'frame_capture'  → 1 portrait 1080×1920; same behaviour as before.

    Args:
        job_id (str):  Unique job identifier e.g. '001'.
        config (dict): Loaded (merged) config.

    Returns:
        dict: {
            'success': bool,
            'output_path': str,          # primary thumbnail path
            'variant_paths': list[str],  # all variant paths (text_template only)
            'error': str                 # if failed
        }
    """
    stage_start = time.time()
    logger.info(f"[JOB {job_id}] Starting thumbnail_engine")

    try:
        thumb_cfg = config['thumbnail']
        mode = thumb_cfg.get('mode', 'frame_capture')
        logger.info(f"[JOB {job_id}] Thumbnail mode: {mode}")

        # ----------------------------------------------------------------
        # text_template mode
        # ----------------------------------------------------------------
        if mode == 'text_template':
            # Load thumbnail_text from metadata JSON
            meta_path = Path(f'output/metadata/{job_id}.json')
            thumbnail_text = ''
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding='utf-8'))
                    thumbnail_text = meta.get('thumbnail_text', '')
                except Exception as e:
                    logger.warning(f"[JOB {job_id}] Could not read metadata JSON: {e}")

            if not thumbnail_text:
                # Fallback to job topic
                from database import get_job
                job = get_job(job_id)
                thumbnail_text = job.get('topic', 'UNTITLED') if job else 'UNTITLED'
                logger.warning(
                    f"[JOB {job_id}] thumbnail_text missing — "
                    f"using topic as fallback: '{thumbnail_text}'"
                )

            video_path = _resolve_video(job_id)
            paths = _generate_text_template_thumbnails(job_id, thumbnail_text, config, video_path)

            if not paths:
                raise RuntimeError("No thumbnail variants were generated")

            # Default to v1 in DB; owner picks variant at review gate
            update_job_field(job_id, 'thumbnail_path', paths[0])

            elapsed = time.time() - stage_start
            logger.info(f"[JOB {job_id}] thumbnail_engine COMPLETED in {elapsed:.1f}s — {len(paths)} variants")
            return {
                'success': True,
                'output_path': paths[0],
                'variant_paths': paths,
            }

        # ----------------------------------------------------------------
        # frame_capture mode (original behaviour)
        # ----------------------------------------------------------------
        capture_seconds = thumb_cfg.get('frame_capture_at_seconds', 5)
        overlay_path_str = thumb_cfg.get('overlay_template', 'assets/thumbnail_template/overlay.png')
        width = thumb_cfg.get('width', 1080)
        height = thumb_cfg.get('height', 1920)

        logger.debug(
            f"[JOB {job_id}] Config: capture_at={capture_seconds}s, "
            f"size={width}x{height}, overlay={overlay_path_str}"
        )

        video_path = _resolve_video(job_id)

        if video_path:
            frame_array = _capture_frame(video_path, capture_seconds, job_id)
        else:
            frame_array = _make_placeholder_frame(width, height, job_id)

        img = Image.fromarray(frame_array, mode='RGB')
        if img.size != (width, height):
            img = img.resize((width, height), Image.LANCZOS)

        overlay_path = Path(overlay_path_str)
        if overlay_path.exists():
            img = _apply_overlay(img, overlay_path, job_id)
        else:
            logger.info(f"[JOB {job_id}] Overlay not found at {overlay_path} — skipping")

        output_dir = Path('output/thumbnails')
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{job_id}.jpg"

        img.save(str(output_path), format='JPEG', quality=95, optimize=True)
        size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info(f"[JOB {job_id}] File created: {output_path} ({size_mb:.3f} MB)")

        update_job_field(job_id, 'thumbnail_path', str(output_path))

        elapsed = time.time() - stage_start
        logger.info(f"[JOB {job_id}] thumbnail_engine COMPLETED in {elapsed:.1f}s")

        return {
            'success': True,
            'output_path': str(output_path),
            'variant_paths': [str(output_path)],
        }

    except Exception as e:
        elapsed = time.time() - stage_start
        logger.error(f"[JOB {job_id}] thumbnail_engine FAILED: {str(e)}", exc_info=True)
        return {'success': False, 'error': str(e)}
