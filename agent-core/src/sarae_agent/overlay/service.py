"""OverlayService: shows the active timeline's overlays in OBS, in step with the video the PlaybackEngine plays.

    Cloud snapshot ──fetch──> images cached on this PC ──> PENDING revision
                                                            │ renderer polls /overlay/state, sees the new revision,
                                                            │ loads the scene, preloads every image and the fonts
                                                            ▼
                                     POST /overlay/ready {revision, ok} ──ok──> ACTIVE revision ──> scene item shown
                                                            └─not ok / no answer in 20 s──> PENDING dropped (rollback:
                                                               the previous revision stays, or the overlay stays hidden)

Time: there is no overlay clock. The state is reconciled from what the PlaybackEngine reports (`observe`): session,
media id, position, media state, loop iteration (the engine reports on every watchdog tick, and at once when a video
starts, on a seek, a pause and a resume). An overlay is active when `start_ms <= position < start_ms + duration_ms` on
that media (`shown_at`, the rule of the web preview); in random mode, when the loop's plan (overlay/schedule.py, seeded
by the loop iteration) has an appearance there (`shown_in`). The renderer polls this state (no queue of show/hide commands:
a reconnecting renderer simply reads the current state, nothing past is replayed); between two reports it may advance
by the elapsed time only while the media state is PLAYING, at most 1.5 s. A paused video holds its position: the
engine does not report while paused, so the pause flag of the engine keeps that position valid.

Preview ("Xem thử trên OBS" of the editor): a version — a draft included — is shown at the editor's position without
activating it or going LIVE; the position runs on by itself while the editor plays. Refused while the engine plays a
session or OBS streams (it would reach the viewers); it ends by itself after `seconds`, and a LIVE start ends it.

Sync: every state carries the scene revision and a clock `seq`; the renderer echoes the revision and seq it drew
(ACK). A new active timeline version (TIMELINE_LOADED of the pin scheduler) asks for the snapshot at once.

Safety: the service has its own thread. Nothing it does can stop or restart the video, the voice or the pin
schedule: every failure ends in "overlay hidden" plus a log line and `status()["error"]`.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Callable

from sarae_agent.errors import AgentError
from sarae_agent.overlay import schedule
from sarae_agent.overlay.cache import OverlayCache
from sarae_agent.overlay.obs_source import URL_MARKER, ObsOverlaySource
from sarae_agent.overlay.server import OverlayServer

log = logging.getLogger("sarae.overlay")

# Resource limits of a snapshot (the Backend enforces the same when a version is approved).
MAX_OVERLAYS = 500
MAX_ANIMATION_MS = 5000
MAX_AT_ONCE = 4


def shown_at(overlays: list[dict], media_id: str | None, position_ms: int) -> list[dict]:
    """Overlays active at `position_ms` of `media_id` (web: overlayModel.isShown), in drawing order."""
    found = [(o.get("layout", {}).get("z_index", 0), i, o) for i, o in enumerate(overlays)
             if o.get("enabled", True) is not False and o.get("media_id") == media_id
             and o["start_ms"] <= position_ms < o["start_ms"] + o["duration_ms"]]
    return [o for _, _, o in sorted(found, key=lambda x: (x[0], x[1]))]


def shown_in(scene: dict, media_id: str | None, position_ms: int, iteration: int = 0, plan: list[dict] | None = None) -> list[dict]:
    """Overlays of `scene` on screen at `position_ms` of loop `iteration` (web: overlaySchedule.sceneAt + isShown).
    Fixed mode: `shown_at`. Random mode: the one appearance of the loop's plan covering that position, if any."""
    overlays = scene.get("overlays") or []
    if not schedule.is_random(scene):
        return shown_at(overlays, media_id, position_ms)
    if plan is None:
        plan = schedule.random_plan(overlays, media_id, schedule.schedule_of(scene), scene.get("timeline_id") or 0, iteration)
    now = next((a for a in plan if a["start_ms"] <= position_ms < a["end_ms"]), None)
    return [] if now is None else [overlays[now["index"]]]


def snapshot_problem(snapshot: dict) -> str | None:
    """Why a snapshot cannot be shown (limits), None when it can."""
    overlays = snapshot.get("overlays") or []
    if len(overlays) > MAX_OVERLAYS:
        return f"quá {MAX_OVERLAYS} overlay"
    for o in overlays:
        if any((o.get(k) or {}).get("duration_ms", 0) > MAX_ANIMATION_MS for k in ("animation_in", "animation_out")):
            return f"hiệu ứng dài hơn {MAX_ANIMATION_MS} ms"
        if not (isinstance(o.get("start_ms"), int) and isinstance(o.get("duration_ms"), int) and o["duration_ms"] > 0):
            return "thời gian overlay không hợp lệ"
    if schedule.is_random(snapshot):
        return None  # windows may overlap: one appearance at a time by construction
    for media_id in {o.get("media_id") for o in overlays}:
        edges = sorted([(o["start_ms"], 1) for o in overlays if o.get("media_id") == media_id and o.get("enabled", True) is not False]
                       + [(o["start_ms"] + o["duration_ms"], -1) for o in overlays
                          if o.get("media_id") == media_id and o.get("enabled", True) is not False], key=lambda e: (e[0], e[1]))
        shown = 0
        for _, step in edges:
            shown += step
            if shown > MAX_AT_ONCE:
                return f"quá {MAX_AT_ONCE} overlay cùng lúc"
    return None


class OverlayService:
    TICK_SECONDS = 2.0
    SYNC_SECONDS = 15.0
    READY_TIMEOUT = 20.0
    RENDERER_LOST = 6.0
    CLOCK_STALE = 5.0
    REFRESH_EVERY = 30.0

    def __init__(self, config, obs_call: Callable[[str, dict | None], dict], obs_connected: Callable[[], bool],
                 video_sources: Callable[[], list[str]], fetch_snapshot: Callable[[int], dict | None],
                 fetch_image: Callable[[str], bytes], cache: OverlayCache, *, clock: Callable[[], float] = time.monotonic,
                 server: OverlayServer | None = None, playback_state: Callable[[], dict] | None = None):
        self._config = config
        self._obs_call = obs_call
        self._obs_connected = obs_connected
        self._video_sources = video_sources
        self._fetch_snapshot = fetch_snapshot
        self._fetch_image = fetch_image
        self._cache = cache
        self._now = clock
        self._playback_state = playback_state or (lambda: {})
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.token = self._token()
        # This run of the service: clock seqs restart at 1 after an Agent restart, so the renderer compares seqs of
        # the same epoch only (a page that outlived the restart must not ignore the new, lower seqs).
        self.epoch = secrets.token_hex(4)
        self.source = ObsOverlaySource(obs_call, lambda: self._config.get().overlay.source_name,
                                       lambda: dict(self._config.get().overlay.input_uuids), self._remember_input)
        self.server = server or OverlayServer(self)
        self.session_id: int | None = None
        self.clock: dict | None = None
        self.observed_at: float | None = None
        self.active: dict | None = None
        self.pending: dict | None = None
        self.ready_revision: str | None = None
        self.renderer_seen: float | None = None
        self.renderer_ack: dict | None = None
        self.renderer_build: str | None = None
        self.source_ok = False
        self.source_info: dict = {}
        self.applied_visible: bool | None = None
        self.error: dict | None = None
        self.missing: list[dict] = []
        self.last_sync = 0.0
        self.last_refresh = 0.0
        self._refused: str | None = None
        # {session_id, timeline_id, media_id, position_ms, playing, at, until} while the editor previews on OBS.
        self.preview: dict | None = None
        self._plan: tuple | None = None  # ((revision, media_id, iteration), random plan) of active_ids
        self._state = "idle"

    # ----- setup -----
    def _token(self) -> str:
        token = self._config.get().overlay.token
        if not token:
            token = secrets.token_urlsafe(24)
            self._config.update({"overlay": {"token": token}})
        return token

    def _remember_input(self, collection: str, uuid: str) -> None:
        uuids = dict(self._config.get().overlay.input_uuids)
        if uuids.get(collection) != uuid:
            uuids[collection] = uuid
            self._config.update({"overlay": {"input_uuids": uuids}})

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.port}/overlay/?t={self.token}&{URL_MARKER}"

    def start(self) -> None:
        if not self._config.get().overlay.enabled or (self._thread and self._thread.is_alive()):
            return
        try:
            self.server.start(self._config.get().overlay.port)
        except OSError as exc:
            self._fail("OVERLAY_PORT_BUSY", f"Không mở được cổng renderer overlay: {exc}")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="overlay-service", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            if self.applied_visible and self._obs_connected():
                self.source.set_visible(False)
        except Exception:  # noqa: BLE001
            pass
        self.server.stop()

    # ----- inputs -----
    def observe(self, session_id: int, media_id: str | None, position_ms: int, media_state: str | None, iteration: int) -> None:
        """Position report of the PlaybackEngine (its watchdog thread): cheap, never raises."""
        with self._lock:
            if self.preview is not None:
                log.info("Phiên LIVE đang phát: dừng xem thử overlay")
                self.preview, self.last_sync = None, 0.0
            if session_id != self.session_id:
                self.session_id, self.last_sync = session_id, 0.0
            seq = (self.clock or {}).get("seq", 0) + 1
            self.clock = {"session_id": session_id, "media_id": media_id, "position_ms": int(position_ms),
                          "media_state": media_state, "iteration": int(iteration or 0), "seq": seq}
            self.observed_at = self._now()

    def clear(self, reason: str) -> None:
        """Playback ended: hide at the next tick (the clock is dropped)."""
        with self._lock:
            if self.clock is not None:
                log.info("Overlay: dừng theo video (%s)", reason)
            self.clock, self.observed_at = None, None

    # ----- renderer side (overlay server threads) -----
    def _paused(self) -> bool:
        try:
            state = self._playback_state() or {}
        except Exception:  # noqa: BLE001
            return False
        return bool(state.get("active") and state.get("paused") and state.get("session_id") == self.session_id)

    def _preview_clock(self, now: float) -> None:
        """While previewing, the clock is the editor's position (running on while the editor plays)."""
        p = self.preview
        if p is None:
            return
        position = int(p["position_ms"] + ((now - p["at"]) * 1000 if p["playing"] else 0))
        iteration = p.get("iteration", 0)
        if (self.clock is None or self.clock.get("position_ms") != position or self.clock.get("media_id") != p["media_id"]
                or self.clock.get("iteration") != iteration):
            seq = (self.clock or {}).get("seq", 0) + 1
            self.clock = {"session_id": p["session_id"], "media_id": p["media_id"], "position_ms": position, "iteration": iteration, "seq": seq,
                          "media_state": "OBS_MEDIA_STATE_PLAYING" if p["playing"] else "OBS_MEDIA_STATE_PAUSED"}
        self.observed_at = now

    def _clock_fresh(self, now: float) -> bool:
        """The reported position still describes the picture: recent, or held by a pause of the same session."""
        if self.preview is not None:
            return now <= self.preview["until"]
        if self.clock is None or self.observed_at is None:
            return False
        return now - self.observed_at <= self.CLOCK_STALE or self._paused()

    def _clock_view(self, now: float) -> dict | None:
        self._preview_clock(now)
        if self.clock is None or self.observed_at is None:
            return None
        paused = self._paused()
        return {**self.clock, "epoch": self.epoch, "age_ms": int((now - self.observed_at) * 1000),
                "playing": self.clock.get("media_state") == "OBS_MEDIA_STATE_PLAYING" and not paused}

    def active_ids(self) -> list:
        """Overlays of the active revision shown at the last reported position (status, tests, readiness)."""
        with self._lock:
            if self.active is None or self.clock is None:
                return []
            scene, media_id, iteration = self.active["scene"], self.clock.get("media_id"), self.clock.get("iteration", 0)
            plan = None
            if schedule.is_random(scene):  # one plan per (revision, video, loop): status asks often
                key = (self.active["revision"], media_id, iteration)
                if self._plan is None or self._plan[0] != key:
                    self._plan = (key, schedule.random_plan(scene.get("overlays") or [], media_id, schedule.schedule_of(scene),
                                                            scene.get("timeline_id") or 0, iteration))
                plan = self._plan[1]
            return [o.get("id") for o in shown_in(scene, media_id, self.clock.get("position_ms", 0), iteration, plan)]

    def renderer_state(self, ack: dict | None = None) -> dict:
        """What the renderer draws now (it polls). `ack`: the revision and clock seq it drew last."""
        with self._lock:
            now = self._now()
            self.renderer_seen = now
            if ack and ack.get("revision"):
                self.renderer_ack = {"revision": ack.get("revision"), "seq": ack.get("seq"), "at": now}
            if ack is not None:
                self.renderer_build = ack.get("build") or ""
                # The page outlived an Agent restart and already draws this exact content (the revision is a hash of
                # it): it will not load it again, so its ACK is its READY.
                staged = self.pending or self.active
                if staged and ack["revision"] == staged["revision"] and self.ready_revision != staged["revision"]:
                    log.info("Renderer đã vẽ sẵn revision %s (trang còn từ trước): coi là READY", ack["revision"])
                    self.renderer_ready({"revision": ack["revision"], "ok": True})
            target = self.pending or self.active
            return {"revision": target["revision"] if target else None,
                    "version": target.get("version") if target else None,
                    "show": self._wanted(now, renderer=False), "clock": self._clock_view(now)}

    def scene_for(self, revision: str) -> dict | None:
        with self._lock:
            for staged in (self.pending, self.active):
                if staged and staged["revision"] == revision:
                    return staged["scene"]
            return None

    def renderer_ready(self, body: dict) -> dict:
        revision, ok = body.get("revision"), body.get("ok") is True
        missing = [str(m)[:300] for m in (body.get("missing") or [])][:20]
        with self._lock:
            if self.pending and revision == self.pending["revision"]:
                if ok:
                    self.active, self.pending, self.ready_revision = self.pending, None, revision
                    self.error = None
                    log.info("Overlay READY: phiên bản %s (revision %s, %s overlay)", self.active["version"], revision,
                             self.active["overlay_count"])
                    return {"accepted": True}
                log.error("Renderer không sẵn sàng với revision %s: %s; giữ bản trước", revision, missing)
                self.pending = None
                self._fail("RENDERER_NOT_READY", "Renderer không tải đủ ảnh/phông của overlay mới; giữ overlay trước", {"missing": missing})
                return {"accepted": False}
            if self.active and revision == self.active["revision"]:
                self.ready_revision = revision if ok else None
                if not ok:
                    self._fail("RENDERER_NOT_READY", "Renderer không tải đủ ảnh overlay", {"missing": missing})
                return {"accepted": ok}
            return {"accepted": False}

    def file_path(self, name: str):
        return self._cache.path(name)

    # ----- the loop -----
    def _fail(self, code: str, message: str, detail: dict | None = None) -> None:
        error = {"code": code, "message": message, **({"detail": detail} if detail else {})}
        if error != self.error:
            log.warning("Overlay: %s", message, extra={"code": code})
        self.error = error

    def _wanted(self, now: float, renderer: bool = True) -> bool:
        """Should the overlay be on screen now (False whenever anything is not right)."""
        active = self.active
        if not self._config.get().overlay.enabled or active is None or not active["overlay_count"]:
            return False
        if not self._clock_fresh(now):
            return False
        if active["session_id"] != self.session_id:
            return False
        if renderer and (self.ready_revision != active["revision"] or self.renderer_seen is None
                         or now - self.renderer_seen > self.RENDERER_LOST or not self.source_ok):
            return False
        return True

    def _sync(self, now: float) -> None:
        session_id = self.session_id
        if self.preview is not None or session_id is None or now - self.last_sync < self.SYNC_SECONDS:
            return  # a preview shows its own version, never replaced by the active one
        self.last_sync = now
        try:
            snapshot = self._fetch_snapshot(session_id)
        except Exception as exc:  # noqa: BLE001 - offline: keep what is loaded
            log.warning("Không lấy được snapshot overlay (giữ bản đang có): %s", exc)
            return
        with self._lock:
            if snapshot is None:
                if self.active or self.pending:
                    log.info("Phiên %s không còn overlay đang kích hoạt", session_id)
                self.active = self.pending = None
                return
            current = (self.pending or self.active or {}).get("revision")
            if snapshot.get("revision") in (current, self._refused):
                return
        problem = snapshot_problem(snapshot)
        if problem:
            self._refused = snapshot.get("revision")
            self._fail("SNAPSHOT_REFUSED", f"Snapshot overlay không hợp lệ ({problem}); giữ overlay trước")
            return
        scene, missing = self._cache.localize(snapshot, self._fetch_image, self.token)
        self.missing = missing
        if any("asset_id" in m for m in missing):
            self._fail("ASSET_MISSING", "Không tải được ảnh overlay; giữ overlay trước", {"missing": missing[:10]})
            self.last_sync = now - self.SYNC_SECONDS + 5  # retry soon
            return
        with self._lock:
            self.pending = {"revision": snapshot["revision"], "scene": scene, "session_id": session_id,
                            "version": snapshot.get("version"), "timeline_id": snapshot.get("timeline_id"),
                            "overlay_count": len(snapshot.get("overlays") or []), "since": now}
            log.info("Overlay: chuẩn bị phiên bản %s (revision %s), chờ renderer READY", snapshot.get("version"), snapshot["revision"])

    def _obs(self, now: float, wanted: Callable[[float], bool]) -> None:
        if not self._obs_connected():
            self.source_ok, self.applied_visible = False, None
            return
        if self.session_id is not None or self.applied_visible:
            try:
                self.source_info = self.source.ensure(self.url, self._video_sources())
                self.source_ok = True
                if "created" in self.source_info["changes"] or any(c.startswith(("added:", "item_changed:", "collection"))
                                                                   for c in self.source_info["changes"]):
                    self.applied_visible = None  # new scene items start hidden: apply again
            except AgentError as exc:
                self.source_ok = False
                self._fail(exc.code, exc.message, getattr(exc, "details", None))
        visible = self.source_ok and wanted(now)  # decided after the source check (a reconnect shows it at once)
        if visible != self.applied_visible and (self.source_ok or not visible):
            try:
                self.source.set_visible(visible)
                self.applied_visible = visible
                log.info("Overlay %s trong OBS", "HIỆN" if visible else "ẩn")
            except AgentError as exc:
                self.applied_visible = None
                self._fail(exc.code, exc.message)

    def tick(self) -> None:
        now = self._now()
        with self._lock:
            if self.preview is not None and now > self.preview["until"]:
                log.info("Hết thời gian xem thử overlay trên OBS")
                self._end_preview()
            self._preview_clock(now)
        if not self._config.get().overlay.enabled:
            if self.applied_visible:
                self._obs(now, lambda _now: False)
            return
        self._sync(now)
        with self._lock:
            if self.pending and now - self.pending["since"] > self.READY_TIMEOUT:
                log.error("Renderer không báo READY sau %ss cho revision %s; giữ bản trước", int(self.READY_TIMEOUT), self.pending["revision"])
                self.pending = None
                self._fail("RENDERER_TIMEOUT", "Renderer không sẵn sàng kịp; giữ overlay trước")
            wanted = self._wanted(now, renderer=False)
        renderer_alive = self.renderer_seen is not None and now - self.renderer_seen <= self.RENDERER_LOST
        expected = getattr(self.server, "build", "")
        if (renderer_alive and expected and self.renderer_build is not None and self.renderer_build != expected
                and self.source_ok and now - self.last_refresh > self.REFRESH_EVERY):
            self.last_refresh = now
            log.info("Trang renderer cũ (%s ≠ %s): tải lại Browser Source", self.renderer_build or "không rõ", expected)
            try:
                self.source.refresh()
            except AgentError as exc:
                self._fail(exc.code, exc.message)
        self._obs(now, self._wanted)
        # A silent page is reloaded whenever there is something for it to draw (also a version waiting for READY
        # while the overlay is hidden: OBS throttles a hidden Browser Source, which may have stopped polling).
        if (wanted or self.pending or self.active) and self.source_ok and not renderer_alive and now - self.last_refresh > self.REFRESH_EVERY:
            self.last_refresh = now
            log.warning("Renderer overlay không phản hồi; tải lại Browser Source (video không bị ảnh hưởng)",
                        extra={"code": "RENDERER_LOST"})
            try:
                self.source.refresh()
            except AgentError as exc:
                self._fail(exc.code, exc.message)
        self._log_state(now)

    def _log_state(self, now: float) -> None:
        state = self.status_state(now)
        if state != self._state:
            log.info("Trạng thái overlay: %s → %s", self._state, state)
            self._state = state

    def status_state(self, now: float | None = None) -> str:
        now = self._now() if now is None else now
        if not self._config.get().overlay.enabled:
            return "disabled"
        if self.applied_visible:
            return "showing"
        if self.pending:
            return "loading"
        if self.error and self.session_id is not None:
            return "error"
        return "hidden" if self.active else "idle"

    def request_sync(self) -> None:
        """A new active version (or another reason to re-read the Cloud now): sync at the next tick, at once."""
        with self._lock:
            self.last_sync = 0.0
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.TICK_SECONDS)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the overlay never takes anything else down
                log.exception("Overlay tick lỗi")

    def _end_preview(self) -> None:
        self.preview, self.clock, self.observed_at, self.last_sync = None, None, None, 0.0

    def _stage(self, snapshot: dict, session_id: int, now: float) -> None:
        """Images on this PC, then the snapshot as the PENDING revision (the renderer loads it, reports READY)."""
        problem = snapshot_problem(snapshot)
        if problem:
            raise AgentError("SNAPSHOT_REFUSED", f"Snapshot overlay không hợp lệ ({problem})")
        with self._lock:
            current = self.pending or self.active
            if current and current["revision"] == snapshot.get("revision") and current["session_id"] == session_id:
                return
        scene, missing = self._cache.localize(snapshot, self._fetch_image, self.token)
        self.missing = missing
        if any("asset_id" in m for m in missing):
            raise AgentError("ASSET_MISSING", "Không tải được ảnh overlay", {"missing": missing[:10]})
        with self._lock:
            self.pending = {"revision": snapshot["revision"], "scene": scene, "session_id": session_id,
                            "version": snapshot.get("version"), "timeline_id": snapshot.get("timeline_id"),
                            "overlay_count": len(snapshot.get("overlays") or []), "since": now}

    def preview_on_obs(self, session_id: int, timeline_id: int | None = None, media_id: str | None = None, position_ms: int = 0,
                       playing: bool = False, seconds: int = 120, stop: bool = False, iteration: int = 0) -> dict:
        """The editor's "Xem thử trên OBS": show this version (a draft included) at the editor's position, without
        activating it or going LIVE; or stop. Repeated calls move the position (seek / play / pause) and renew it.
        `iteration`: the editor's loop of the video (random mode draws a plan per loop)."""
        if stop:
            with self._lock:
                if self.preview is not None:
                    log.info("Tắt xem thử overlay trên OBS")
                    self._end_preview()
            self._wake.set()
            return {"preview": False, **self.status()}
        state = self._playback_state() or {}
        if state.get("active"):
            raise AgentError("PREVIEW_BUSY", "Agent đang phát một phiên LIVE: không xem thử được (tránh lẫn vào LIVE)")
        if not self._obs_connected():
            raise AgentError("OBS_DISCONNECTED", "Chưa kết nối OBS")
        if self._obs_call("GetStreamStatus", None).get("outputActive"):
            raise AgentError("PREVIEW_ON_AIR", "OBS đang stream: overlay xem thử sẽ hiện cho người xem, nên không thực hiện")
        now = self._now()
        same = (self.preview or {}).get("timeline_id") == timeline_id and (self.preview or {}).get("session_id") == session_id
        if not same or self.pending is None and (self.active or {}).get("session_id") != session_id:
            snapshot = self._fetch_snapshot(session_id, timeline_id)
            if snapshot is None:
                raise AgentError("PREVIEW_EMPTY", "Phiên bản này chưa có overlay")
            self._stage(snapshot, session_id, now)
        with self._lock:
            if self.preview is None:
                log.info("Xem thử overlay trên OBS: phiên %s, phiên bản #%s", session_id, timeline_id)
            self.session_id = session_id
            self.preview = {"session_id": session_id, "timeline_id": timeline_id, "media_id": media_id,
                            "position_ms": int(position_ms), "playing": bool(playing), "iteration": max(0, int(iteration or 0)),
                            "at": now, "until": now + seconds}
            self._preview_clock(now)
        self._wake.set()
        return {"preview": True, **self.status()}

    CHECK_READY_SECONDS = 12.0

    def check(self, session_id: int, *, sleep: Callable[[float], None] = time.sleep) -> dict:
        """Pre-LIVE readiness of the OBS overlay for `session_id` (the operator's "Kiểm tra OBS Overlay"): downloads
        the active version's snapshot and images, creates or checks the hidden Browser Source (canvas, transform read
        back, above the video), and waits for the renderer to report READY. Never shows the overlay, never touches
        playback. The prepared revision is kept, so the overlay shows at once when the session plays."""
        checks: list[dict] = []

        def add(code: str, ok: bool, message: str, level: str = "error", **detail) -> None:
            checks.append({"code": code, "ok": bool(ok), "level": "ok" if ok else level, "message": message, **detail})

        with self._lock:
            if self.session_id != session_id:
                self.session_id = session_id
            self.last_sync = 0.0
        add("RENDERER_BUILT", bool(getattr(self.server, "build", "")), "Agent có sẵn trang renderer overlay (chạy offline)")
        add("RENDERER_SERVER", self.server.port is not None, f"Renderer chạy ở 127.0.0.1:{self.server.port}")
        self._sync(self._now())
        staged = self.pending or self.active
        if staged is None or staged["session_id"] != session_id:
            add("ACTIVE_VERSION", False, "Phiên chưa có timeline ACTIVE có overlay (hoặc chưa tải được từ Cloud)", level="warning")
        else:
            add("ACTIVE_VERSION", True, f"Phiên bản {staged['version']}: {staged['overlay_count']} overlay (revision {staged['revision']})")
            assets = [m for m in self.missing if "asset_id" in m]
            add("ASSETS_CACHED", not assets, "Ảnh overlay đã tải về máy" if not assets else f"{len(assets)} ảnh overlay chưa tải được",
                missing=self.missing[:10])
            photos = [m for m in self.missing if "product_id" in m]
            if photos:
                add("PRODUCT_PHOTOS", False, f"{len(photos)} ảnh sản phẩm chưa tải được (khối ảnh đó sẽ trống)", level="warning")
        connected = self._obs_connected()
        add("OBS_CONNECTED", connected, "Đã kết nối OBS" if connected else "Chưa kết nối OBS WebSocket")
        if connected:
            try:
                info = self.source.ensure(self.url, self._video_sources())
                self.source_ok, self.source_info = True, info
                width, height = info["canvas"]
                add("OBS_CANVAS", True, f"Canvas OBS {width}×{height} (9:16)")
                add("OBS_SOURCE", True, "Browser Source overlay có trong " + ", ".join(f"'{n}'" for n in info["scenes"]), scenes=info["scenes"])
                add("OBS_TRANSFORM", True, "Vị trí (0,0), tỉ lệ khớp canvas, không crop, nằm trên video (đã đọc lại từ OBS)")
                if self.applied_visible is None:
                    self.source.set_visible(False)
                    self.applied_visible = False
            except AgentError as exc:
                self.source_ok = False
                add(exc.code, False, exc.message)
        staged = self.pending or self.active
        if staged is not None and self.source_ok:
            waited = 0.0
            while self.ready_revision != staged["revision"] and waited < self.CHECK_READY_SECONDS:
                sleep(0.5)
                waited += 0.5
                staged = self.pending or self.active
                if staged is None:
                    break
            ready = staged is not None and self.ready_revision == staged["revision"]
            add("RENDERER_READY", ready, "Renderer đã tải xong overlay và ảnh (READY)" if ready else
                "Renderer chưa báo READY (Browser Source chưa tải trang?)")
        now = self._now()
        build = getattr(self.server, "build", "")
        if self.renderer_build is not None and build:
            add("RENDERER_VERSION", self.renderer_build == build, "Trang renderer trong OBS đúng phiên bản Agent" if self.renderer_build == build
                else "Trang renderer trong OBS là bản cũ; Agent đang tải lại", level="warning")
        ok = all(c["ok"] or c["level"] == "warning" for c in checks)
        return {"session_id": session_id, "ok": ok, "checks": checks, "status": self.status(),
                "checked_ms": int(now * 1000)}

    def status(self) -> dict:
        with self._lock:
            now = self._now()
            active = self.active or {}
            return {"enabled": self._config.get().overlay.enabled, "state": self.status_state(now),
                    "url_port": self.server.port, "session_id": self.session_id,
                    "revision": active.get("revision"), "version": active.get("version"),
                    "overlay_count": active.get("overlay_count", 0),
                    "pending_revision": (self.pending or {}).get("revision"), "ready_revision": self.ready_revision,
                    "renderer_seen_ms": int((now - self.renderer_seen) * 1000) if self.renderer_seen is not None else None,
                    "renderer_ack": {k: v for k, v in (self.renderer_ack or {}).items() if k != "at"} or None,
                    "clock": self._clock_view(now), "active_overlays": self.active_ids(),
                    "visible": bool(self.applied_visible), "source": self.source_info if self.source_ok else None,
                    "missing": self.missing[:10], "error": self.error,
                    "preview": {"timeline_id": self.preview["timeline_id"], "media_id": self.preview["media_id"],
                                "seconds_left": max(0, int(self.preview["until"] - now))} if self.preview else None}
