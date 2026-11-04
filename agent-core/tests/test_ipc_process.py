"""Core as a real child process: framing, concurrency, shutdown paths and singleton."""
import json
import subprocess
import sys
import time

import pytest

from ipc_harness import CoreProcess, core_command


@pytest.fixture()
def core(tmp_path):
    process = CoreProcess(tmp_path / "home")
    process.wait_event("core.ready", 30)
    yield process
    process.close()


def test_ready_event_and_status(core):
    ready = core.events[0]
    assert ready["event"] == "core.ready" and ready["v"] == 1
    assert "media.cloud_cache.prepare" in ready["data"]["methods"] and "system.shutdown" in ready["data"]["methods"]
    status = core.request("system.status")
    assert status["ok"] and status["result"]["agent"]["state"] == "stopped"
    methods = ready["data"]["methods"]
    assert not any(m.startswith(("media.folders", "media.scan")) or m in ("media.list", "media.summary")
                   for m in methods)  # local video folders are retired


def test_builtin_templates_ready_without_custom_or_cloud(core):
    monitor = core.request("monitor.status")["result"]
    assert monitor["builtins_ready"] is True
    assert monitor["templates"] == 8
    templates = core.request("monitor.templates.list")["result"]["templates"]
    assert {t["name"] for t in templates if t["source"] == "built-in" and t["status"] == "Ready"} >= {
        "inactivity_modal_top.png", "shapes_challenge_popup.png", "puzzle_challenge_popup.png"}


def test_bad_frames_do_not_kill_core(core):
    core.send_raw(b"this is not json\n")
    core.send_raw(b'{"v":1,"type":"request","request_id":"x1","method":"playback.play","params":{"media_id":1,"extra":2}}\n')
    bad = core.wait_response("x1")
    assert bad["ok"] is False and bad["error"]["code"] == "INVALID_PARAMS"
    assert core.request("system.ping")["result"] == {"pong": True}
    assert core.request("system.exec", {"cmd": "calc"})["error"]["code"] == "METHOD_NOT_FOUND"
    assert core.request("system.status", {"unexpected": 1})["error"]["code"] == "INVALID_PARAMS"


def test_slow_request_does_not_block_others(core):
    # 10.255.255.1 is unroutable: obs.connect blocks until its timeout.
    core.send_raw(b'{"v":1,"type":"request","request_id":"slow","method":"obs.connect","params":{"host":"10.255.255.1","port":4455}}\n')
    started = time.monotonic()
    assert core.request("system.ping", timeout=3)["ok"]
    assert time.monotonic() - started < 2
    slow = core.wait_response("slow", timeout=20)
    assert slow["ok"] is False and slow["error"]["code"] in ("OBS_TIMEOUT", "OBS_CONNECT_FAILED", "OBS_NOT_RUNNING")


def test_stdout_only_carries_frames(core):
    core.request("media.cloud_cache.status")
    core.request("playback.play", {"media_id": "a" * 32})  # not cached: fails cleanly
    core.request("diagnostics.export")
    time.sleep(0.3)
    for line in core.raw_lines:
        frame = json.loads(line)
        assert frame["type"] in ("event", "response") and frame["v"] == 1


def test_graceful_shutdown_exit_code(core):
    response = core.request("system.shutdown")
    assert response["ok"]
    assert core.proc.wait(timeout=15) == 0
    assert any(e["event"] == "core.stopped" for e in core.events)


def test_stdin_close_stops_core(tmp_path):
    process = CoreProcess(tmp_path / "home")
    process.wait_event("core.ready", 30)
    process.proc.stdin.close()
    assert process.proc.wait(timeout=15) == 0


def test_parent_exit_stops_core(tmp_path):
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
    process = CoreProcess(tmp_path / "home", command=core_command("--parent-pid", str(parent.pid)))
    process.wait_event("core.ready", 30)
    parent.wait()
    assert process.proc.wait(timeout=20) == 0  # no orphan Core after the GUI dies


def test_single_core_per_data_folder(tmp_path):
    first = CoreProcess(tmp_path / "home")
    first.wait_event("core.ready", 30)
    try:
        second = CoreProcess(tmp_path / "home")
        assert second.proc.wait(timeout=25) == 3
        error = second.wait_event("core.error", 5)
        assert error["data"]["code"] == "CORE_ALREADY_RUNNING"
    finally:
        first.close()
