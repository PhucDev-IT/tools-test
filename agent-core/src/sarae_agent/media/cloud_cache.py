"""Verified, resumable private media cache for the Windows Agent.

Cloud is authoritative for the session manifest. Only verified files become playable.
Never modify or remove LOCAL_LEGACY files; cache lives in its own directory.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from sarae_agent.config import paths
from sarae_agent.errors import AgentError

MEDIA_ID = re.compile(r"^[0-9a-f]{32}$")
CHUNK = 1024 * 1024
CACHE_EXTENSIONS = (".mp4", ".mov")
PROGRESS_SECONDS = 0.5


def cache_dir(agent_id: str) -> Path:
    """Cache namespace of one Agent: <data root>/cloud-media-cache/<agent_id>."""
    if not agent_id or Path(agent_id).name != agent_id or agent_id in (".", ".."):
        raise AgentError("CACHE_AGENT_INVALID", "Agent ID không hợp lệ")
    return paths.root / "cloud-media-cache" / agent_id


def cached_path(root: Path, media_id: str) -> Path | None:
    """Verified cached file of a Media Library video (32-hex id), or None.

    Only the published ``<id>.mp4`` / ``<id>.mov`` count; an unfinished ``.part`` never does.
    """
    if not isinstance(media_id, str) or not MEDIA_ID.fullmatch(media_id):
        return None
    for extension in CACHE_EXTENSIONS:
        path = root / f"{media_id}{extension}"
        if path.is_file():
            return path
    return None


def _digest(path: Path, algorithm: str) -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as source:
        for data in iter(lambda: source.read(CHUNK), b""):
            h.update(data)
    return h.hexdigest()


class CloudMediaCache:
    """One cache namespace per Agent. No provider credentials or URLs are persisted."""

    def __init__(self, client: httpx.Client, agent_id: str, key: str, root: Path | None = None):
        self.client, self.agent_id, self.key = client, agent_id, key
        self.root = root if root is not None else cache_dir(agent_id)
        self._lock = threading.RLock()

    def _headers(self) -> dict[str, str]:
        return {"X-Sarae-Agent-Key": self.key}

    def _check(self, response: httpx.Response, code: str):
        if response.status_code not in (200, 206):
            raise AgentError(code, f"Cloud Media HTTP {response.status_code}", retryable=response.status_code in (429, 500, 502, 503, 504))

    def manifest(self, session_id: int) -> dict:
        if session_id <= 0:
            raise AgentError("CACHE_SESSION_INVALID", "Session ID không hợp lệ")
        try:
            response = self.client.get("agent-media/manifest", params={"agent_id": self.agent_id, "session_id": session_id},
                                       headers=self._headers())
            self._check(response, "CACHE_MANIFEST_FAILED")
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AgentError("CACHE_MANIFEST_FAILED", "Không tải được media manifest", retryable=True) from exc
        if payload.get("session_id") != session_id or payload.get("agent_id") != self.agent_id or not isinstance(payload.get("items"), list):
            raise AgentError("CACHE_MANIFEST_INVALID", "Cloud trả về manifest không hợp lệ")
        return payload

    def _target(self, item: dict) -> Path:
        media_id = item.get("media_id")
        if not isinstance(media_id, str) or not MEDIA_ID.fullmatch(media_id):
            raise AgentError("CACHE_MEDIA_INVALID", "Media ID không hợp lệ")
        extension = Path(str(item.get("filename") or "")).suffix.lower()
        if extension not in CACHE_EXTENSIONS:
            raise AgentError("CACHE_MEDIA_INVALID", "Định dạng video không hợp lệ")
        return self.root / f"{media_id}{extension}"

    def _valid(self, path: Path, item: dict) -> bool:
        if not path.is_file() or path.stat().st_size != item["size_bytes"]:
            return False
        sha = item.get("sha256")
        md5 = item.get("md5")
        if sha:
            return _digest(path, "sha256") == sha.lower()
        if md5:
            return _digest(path, "md5") == md5.lower()
        return False  # no checksum: cannot mark ready

    def prepare(self, session_id: int, *, max_items: int = 300, on_progress: Callable[[dict], None] | None = None) -> dict:
        """Download all session videos before playback; return an explicit READY/FAILED manifest.

        Every video of the session is required (random playback may pick any of them). Interrupted .part files are
        retained to resume; a file already cached with the same checksum is not downloaded again. ``on_progress``
        receives the running state (no file paths) at most every PROGRESS_SECONDS.
        """
        with self._lock:
            manifest = self.manifest(session_id)
            items = manifest["items"]
            if len(items) > max_items:
                raise AgentError("CACHE_LIMIT", "Quá nhiều video trong phiên")
            self.root.mkdir(parents=True, exist_ok=True)
            ready, failed = [], []
            state = {"session_id": session_id, "state": "PREPARING", "total": len(items), "ready": ready, "failed": failed,
                     "bytes_total": sum(int(i.get("size_bytes") or 0) for i in items), "bytes_done": 0, "current": None}
            last = [0.0]

            def report(force: bool = False):
                if on_progress is not None and (force or time.monotonic() - last[0] >= PROGRESS_SECONDS):
                    last[0] = time.monotonic()
                    on_progress(dict(state, ready=list(ready), failed=list(failed)))

            report(force=True)
            for item in items:
                state["current"] = item.get("media_id")
                done_before = state["bytes_done"]

                def advance(received: int, base=done_before):
                    state["bytes_done"] = base + received
                    report()
                try:
                    path = self._prepare_item(item, advance)
                    ready.append({"media_id": item["media_id"], "path": str(path), "size_bytes": item["size_bytes"],
                                  "revision": item.get("revision")})
                except (AgentError, OSError, httpx.HTTPError, ValueError) as exc:
                    failed.append({"media_id": str(item.get("media_id", "")), "error": str(exc)[:200]})
                state["bytes_done"] = done_before + int(item.get("size_bytes") or 0)
                report(force=True)
            missing = manifest.get("products_without_video") or []
            return {"session_id": session_id, "state": "READY" if not failed and not missing and ready else "FAILED",
                    "ready": ready, "failed": failed, "products_without_video": missing,
                    "total": len(items), "bytes_total": state["bytes_total"], "bytes_done": state["bytes_done"]}

    def _prepare_item(self, item: dict, advance: Callable[[int], None] | None = None) -> Path:
        length = item.get("size_bytes")
        if not isinstance(length, int) or not (0 < length <= 4 * 1024**3):
            raise AgentError("CACHE_MEDIA_INVALID", "Dung lượng file không hợp lệ")
        sha, md5 = item.get("sha256"), item.get("md5")
        if not ((isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{64}", sha)) or
                (isinstance(md5, str) and re.fullmatch(r"[0-9a-fA-F]{32}", md5))):
            raise AgentError("CACHE_CHECKSUM_MISSING", "Không có checksum để xác minh video")
        path = self._target(item)
        if self._valid(path, item):
            return path
        part = path.with_suffix(path.suffix + ".part")
        if self._valid(part, item):
            os.replace(part, path)
            return path
        if part.exists() and part.stat().st_size >= length:
            part.unlink()
        offset = part.stat().st_size if part.exists() else 0
        headers = self._headers()
        if offset:
            headers["Range"] = f"bytes={offset}-"
        with self.client.stream("GET", f"agent-media/{item['media_id']}/content",
                                params={"agent_id": self.agent_id}, headers=headers, timeout=120) as response:
            self._check(response, "CACHE_DOWNLOAD_FAILED")
            if offset:
                expected = f"bytes {offset}-"
                if response.status_code != 206 or not response.headers.get("content-range", "").startswith(expected):
                    raise AgentError("CACHE_RANGE_INVALID", "Cloud không hỗ trợ tiếp tục tải đúng offset")
            elif response.status_code == 206 and not response.headers.get("content-range", "").startswith("bytes 0-"):
                raise AgentError("CACHE_RANGE_INVALID", "Cloud trả về Range không hợp lệ")
            received = offset
            with part.open("ab" if offset else "wb") as output:
                for data in response.iter_bytes(CHUNK):
                    output.write(data)
                    received += len(data)
                    if advance is not None:
                        advance(received)
        if not self._valid(part, item):
            # Keep partial only if incomplete, discard corrupt complete file.
            if part.exists() and part.stat().st_size == length:
                part.unlink()
            raise AgentError("CACHE_INTEGRITY_FAILED", "Video tải xuống không khớp dung lượng/checksum", retryable=True)
        os.replace(part, path)
        return path
