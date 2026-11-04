"""How the timeline scheduler pins on TikTok, and how it knows the broadcast is on air.

- ``ConsolePinExecutor`` clicks on the LIVE Console through AutoPinService (Playwright worker, the same code as a
  manual pin). ``DryRunPinExecutor`` only logs what would be clicked: the default until the operator turns
  ``timeline.dry_run`` off in the Agent config.
- ``ConsoleLiveGate`` reads LIVE / OFFLINE / UNKNOWN from the Console page every few seconds (never blocking the
  playback watchdog); a reading older than ``max_age`` counts as unknown. ``AssumeLiveGate`` opens the gate without a
  Console and is only used together with the dry-run executor (tests, rehearsal).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger("sarae.timeline")


class DryRunPinExecutor:
    dry_run = True

    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []
        self.pinned: str | None = None  # the card a real Console would now show pinned

    def submit(self, action: str, target: dict | None, current: Callable[[], bool],
               done: Callable[[bool, str | None], None]) -> None:
        if not current():
            done(False, "Lệnh ghim đã cũ")
            return
        self.calls.append((action, target))
        log.info("[dry-run] %s %s", action, (target or {}).get("name") or (target or {}).get("tiktok_product_id") or "")
        self.pinned = (target or {}).get("tiktok_product_id") if action == "PIN_PRODUCT" else None
        done(True, None)

    def read(self, done: Callable[..., None]) -> None:
        done(True, self.pinned, None)


class ConsolePinExecutor:
    dry_run = False

    def __init__(self, auto_pin, guard=None):
        self.auto_pin = auto_pin
        self.guard = guard

    def submit(self, action: str, target: dict | None, current: Callable[[], bool],
               done: Callable[[bool, str | None], None]) -> None:
        if action == "PIN_PRODUCT":
            future = self.auto_pin.submit_pin(target, current, self.guard)
        else:
            future = self.auto_pin.submit_unpin((target or {}).get("expected_user_id") or None, current, self.guard)

        def finished(f):
            error = f.exception()
            done(error is None, None if error is None else str(error)[:300])

        future.add_done_callback(finished)

    def read(self, done: Callable[..., None]) -> None:
        def finished(f):
            error = f.exception()
            if error is not None:
                done(False, None, str(error)[:300])
                return
            state = f.result() or {}
            if state.get("multiple_pins"):
                done(False, None, "Nhiều product card đang ghim")
                return
            if state.get("pinned_product_id") is None and state.get("pin_maybe_hidden"):
                # The virtual product list shows no pinned card, but one may be out of view: not a reading.
                done(False, None, "Card đang ghim có thể nằm ngoài phần danh sách đang hiển thị")
                return
            done(True, state.get("pinned_product_id"), None)

        self.auto_pin.submit_pin_state().add_done_callback(finished)


class AssumeLiveGate:
    def is_live(self) -> bool | None:
        return True


class ConsoleLiveGate:
    def __init__(self, auto_pin, *, clock: Callable[[], float] = time.monotonic, refresh_seconds: float = 5,
                 max_age: float = 20, on_change: Callable[[str, list], None] | None = None):
        self.auto_pin = auto_pin
        self.clock = clock
        self.refresh_seconds = refresh_seconds
        self.max_age = max_age
        self.on_change = on_change
        self._lock = threading.Lock()
        self._state = "UNKNOWN"
        self._read_at: float | None = None
        self._asked_at: float | None = None
        self._pending = False

    def is_live(self) -> bool | None:
        now = self.clock()
        with self._lock:
            if not self._pending and (self._asked_at is None or now - self._asked_at >= self.refresh_seconds):
                self._pending = True
                self._asked_at = now
                ask = True
            else:
                ask = False
            fresh = self._read_at is not None and now - self._read_at <= self.max_age
            state = self._state if fresh else "UNKNOWN"
        if ask:
            try:
                self.auto_pin.submit_live_state().add_done_callback(self._read)
            except Exception as exc:  # noqa: BLE001
                self._read_failed(exc)
        return {"LIVE": True, "OFFLINE": False}.get(state)

    def _read(self, future) -> None:
        error = future.exception()
        if error is not None:
            self._read_failed(error)
            return
        result = future.result() or {}
        state = result.get("state") if result.get("state") in ("LIVE", "OFFLINE") else "UNKNOWN"
        with self._lock:
            changed = state != self._state
            self._state, self._read_at, self._pending = state, self.clock(), False
        if changed and self.on_change is not None:
            self.on_change(state, list(result.get("evidence") or []))

    def _read_failed(self, error) -> None:
        with self._lock:
            changed = self._state != "UNKNOWN"
            self._state, self._read_at, self._pending = "UNKNOWN", self.clock(), False
        log.warning("Không đọc được trạng thái LIVE trên Console: %s", error)
        if changed and self.on_change is not None:
            self.on_change("UNKNOWN", [])

    def status(self) -> dict:
        with self._lock:
            return {"state": self._state, "age_seconds": None if self._read_at is None else round(self.clock() - self._read_at, 1)}
