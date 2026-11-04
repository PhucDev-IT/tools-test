"""AgentRuntime: owns every service and the Agent lifecycle state machine.

    stopped --start--> starting --heartbeat ok--> running <--> disconnected
       ^                  |                          |
       |                  +-------fatal------------> error
       +------stopping <--stop----------------------+

Exactly one CloudWorker may exist at a time (guarded by _lock), so there is never a duplicate
heartbeat/poll loop inside one Core, and the Core itself is a per-session singleton (mutex).
Playback, OBS and the CAPTCHA monitor are independent of the Cloud worker: stopping the
worker never interrupts a running LIVE playlist (the engine watchdog keeps advancing videos), and
playback events wait in the outbox until the worker delivers them.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Callable

from sarae_agent import IPC_PROTOCOL_VERSION, __version__
from sarae_agent.activation.service import ActivationService
from sarae_agent.auto_pin import AutoPinService
from sarae_agent.cloud.commands import CommandRouter
from sarae_agent.cloud.journal import CommandJournal, CommandLock, EventOutbox
from sarae_agent.cloud.worker import CloudWorker
from sarae_agent.config import ConfigError, ConfigStore, normalize_cloud_url, paths
from sarae_agent.diagnostics import export_bundle
from sarae_agent.errors import AgentError
from sarae_agent.logging_setup import event_handler
from sarae_agent.media.cloud_cache import CloudMediaCache, cache_dir, cached_path
import httpx
from sarae_agent.monitoring.captcha import CaptchaMonitor
from sarae_agent.monitoring.sadcaptcha import SadCaptchaAnalyzer
from sarae_agent.obs.controller import ObsController, ObsSettingsStore
from sarae_agent.overlay.cache import OverlayCache
from sarae_agent.overlay.service import OverlayService
from sarae_agent.playback.engine import PlaybackEngine
from sarae_agent.playback.service import PlaybackService
from sarae_agent.tiktok_chat_sender import SELECTORS_FILE, ChatSender
from sarae_agent.timeline.executors import AssumeLiveGate, ConsoleLiveGate, ConsolePinExecutor, DryRunPinExecutor
from sarae_agent.timeline.scheduler import TimelineScheduler
from sarae_agent.voice.tts import VoiceService
from sarae_agent.windows.win32 import hostname

log = logging.getLogger("sarae.runtime")

AGENT_STATES = ("stopped", "starting", "running", "disconnected", "error", "stopping")
# Events that should reach the Cloud quickly instead of waiting for the next heartbeat.
URGENT_EVENTS = {"monitor.verification_detected", "monitor.verification_cleared",
                 "monitor.captcha_detected", "monitor.captcha_cleared", "playback.changed", "obs.status_changed"}
# Heartbeat `obs` subset still read by the web Agent page (AgentCloudStatus).
OBS_SNAPSHOT_KEYS = ("connected", "running", "paused", "media_state", "product_id", "index", "count",
                     "last_status", "error")


def _without_paths(verification: dict) -> dict:
    """Keep the watched program's file name only: no absolute path of this PC goes to the Cloud."""
    state = dict(verification.get("state") or {})
    info = state.get("window_info")
    if isinstance(info, dict) and info.get("exe_path"):
        state["window_info"] = {k: v for k, v in info.items() if k != "exe_path"}
    return {**verification, "state": state}


class AgentRuntime:
    def __init__(self, emit: Callable[[str, dict], None]):
        self._emit_raw = emit
        self._lock = threading.RLock()
        self._state_lock = threading.Lock()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.state = "stopped"
        self.state_error: dict | None = None
        self.worker: CloudWorker | None = None
        self._closing = threading.Event()

        self.config = ConfigStore()
        self.activation = ActivationService()
        self._restore_activated_cloud_url()
        self._cloud_cache_lock = threading.Lock()
        self._cloud_cache_job: dict = {"state": "IDLE"}
        self.obs = ObsController(self.config, self.emit)
        self.outbox = EventOutbox(path=paths.root / "event_outbox.json")
        self.journal = CommandJournal(paths.command_journal)
        self.command_lock = CommandLock(paths.command_lock)
        self.engine = PlaybackEngine(resolve=self.resolve_media, on_event=self._on_playback_event,
                                     client_factory=self.obs.requests_for,
                                     store=ObsSettingsStore(self.config, self.obs))
        self.playback = PlaybackService(self.engine, self.emit)
        self.monitor = CaptchaMonitor(self.config, self.emit)
        self.sadcaptcha = SadCaptchaAnalyzer()
        self.voice = VoiceService(self.config, self.obs)
        from sarae_agent.voice.omnivoice import OmniVoiceProvider
        self.voice.job_provider = OmniVoiceProvider(self._cloud)
        self.voice._registry.register("omnivoice", self.voice.job_provider)
        self.voice.event_sink = self.outbox.push
        self.auto_pin = AutoPinService(paths.root / 'chrome-auto-pin-profile',
                                       chat_sender=ChatSender(paths.root / SELECTORS_FILE))
        self._auto_pin_guard = threading.Lock()
        self._auto_pin_last: tuple | None = None
        self.timeline = self._timeline_scheduler()
        self.overlay = OverlayService(self.config, self.obs.request, lambda: self.obs.status().get("connected", False),
                                      self._video_sources, self._overlay_snapshot, self._overlay_image,
                                      OverlayCache(paths.overlay_cache),
                                      playback_state=lambda: {"active": self.engine.active, "paused": self.engine.paused,
                                                              "session_id": self.engine.session_id})
        self.engine.on_position = self._on_position
        self.router = CommandRouter(playback=self.engine, monitor=self.monitor, voice=self.voice,
                                    tiktok_sync=self.sync_tiktok_products, media_prepare=self.prepare_cloud_cache,
                                    chat=self.auto_pin, auto_pin=self.auto_pin, timeline=self.timeline,
                                    overlay=self.overlay)
        self._watchdog = threading.Thread(target=self._watch, name="runtime-watchdog", daemon=True)

    def _timeline_scheduler(self) -> TimelineScheduler:
        """Pins of Single Video Timeline sessions: dry-run unless `timeline.dry_run` is off in this PC's config."""
        options = self.config.get().timeline
        if options.dry_run:
            executor = DryRunPinExecutor()
            gate = AssumeLiveGate() if options.assume_live_in_dry_run else self._console_gate()
        else:
            # The engine lock is held while clicking: a stop or a new video cannot interleave with a pin.
            executor = ConsolePinExecutor(self.auto_pin, guard=self.engine.lock)
            gate = self._console_gate()
        log.info("Timeline ghim: %s", "dry-run" if options.dry_run else "LIVE Console")
        return TimelineScheduler(executor, gate, emit=self._on_timeline_event)

    def _on_position(self, p: dict) -> None:
        """Engine watchdog position report: pins first (unchanged), then overlays; an overlay error never reaches pins."""
        self.timeline.observe(p["session_id"], p["position_ms"], p["iteration"], media_id=p.get("media_id"))
        try:
            self.overlay.observe(p["session_id"], p.get("media_id"), p["position_ms"], p.get("media_state"), p["iteration"])
        except Exception:  # noqa: BLE001
            log.exception("Overlay không nhận được vị trí phát")

    def _video_sources(self) -> list[str]:
        """Names of the OBS Media Sources the engine plays the video in (the overlay goes above them)."""
        config = self.engine.config
        names = [config.input_name]
        if config.dual_mode:
            names += [config.source_a.strip() or config.input_name + " A", config.source_b.strip() or config.input_name + " B"]
        return names

    def _cloud(self):
        identity = self.activation.identity
        api_url = (identity.cloud_url if identity else "") or self.config.get().cloud.api_url
        if identity is None or not api_url:
            raise AgentError("CLOUD_URL_MISSING", "Chưa kết nối Cloud")
        return identity, api_url

    def _overlay_snapshot(self, session_id: int, timeline_id: int | None = None) -> dict | None:
        """Overlays of the session's active timeline version, or of `timeline_id` (editor preview)
        (GET /agent-media/overlay-snapshot)."""
        identity, api_url = self._cloud()
        params = {"agent_id": identity.agent_id, "session_id": session_id, **({"timeline_id": timeline_id} if timeline_id else {})}
        with httpx.Client(base_url=api_url.rstrip("/") + "/", timeout=15, follow_redirects=False) as client:
            response = client.get("agent-media/overlay-snapshot", params=params,
                                  headers={"X-Sarae-Agent-Key": identity.credential})
            response.raise_for_status()
            body = response.json()
        if body.get("session_id") != session_id or body.get("agent_id") != identity.agent_id:
            raise AgentError("OVERLAY_SNAPSHOT_INVALID", "Cloud trả về snapshot overlay không đúng phiên")
        return body.get("snapshot")

    def _overlay_image(self, url: str) -> bytes:
        """One overlay image: a signed Cloud URL (no Agent key sent: the signature is the permission) or an https
        catalog photo; at most 5 MB."""
        from sarae_agent.overlay.cache import MAX_FILE_BYTES
        _identity, api_url = self._cloud()
        full = api_url.rstrip("/").removesuffix("/api") + url if url.startswith("/api/") else url
        with httpx.Client(timeout=20, follow_redirects=not url.startswith("/api/"), max_redirects=3) as client:
            with client.stream("GET", full) as response:
                response.raise_for_status()
                data = bytearray()
                for piece in response.iter_bytes():
                    data += piece
                    if len(data) > MAX_FILE_BYTES:
                        raise AgentError("OVERLAY_IMAGE_TOO_LARGE", "Ảnh overlay lớn hơn 5 MB")
                return bytes(data)

    def _console_gate(self) -> ConsoleLiveGate:
        def changed(state: str, evidence: list) -> None:
            timeline = getattr(self, "timeline", None)
            if timeline is not None and timeline.session_id is not None:
                self._on_timeline_event({"type": "LIVE_GATE_CHANGED", "session_id": timeline.session_id,
                                         "product_id": None, "detail": {"state": state, "evidence": evidence[:3]}})
        return ConsoleLiveGate(self.auto_pin, on_change=changed)

    def _on_timeline_event(self, event: dict) -> None:
        overlay = getattr(self, "overlay", None)
        if overlay is not None:
            if event.get("type") == "TIMELINE_LOADED":
                overlay.request_sync()  # a new active version: its overlays follow without waiting for the poll
            elif event.get("type") == "TIMELINE_STOPPED":
                overlay.clear("timeline_stopped")
        self.outbox.push(event)
        worker = self.worker
        if worker is not None:
            worker.poke()

    def resolve_media(self, media_id: str):
        """Media Library id -> verified file in this Agent's cloud media cache (None if not downloaded).

        Looked up at call time: the Agent may be activated after the Core booted.
        """
        identity = self.activation.identity
        if identity is None:
            return None
        return cached_path(cache_dir(identity.agent_id), media_id)

    def _restore_activated_cloud_url(self) -> None:
        """Repair an empty local Cloud URL using this device's existing activation.

        Never replace a nonempty URL: start_agent must still reject an identity mismatch.
        """
        identity = self.activation.identity
        if not identity or not identity.cloud_url or self.config.get().cloud.api_url:
            return
        try:
            restored = normalize_cloud_url(identity.cloud_url)
            if restored:
                self.config.update({"cloud": {"api_url": restored}})
                log.info("Đã khôi phục Cloud API URL từ thông tin kích hoạt")
        except (ConfigError, ValueError, OSError) as exc:
            log.warning("Không thể khôi phục Cloud API URL: %s", exc)

    # --- events --------------------------------------------------------------------------
    def emit(self, name: str, data: dict) -> None:
        worker = self.worker
        if worker is not None and name in URGENT_EVENTS:
            worker.poke()
        if worker is not None and name == "monitor.verification_detected" and data.get("image_path"):
            worker.upload_capture(str(data.get("at") or ""), str(data["image_path"]))
        self._emit_raw(name, data)
        if name == "command.finished":
            self._maybe_pin_current_video()
            try:
                self.playback.changed()  # a Cloud command may have changed playback: refresh the GUI
            except Exception:  # noqa: BLE001
                log.exception("Không cập nhật được trạng thái phát")

    def _on_playback_event(self, event: dict) -> None:
        """PRODUCT_SWITCHED / PLAYBACK_ENDED / ERROR from the engine: queue for the Cloud, refresh the GUI."""
        self.outbox.push(event)
        if event.get("type") == "PLAYBACK_ENDED":
            self.timeline.clear("playback_ended")
            self.overlay.clear("playback_ended")
        if event.get("type") in ("PRODUCT_SWITCHED", "PLAYBACK_ENDED"):
            self._maybe_pin_current_video()
        worker = self.worker
        if worker is not None:
            worker.poke()
        try:
            self.playback.changed()
        except Exception:  # noqa: BLE001 - never break the engine watchdog
            log.exception("Không cập nhật được trạng thái phát")

    def _maybe_pin_current_video(self) -> None:
        with self.engine.lock:
            running = self.engine.active
            product_id = self.engine.product_id
            marker = (self.engine.session_id, product_id, self.engine.transition_count)
        with self._auto_pin_guard:
            if not running:
                self._auto_pin_last = None
                return
            if not product_id or not self.auto_pin.video_product(product_id, marker[0]):
                self._auto_pin_last = None
                return
            if marker == self._auto_pin_last:
                return
            self._auto_pin_last = marker
        def still_current() -> bool:
            with self.engine.lock:
                return bool(self.engine.active and
                            (self.engine.session_id, self.engine.product_id, self.engine.transition_count) == marker and
                            self.auto_pin.video_product(product_id, marker[0]))
        self.auto_pin.queue_video_pin(product_id, still_current, self.engine.lock, marker[0])

    def reload_obs_settings(self) -> None:
        """OBS settings changed from the GUI: the engine uses them from its next request."""
        config = self.engine.store.load()
        with self.engine.lock:
            if self.engine.active:
                if not self.engine.camera_mode:
                    self.engine.config = self.engine.config.model_copy(
                        update={"camera_input": config.camera_input})
            else:
                self.engine.config = config
                self.engine.dual_mode = config.dual_mode
                self.engine.slots = []

    def _set_state(self, state: str, error: dict | None = None) -> None:
        # Separate from the lifecycle lock: the worker reports state while stop_agent() joins it.
        with self._state_lock:
            if self.state == state and self.state_error == error:
                return
            self.state, self.state_error = state, error
        log.info("Trạng thái Agent: %s", state, extra={"code": (error or {}).get("code", "-")})
        self.emit("agent.status_changed", self.agent_status())

    # --- startup -------------------------------------------------------------------------
    def boot(self) -> None:
        """Called once after the IPC channel is ready. Never blocks the IPC reader."""
        self._watchdog.start()
        self.engine.start_monitor()
        self.overlay.start()
        cfg = self.config.get()
        if cfg.obs.auto_connect:
            threading.Thread(target=self.obs.ensure_connected, name="obs-initial", daemon=True).start()
        if cfg.monitor.autostart:
            try:
                self.monitor.start()
            except AgentError as exc:
                log.warning("Không tự bật được CAPTCHA Monitor: %s", exc.message, extra={"code": exc.code})
        if cfg.agent.autostart and self.activation.identity is not None:
            try:
                self.start_agent()
            except AgentError as exc:
                log.warning("Không tự khởi động Agent: %s", exc.message, extra={"code": exc.code})

    def _watch(self) -> None:
        while not self._closing.wait(5):
            try:
                if self.obs.wanted:
                    self.obs.ensure_connected()
            except Exception:  # noqa: BLE001
                log.exception("Watchdog lỗi")

    # --- agent lifecycle -----------------------------------------------------------------
    def start_agent(self) -> dict:
        with self._lock:
            if self.worker is not None and self.worker.is_alive():
                return self.agent_status()
            if self.worker is not None:
                self.worker.stop(timeout=5)  # stopped on a fatal error: release its threads and command lock
                self.worker = None
            identity = self.activation.require()
            self._restore_activated_cloud_url()
            cfg = self.config.get()
            api_url = cfg.cloud.api_url
            if not api_url:
                raise AgentError("CLOUD_URL_MISSING", "Chưa cấu hình Cloud API URL")
            if identity.cloud_url and identity.cloud_url != api_url:
                # The credential belongs to the Cloud that issued it; never send it elsewhere.
                raise AgentError("CLOUD_URL_MISMATCH",
                                 "Cloud URL khác với Cloud đã kích hoạt Agent; khôi phục URL cũ hoặc kích hoạt lại",
                                 {"activated_cloud_url": identity.cloud_url})
            worker = CloudWorker(
                agent_id=identity.agent_id, credential=identity.credential, api_url=api_url,
                timeouts={"request": cfg.cloud.request_timeout_seconds, "heartbeat": cfg.cloud.heartbeat_seconds,
                          "poll": cfg.cloud.poll_seconds},
                router=self.router, build_report=self.build_report, outbox=self.outbox,
                journal=self.journal, lock=self.command_lock, monitor=self.monitor, emit=self.emit,
                on_state=self._on_worker_state)
            self._set_state("starting")
            try:
                worker.start()  # AGENT_ALREADY_RUNNING when another process holds the command lock
            except AgentError as exc:
                self._set_state("error", exc.to_dict())
                raise
            self.worker = worker
            return self.agent_status()

    def _on_worker_state(self, state: str, error: dict | None) -> None:
        if self.state == "stopping":
            return
        self._set_state(state, error)

    def stop_agent(self) -> dict:
        with self._lock:
            worker = self.worker
            if worker is None:
                self._set_state("stopped")
                return self.agent_status()
            self._set_state("stopping")
            stopped = worker.stop(timeout=10)
            self.worker = None
            if not stopped:
                log.error("Cloud worker không dừng trong 10 giây", extra={"code": "WORKER_STOP_TIMEOUT"})
            self._set_state("stopped")
            return self.agent_status()

    def restart_agent(self) -> dict:
        self.stop_agent()
        return self.start_agent()

    # --- views ---------------------------------------------------------------------------
    def agent_status(self) -> dict:
        return {"state": self.state, "error": self.state_error, **self.activation.status()}

    def cloud_status(self) -> dict:
        worker = self.worker
        if worker is None:
            return {"connected": False, "state": "idle", "api_url": self.config.get().cloud.api_url}
        return worker.status()

    def config_view(self) -> dict:
        data = self.config.get().model_dump()
        data["obs"]["password_set"] = self.obs.password_set()
        data["paths"] = {"data_root": str(paths.root), "logs": str(paths.logs),
                         "templates": str(paths.monitor_templates)}
        return data

    def system_status(self) -> dict:
        return {
            "core": {"version": __version__, "ipc_protocol": IPC_PROTOCOL_VERSION, "pid": os.getpid(),
                     "started_at": self.started_at, "data_root": str(paths.root), "hostname": hostname()},
            "agent": self.agent_status(),
            "cloud": self.cloud_status(),
            "obs": self.obs.status(),
            "media": {"cloud_cache": self.cloud_cache_status()},
            "playback": self.playback.status(live=False),
            "monitor": self.monitor.status(),
            "voice": self.voice.status(),
            "auto_pin": self.auto_pin.status(),
        }

    def build_report(self) -> dict:
        """POST /agents/heartbeat body (backend/app/cloud_agents.py Heartbeat).

        ``playback`` is PlaybackEngine.status() (same shape as GET /api/live/status; the Cloud reconciles its
        LIVE sessions with it). ``obs`` and ``local_playback`` are the older subsets the web Agent page reads.
        """
        identity = self.activation.require()
        play = {**self.playback.engine_status(), "timeline": self.timeline.status(), "overlay": self.overlay.status()}
        return {
            "agent_id": identity.agent_id,
            "hostname": hostname()[:160],
            "verification": _without_paths(self.monitor.heartbeat_view()),
            "obs": {k: play.get(k) for k in OBS_SNAPSHOT_KEYS},
            "local_playback": {"active": play.get("running", False), "media_ids": play.get("queue_ids", []),
                               "index": play.get("index", 0), "count": play.get("count", 0),
                               "loop": play.get("loop", False), "paused": play.get("paused", False),
                               "needs_manual_resume": False},
            "playback": play,
            "media_cache": self.media_cache_report(),
            "voice": self.voice.status(),  # queue, stats, last sentence; no file paths
            "pin": self.auto_pin.pin_status(),
        }

    def sync_tiktok_products(self, expected_user_id: str | None = None) -> dict:
        """Scan browser on its worker, then send the verified complete snapshot to Cloud."""
        from sarae_agent.auto_pin import AccountMismatch
        try:
            scan=self.auto_pin.scan_products(expected_user_id)
        except AccountMismatch as exc:
            raise AgentError("TIKTOK_ACCOUNT_MISMATCH", str(exc)) from exc
        snapshot=scan.get("product_snapshot") or {}
        account=scan.get("account_profile")
        if not snapshot.get("complete"):
            raise AgentError("TIKTOK_SCAN_PARTIAL",
                             "TikTok chưa trả danh sách sản phẩm kèm mã (chỉ đọc được tên trên trang); "
                             "đợi LIVE Console tải xong rồi bấm Đồng bộ lại. Chưa gửi gì lên Cloud")
        if not account or not account.get("user_id"):
            raise AgentError("TIKTOK_ACCOUNT_UNVERIFIED", "Chưa xác minh được tài khoản TikTok đang đăng nhập")
        if expected_user_id is not None and account["user_id"] != expected_user_id:
            raise AgentError("TIKTOK_ACCOUNT_MISMATCH",
                             "TikTok LIVE Console đang đăng nhập tài khoản khác với phiên LIVE")
        identity=self.activation.require()
        from sarae_agent.cloud.client import CloudClient
        with CloudClient(identity.cloud_url or self.config.get().cloud.api_url) as client:
            result=client.sync_tiktok_products(identity.credential,identity.agent_id,
                                               snapshot["products"],account)
        log.info("Đồng bộ TikTok: %s sản phẩm, khớp Sarae: %s",
                 result.get("summary",{}).get("tiktok"),result.get("summary",{}).get("matched"))
        return result

    def cloud_cache_status(self) -> dict:
        return dict(self._cloud_cache_job)

    def media_cache_report(self) -> dict:
        """Cache job for the Cloud: ids, revisions and progress only (never local file paths)."""
        job = self._cloud_cache_job
        return {"session_id": job.get("session_id"), "state": job.get("state", "IDLE"), "total": job.get("total"),
                "bytes_total": job.get("bytes_total"), "bytes_done": job.get("bytes_done"), "current": job.get("current"),
                "ready": [{"media_id": r.get("media_id"), "revision": r.get("revision")} for r in job.get("ready") or []][:300],
                "failed": [{"media_id": f.get("media_id"), "error": str(f.get("error") or "")[:200]} for f in job.get("failed") or []][:50],
                "products_without_video": list(job.get("products_without_video") or [])[:300]}

    def _cache_progress(self, status: dict) -> None:
        self._cloud_cache_job = status
        self.emit("media.cache_changed", status)
        worker = self.worker
        if worker is not None:
            worker.poke()  # the Web shows download progress from heartbeats

    def prepare_cloud_cache(self, session_id: int) -> dict:
        """Queue preparation on a separate thread: IPC never blocks on large video downloads."""
        identity = self.activation.require()
        api_url = identity.cloud_url or self.config.get().cloud.api_url
        if not api_url:
            raise AgentError("CLOUD_URL_MISSING", "Chưa kết nối Cloud")
        if not self._cloud_cache_lock.acquire(blocking=False):
            if self._cloud_cache_job.get("session_id") == session_id:
                return dict(self._cloud_cache_job)  # a repeated request for the same session: keep the running job
            raise AgentError("CACHE_BUSY", "Agent đang chuẩn bị cache cho phiên khác")
        self._cloud_cache_job = {"session_id": session_id, "state": "PREPARING", "ready": [], "failed": []}

        def work():
            try:
                with httpx.Client(base_url=api_url, timeout=120, follow_redirects=False) as client:
                    cache = CloudMediaCache(client, identity.agent_id, identity.credential)
                    result = cache.prepare(session_id, on_progress=self._cache_progress)
                # Rotated or other-aspect videos get their OBS copy now, while the job still reads PREPARING.
                self.engine.prewarm([item["media_id"] for item in result.get("ready") or []])
                self._cache_progress(result)
            except Exception as exc:
                log.exception("Cloud media cache failed")
                self._cache_progress({"session_id": session_id, "state": "FAILED",
                                      "ready": [], "failed": [{"error": str(exc)[:200]}]})
            finally:
                self._cloud_cache_lock.release()

        threading.Thread(target=work, name="cloud-media-cache", daemon=True).start()
        return dict(self._cloud_cache_job)

    def diagnostics(self) -> dict:
        return export_bundle(self.system_status(), self.config_view(), list(event_handler.recent))

    # --- shutdown ------------------------------------------------------------------------
    def shutdown(self) -> None:
        if self._closing.is_set():
            return
        self._closing.set()
        log.info("Agent Core đang tắt")
        try:
            self.stop_agent()
        except Exception:  # noqa: BLE001
            log.exception("Lỗi khi dừng Cloud worker")
        try:
            self.monitor.stop(wait=5)
        except Exception:  # noqa: BLE001
            log.exception("Lỗi khi dừng CAPTCHA Monitor")
        self.engine.stop_monitor()
        try:
            self.voice.close()  # restores the video volume if a sentence was playing
        except Exception:  # noqa: BLE001
            log.exception("Lỗi khi dừng giọng nói")
        try:
            self.auto_pin.close()
        except Exception:
            log.exception('Không đóng được Chrome Auto Pin')
        try:
            self.overlay.close()  # hides our overlay; the video keeps playing in OBS
        except Exception:  # noqa: BLE001
            log.exception("Không đóng được overlay")
        self.obs.close()
        log.info("Agent Core đã giải phóng tài nguyên")
