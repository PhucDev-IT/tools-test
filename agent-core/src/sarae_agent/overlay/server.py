"""Local HTTP server of the OBS overlay renderer (127.0.0.1 only).

    GET  /overlay/                 the renderer page (overlay.html) (built from web/src/overlay/obs, shipped with the Agent)
    GET  /overlay/assets/<file>    its JS/CSS
    GET  /overlay/state?t=&rev=&seq=   what to show now: scene revision, playback clock, show flag (renderer polls
                                   it); rev / seq = what it drew last (ACK)
    GET  /overlay/scene?t=&revision=   the scene of that revision (images point at local copies)
    POST /overlay/ready?t=         {revision, ok, missing}: the renderer loaded and preloaded that revision
    GET  /api/overlay-files/<sha256>.<ext>?t=   a cached image

Everything but the page's own code needs the token of the Browser Source URL, and the Host header must be this
server (a web page in another browser cannot read or drive the renderer, DNS rebinding included). No CORS headers.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("sarae.overlay")

RENDERER_DIR = Path(__file__).with_name("renderer")
STATIC_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
MAX_BODY = 64 * 1024
TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8",
         ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif", ".woff2": "font/woff2", ".woff": "font/woff"}


class OverlayHandlers:
    """What the server asks the overlay service (kept small so it can be faked in tests)."""
    token: str
    renderer_state: Callable[[dict | None], dict]
    scene_for: Callable[[str], dict | None]
    renderer_ready: Callable[[dict], dict]
    file_path: Callable[[str], Path | None]


def _handler(handlers: OverlayHandlers, renderer_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SaraeOverlay"
        sys_version = ""

        def log_message(self, *_args):  # the Agent log has its own lines; no access log
            pass

        def _send(self, status: int, body: bytes, content_type: str, cache: str = "no-store"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, value) -> None:
            self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _allowed_host(self) -> bool:
            port = self.server.server_address[1]
            return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _token_ok(self, query: dict) -> bool:
            return bool(handlers.token) and query.get("t", [""])[0] == handlers.token

        def _route(self):
            if not self._allowed_host():
                return self._json(403, {"error": "host"})
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            path = parts.path
            if self.command == "GET" and path in ("/overlay/", "/overlay/index.html"):
                page = renderer_dir / "overlay.html"
                if not page.is_file():
                    return self._send(503, b"Overlay renderer not built", "text/plain; charset=utf-8")
                return self._send(200, page.read_bytes(), TYPES[".html"])
            if self.command == "GET" and path.startswith("/overlay/assets/"):
                name = path.rsplit("/", 1)[-1]
                target = renderer_dir / "assets" / name
                if not STATIC_NAME.fullmatch(name) or not target.is_file():
                    return self._json(404, {"error": "not_found"})
                return self._send(200, target.read_bytes(), TYPES.get(target.suffix, "application/octet-stream"),
                                  "public, max-age=31536000, immutable")
            if not self._token_ok(query):
                return self._json(403, {"error": "token"})
            if self.command == "GET" and path == "/overlay/state":
                seq = query.get("seq", [""])[0]
                ack = {"revision": query.get("rev", [""])[0][:64], "seq": int(seq) if seq.isdigit() else None,
                       "build": query.get("build", [""])[0][:120]}
                return self._json(200, handlers.renderer_state(ack))
            if self.command == "GET" and path == "/overlay/scene":
                scene = handlers.scene_for(query.get("revision", [""])[0])
                return self._json(200, scene) if scene is not None else self._json(404, {"error": "revision"})
            if self.command == "POST" and path == "/overlay/ready":
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._json(413, {"error": "too_large"})
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._json(400, {"error": "json"})
                return self._json(200, handlers.renderer_ready(body if isinstance(body, dict) else {}))
            if self.command == "GET" and path.startswith("/api/overlay-files/"):
                target = handlers.file_path(path.rsplit("/", 1)[-1])
                if target is None:
                    return self._json(404, {"error": "not_found"})
                return self._send(200, target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream",
                                  "private, max-age=86400")
            return self._json(404, {"error": "not_found"})

        def do_GET(self):  # noqa: N802
            try:
                self._route()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:  # noqa: BLE001
                log.exception("Overlay server lỗi")
                try:
                    self._json(500, {"error": "internal"})
                except OSError:
                    pass

        do_POST = do_GET
        do_HEAD = do_GET

    return Handler


def renderer_build(renderer_dir: Path = RENDERER_DIR) -> str:
    """The script file name of the renderer this Agent serves (what an up-to-date page reports as its build)."""
    try:
        found = re.search(r'src="\./assets/([^"]+\.js)"', (renderer_dir / "overlay.html").read_text(encoding="utf-8"))
    except OSError:
        return ""
    return found.group(1) if found else ""


class OverlayServer:
    """Starts on the configured port, else the next free one of the 10 after it (the Browser Source URL follows)."""

    def __init__(self, handlers: OverlayHandlers, renderer_dir: Path = RENDERER_DIR):
        self.handlers = handlers
        self.renderer_dir = renderer_dir
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None
        self.build = renderer_build(renderer_dir)

    def start(self, port: int) -> int:
        if self._server is not None:
            return self.port
        last: OSError | None = None
        for candidate in [port + i for i in range(10)] if port else [0]:
            try:
                server = ThreadingHTTPServer(("127.0.0.1", candidate), _handler(self.handlers, self.renderer_dir))
                break
            except OSError as exc:
                last = exc
        else:
            raise last or OSError("no port")
        server.daemon_threads = True
        self._server, self.port = server, server.server_address[1]
        self._thread = threading.Thread(target=server.serve_forever, name="overlay-server", daemon=True)
        self._thread.start()
        log.info("Overlay renderer: http://127.0.0.1:%s/overlay/", self.port)
        return self.port

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
