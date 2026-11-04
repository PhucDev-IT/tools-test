"""Pin scheduler of a Single Video Timeline session (docs/SINGLE_VIDEO_TIMELINE.md).

The playback position reported by OBS is the clock: the engine's watchdog calls ``observe`` about once a second with
the video playing, its cursor and the play iteration (+1 at every video start). Wall-clock time is used only for
retry back-off and ack timeouts.

State, not events: at every observation the scheduler computes the pin the timeline wants at that position of that
video (the last PIN/UNPIN marker of the video at or before it, see ``pin_at``) and acts only when it differs from the
pin it knows is on TikTok. Before a video's first marker, the pin the previous video left stays (``carried``): moving
to the next video, or the same one again on loop, never unpins by itself. So a seek never replays the markers it
jumped over, a pause does nothing, and polling the same position twice never pins twice.

Safety rules:
- one action in flight at a time; its result is matched by token, a late or stale result never starts another one;
- each wanted state (timeline version, loop iteration, marker, product) gets a bounded number of attempts with
  back-off, then PIN_FAILED (final) and no more tries until the wanted state changes: no infinite pin loop;
- nothing is executed while the LIVE gate is not open (TikTok LIVE not confirmed on the Console) or while the session
  is not the one loaded;
- ``clear`` (LIVE stopped) drops the timeline and makes any result still on its way stale.

Actual state: what is pinned is read back from the Console when unknown (start, reconnect, timed-out ack) and every
``reconcile_seconds`` while idle. The timeline always wins: a pin changed outside Sarae (someone pinned on the Console)
is reported (PIN_DRIFT) and the next observation pins the product the timeline wants again.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Protocol

log = logging.getLogger("sarae.timeline")

UNKNOWN = "UNKNOWN"


class PinExecutor(Protocol):
    dry_run: bool

    def submit(self, action: str, target: dict | None, current: Callable[[], bool],
               done: Callable[[bool, str | None], None]) -> None:
        """Start PIN_PRODUCT (target) or UNPIN_PRODUCT on TikTok without blocking; call done(ok, error) once."""

    def read(self, done: Callable[..., None]) -> None:
        """Read the pinned card without blocking; call done(ok, tiktok_product_id or None, error) once."""


class LiveGate(Protocol):
    def is_live(self) -> bool | None:
        """True only when TikTok LIVE is confirmed; False when it is known not to be live; None when unknown."""


def pin_at(markers: list[dict], position_ms: int, carried: int | None = None) -> int | None:
    """Product to be pinned at ``position_ms`` of one video (its markers, ordered by at_ms, seq); before its first
    marker, ``carried``."""
    pinned = carried
    for marker in markers:
        if marker["at_ms"] > position_ms:
            break
        if marker["action"] == "PIN_PRODUCT":
            pinned = marker["product_id"]
        elif marker["action"] == "UNPIN_PRODUCT":
            pinned = None
    return pinned


def marker_at(markers: list[dict], position_ms: int) -> dict | None:
    """The last PIN/UNPIN marker in force at ``position_ms`` (None before the first one)."""
    found = None
    for marker in markers:
        if marker["at_ms"] > position_ms:
            break
        if marker["action"] in ("PIN_PRODUCT", "UNPIN_PRODUCT"):
            found = marker
    return found


class TimelineScheduler:
    def __init__(self, executor: PinExecutor, gate: LiveGate, emit: Callable[[dict], None] | None = None, *,
                 clock: Callable[[], float] = time.monotonic, max_attempts: int = 3,
                 retry_seconds: tuple[float, ...] = (3, 10, 30), ack_timeout: float = 75,
                 reconcile_seconds: float = 30):
        self.executor = executor
        self.gate = gate
        self.emit = emit
        self.clock = clock
        self.max_attempts = max_attempts
        self.retry_seconds = retry_seconds
        self.ack_timeout = ack_timeout
        self.reconcile_seconds = reconcile_seconds
        self.lock = threading.RLock()
        self._token = 0
        self._reset()

    def _reset(self):
        self.session_id: int | None = None
        self.version: int | None = None
        self.markers: list[dict] = []
        self.targets: dict[int, dict] = {}
        self.expected_user_id = ""
        self.media_id: str | None = None
        self.position_ms: int | None = None
        self.iteration = 0
        # What the previous video left pinned: wanted in this video until its first marker.
        self.carried: int | None = None
        self.desired: int | None = None
        self.desired_marker: dict | None = None
        # What is pinned on TikTok as far as this Agent knows; UNKNOWN until an action of ours succeeded.
        self.actual: int | None | str = UNKNOWN
        self.in_flight: dict | None = None
        self._attempts: dict[tuple, int] = {}
        self._retry_at: dict[tuple, float] = {}
        self._failed: set[tuple] = set()
        self.live: bool | None = None
        self.last_error: str | None = None
        self.last_applied: dict | None = None
        self._reading: int | None = None  # token of a Console read on its way
        self._next_read_at = 0.0
        self._actions = 0  # actions started: a read begun before the last one says nothing about now
        self.last_read: dict | None = None

    # ----- control ----------------------------------------------------------------------------------------------

    def load(self, session_id: int, timeline: dict | None) -> dict:
        """Run ``timeline`` (version, markers, products, expected_user_id) for ``session_id``; None = no pins.

        Loading a new version of the running session keeps what is pinned and the position: the next observation
        computes the wanted pin at the current position (no playback or OBS change).
        """
        with self.lock:
            same_session = session_id == self.session_id
            kept = (self.actual, self.media_id, self.position_ms, self.iteration, self.carried)
            self._token += 1  # a result of the previous version may still arrive: it only updates `actual`
            self._reset()
            self.session_id = session_id
            if same_session:
                self.actual, self.media_id, self.position_ms, self.iteration, self.carried = kept
            if timeline:
                self.version = int(timeline.get("version") or 0)
                self.markers = sorted((dict(m) for m in timeline.get("markers") or []),
                                      key=lambda m: (m["at_ms"], m.get("seq", 0), m.get("id", 0)))
                self.targets = {int(p["sarae_product_id"]): {k: v for k, v in p.items() if k != "sarae_product_id"}
                                for p in timeline.get("products") or []}
                self.expected_user_id = timeline.get("expected_user_id") or ""
            self._event("TIMELINE_LOADED", {"version": self.version, "markers": len(self.markers),
                                            "kept_state": same_session})
            return self.status()

    def clear(self, reason: str = "stopped") -> None:
        """LIVE stopped: forget the timeline; a pin still on its way can no longer start anything."""
        with self.lock:
            if self.session_id is None:
                return
            cancelled = self.in_flight is not None
            session_id = self.session_id
            self._token += 1
            self._reset()
            self._event("TIMELINE_STOPPED", {"reason": reason, "cancelled_in_flight": cancelled}, session_id)

    # ----- the clock --------------------------------------------------------------------------------------------

    def markers_of(self, media_id: str | None) -> list[dict]:
        """The markers of one video (a marker without media_id, from a one-video session, belongs to any)."""
        return [m for m in self.markers if m.get("media_id") in (None, media_id)]

    def observe(self, session_id: int | None, position_ms: int | None, iteration: int = 0,
                media_id: str | None = None) -> None:
        """One playback observation (engine watchdog, ~1 s). Never blocks: an action is only submitted."""
        with self.lock:
            if session_id is None or session_id != self.session_id or not isinstance(position_ms, (int, float)):
                return
            if self.position_ms is not None and (media_id, int(iteration)) != (self.media_id, self.iteration):
                self.carried = self.desired  # another video (or the same one again) starts: the pin stays
            self.media_id, self.position_ms, self.iteration = media_id, int(position_ms), int(iteration)
            markers = self.markers_of(media_id)
            self.desired_marker = marker_at(markers, self.position_ms)
            self.desired = pin_at(markers, self.position_ms, self.carried)
            self._expire_in_flight()
            self.live = self.gate.is_live()
            reading_due = self.clock() >= self._next_read_at
            if self.live is True and self.in_flight is None and self._reading is None and reading_due:
                self._read()
            if self.in_flight is not None or self.desired == self.actual:
                return
            if self.desired is None and self.actual == UNKNOWN:
                return  # nothing known to be pinned and nothing wanted: never unpin blindly
            if self.live is not True:
                return
            key = self._key()
            if key in self._failed or self.clock() < self._retry_at.get(key, 0):
                return
            self._start(key)

    def _key(self) -> tuple:
        marker = self.desired_marker or {}
        return (self.version, self.iteration, marker.get("id"), self.desired)

    def _start(self, key: tuple) -> None:
        action = "PIN_PRODUCT" if self.desired is not None else "UNPIN_PRODUCT"
        target = None
        if action == "PIN_PRODUCT":
            target = self.targets.get(self.desired)
            if target is None:
                self._fail(key, f"Sản phẩm #{self.desired} không có thông tin để ghim", final=True)
                return
            target = {**target, "expected_user_id": self.expected_user_id}
        self._token += 1
        self._actions += 1
        token = self._token
        attempt = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempt
        self.in_flight = {"token": token, "key": key, "action": action, "product_id": self.desired,
                          "marker_id": key[2], "attempt": attempt, "started_at": self.clock()}

        def current() -> bool:
            with self.lock:
                return self.in_flight is not None and self.in_flight["token"] == token

        def done(ok: bool, error: str | None = None) -> None:
            self._finished(token, ok, error)

        try:
            self.executor.submit(action, target if action == "PIN_PRODUCT" else {"expected_user_id": self.expected_user_id},
                                 current, done)
        except Exception as exc:  # noqa: BLE001 - a broken executor is a failed attempt, not a crash of the watchdog
            self._finished(token, False, f"{type(exc).__name__}: {exc}")

    def _finished(self, token: int, ok: bool, error: str | None) -> None:
        with self.lock:
            flight = self.in_flight
            if flight is None or flight["token"] != token:
                # Stale (timed out, new version, stopped): a success still changed TikTok, so what is pinned is no
                # longer known; the next observation pins the wanted product again (a no-op click if already pinned).
                if ok and self.session_id is not None:
                    self.actual = UNKNOWN
                return
            self.in_flight = None
            key = flight["key"]
            if ok:
                self.actual = flight["product_id"]
                self.last_error = None
                self._attempts.pop(key, None)
                self.last_applied = {"action": flight["action"], "product_id": flight["product_id"],
                                     "marker_id": flight["marker_id"], "media_id": self.media_id,
                                     "position_ms": self.position_ms, "iteration": self.iteration}
                self._event("PIN_APPLIED", {**self.last_applied, "version": self.version,
                                            "dry_run": bool(getattr(self.executor, "dry_run", False))},
                            product_id=flight["product_id"])
                return
            self._fail(key, error or "Ghim thất bại", final=flight["attempt"] >= self.max_attempts,
                       attempt=flight["attempt"])

    def _read(self) -> None:
        self._token += 1
        token, actions = self._token, self._actions
        self._reading = token
        self._next_read_at = self.clock() + self.reconcile_seconds

        def done(ok: bool, tiktok_product_id: str | None = None, error: str | None = None) -> None:
            self._read_done(token, actions, ok, tiktok_product_id, error)

        try:
            self.executor.read(done)
        except Exception as exc:  # noqa: BLE001
            self._read_done(token, actions, False, None, f"{type(exc).__name__}: {exc}")

    def _product_of(self, tiktok_product_id: str | None) -> int | None | str:
        if not tiktok_product_id:
            return None
        for pid, target in self.targets.items():
            if str(target.get("tiktok_product_id") or "") == str(tiktok_product_id):
                return pid
        return f"TIKTOK:{tiktok_product_id}"  # a card the timeline does not know

    def _read_done(self, token: int, actions: int, ok: bool, tiktok_product_id: str | None, error: str | None) -> None:
        with self.lock:
            if self._reading != token:
                return
            self._reading = None
            if not ok:
                self.last_read = {"ok": False, "error": (error or "")[:200]}
                return
            if actions != self._actions or self.in_flight is not None:
                return  # an action started meanwhile: this reading may predate it
            observed = self._product_of(tiktok_product_id)
            self.last_read = {"ok": True, "tiktok_product_id": tiktok_product_id, "position_ms": self.position_ms}
            drifted = self.actual != UNKNOWN and observed != self.actual
            self.actual = observed
            if drifted:
                # Changed on TikTok outside Sarae: the timeline wins, the next observation pins the wanted product.
                self._event("PIN_DRIFT", {"tiktok_product_id": tiktok_product_id, "wanted_product_id": self.desired,
                                          "position_ms": self.position_ms},
                            product_id=observed if isinstance(observed, int) else None)

    def _fail(self, key: tuple, error: str, final: bool, attempt: int = 1) -> None:
        self.last_error = error[:300]
        if final:
            self._failed.add(key)
        else:
            delay = self.retry_seconds[min(attempt - 1, len(self.retry_seconds) - 1)]
            self._retry_at[key] = self.clock() + delay
        self._event("PIN_FAILED", {"action": "PIN_PRODUCT" if key[3] is not None else "UNPIN_PRODUCT",
                                   "product_id": key[3], "marker_id": key[2], "version": self.version,
                                   "iteration": key[1], "attempt": attempt, "final": final, "error": self.last_error},
                    product_id=key[3])

    def _expire_in_flight(self) -> None:
        flight = self.in_flight
        if flight is not None and self.clock() - flight["started_at"] > self.ack_timeout:
            self.in_flight = None
            # The click may have happened: what is pinned is no longer known for sure.
            self.actual = UNKNOWN
            self._next_read_at = 0  # read it back at once
            self._fail(flight["key"], "Hết thời gian chờ xác nhận ghim", final=flight["attempt"] >= self.max_attempts,
                       attempt=flight["attempt"])

    # ----- reporting --------------------------------------------------------------------------------------------

    def _event(self, kind: str, detail: dict, session_id: int | None = None, product_id: int | None = None) -> None:
        if self.emit is None:
            return
        try:
            self.emit({"type": kind, "session_id": session_id if session_id is not None else self.session_id,
                       "product_id": product_id, "detail": detail})
        except Exception:  # noqa: BLE001
            log.exception("Không gửi được sự kiện timeline %s", kind)

    def status(self) -> dict:
        """Heartbeat view (inside `playback.timeline`): wanted vs actual pin, mode, gate."""
        with self.lock:
            return {"session_id": self.session_id, "version": self.version, "markers": len(self.markers),
                    "media_id": self.media_id, "position_ms": self.position_ms, "iteration": self.iteration,
                    "desired_product_id": self.desired,
                    "desired_marker_id": (self.desired_marker or {}).get("id"),
                    "actual_product_id": self.actual if isinstance(self.actual, int) else None,
                    "actual_known": self.actual != UNKNOWN,
                    "actual_tiktok_product_id": self.actual.split(":", 1)[1]
                    if isinstance(self.actual, str) and self.actual.startswith("TIKTOK:") else None,
                    "last_read": self.last_read,
                    "live": self.live, "in_flight": {k: v for k, v in self.in_flight.items() if k != "key"}
                    if self.in_flight else None,
                    "last_applied": self.last_applied, "last_error": self.last_error,
                    "dry_run": bool(getattr(self.executor, "dry_run", False))}
