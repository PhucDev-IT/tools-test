"""Stdin reader + stdout writer for the JSON Lines protocol.

Deadlock rules:
* Producers never write to the pipe directly; frames go through a queue drained by one
  writer thread, so a slow GUI can never block a worker that holds a business lock.
* The reader only parses and hands requests to a thread pool; long work (scan, activation,
  OBS) never stalls reading the next request.
* When the queue is saturated, `log.entry` events are dropped first; responses are never dropped.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable

from pydantic import BaseModel, ValidationError

from sarae_agent.errors import AgentError, as_agent_error
from sarae_agent.ipc import protocol
from sarae_agent.logging_setup import correlation_id

log = logging.getLogger("sarae.ipc")

Handler = Callable[[Any], Any]


@dataclass(frozen=True)
class MethodSpec:
    name: str
    handler: Handler
    params: type[BaseModel] | None = None


class IpcServer:
    QUEUE_LIMIT = 5000

    def __init__(self, reader: BinaryIO, writer: BinaryIO, *, workers: int = 8):
        self._reader = reader
        self._writer = writer
        self._methods: dict[str, MethodSpec] = {}
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ipc-req")
        self._stopping = threading.Event()
        self._writer_thread = threading.Thread(target=self._drain, name="ipc-writer", daemon=True)
        self._writer_thread.start()
        self._dropped = 0

    # --- registration --------------------------------------------------------------------
    def register(self, name: str, handler: Handler, params: type[BaseModel] | None = None) -> None:
        if not protocol.METHOD_RE.fullmatch(name):
            raise ValueError(f"invalid method name {name}")
        self._methods[name] = MethodSpec(name, handler, params)

    @property
    def methods(self) -> list[str]:
        return sorted(self._methods)

    # --- output --------------------------------------------------------------------------
    def _put(self, frame: dict, *, droppable: bool = False) -> None:
        if droppable and self._queue.qsize() >= self.QUEUE_LIMIT:
            self._dropped += 1
            return
        self._queue.put(protocol.encode(frame))

    def emit(self, name: str, data: Any = None) -> None:
        self._put(protocol.event(name, data), droppable=name == "log.entry")

    def _drain(self) -> None:
        while True:
            frame = self._queue.get()
            if frame is None:
                return
            try:
                self._writer.write(frame)
                self._writer.flush()
            except (OSError, ValueError):
                # GUI closed the pipe: nothing more can be delivered.
                self._stopping.set()
                return

    def flush(self, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.02)

    # --- dispatch ------------------------------------------------------------------------
    def _execute(self, request: protocol.Request) -> None:
        token = correlation_id.set(request.request_id)
        try:
            spec = self._methods.get(request.method)
            if spec is None:
                raise AgentError("METHOD_NOT_FOUND", f"Method không được hỗ trợ: {request.method}")
            if spec.params is not None:
                try:
                    arg = spec.params.model_validate(request.params)
                except ValidationError as exc:
                    first = exc.errors()[0]
                    field = ".".join(str(p) for p in first.get("loc", ())) or "params"
                    raise AgentError("INVALID_PARAMS", f"{field}: {first.get('msg')}") from exc
                result = spec.handler(arg)
            else:
                if request.params:
                    raise AgentError("INVALID_PARAMS", f"{request.method} không nhận tham số")
                result = spec.handler(None)
            self._put(protocol.response_ok(request.request_id, result))
        except Exception as exc:  # noqa: BLE001 - every failure becomes a typed response
            error = as_agent_error(exc)
            level = logging.ERROR if error.code == "INTERNAL_ERROR" else logging.INFO
            log.log(level, "%s thất bại: %s", request.method, error.message, extra={"code": error.code},
                    exc_info=error.code == "INTERNAL_ERROR")
            self._put(protocol.response_error(request.request_id, error.to_dict()))
        finally:
            correlation_id.reset(token)

    def serve(self, on_shutdown_request: Callable[[], bool]) -> str:
        """Blocks until stdin closes or a shutdown completes. Returns the stop reason."""
        while not self._stopping.is_set():
            try:
                line = self._reader.readline(protocol.MAX_FRAME_BYTES + 2)
            except (OSError, ValueError):
                return "stdin_error"
            if not line:
                return "stdin_closed"
            if not line.strip():
                continue
            try:
                request = protocol.parse_request(line)
            except protocol.ProtocolError as exc:
                self._put(protocol.response_error(exc.request_id, {"code": exc.code, "message": exc.message}))
                continue
            except UnicodeDecodeError:
                self._put(protocol.response_error(None, {"code": "INVALID_ENCODING",
                                                         "message": "Thông điệp IPC phải là UTF-8"}))
                continue
            if request.method == "system.shutdown":
                # Run inline so no further requests are accepted while stopping.
                self._execute(request)
                if on_shutdown_request():
                    return "shutdown_requested"
                continue
            self._pool.submit(self._execute, request)
        return "pipe_closed"

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.flush()
        self._queue.put(None)
        self._writer_thread.join(timeout=2)
