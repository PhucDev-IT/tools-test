"""Hard-kill of the process the GUI spawned must not leave a Core behind.

With the frozen one-file exe (SARAE_CORE_EXE) the spawned PID is the PyInstaller bootloader
and the real Core is its child; from source the spawned PID is the Core itself.
"""
import os
import subprocess
import time

import psutil

from conftest import windows_only
from ipc_harness import core_command


@windows_only
def test_hard_kill_of_spawned_process_leaves_no_core(tmp_path):
    env = {**os.environ, "SARAE_AGENT_HOME": str(tmp_path / "home"),
           "PYTHONPATH": str(os.path.join(os.path.dirname(__file__), "..", "src"))}
    proc = subprocess.Popen(core_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env)
    try:
        assert b"core.ready" in proc.stdout.readline()
        family = [psutil.Process(proc.pid), *psutil.Process(proc.pid).children(recursive=True)]
        proc.kill()  # TerminateProcess on the spawned PID only, like CommandChild::kill()
        proc.wait(10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(p.is_running() for p in family):
            time.sleep(0.2)
        assert not [p.pid for p in family if p.is_running()]
    finally:
        proc.stdin.close()
