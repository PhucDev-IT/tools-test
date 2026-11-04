"""Cloud worker: heartbeat, command long-poll, journaled ACKs, playback events and CAPTCHA template sync.

Protocol: backend/app/cloud_agents.py (the Cloud owns each command's lifecycle).

    PENDING --claim (long-poll)--> RUNNING --ack {claim_token, success, result}--> SUCCESS | FAILED
    A claim without an ack before the lease ends becomes TIMEOUT, or PENDING again for idempotent kinds.

Threads (all outbound, no inbound port):
  * main: heartbeat every 2 s while a video plays (idle: cloud.heartbeat_seconds), with ``playback`` =
    PlaybackEngine.status(); flushes playback events to POST /agents/events as soon as they happen.
  * commands: long-polls GET /agents/commands/{agent_id}/next?wait=N and hands each command to its lane.
  * one executor per lane ("live", "background", "chat"), so a long TTS never blocks a product switch or a
    LIVE chat reply, and each lane
    runs in the order the Cloud hands it out (the Cloud keeps at most one RUNNING command per lane).
  * templates: mirrors the CAPTCHA reference images Sarae's SUPER_ADMIN publishes: they rarely change, so once
    each time the worker connects (every app start) and then every TEMPLATE_SYNC_SECONDS.
    (Videos are not synced from here: the Agent plays Media Library videos from its cloud media cache.)

At-most-once: every outcome goes to the CommandJournal before its ack. A command delivered again is acked
with the stored outcome (with the new claim token) instead of running twice. Acks are retried on network
errors (the Cloud still records a late ack); a 404/409 means the Cloud will not take it, so retrying stops.
Transient failures (offline, timeout, 5xx) -> ``disconnected`` + backoff. Fatal failures (revoked, license
expired, bad credential) -> the worker stops with ``error``.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

from sarae_agent.cloud.client import CloudClient, CloudError
from sarae_agent.cloud.commands import CommandRouter, lane
from sarae_agent.cloud.journal import CommandJournal, CommandLock, EventOutbox
from sarae_agent.errors import AgentError, as_agent_error
from sarae_agent.logging_setup import correlation_id

log = logging.getLogger("sarae.cloud.worker")
CAPTURE_MAX_SIDE = 2560  # verification screenshots are downscaled above this before upload

class Backoff:
    def __init__(self, base: float = 2.0, cap: float = 60.0):
        self.base, self.cap, self.attempt = base, cap, 0

    def next(self) -> float:
        delay = min(self.cap, self.base * (2 ** self.attempt))
        self.attempt += 1
        return delay * random.uniform(0.8, 1.2)

    def reset(self) -> None:
        self.attempt = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CloudWorker:
    HEARTBEAT_PLAYING_SECONDS = 2.0
    TEMPLATE_SYNC_SECONDS = 6 * 3600.0
    TEMPLATE_RETRY_SECONDS = 1800.0
    RESULT_LIMIT = 60000
    ERROR_LIMIT = 1900
    ACK_RETRY_DELAYS = (1, 2, 4, 8, 15)
    POLL_RETRY_SECONDS = 3.0

    def __init__(self, *, agent_id: str, credential: str, api_url: str, timeouts: dict,
                 router: CommandRouter, build_report: Callable[[], dict], outbox: EventOutbox,
                 journal: CommandJournal, emit: Callable[[str, dict], None],
                 on_state: Callable[[str, dict | None], None], lock: CommandLock | None = None,
                 monitor=None, client_factory: Callable[..., CloudClient] = CloudClient):
        self.agent_id = agent_id
        self._credential = credential
        self.api_url = api_url
        self._timeouts = timeouts
        self._router = router
        self._build_report = build_report
        self._outbox = outbox
        self._journal = journal
        self._emit = emit
        self._on_state = on_state
        self._lock = lock
        self._monitor = monitor  # CaptchaMonitor: receives the CAPTCHA templates
        self._factory = client_factory
        self._client: CloudClient | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lanes = {name: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"cloud-{name}")
                       for name in ("live", "background", "chat")}
        self._guard = threading.Lock()
        self._inflight: dict[str, dict] = {}
        self._pending_acks = 0
        self._fatal_error: dict | None = None
        self.recent: deque[dict] = deque(maxlen=50)
        self.connection = {"connected": False, "state": "idle", "last_ok": None, "last_error": None,
                           "next_retry_seconds": None}

    # --- lifecycle -----------------------------------------------------------------------
    def start(self) -> None:
        if self.is_alive():
            return
        if self._lock is not None and not self._lock.acquire():
            raise AgentError("AGENT_ALREADY_RUNNING",
                             "Một tiến trình Sarae Agent khác đang nhận lệnh trên máy này; "
                             "không chạy song song để tránh thực thi trùng")
        self._stop.clear()
        self._client = self._factory(self.api_url, timeout=self._timeouts["request"])
        self._threads = [threading.Thread(target=target, name=name, daemon=True)
                         for target, name in ((self._run, "cloud-worker"), (self._poll_loop, "cloud-commands"),
                                              (self._template_loop, "cloud-templates"))]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout: float = 10.0) -> bool:
        self._stop.set()
        self._wake.set()
        deadline = time.monotonic() + timeout
        main = self._threads[0] if self._threads else None
        if main is not None and main.is_alive() and main is not threading.current_thread():
            main.join(timeout=max(0.1, deadline - time.monotonic()))
        for executor in self._lanes.values():
            executor.shutdown(wait=False, cancel_futures=True)
        client, self._client = self._client, None
        if client is not None:
            client.close()  # also ends a long-poll still waiting on the Cloud
        if self._lock is not None:
            self._lock.release()
        return not (main is not None and main.is_alive())

    def is_alive(self) -> bool:
        return bool(self._threads) and self._threads[0].is_alive()

    def poke(self) -> None:
        """Send a heartbeat as soon as possible (e.g. CAPTCHA detected, playback changed)."""
        self._wake.set()

    def upload_capture(self, at: str, image_path: str) -> None:
        """Send the full Studio-window screenshot of a confirmed detection so the OWNER's Telegram alert can show
        it (the Cloud waits briefly for it). Best effort, off the heartbeat thread."""
        threading.Thread(target=self._upload_capture, args=(at, image_path), daemon=True,
                         name="cloud-capture").start()

    def _upload_capture(self, at: str, image_path: str) -> None:
        try:
            import cv2
            image = cv2.imread(image_path, cv2.IMREAD_COLOR)
            if image is None:
                return
            scale = CAPTURE_MAX_SIDE / max(image.shape[:2])
            if scale < 1:
                image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok:
                return
            for attempt in range(2):
                client = self._client
                if client is None:
                    return
                try:
                    client.upload_verification_capture(self._credential, self.agent_id, at, jpeg.tobytes())
                    return
                except CloudError as exc:
                    if attempt or not exc.retryable:
                        raise
                    self._stop.wait(3)
        except Exception as exc:  # noqa: BLE001 - the text alert still goes out without the picture
            log.warning("Không gửi được ảnh màn hình xác minh lên Cloud: %s", exc,
                        extra={"code": getattr(exc, "code", "CAPTURE_UPLOAD_FAILED")})

    # --- connection state ----------------------------------------------------------------
    def _set_connection(self, connected: bool, state: str, error: dict | None = None,
                        retry: float | None = None) -> None:
        changed = (self.connection["connected"], self.connection["state"]) != (connected, state)
        self.connection.update(connected=connected, state=state, next_retry_seconds=retry)
        if connected:
            self.connection["last_ok"] = _now()
            self.connection["last_error"] = None
        elif error:
            self.connection["last_error"] = error
        if changed or error:
            self._emit("cloud.connection_changed", dict(self.connection))

    def _fatal(self, exc: CloudError) -> None:
        with self._guard:
            if self._fatal_error is not None:
                return
            self._fatal_error = exc.to_dict()
        log.error("Cloud từ chối Agent: %s", exc.message, extra={"code": exc.code})
        self._set_connection(False, "rejected", exc.to_dict())
        self._on_state("error", exc.to_dict())
        self._stop.set()
        self._wake.set()

    # --- main loop: heartbeat + events ---------------------------------------------------
    def _run(self) -> None:
        backoff = Backoff()
        next_heartbeat = 0.0
        retry_events_at = 0.0
        try:
            self._set_connection(False, "connecting")
            while not self._stop.is_set():
                client = self._client
                if client is None:
                    break
                try:
                    now = time.monotonic()
                    if self._outbox.ready.is_set() and now >= retry_events_at:
                        try:
                            self._outbox.flush(lambda batch: client.post_events(self._credential, self.agent_id, batch))
                        except CloudError as exc:
                            if exc.fatal:
                                raise
                            retry_events_at = now + 3
                            log.warning("Không gửi được sự kiện phát: %s", exc.message, extra={"code": exc.code})
                    if self._wake.is_set() or now >= next_heartbeat:
                        self._wake.clear()
                        report = self._build_report()
                        reply = client.heartbeat(self._credential, report)
                        playing = bool((report.get("playback") or {}).get("running"))
                        next_heartbeat = time.monotonic() + (self.HEARTBEAT_PLAYING_SECONDS if playing
                                                             else self._timeouts["heartbeat"])
                        if not self.connection["connected"]:
                            log.info("Đã kết nối Cloud %s", self.api_url)
                            self._on_state("running", None)
                        self._set_connection(True, "connected")
                        self.connection["server_time"] = reply.get("server_time")
                        try:
                            self._router.voice.server_offset = datetime.fromisoformat(reply["server_time"]).timestamp() - time.time()
                        except (KeyError, TypeError, ValueError):
                            pass
                        backoff.reset()
                except CloudError as exc:
                    if exc.fatal:
                        self._fatal(exc)
                        return
                    delay = backoff.next()
                    if self.connection["connected"] or backoff.attempt == 1:
                        log.warning("Mất kết nối Cloud (%s), thử lại sau %.0fs", exc.message, delay,
                                    extra={"code": exc.code})
                    self._set_connection(False, "disconnected", exc.to_dict(), retry=round(delay, 1))
                    self._on_state("disconnected", exc.to_dict())
                    next_heartbeat = 0.0
                    self._stop.wait(delay)
                    continue
                # Wake for new playback events or a poke; otherwise sleep until the next heartbeat.
                timeout = max(0.05, min(1.0, next_heartbeat - time.monotonic()))
                if self._outbox.ready.is_set() and retry_events_at <= time.monotonic():
                    continue
                self._wake.wait(timeout)
        except Exception as exc:  # noqa: BLE001
            log.exception("Cloud worker dừng bất thường")
            self._on_state("error", as_agent_error(exc).to_dict())
        finally:
            self._stop.set()
            if self._fatal_error is None:
                self._set_connection(False, "stopped")

    # --- commands ------------------------------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            client = self._client
            if client is None:
                return
            try:
                command = client.next_command(self._credential, self.agent_id, wait=self._timeouts["poll"])
            except CloudError as exc:
                if self._stop.is_set():
                    return
                if exc.fatal:
                    self._fatal(exc)
                    return
                log.debug("Kênh lệnh lỗi (%s), thử lại", exc.message, extra={"code": exc.code})
                self._stop.wait(self.POLL_RETRY_SECONDS)
                continue
            if command and not self._stop.is_set():
                self._dispatch(command)

    def _dispatch(self, command: dict) -> None:
        command_id = str(command.get("command_id") or "")
        kind = str(command.get("kind") or "")
        if not command_id:
            return
        with self._guard:
            running = self._inflight.get(command_id)
            if running is not None:
                # Re-delivered while still executing (lease ran out): ack once, with the newest claim token.
                running["claim_token"] = command.get("claim_token", "")
                log.info("Lệnh %s đang chạy; dùng claim token mới khi xác nhận", kind, extra={"cid": command_id})
                return
            self._inflight[command_id] = command
        entry = {"command_id": command_id, "kind": kind, "status": "accepted", "attempt": command.get("attempt", 1),
                 "received_at": _now()}
        self.recent.appendleft(entry)
        self._emit("command.received", dict(entry))
        try:
            self._lanes[lane(kind)].submit(self._handle, command, entry)
        except RuntimeError:  # executor shut down: worker stopping
            with self._guard:
                self._inflight.pop(command_id, None)

    def _result_text(self, value: object) -> str:
        text = json.dumps(value, ensure_ascii=False, default=str)
        if len(text) > self.RESULT_LIMIT:
            text = json.dumps({"truncated": True, "preview": text[:1000]}, ensure_ascii=False)
        return text

    def _handle(self, command: dict, entry: dict) -> None:
        command_id, kind = entry["command_id"], entry["kind"]
        token = correlation_id.set(command_id)
        started = time.monotonic()
        try:
            done = self._journal.get(command_id)
            if done is not None:
                log.info("Lệnh %s đã thực thi trước đó; chỉ gửi lại kết quả", kind)
                success, result, error = done["success"], done["result"], None
                entry["replayed"] = True
            else:
                entry["status"] = "running"
                log.info("Thực thi lệnh Cloud %s (lần %s)", kind, command.get("attempt", 1))
                try:
                    success, result, error = True, self._result_text(self._router.execute(kind, command.get("payload"), **({"command_id": command_id, "session_id": command.get("session_id")} if (command.get("payload") or {}).get("provider") == "omnivoice" else {}))), None
                except Exception as exc:  # noqa: BLE001
                    failure = as_agent_error(exc)
                    # The Cloud shows this text to the OWNER (rpc -> HTTP 502 detail): plain message, no JSON.
                    success, result, error = False, failure.message[:self.ERROR_LIMIT], failure.to_dict()
                    log.warning("Lệnh %s thất bại: %s", kind, failure.message, extra={"code": failure.code})
                # Record before acking: if the ack is lost, a re-delivery must not run the action again.
                self._journal.record(command_id, success, result)
            entry.update(status="succeeded" if success else "failed", error=error,
                         duration_ms=int((time.monotonic() - started) * 1000), finished_at=_now())
            self._emit("command.finished", dict(entry))
            self.poke()  # report the new playback/monitor state promptly
            with self._guard:
                latest = self._inflight.get(command_id, command)
            entry["acked"] = self._ack(latest, success, result)
        finally:
            with self._guard:
                self._inflight.pop(command_id, None)
            correlation_id.reset(token)

    def _ack(self, command: dict, success: bool, result: str) -> bool:
        with self._guard:
            self._pending_acks += 1
        try:
            for delay in (0, *self.ACK_RETRY_DELAYS):
                if delay and self._stop.wait(delay):
                    return False  # stopping: the journal re-acks it if the Cloud delivers it again
                client = self._client
                if client is None:
                    return False
                try:
                    client.ack(self._credential, self.agent_id, command, success, result)
                    return True
                except CloudError as exc:
                    if exc.fatal:
                        self._fatal(exc)
                        return False
                    if exc.code == "ACK_REJECTED":
                        log.warning("Cloud từ chối xác nhận lệnh: %s", exc.message, extra={"code": exc.code})
                        return False
                    log.info("Không gửi được xác nhận lệnh, thử lại: %s", exc.message, extra={"code": exc.code})
            return False
        finally:
            with self._guard:
                self._pending_acks -= 1

    # --- CAPTCHA templates ---------------------------------------------------------------
    def _template_loop(self) -> None:
        next_templates = 0.0
        while not self._stop.is_set():
            if self._monitor is not None and time.monotonic() >= next_templates and self._client is not None:
                try:
                    self.sync_templates(self._client)
                    next_templates = time.monotonic() + self.TEMPLATE_SYNC_SECONDS
                except CloudError as exc:
                    if exc.fatal:
                        self._fatal(exc)
                        return
                    next_templates = time.monotonic() + self.TEMPLATE_RETRY_SECONDS
                except Exception:  # noqa: BLE001
                    log.exception("Cập nhật ảnh mẫu CAPTCHA lỗi")
                    next_templates = time.monotonic() + self.TEMPLATE_SYNC_SECONDS
            self._stop.wait(1.0)

    def sync_templates(self, client: CloudClient) -> dict:
        remote = client.captcha_templates(self._credential, self.agent_id)
        return self._monitor.sync_templates(
            remote, lambda template_id: client.captcha_template_image(self._credential, self.agent_id, template_id))

    def status(self) -> dict:
        with self._guard:
            pending_acks, in_flight = self._pending_acks, len(self._inflight)
        return {**self.connection, "agent_id": self.agent_id, "api_url": self.api_url,
                "pending_acks": pending_acks, "commands_in_flight": in_flight,
                "events_pending": self._outbox.pending(), "recent_commands": list(self.recent)[:20]}
