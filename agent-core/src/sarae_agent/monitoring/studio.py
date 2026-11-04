"""Locate TikTok LIVE Studio by process executable identity, not by window title alone."""
from __future__ import annotations

import os
from pathlib import Path

from sarae_agent.windows import win32

# TikTok has shipped more than one executable name across releases.
DEFAULT_NAMES = ("tiktok live studio", "tiktoklivestudio", "tiktok_live_studio")


def _normalize(value: str) -> str:
    return value.lower().replace("-", "").replace("_", "").replace(" ", "")


def is_studio_executable(name: str, extra_names: str = "") -> bool:
    normalized = _normalize(name)
    allowed = {_normalize(v) for v in (",".join(DEFAULT_NAMES) + "," + extra_names).split(",") if v.strip()}
    return normalized.endswith(".exe") and normalized[:-4] in allowed


def find_studio_window(extra_names: str | None = None) -> dict | None:
    """Largest visible top-level window owned by a TikTok LIVE Studio process (non-minimized first)."""
    extra = extra_names if extra_names is not None else os.getenv("SARAE_TIKTOK_EXE_NAMES", "")
    image_cache: dict[int, str | None] = {}
    candidates = []
    for window in win32.top_level_windows():
        pid = window["pid"]
        if pid not in image_cache:
            image_cache[pid] = win32.process_image_path(pid)
        exe_path = image_cache[pid]
        if not exe_path or not is_studio_executable(Path(exe_path).name, extra):
            continue
        left, top, right, bottom = window["rect"]
        area = max(0, right - left) * max(0, bottom - top)
        if area < 120 * 100:
            continue
        candidates.append((area, {"hwnd": window["hwnd"], "pid": pid, "exe_name": Path(exe_path).name,
                                  "exe_path": exe_path, "title": window["title"],
                                  "minimized": window["minimized"]}))
    if not candidates:
        return None
    return max(candidates, key=lambda row: (not row[1]["minimized"], row[0]))[1]


def studio_process_running(extra_names: str | None = None) -> bool:
    extra = extra_names if extra_names is not None else os.getenv("SARAE_TIKTOK_EXE_NAMES", "")
    try:
        import psutil
    except ImportError:
        return False
    for proc in psutil.process_iter(["name"]):
        name = proc.info.get("name") or ""
        if is_studio_executable(name, extra):
            return True
    return False
