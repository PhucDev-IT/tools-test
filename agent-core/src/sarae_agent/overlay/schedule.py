"""Random overlay mode: an exact port of web/src/overlay/overlaySchedule.ts (docs/LIVE_OVERLAYS.md §13).

When a timeline version's schedule is `random`, an overlay's `start_ms`..`start_ms + duration_ms` is the window it MAY
show in, `show_ms` at a time. Each loop of a video draws its own plan, seeded by `timeline_id:media_id:iteration`
(iteration = the engine's loop count), so the web editor, the OBS renderer and the Agent agree on the same loop. Integer
arithmetic only, 32-bit masking where JS works on uint32 (both are checked against tests/fixtures/overlay_schedule_vectors.json).
"""
from __future__ import annotations

from typing import Callable

DEFAULT_SCHEDULE = {"mode": "fixed", "every_ms": 120_000, "min_gap_ms": 20_000}
DEFAULT_SHOW_MS = 6000
STEP_MS = 100
_U32 = 0xFFFFFFFF


def _imul(a: int, b: int) -> int:
    """Math.imul, as uint32 (callers only use it modulo 2^32)."""
    return (a * b) & _U32


def seed_of(key: str) -> int:
    """FNV-1a 32-bit of a string (seeds are ASCII, so code points equal UTF-16 code units)."""
    h = 0x811C9DC5
    for ch in key:
        h = _imul((h ^ ord(ch)) & _U32, 0x01000193)
    return h


def generator(seed: int) -> Callable[[], int]:
    """mulberry32: a uint32 per call."""
    a = seed & _U32

    def next_() -> int:
        nonlocal a
        a = (a + 0x6D2B79F5) & _U32
        t = a
        t = _imul(t ^ (t >> 15), t | 1)
        t = (t ^ ((t + _imul(t ^ (t >> 7), t | 61)) & _U32)) & _U32
        return (t ^ (t >> 14)) & _U32
    return next_


def show_ms_of(overlay: dict) -> int:
    show = overlay.get("show_ms")
    return DEFAULT_SHOW_MS if show is None else show


def _js(value) -> str:
    """`${value}` of JS for the seed key (null → "null")."""
    return "null" if value is None else str(value)


def schedule_of(scene: dict) -> dict:
    return {**DEFAULT_SCHEDULE, **(scene.get("schedule") or {})}


def random_plan(overlays: list[dict], media_id: str | None, schedule: dict, timeline_id: int, iteration: int) -> list[dict]:
    """The appearances {index, start_ms, end_ms} of one loop of `media_id`: enabled overlays of that video, in list
    order (index = position in `overlays`). Pure: same input, same plan."""
    own = [(i, o) for i, o in enumerate(overlays) if o.get("enabled", True) is not False and o.get("media_id") == media_id]
    every = schedule.get("every_ms", DEFAULT_SCHEDULE["every_ms"])
    gap = schedule.get("min_gap_ms", DEFAULT_SCHEDULE["min_gap_ms"])
    if not own or every <= 0:
        return []
    next_ = generator(seed_of(f"{_js(timeline_id)}:{_js(media_id)}:{_js(iteration)}"))
    end = max(o["start_ms"] + o["duration_ms"] for _, o in own)
    shown: dict[int, int] = {}
    plan: list[dict] = []
    last, free = -1, None
    slot = 0
    while slot < end:
        fits = []
        for index, o in own:
            show = show_ms_of(o)
            lo = max(slot, o["start_ms"]) if free is None else max(slot, o["start_ms"], free)
            hi = min(slot + every, o["start_ms"] + o["duration_ms"]) - show
            if lo <= hi:
                fits.append((index, show, lo, hi))
        if fits:
            fewest = min(shown.get(f[0], 0) for f in fits)
            pool = [f for f in fits if shown.get(f[0], 0) == fewest]
            # Last chance first: an overlay whose window ends before the next slot could hold it.
            urgent = [f for f in pool if overlays[f[0]]["start_ms"] + overlays[f[0]]["duration_ms"] - f[1] < slot + every]
            pool = urgent or pool
            if len(pool) > 1:
                pool = [f for f in pool if f[0] != last]
            index, show, lo, hi = pool[next_() % len(pool)]
            start = lo + (next_() % ((hi - lo) // STEP_MS + 1)) * STEP_MS
            plan.append({"index": index, "start_ms": start, "end_ms": start + show})
            shown[index] = shown.get(index, 0) + 1
            last, free = index, start + show + gap
        slot += every
    return plan


def is_random(scene: dict) -> bool:
    return (scene.get("schedule") or {}).get("mode") == "random"
