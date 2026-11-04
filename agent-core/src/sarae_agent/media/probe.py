"""FFprobe metadata used before handing a local file to OBS.

OBS receives the original file for zero-rotation media whose aspect ratio matches
the OBS canvas (a 720x1280 video on a 1080x1920 canvas is only scaled by OBS).
Rotated files get a cached, display-oriented copy, and files of another aspect
ratio a canvas-sized copy over a blurred fill of themselves; the source file is
never changed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from sarae_agent.config import paths

log = logging.getLogger("sarae.media.probe")

# Display aspect ratios within 1% of the canvas count as the same (rounded encoder sizes such as 1080x1918).
ASPECT_TOLERANCE = 0.01
# One FFmpeg run per cached copy: a pre-LIVE warm-up and playback may ask for the same file at once.
_render_locks: dict[str, threading.Lock] = {}
_render_locks_guard = threading.Lock()


@dataclass(frozen=True)
class MediaInfo:
    width: int
    height: int
    fps: str
    rotation: int
    audio: bool
    sample_rate: int | None
    channels: int | None


def probe_media(path: Path) -> MediaInfo:
    if not shutil.which("ffprobe"):
        raise FileNotFoundError("Thiếu ffprobe.exe trong PATH của Windows Agent")
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height,avg_frame_rate,sample_rate,channels:stream_tags=rotate:stream_side_data=rotation",
         "-of", "json", str(path)], capture_output=True, text=True, timeout=15, check=True,
    )
    streams = json.loads(result.stdout).get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("File không có video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    side = next((s for s in video.get("side_data_list", []) if "rotation" in s), {})
    angle = side.get("rotation", video.get("tags", {}).get("rotate", 0))
    rotation = round(float(angle)) % 360
    if rotation not in (0, 90, 180, 270):
        raise ValueError(f"Rotation metadata không hỗ trợ: {angle}")
    info = MediaInfo(int(video["width"]), int(video["height"]),
                     video.get("avg_frame_rate") or "0/0", rotation, bool(audio),
                     int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
                     int(audio["channels"]) if audio and audio.get("channels") else None)
    log.info("Media %s: %sx%s fps=%s rotation=%s audio=%s sample_rate=%s channels=%s",
             path.name, info.width, info.height, info.fps, info.rotation, info.audio,
             info.sample_rate, info.channels)
    return info


def same_aspect(width: int, height: int, canvas: tuple[int, int]) -> bool:
    ratio, target = width / height, canvas[0] / canvas[1]
    return abs(ratio - target) <= ASPECT_TOLERANCE * target


def _blur_fill(canvas: tuple[int, int]) -> list[str]:
    """Video fitted inside the canvas over a darkened, blurred, canvas-filling copy of itself (blurred small: fast)."""
    width, height = canvas
    small_w, small_h = max(2, width // 4 // 2 * 2), max(2, height // 4 // 2 * 2)
    radius = max(1, min(10, min(small_w, small_h) // 4))  # boxblur caps the radius at half a chroma plane side
    graph = (f"[0:v]split=2[bg][fg];"
             f"[bg]scale={small_w}:{small_h}:force_original_aspect_ratio=increase,crop={small_w}:{small_h},"
             f"boxblur={radius}:2,eq=brightness=-0.08,scale={width}:{height},setsar=1[fill];"
             f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[fit];"
             f"[fill][fit]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]")
    return ["-filter_complex", graph, "-map", "[v]"]


def obs_file(path: Path, info: MediaInfo, canvas: tuple[int, int] | None = None) -> Path:
    """Bake display matrix into a local cache copy using FFmpeg autorotation; with ``canvas`` (OBS base width,
    height), a video of another aspect ratio becomes a canvas-sized copy with a blurred fill instead of black bars."""
    width, height = (info.height, info.width) if info.rotation in (90, 270) else (info.width, info.height)
    fill = canvas is not None and not same_aspect(width, height, canvas)
    if info.rotation == 0 and not fill:
        return path
    if not shutil.which("ffmpeg"):
        raise FileNotFoundError("Thiếu ffmpeg.exe trong PATH của Windows Agent")
    stat = path.stat()
    variant = f":fill={canvas[0]}x{canvas[1]}" if fill else ""
    key = hashlib.sha256(f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}{variant}".encode()).hexdigest()
    folder = paths.root / "obs_media"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{key}.mp4"
    with _render_locks_guard:
        lock = _render_locks.setdefault(key, threading.Lock())
    with lock:
        if target.is_file():
            return target
        temporary = folder / f"{key}.partial.mp4"
        video = _blur_fill(canvas) if fill else ["-map", "0:v:0"]
        try:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                            *video, "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
                            "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", "-metadata:s:v:0",
                            "rotate=0", str(temporary)], capture_output=True, text=True, timeout=1800,
                           check=True)
            rendered = probe_media(temporary)
            expected = canvas if fill else (width, height)
            if rendered.rotation != 0 or (rendered.width, rendered.height) != expected or rendered.audio != info.audio:
                raise ValueError("FFmpeg không chuẩn hóa đúng orientation/khung hình/audio")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        if fill:
            log.info("Media %s: %sx%s -> %sx%s nền mờ", path.name, width, height, *canvas)
        return target
