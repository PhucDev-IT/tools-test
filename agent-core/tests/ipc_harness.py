"""Spawn the Core as a real child process and talk JSON Lines to it (used by integration tests)."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def core_command(*extra: str) -> list[str]:
    """Source Core by default; set SARAE_CORE_EXE to test the frozen PyInstaller sidecar instead."""
    exe = os.environ.get("SARAE_CORE_EXE")
    base = [exe] if exe else [sys.executable, "-m", "sarae_agent"]
    return [*base, "--ipc", *extra]


class CoreProcess:
    def __init__(self, home: Path, command: list[str] | None = None, env: dict | None = None):
        environment = {**os.environ, "SARAE_AGENT_HOME": str(home), "PYTHONPATH": str(SRC),
                       "PYTHONUTF8": "1", **(env or {})}
        self.proc = subprocess.Popen(command or core_command(),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=environment)
        self.frames: queue.Queue = queue.Queue()
        self.events: list[dict] = []
        self.raw_lines: list[bytes] = []
        self.stderr: list[str] = []
        self._counter = 0
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.raw_lines.append(line)
            frame = json.loads(line)
            if frame.get("type") == "event":
                self.events.append(frame)
            self.frames.put(frame)

    def _read_err(self) -> None:
        for line in self.proc.stderr:
            self.stderr.append(line.decode("utf-8", "replace"))

    def send_raw(self, data: bytes) -> None:
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        self._counter += 1
        request_id = f"t-{self._counter}"
        self.send_raw((json.dumps({"v": 1, "type": "request", "request_id": request_id, "method": method,
                                   "params": params or {}}) + "\n").encode())
        return self.wait_response(request_id, timeout)

    def wait_response(self, request_id: str, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        stash = []
        try:
            while time.monotonic() < deadline:
                try:
                    frame = self.frames.get(timeout=0.2)
                except queue.Empty:
                    continue
                if frame.get("type") == "response" and frame.get("request_id") == request_id:
                    return frame
                if frame.get("type") == "response":
                    stash.append(frame)
            raise TimeoutError(f"no response for {request_id}; stderr={''.join(self.stderr)[-2000:]}")
        finally:
            for frame in stash:
                self.frames.put(frame)

    def mark(self) -> int:
        """Cursor for wait_event(since=...): ignore events received before this point."""
        return len(self.events)

    def wait_event(self, name: str, timeout: float = 15.0, predicate=None, since: int = 0) -> dict:
        deadline = time.monotonic() + timeout
        seen = since
        while time.monotonic() < deadline:
            for frame in self.events[seen:]:
                seen += 1
                if frame["event"] == name and (predicate is None or predicate(frame["data"])):
                    return frame
            time.sleep(0.05)
        raise TimeoutError(f"event {name} not received; stderr={''.join(self.stderr)[-2000:]}")

    def close(self, timeout: float = 15.0) -> int:
        if self.proc.poll() is None:
            try:
                self.request("system.shutdown", timeout=timeout)
            except (TimeoutError, OSError, ValueError):
                pass
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return -9
