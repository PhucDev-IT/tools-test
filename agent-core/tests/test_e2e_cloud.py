"""End-to-end: real Cloud API (uvicorn, Cloud-only venv) <-> real Agent Core process.

Requires a Python with backend/requirements.txt installed:
    set SARAE_CLOUD_PYTHON=C:\\path\\to\\cloud-venv\\Scripts\\python.exe
Skipped when unavailable. OBS is intentionally NOT running here: OBS commands must fail honestly.
"""
import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from conftest import windows_only
from ipc_harness import CoreProcess

BACKEND = Path(__file__).resolve().parents[2] / "backend"
CLOUD_PYTHON = os.environ.get("SARAE_CLOUD_PYTHON") or str(BACKEND / ".venv" / "Scripts" / "python.exe")
PASSWORD = "OwnerSecretPassword123!"

pytestmark = [windows_only, pytest.mark.skipif(not Path(CLOUD_PYTHON).is_file(), reason="no Cloud venv")]


def make_clip(path, seconds=1, fps=10):
    import cv2
    import numpy as np
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (320, 240))
    for _ in range(seconds * fps):
        writer.write(np.zeros((240, 320, 3), dtype=np.uint8))
    writer.release()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Cloud:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/api"
        self.env = {**os.environ, "SARAE_DATA_DIR": str(data_dir), "SARAE_SAAS_MODE": "true",
                    "PYTHONIOENCODING": "utf-8"}
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen([CLOUD_PYTHON, "-m", "uvicorn", "app.main:app", "--port", str(self.port),
                                      "--log-level", "warning"], cwd=BACKEND, env=self.env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx.get(self.url + "/health", timeout=1).status_code == 200:
                    return
            except httpx.TransportError:
                time.sleep(0.2)
        raise RuntimeError("Cloud did not start")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)

    def bootstrap_admin(self):
        subprocess.run([CLOUD_PYTHON, "-c", "from app.identity import bootstrap_admin;"
                        "bootstrap_admin('admin@example.test','AdminSecretPassword123!')"],
                       cwd=BACKEND, env=self.env, check=True)

    def login(self, email, password):
        token = httpx.post(self.url + "/identity/login", json={"email": email, "password": password}).json()
        return {"Authorization": "Bearer " + token["access_token"]}


@pytest.fixture()
def cloud(tmp_path):
    server = Cloud(tmp_path / "cloud-data")
    server.data_dir.mkdir()
    server.bootstrap_admin()
    server.start()
    yield server
    server.stop()


def wait_for(predicate, timeout=20.0, interval=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise TimeoutError("condition not met")


def test_agent_full_flow_against_real_cloud(cloud, tmp_path):
    admin = cloud.login("admin@example.test", "AdminSecretPassword123!")
    owner = httpx.post(cloud.url + "/identity/owners", headers=admin,
                       json={"email": "a@example.test", "password": PASSWORD, "max_agents": 1}).json()
    owner_auth = cloud.login("a@example.test", PASSWORD)
    key = httpx.post(f"{cloud.url}/identity/owners/{owner['id']}/activation-keys", headers=admin).json()["activation_key"]
    home = tmp_path / "agent-home"
    media_id = "a" * 32  # Media Library id; the Agent plays it from its cloud media cache

    core = CoreProcess(home)
    try:
        core.wait_event("core.ready", 30)
        assert core.request("config.set", {"cloud": {"api_url": cloud.url}})["ok"]
        wrong = core.request("activation.activate", {"activation_key": "SARAE-ACT-" + "z" * 30}, timeout=30)
        assert wrong["error"]["code"] == "ACTIVATION_KEY_INVALID"
        activated = core.request("activation.activate", {"activation_key": key}, timeout=30)
        assert activated["ok"], activated
        agent_id = activated["result"]["agent_id"]
    finally:
        assert core.close() == 0

    # The one-time key cannot activate a second PC.
    other = CoreProcess(tmp_path / "other-home")
    try:
        other.wait_event("core.ready", 30)
        reused = other.request("activation.activate", {"activation_key": key, "cloud_url": cloud.url}, timeout=30)
        assert reused["error"]["code"] == "ACTIVATION_KEY_USED"  # X-Sarae-Error tells it apart from a wrong key
    finally:
        other.close()

    core = CoreProcess(home)  # restart: same identity from DPAPI store
    try:
        core.wait_event("core.ready", 30)
        assert core.request("system.status")["result"]["agent"]["agent_id"] == agent_id
        # Stand-in for a file CloudMediaCache.prepare() published (local video folders are retired).
        cached = home / "cloud-media-cache" / agent_id
        cached.mkdir(parents=True, exist_ok=True)
        make_clip(cached / f"{media_id}.mp4")  # a real clip: ffprobe passes, so the failure below is OBS

        assert core.request("agent.start")["ok"]
        core.wait_event("agent.status_changed", 30, lambda d: d["state"] == "running")
        status = httpx.get(cloud.url + "/agents/status", headers=owner_auth).json()["agents"][0]
        assert status["online"] and status["playback"]["running"] is False
        assert status["playback"]["queue_ids"] == [] and "obs" in status["playback"]

        # OWNER command API + lifecycle of backend/app/cloud_agents.py.
        def queue(kind, payload=None, key=None):
            res = httpx.post(f"{cloud.url}/agents/{agent_id}/commands", headers=owner_auth,
                             json={"kind": kind, "payload": payload or {}, "idempotency_key": key})
            assert res.status_code in (200, 202), res.text
            return res.json()["command_id"]

        def cloud_state(command_id):
            return httpx.get(f"{cloud.url}/agents/{agent_id}/commands/{command_id}", headers=owner_auth).json()

        ok_id = queue("playback_cancel_pending", key="cancel-1")
        assert queue("playback_cancel_pending", key="cancel-1") == ok_id  # Idempotency-Key: same command
        done = core.wait_event("command.finished", 30, lambda d: d["command_id"] == ok_id)
        assert done["data"]["status"] == "succeeded"
        job = wait_for(lambda: (j := cloud_state(ok_id))["status"] == "SUCCESS" and j)
        assert job["result"] == {"ok": True, "cancelled_product_id": None}

        fail_id = queue("product_play", {"session_id": 1, "product_id": 1, "media_ids": [media_id]})
        failed = core.wait_event("command.finished", 30, lambda d: d["command_id"] == fail_id)
        assert failed["data"]["status"] == "failed"
        assert failed["data"]["error"]["code"] == "OBS_UNAVAILABLE"  # OBS not running: no fake success
        job = wait_for(lambda: (j := cloud_state(fail_id))["status"] == "FAILED" and j)
        assert "OBS" in job["error"]

        # Cloud outage -> disconnected with backoff -> automatic recovery.
        mark = core.mark()
        cloud.stop()
        core.wait_event("cloud.connection_changed", 40, lambda d: d["state"] == "disconnected", since=mark)
        assert core.request("system.status")["result"]["agent"]["state"] == "disconnected"
        mark = core.mark()
        cloud.start()
        core.wait_event("cloud.connection_changed", 90, lambda d: d["state"] == "connected", since=mark)

        # License expiry is fatal: the worker stops and reports LICENSE_EXPIRED.
        mark = core.mark()
        httpx.put(f"{cloud.url}/identity/owners/{owner['id']}/license", headers=admin,
                  json={"max_agents": 1, "expires_at": "2020-01-01T00:00:00+00:00", "enabled": True})
        expired = core.wait_event("agent.status_changed", 40, lambda d: d["state"] == "error", since=mark)
        assert expired["data"]["error"]["code"] == "LICENSE_EXPIRED"
        httpx.put(f"{cloud.url}/identity/owners/{owner['id']}/license", headers=admin,
                  json={"max_agents": 1, "expires_at": None, "enabled": True})
        mark = core.mark()
        assert core.request("agent.restart")["ok"]
        core.wait_event("agent.status_changed", 30, lambda d: d["state"] == "running", since=mark)

        # Revocation is fatal as well.
        mark = core.mark()
        assert httpx.delete(f"{cloud.url}/identity/agents/{agent_id}", headers=owner_auth).status_code == 200
        revoked = core.wait_event("agent.status_changed", 40, lambda d: d["state"] == "error", since=mark)
        assert revoked["data"]["error"]["code"] == "AGENT_REVOKED"  # same 403 as an expired license, typed header
        assert core.request("agent.stop")["result"]["state"] == "stopped"
    finally:
        assert core.close() == 0
