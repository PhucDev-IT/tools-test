"""Agent Core entrypoint (PyInstaller sidecar `sarae-agent-core`).

    sarae-agent-core --ipc [--parent-pid PID]   JSON Lines IPC on stdin/stdout (used by the GUI)
    sarae-agent-core --self-check                verify bundled native deps, print JSON, exit
    sarae-agent-core --version

stdout carries IPC frames only. The original stdout handle is duplicated for IPC and fd 1 is
pointed at stderr, so stray print() calls or native libraries can never corrupt a frame.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import threading
import time

from sarae_agent import IPC_PROTOCOL_VERSION, __version__

EXIT_OK, EXIT_ALREADY_RUNNING, EXIT_INIT_FAILED = 0, 3, 2


def _isolate_stdout() -> io.BufferedWriter:
    sys.stdout.flush()
    ipc_fd = os.dup(1)
    os.dup2(2, 1)  # native writes to fd 1 now land on stderr
    sys.stdout = io.TextIOWrapper(os.fdopen(os.dup(2), "wb", buffering=0), encoding="utf-8",
                                  errors="replace", line_buffering=True)
    return os.fdopen(ipc_fd, "wb", buffering=0)


def self_check() -> int:
    report: dict = {"version": __version__, "ipc_protocol": IPC_PROTOCOL_VERSION, "python": sys.version,
                    "frozen": bool(getattr(sys, "frozen", False)), "modules": {}}
    for name in ("httpx", "pydantic", "obsws_python", "psutil", "mss", "numpy", "cv2", "edge_tts"):
        try:
            module = __import__(name)
            report["modules"][name] = getattr(module, "__version__", "ok")
        except Exception as exc:  # noqa: BLE001
            report["modules"][name] = f"ERROR {type(exc).__name__}: {exc}"
    try:
        from sarae_agent.activation import dpapi
        report["dpapi_roundtrip"] = dpapi.unprotect(dpapi.protect(b"probe")) == b"probe"
    except Exception as exc:  # noqa: BLE001
        report["dpapi_roundtrip"] = f"ERROR {exc}"
    from sarae_agent.config import paths
    report["data_root"] = str(paths.root)
    from sarae_agent.monitoring.captcha import builtin_templates, template_specs
    import cv2
    import numpy as np
    loaded = {name for name, _ in builtin_templates(cv2, np)}
    report["builtins"] = {name: name in loaded for name in template_specs()}
    ok = (all(not str(v).startswith("ERROR") for v in report["modules"].values()) and
          report["dpapi_roundtrip"] is True and bool(report["builtins"]) and all(report["builtins"].values()))
    report["ok"] = ok
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if ok else 1


def _preload_native_libraries() -> None:
    """Load numpy/OpenCV on the main thread before it blocks reading stdin.

    On Windows a DLL whose CRT start-up queries the std handles (numpy's OpenBLAS) waits, under the
    loader lock, for a pending synchronous read on the stdin pipe to finish. Imported later from a
    worker thread (media thumbnails, CAPTCHA monitor) it would hang until the GUI's next request.
    """
    for name in ("numpy", "cv2"):
        try:
            __import__(name)
        except Exception as exc:  # noqa: BLE001 - optional features report the missing library themselves
            logging.getLogger("sarae.main").warning("Không nạp được thư viện %s: %s", name, exc)


def _watch_parents(pids: set[int], on_exit) -> None:
    from sarae_agent.windows.win32 import is_process_alive

    while True:
        time.sleep(1.5)
        if any(not is_process_alive(pid) for pid in pids):
            on_exit()
            return


def run_ipc(parent_pid: int | None) -> int:
    ipc_out = _isolate_stdout()
    stdin = sys.stdin.buffer

    from sarae_agent.ipc import protocol
    from sarae_agent.logging_setup import event_handler, setup_logging
    from sarae_agent.windows.win32 import SingleInstance

    setup_logging()
    log = logging.getLogger("sarae.main")

    import hashlib
    from sarae_agent.config import paths

    # One Core per data folder (= per Windows user in production) owns the state files.
    digest = hashlib.sha256(str(paths.root).casefold().encode()).hexdigest()[:16]
    instance = SingleInstance(f"Local\\SaraeAgentCore.v1.{digest}")
    deadline = time.monotonic() + 8  # a previous Core may still be releasing resources
    while not instance.acquire():
        if time.monotonic() > deadline:
            ipc_out.write(protocol.encode(protocol.event("core.error", {
                "code": "CORE_ALREADY_RUNNING", "message": "Một Agent Core khác đang chạy trong phiên Windows này",
                "fatal": True})))
            return EXIT_ALREADY_RUNNING
        time.sleep(0.3)

    from sarae_agent.ipc.methods import register_all
    from sarae_agent.ipc.server import IpcServer
    from sarae_agent.runtime import AgentRuntime

    server = IpcServer(stdin, ipc_out)
    try:
        runtime = AgentRuntime(server.emit)
    except Exception as exc:  # noqa: BLE001
        log.exception("Không khởi tạo được Agent Core")
        server.emit("core.error", {"code": "CORE_INIT_FAILED", "message": str(exc)[:500], "fatal": True})
        server.close()
        instance.release()
        return EXIT_INIT_FAILED
    register_all(server, runtime)

    stopping = threading.Lock()

    def stop_everything(reason: str) -> None:
        if not stopping.acquire(blocking=False):
            return
        runtime.shutdown()
        server.emit("core.stopped", {"reason": reason})
        event_handler.sink = None
        server.close()
        instance.release()
        log.info("Agent Core thoát (%s)", reason)
        logging.shutdown()

    # Exit when the GUI (--parent-pid) or our direct parent dies. In the PyInstaller one-file
    # build the direct parent is the bootloader: if the GUI hard-kills it, this child must not
    # linger (it would hold the singleton mutex and block the next start).
    watched = {pid for pid in (parent_pid, os.getppid()) if pid and pid > 4}

    def parent_gone() -> None:
        stop_everything("parent_exited")
        os._exit(EXIT_OK)

    if watched:
        threading.Thread(target=_watch_parents, args=(watched, parent_gone), name="parent-watch",
                         daemon=True).start()

    # core.ready is always the first event; earlier log lines stay available via logs.recent.
    server.emit("core.ready", {"version": __version__, "ipc_protocol": IPC_PROTOCOL_VERSION, "pid": os.getpid(),
                               "methods": server.methods, "status": runtime.system_status()})
    event_handler.sink = lambda entry: server.emit("log.entry", entry)
    log.info("Agent Core %s sẵn sàng (pid %s, IPC v%s)", __version__, os.getpid(), IPC_PROTOCOL_VERSION)
    _preload_native_libraries()  # before serve(): requests wait in the pipe meanwhile
    runtime.boot()
    reason = server.serve(lambda: True)
    stop_everything(reason)
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(prog="sarae-agent-core", description="Sarae Windows Agent Core")
    parser.add_argument("--ipc", action="store_true", help="run JSON Lines IPC on stdin/stdout (default)")
    parser.add_argument("--parent-pid", type=int, default=None, help="exit when this process exits")
    parser.add_argument("--self-check", action="store_true", help="verify bundled dependencies and exit")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()
    if args.version:
        print(__version__)
        return EXIT_OK
    if args.self_check:
        return self_check()
    return run_ipc(args.parent_pid)


if __name__ == "__main__":
    sys.exit(main())
