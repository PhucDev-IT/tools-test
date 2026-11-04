"""At-most-once execution and event delivery state, ported from the retired backend/agent/main.py.

* CommandJournal: command ids this Agent executed and their outcome, on disk. A command delivered
  again (its ack was lost, the Cloud re-delivered a retryable command, or the Core restarted) is acked
  with the stored outcome instead of being executed twice.
* CommandLock: only one process per PC takes Cloud commands (OS file lock, released with the process).
* EventOutbox: playback events waiting for POST /agents/events. seq starts at the clock in ms, so it keeps
  increasing across restarts and the Cloud drops re-sent duplicates (agent_id, seq).
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path


class CommandJournal:
    def __init__(self, path: Path, limit: int = 500):
        self.path = Path(path)
        self.limit = limit
        self.lock = threading.Lock()
        try:
            entries = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries: dict = entries if isinstance(entries, dict) else {}
        except (OSError, ValueError):
            self.entries = {}

    def get(self, command_id: str) -> dict | None:
        with self.lock:
            return self.entries.get(command_id)

    def record(self, command_id: str, success: bool, result: str) -> None:
        with self.lock:
            self.entries[command_id] = {"success": success, "result": result, "at": time.time()}
            while len(self.entries) > self.limit:
                self.entries.pop(next(iter(self.entries)))
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.entries, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)


class CommandLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        handle = open(self.path, "a+")
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            handle.close()


class EventOutbox:
    def __init__(self, limit: int = 1000, path: Path | None = None):
        self.lock = threading.Lock()
        self.items: list[dict] = []
        self.limit = limit
        self.seq = int(time.time() * 1000)
        self.path = path
        if path and path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.items = saved if isinstance(saved, list) else saved["items"]
            self.seq = max([self.seq, saved.get("seq", 0) if isinstance(saved, dict) else 0] + [e["seq"] for e in self.items])
        self.ready = threading.Event()
        if self.items:
            self.ready.set()

    def _save(self):
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"seq": self.seq, "items": self.items}), encoding="utf-8")
            tmp.replace(self.path)

    def push(self, event: dict) -> None:
        with self.lock:
            self.seq += 1
            self.items.append({**event, "seq": self.seq})
            del self.items[:-self.limit]
            self._save()
        self.ready.set()

    def flush(self, send) -> int:
        """``send(events)`` posts one batch (≤ 200); events stay queued when it raises."""
        with self.lock:
            batch = self.items[:200]
        if not batch:
            self.ready.clear()
            return 0
        send(batch)
        sent = {e["seq"] for e in batch}
        with self.lock:
            self.items = [e for e in self.items if e["seq"] not in sent]
            self._save()
            if not self.items:
                self.ready.clear()
        return len(batch)

    def pending(self) -> int:
        with self.lock:
            return len(self.items)
