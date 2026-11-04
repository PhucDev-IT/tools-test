"""Structured business logging: timestamp, level, error code and correlation ID.

Destinations: rotating file under %LOCALAPPDATA%\\SaraeAgent\\logs, stderr (captured by the
desktop shell) and `log.entry` IPC events. stdout is reserved exclusively for IPC frames.
"""
from __future__ import annotations

import contextvars
import logging
import re
import sys
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Callable

from sarae_agent.config import paths

correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)

_SECRET_PATTERNS = [
    re.compile(r"SARAE-ACT-[A-Za-z0-9_\-]+"),
    re.compile(r"(?i)(x-sarae-agent-key|agent_credential|password|authorization)([\"'=:\s]+)([^\s\"',}]+)"),
]


def redact(text: str) -> str:
    text = _SECRET_PATTERNS[0].sub("SARAE-ACT-***", text)
    return _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "cid"):
            record.cid = correlation_id.get() or "-"
        if not hasattr(record, "code"):
            record.code = "-"
        # obsws_python logs full tracebacks whenever OBS closes; keep a single line.
        if record.name.startswith("obsws_python"):
            record.exc_info = None
            record.exc_text = None
        return True


class RedactingFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):  # noqa: N802 - logging API
        return datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds")

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


FORMAT = "%(asctime)s %(levelname)-7s %(name)s [code=%(code)s cid=%(cid)s] %(message)s"


class EventHandler(logging.Handler):
    """Forwards INFO+ business logs to the GUI and keeps a ring buffer for logs.recent."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.sink: Callable[[dict], None] | None = None
        self.recent: deque[dict] = deque(maxlen=500)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith(("httpx", "httpcore", "websocket", "asyncio")):
                return
            entry = {
                "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
                "level": record.levelname,
                "logger": record.name,
                "code": getattr(record, "code", "-"),
                "correlation_id": getattr(record, "cid", "-"),
                "message": redact(record.getMessage())[:2000],
            }
            self.recent.append(entry)
            if self.sink:
                self.sink(entry)
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


event_handler = EventHandler()


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    context = ContextFilter()
    formatter = RedactingFormatter(FORMAT)

    file_handler = RotatingFileHandler(paths.logs / "agent.log", maxBytes=2_000_000, backupCount=5,
                                       encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(context)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(formatter)
    stderr_handler.addFilter(context)

    event_handler.addFilter(context)
    for handler in (file_handler, stderr_handler, event_handler):
        root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "websocket", "asyncio", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # obsws_python logs ERROR on every refused connection; ObsController already reports
    # these with a typed code (OBS_NOT_RUNNING...), so only keep its critical messages.
    logging.getLogger("obsws_python").setLevel(logging.CRITICAL)
