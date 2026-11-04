"""Support bundle: logs + redacted config + status snapshot. Never includes credentials."""
from __future__ import annotations

import json
import platform
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sarae_agent import IPC_PROTOCOL_VERSION, __version__
from sarae_agent.config import paths
from sarae_agent.logging_setup import redact
from sarae_agent.windows.win32 import frozen, os_description


def export_bundle(status: dict, config: dict, recent_logs: list[dict]) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = paths.diagnostics / f"sarae-diagnostics-{stamp}.zip"
    environment = {
        "agent_version": __version__, "ipc_protocol": IPC_PROTOCOL_VERSION, "python": sys.version,
        "frozen": frozen(), "executable": sys.executable, "os": os_description(),
        "machine": platform.machine(), "data_root": str(paths.root),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("environment.json", json.dumps(environment, ensure_ascii=False, indent=2))
        bundle.writestr("status.json", redact(json.dumps(status, ensure_ascii=False, indent=2, default=str)))
        bundle.writestr("config.json", redact(json.dumps(config, ensure_ascii=False, indent=2)))
        bundle.writestr("recent_events.json", json.dumps(recent_logs, ensure_ascii=False, indent=2))
        for log_file in sorted(paths.logs.glob("agent.log*")):
            data = log_file.read_text(encoding="utf-8", errors="replace")[-3_000_000:]
            bundle.writestr(f"logs/{log_file.name}", redact(data))
        templates = [p.name for p in paths.monitor_templates.glob("*.png")]
        bundle.writestr("monitor_templates.json", json.dumps(templates))
    return {"path": str(target), "folder": str(target.parent), "bytes": Path(target).stat().st_size}
