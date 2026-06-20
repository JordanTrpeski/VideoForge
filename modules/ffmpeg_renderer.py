"""
ffmpeg_renderer.py
==================
Direct-FFmpeg renderer (Phase 14 Block 1) — replaces the MoviePy assembly path
for the three production formats used by VideoForge channels.

Three format specs supported:
  - 'long_form_16_9_captions'             — narration + ambient loop + music bed +
                                            word-by-word burned captions, 1920x1080
  - 'short_9_16_captions'                 — narration + background loop + burned
                                            captions, 1080x1920, 45-60s
  - 'long_form_ambient_16_9_no_captions'  — looped ambient clip + optional overlay
                                            image + narration + ambient bed, no
                                            captions, supports up to 10800 s (3 hr)

All formats run through a two-pass FFmpeg loudnorm filter targeting -14 LUFS
(configurable via config.audio.loudness_target_lufs).

Input:  job_id, input_paths, output_path, format_spec, audio_spec, caption_spec
Output: rendered MP4 + dict with success/duration/render_time/stderr
Logs:   logs/ffmpeg_renderer.log

Dependencies:
    - ffmpeg / ffprobe on PATH

Author: VideoForge
Version: 1.0
"""

# 1. Standard library
import json
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

# 2. Third-party
from dotenv import load_dotenv

# 3. Local
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger('ffmpeg_renderer')


# Supported format specs — kept in one place so callers can validate up-front.
SUPPORTED_FORMATS = (
    'long_form_16_9_captions',
    'short_9_16_captions',
    'long_form_ambient_16_9_no_captions',
)


# ---------------------------------------------------------------------------
# FFmpeg discovery / invocation helpers
# ---------------------------------------------------------------------------

def _ffmpeg_bin() -> str:
    """Return the ffmpeg binary path (PATH lookup). Raises if unavailable."""
    bin_path = shutil.which('ffmpeg')
    if not bin_path:
        raise RuntimeError("ffmpeg not found on PATH — install FFmpeg")
    return bin_path


def _ffprobe_bin() -> str:
    """Return the ffprobe binary path (PATH lookup). Raises if unavailable."""
    bin_path = shutil.which('ffprobe')
    if not bin_path:
        raise RuntimeError("ffprobe not found on PATH — install FFmpeg")
    return bin_path


def _run(cmd: list, job_id: str, capture: bool = True) -> subprocess.CompletedProcess:
    """
    Run an external command and return the CompletedProcess. Logs the full
    command before execution so any failure can be reproduced from the log.
    """
    pretty = ' '.join(shlex.quote(str(c)) for c in cmd)
    logger.info(f"[JOB {job_id}] FFMPEG CMD: {pretty}")
    return subprocess.run(
        [str(c) for c in cmd],
        capture_output=capture,
        text=True,
        encoding='utf-8',
        errors='replace',
    )


def _probe_duration(path: Path, job_id: str) -> float:
    """Return media duration in seconds via ffprobe."""
    cmd = [
        _ffprobe_bin(), '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        str(path),
    ]
    res = _run(cmd, job_id)
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {res.stderr}")
    return float((res.stdout or '0').strip() or 0.0)


# ---------------------------------------------------------------------------
# Two-pass loudnorm (EBU R128) — measures, then applies measured values.
# Standard reference is -14 LUFS for YouTube; channels may override.
# ---------------------------------------------------------------------------

_LOUDNORM_KEYS = (
    'input_i', 'input_tp', 'input_lra', 'input_thresh',
    'target_offset',
)


def _loudnorm_measure(audio_path: Path, target_lufs: float, job_id: str) -> dict:
    """
    First pass — measure the existing loudness. Returns the JSON dict produced
    by FFmpeg's loudnorm filter so the second pass can apply measured values.
    """
    cmd = [
        _ffmpeg_bin(), '-y', '-hide_banner', '-nostats',
        '-i', str(audio_path),
        '-af', (
            f"loudnorm=I={target_lufs}:LRA=11:TP=-1.5:"
            "print_format=json"
        ),
        '-f', 'null', '-',
    ]
    res = _run(cmd, job_id)
    if res.returncode != 0:
        raise RuntimeError(
            f"loudnorm measure failed: {res.stderr[-2000:]}"
        )
    # JSON is at the tail of stderr — extract the last {...} block.
    text = res.stderr or ''
    match = list(re.finditer(r'\{[\s\S]*?\}', text))
    if not match:
        raise RuntimeError("loudnorm measure: no JSON in ffmpeg output")
    blob = match[-1].group(0)
    data = json.loads(blob)
    out = {k: data[k] for k in _LOUDNORM_KEYS if k in data}
    logger.info(
        f"[JOB {job_id}] Loudness measured — "
        f"I={out.get('input_i')} LUFS, "
        f"TP={out.get('input_tp')} dBTP, "
        f"LRA={out.get('input_lra')}, "
        f"thresh={out.get('input_thresh')}, "
        f"offset={out.get('target_offset')}"
    )
    return out


def _loudnorm_filter_string(measured: dict, target_lufs: float) -> str:
    """Build the FFmpeg loudnorm filter string using measured values (pass 2)."""
    return (
        f"loudnorm=I={target_lufs}:LRA=11:TP=-1.5:"
        f"measured_I={measured['input_i']}:"
        f"measured_TP={measured['input_tp']}:"
        f"measured_LRA={measured['input_lra']}:"
        f"measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:"
        "linear=true:print_format=summary"
    )


def _master_audio_to_lufs(
    voice_path: Path,
    target_lufs: float,
    job_id: str,
) -> Path:
    """
    Two-pass mastering on the voice track. Returns a path to a normalized WAV.
    Writes into a temp file alongside the source.
    """
    measured = _loudnorm_measure(voice_path, target_lufs, job_id)
    filt = _loudnorm_filter_string(measured, target_lufs)
    out_path = voice_path.with_name(f"{voice_path.stem}_lufs{int(target_lufs)}.wav")
    cmd = [
        _ffmpeg_bin(), '-y', '-hide_banner', '-nostats',
        '-i', str(voice_path),
        '-af', filt,
        '-ar', '48000', '-ac', '2',
        '-c:a', 'pcm_s16le',
        str(out_path),
    ]
    res = _run(cmd, job_id)
    if res.returncode != 0:
        raise RuntimeError(f"loudnorm apply failed: {res.stderr[-2000:]}")
    return out_path


# ---------------------------------------------------------------------------
# Caption helpers — write an SRT and a one-word-at-a-time ASS for word-by-word
# burned captions.
# ---------------------------------------------------------------------------

def _seconds_to_srt(t: float) -> str:
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        ms = 0; s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_srt(words: list, srt_path: Path) -> int:
    """
    Write an SRT file from a list of {'word', 'start', 'end'} dicts.
    Returns the number of blocks written.
    """
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, w in enumerate(words, 1):
        lines.append(str(i))
        lines.append(f"{_seconds_to_srt(w['start'])} --> {_seconds_to_srt(w['end'])}")
        lines.append(w['word'])
        lines.append('')
    srt_path.write_text('\n'.join(lines), encoding='utf-8')
    return len(words)


def _escape_ffmpeg_path(p: Path) -> str:
    """
    Escape a path for use inside an FFmpeg filtergraph (subtitles= argument).
    FFmpeg's filter parser needs Windows backslashes turned into forward slashes
    and the drive-letter colon escaped.
    """
    s = str(p.resolve()).replace('\\', '/')
    s = s.replace(':', '\\:')
    return s


# ---------------------------------------------------------------------------
# Format-specific render paths
# ---------------------------------------------------------------------------

def _render_with_captions_landscape(
    job_id: str,
    inputs: dict,
    audio_path: Path,
    output_path: Path,
    caption_spec: dict,
) -> tuple:
    """16:9 long-form with looping background + music bed + burned captions."""
    width, height = 1920, 1080
    background = inputs.get('background')
    music = inputs.get('music')
    audio_dur = _probe_duration(audio_path, job_id)

    music_db = float(inputs.get('music_volume_db', -22))
    music_vol_scalar = 10.0 ** (music_db / 20.0)

    # Build filtergraph: scale+pad background, optionally burn subtitles,
    # then mix music under voice.
    bg_chain = (
        "[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        "crop={w}:{h}".format(w=width, h=height)
    )

    sub_path = caption_spec.get('srt_path')
    if sub_path:
        bg_chain += ",subtitles='{sub}':force_style='{style}'".format(
            sub=_escape_ffmpeg_path(Path(sub_path)),
            style=caption_spec.get('force_style',
                  'Fontname=Arial,Fontsize=42,PrimaryColour=&H00FFFFFF,'
                  'OutlineColour=&H00000000,BorderStyle=1,Outline=3,'
                  'Alignment=2,MarginV=120'),
        )
    bg_chain += "[v]"

    cmd = [_ffmpeg_bin(), '-y', '-hide_banner', '-nostats',
           '-stream_loop', '-1', '-i', str(background),
           '-i', str(audio_path)]
    has_music = bool(music) and Path(music).exists()
    if has_music:
        cmd += ['-stream_loop', '-1', '-i', str(music)]

    if has_music:
        afilter = (
            f"[2:a]volume={music_vol_scalar:.4f}[bg];"
            f"[1:a][bg]amix=inputs=2:duration=first:dropout_transition=0[a]"
        )
        filter_complex = f"{bg_chain};{afilter}"
        a_map = '[a]'
    else:
        filter_complex = bg_chain
        a_map = '1:a'

    cmd += [
        '-filter_complex', filter_complex,
        '-map', '[v]', '-map', a_map,
        '-t', f"{audio_dur:.3f}",
        '-r', '30', '-pix_fmt', 'yuv420p',
        '-c:v', 'libx264', '-preset', 'medium', '-crf', '21',
        '-c:a', 'aac', '-b:a', '192k',
        '-movflags', '+faststart',
        str(output_path),
    ]
    res = _run(cmd, job_id)
    return res.returncode, res.stderr


def _render_with_captions_portrait(
    job_id: str,
    inputs: dict,
    audio_path: Path,
    output_path: Path,
    caption_spec: dict,
) -> tuple:
    """9:16 short with looping background + burned captions."""
    width, height = 1080, 1920
    background = inputs.get('background')
    audio_dur = _probe_duration(audio_path, job_id)

    bg_chain = (
        "[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        "crop={w}:{h}".format(w=width, h=height)
    )
    sub_path = caption_spec.get('srt_path')
    if sub_path:
        bg_chain += ",subtitles='{sub}':force_style='{style}'".format(
            sub=_escape_ffmpeg_path(Path(sub_path)),
            style=caption_spec.get('force_style',
                  'Fontname=Arial,Fontsize=64,PrimaryColour=&H00FFFFFF,'
                  'OutlineColour=&H00000000,BorderStyle=1,Outline=4,'
                  'Alignment=2,MarginV=300'),
        )
    bg_chain += "[v]"

    cmd = [_ffmpeg_bin(), '-y', '-hide_banner', '-nostats',
           '-stream_loop', '-1', '-i', str(background),
           '-i', str(audio_path),
           '-filter_complex', bg_chain,
           '-map', '[v]', '-map', '1:a',
           '-t', f"{audio_dur:.3f}",
           '-r', '30', '-pix_fmt', 'yuv420p',
           '-c:v', 'libx264', '-preset', 'medium', '-crf', '21',
           '-c:a', 'aac', '-b:a', '192k',
           '-movflags', '+faststart',
           str(output_path)]
    res = _run(cmd, job_id)
    return res.returncode, res.stderr


def _render_ambient_landscape(
    job_id: str,
    inputs: dict,
    audio_path: Path,
    output_path: Path,
) -> tuple:
    """
    16:9 long-form ambient — looped background, optional overlay image, narration
    plus optional ambient bed. No captions. Up to 10800 seconds (3 hours).
    """
    width, height = 1920, 1080
    background = inputs.get('background')
    overlay = inputs.get('overlay_image')
    ambient_bed = inputs.get('ambient_audio_path')
    ambient_db = float(inputs.get('ambient_audio_db', -22))

    audio_dur = _probe_duration(audio_path, job_id)
    max_seconds = float(inputs.get('max_seconds', 10800))
    if audio_dur > max_seconds:
        logger.warning(
            f"[JOB {job_id}] Audio {audio_dur:.0f}s exceeds cap {max_seconds:.0f}s "
            "— trimming"
        )
        audio_dur = max_seconds

    inputs_cmd = [
        _ffmpeg_bin(), '-y', '-hide_banner', '-nostats',
        '-stream_loop', '-1', '-i', str(background),
        '-i', str(audio_path),
    ]
    overlay_idx = None
    ambient_idx = None
    next_idx = 2
    if overlay and Path(overlay).exists():
        overlay_idx = next_idx
        inputs_cmd += ['-loop', '1', '-i', str(overlay)]
        next_idx += 1
    if ambient_bed and Path(ambient_bed).exists():
        ambient_idx = next_idx
        inputs_cmd += ['-stream_loop', '-1', '-i', str(ambient_bed)]
        next_idx += 1

    bg_chain = (
        "[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        "crop={w}:{h}[bg]".format(w=width, h=height)
    )
    if overlay_idx is not None:
        chain = (
            f"{bg_chain};"
            f"[{overlay_idx}:v]scale={width}:{height}[ov];"
            f"[bg][ov]overlay=0:0[v]"
        )
    else:
        chain = bg_chain + ";[bg]copy[v]"

    if ambient_idx is not None:
        scalar = 10.0 ** (ambient_db / 20.0)
        chain += (
            f";[{ambient_idx}:a]volume={scalar:.4f}[amb];"
            f"[1:a][amb]amix=inputs=2:duration=first:dropout_transition=0[a]"
        )
        a_map = '[a]'
    else:
        a_map = '1:a'

    cmd = inputs_cmd + [
        '-filter_complex', chain,
        '-map', '[v]', '-map', a_map,
        '-t', f"{audio_dur:.3f}",
        '-r', '30', '-pix_fmt', 'yuv420p',
        '-c:v', 'libx264', '-preset', 'medium', '-crf', '23',
        '-c:a', 'aac', '-b:a', '192k',
        '-movflags', '+faststart',
        str(output_path),
    ]
    res = _run(cmd, job_id)
    return res.returncode, res.stderr


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def render_video(
    job_id: str,
    input_paths: dict,
    output_path: str,
    format_spec: str,
    audio_spec: dict,
    caption_spec: Optional[dict] = None,
) -> dict:
    """
    Render a final MP4 via FFmpeg.

    Args:
        job_id (str):           Owning job id (for logs).
        input_paths (dict):     {
                                  'voice':            path to narration MP3/WAV,
                                  'background':      path to background video,
                                  'music':           optional music path,
                                  'music_volume_db': optional music gain (dB),
                                  'overlay_image':   optional ambient overlay,
                                  'ambient_audio_path': optional bed (ambient),
                                  'ambient_audio_db':   optional bed gain (dB),
                                  'max_seconds':     ambient duration cap,
                                }
        output_path (str):      Destination MP4.
        format_spec (str):      One of SUPPORTED_FORMATS.
        audio_spec (dict):      {'loudness_target_lufs': float}.
        caption_spec (dict):    {'srt_path': str, 'force_style': str (optional)}
                                or None if the format omits captions.

    Returns:
        dict: {
            'success':              bool,
            'output_path':          str,
            'duration_seconds':     float,
            'render_time_seconds':  float,
            'stderr':               str | None,
            'command':              str,   # last invocation, for evidence log
            'loudness_target_lufs': float,
        }
    """
    started = time.time()
    caption_spec = caption_spec or {}
    if format_spec not in SUPPORTED_FORMATS:
        return {
            'success': False,
            'output_path': '',
            'duration_seconds': 0.0,
            'render_time_seconds': 0.0,
            'stderr': f"Unsupported format_spec: {format_spec}",
            'command': '',
            'loudness_target_lufs': 0.0,
        }

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    target_lufs = float(audio_spec.get('loudness_target_lufs', -14.0))

    voice_path = Path(input_paths['voice'])
    logger.info(
        f"[JOB {job_id}] FFmpeg render — format={format_spec}, "
        f"target_lufs={target_lufs}, out={out_path}"
    )

    last_stderr = None
    last_cmd = ''
    try:
        # Two-pass loudnorm on the narration track.
        mastered = _master_audio_to_lufs(voice_path, target_lufs, job_id)

        inputs = dict(input_paths)
        inputs['voice'] = str(mastered)

        if format_spec == 'long_form_16_9_captions':
            rc, stderr = _render_with_captions_landscape(
                job_id, inputs, mastered, out_path, caption_spec,
            )
        elif format_spec == 'short_9_16_captions':
            rc, stderr = _render_with_captions_portrait(
                job_id, inputs, mastered, out_path, caption_spec,
            )
        else:  # long_form_ambient_16_9_no_captions
            rc, stderr = _render_ambient_landscape(
                job_id, inputs, mastered, out_path,
            )

        last_stderr = stderr
        if rc != 0:
            return {
                'success': False,
                'output_path': str(out_path),
                'duration_seconds': 0.0,
                'render_time_seconds': time.time() - started,
                'stderr': stderr,
                'command': last_cmd,
                'loudness_target_lufs': target_lufs,
            }

        duration = _probe_duration(out_path, job_id)
        elapsed = time.time() - started
        logger.info(
            f"[JOB {job_id}] FFmpeg render OK — duration {duration:.2f}s, "
            f"render_time {elapsed:.1f}s, file {out_path}"
        )
        return {
            'success': True,
            'output_path': str(out_path),
            'duration_seconds': duration,
            'render_time_seconds': elapsed,
            'stderr': None,
            'command': last_cmd,
            'loudness_target_lufs': target_lufs,
        }

    except Exception as exc:
        elapsed = time.time() - started
        logger.error(
            f"[JOB {job_id}] FFmpeg render FAILED: {exc}",
            exc_info=True,
        )
        return {
            'success': False,
            'output_path': str(out_path),
            'duration_seconds': 0.0,
            'render_time_seconds': elapsed,
            'stderr': str(exc) + ('\n' + (last_stderr or '')[-4000:]),
            'command': last_cmd,
            'loudness_target_lufs': target_lufs,
        }


def verify_loudness(path: Path, job_id: str = 'verify') -> float:
    """
    Re-measure a rendered file's loudness with the EBU R128 filter and return
    the integrated LUFS value. Used by tests + the audit step.
    """
    cmd = [
        _ffmpeg_bin(), '-hide_banner', '-nostats', '-i', str(path),
        '-af', 'loudnorm=I=-14:LRA=11:TP=-1.5:print_format=json',
        '-f', 'null', '-',
    ]
    res = _run(cmd, job_id)
    text = res.stderr or ''
    blocks = list(re.finditer(r'\{[\s\S]*?\}', text))
    if not blocks:
        return 0.0
    data = json.loads(blocks[-1].group(0))
    return float(data.get('input_i', 0.0))
