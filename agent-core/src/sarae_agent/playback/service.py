"""Local operator view over PlaybackEngine for the desktop app (IPC ``playback.*``).

The Cloud reads PlaybackEngine.status() as is (heartbeat ``playback``); the desktop keeps its own
PlaybackStatus shape (desktop/src/types/ipc.ts), built here from the same status. After a restart the
previous session is reported (needs_manual_resume) but never resumed: a LIVE is restarted by an operator.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from sarae_agent.config import paths, read_json, write_json
from sarae_agent.playback.engine import PlaybackEngine

log = logging.getLogger("sarae.playback")


class PlaybackService:
    def __init__(self, engine: PlaybackEngine, emit: Callable[[str, dict], None]):
        self.engine = engine
        self._emit = emit
        previous = read_json(paths.playback_state, None)
        self.recovery = previous if isinstance(previous, dict) and previous.get("was_active") else None
        self._saved_active: bool | None = None
        self._last: dict = {}  # last PlaybackEngine.status(): OBS-side fields for status(live=False)

    # --- operator actions ----------------------------------------------------------------
    def _run(self, action: Callable[[], object]) -> dict:
        try:
            action()
        finally:
            self.changed()
        return self.status()

    def play(self, media_id: str) -> dict:
        return self._run(lambda: self.engine.start_playlist([media_id], loop=False))

    def play_playlist(self, media_ids: list[str], loop: bool = True) -> dict:
        return self._run(lambda: self.engine.start_playlist(media_ids, loop=loop))

    def pause(self) -> dict:
        return self._run(self.engine.pause)

    def resume(self) -> dict:
        return self._run(self.engine.resume)

    def next(self) -> dict:
        return self._run(self.engine.next)

    def previous(self) -> dict:
        return self._run(self.engine.previous)

    def stop(self) -> dict:
        return self._run(self.engine.stop)

    def set_loop(self, loop: bool) -> dict:
        return self._run(lambda: self.engine.set_loop(loop))

    def set_policy(self, session_id: int, policy: dict, lineup: list[dict]) -> dict:
        return self._run(lambda: self.engine.set_policy(session_id, policy, lineup))

    def use_camera(self) -> dict:
        return self._run(self.engine.use_camera)

    def use_video(self) -> dict:
        return self._run(self.engine.use_video)

    # --- state ---------------------------------------------------------------------------
    def changed(self) -> None:
        """After a command or an engine event: persist the restart marker and notify the GUI."""
        status = self.status()
        if status["active"]:
            self.recovery = None
        if status["active"] != self._saved_active:
            self._saved_active = status["active"]
            write_json(paths.playback_state, {"media_ids": status["media_ids"], "index": status["index"],
                                              "loop": status["loop"], "was_active": status["active"],
                                              "saved_at": datetime.now(timezone.utc).isoformat()})
        self._emit("playback.changed", status)

    def engine_status(self) -> dict:
        """PlaybackEngine.status() (asks OBS); also refreshes the snapshot used by status(live=False)."""
        st = self.engine.status()
        self._last = st
        return st

    def _snapshot(self) -> dict:
        engine = self.engine
        with engine.lock:
            return {"connected": self._last.get("connected", False), "error": self._last.get("error"),
                    "media_state": self._last.get("media_state"), "running": engine.active,
                    "playlist_id": engine.playlist_id, "index": engine.index, "count": len(engine.ids),
                    "input_name": engine.config.input_name, "last_status": engine.last_status, **engine.metrics()}

    def status(self, live: bool = True) -> dict:
        """Desktop PlaybackStatus. ``live=False`` answers without an OBS round trip (system.status, core.ready)."""
        st = self.engine_status() if live else self._snapshot()
        ids = list(st.get("queue_ids") or [])
        index = int(st.get("index") or 0)
        current = ids[index] if 0 <= index < len(ids) else None
        started = self.engine.session_started_at
        error = st.get("error")
        return {
            **st,
            "active": bool(st.get("running")), "paused": bool(st.get("paused")), "loop": bool(st.get("loop")),
            "media_ids": ids, "index": index, "count": len(ids), "current_media_id": current,
            # Cloud-cached files are named by media id; the Media Library title lives on the Cloud.
            "current_filename": current,
            "media_state": st.get("media_state"), "skipped": [],
            "last_error": {"code": "OBS_UNAVAILABLE", "message": str(error)} if error else None,
            "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat() if started else None,
            "needs_manual_resume": bool(self.recovery and not st.get("running")),
            "source": self.engine.active_source(),
        }
