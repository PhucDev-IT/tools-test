"""DPAPI-protected secret files, kept apart from config.json.

File format: {"format": "sarae-dpapi", "version": 2, "blob": base64(CryptProtectData(json))}
"""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from sarae_agent.activation import dpapi
from sarae_agent.config import paths, read_json
from sarae_agent.errors import AgentError

log = logging.getLogger("sarae.secrets")


class SecretFile:
    def __init__(self, name: str):
        self.name = name

    @property
    def path(self) -> Path:
        return paths.secrets_dir / f"{self.name}.dpapi.json"

    def exists(self) -> bool:
        return self.path.is_file()

    def save(self, value: dict) -> None:
        blob = dpapi.protect(json.dumps(value).encode("utf-8"))
        body = json.dumps({"format": "sarae-dpapi", "version": 2,
                           "blob": base64.b64encode(blob).decode("ascii")})
        temp = self.path.with_suffix(".tmp")
        temp.write_text(body, encoding="utf-8")
        os.replace(temp, self.path)

    def load(self) -> dict | None:
        raw = read_json(self.path, None)
        if raw is None:
            return None
        if not isinstance(raw, dict) or raw.get("format") != "sarae-dpapi":
            raise AgentError("CREDENTIAL_CORRUPT", f"Tệp bí mật {self.name} không đúng định dạng")
        data = dpapi.unprotect(base64.b64decode(raw["blob"]))
        value = json.loads(data.decode("utf-8"))
        if not isinstance(value, dict):
            raise AgentError("CREDENTIAL_CORRUPT", f"Tệp bí mật {self.name} hỏng")
        return value

    def delete(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


credential_file = SecretFile("agent_credential")
obs_secret_file = SecretFile("obs")


def import_legacy_credential() -> dict | None:
    """The retired backend agent stored {"version":2,"dpapi":...} without entropy in data/."""
    legacy = paths.legacy_data / "agent_activation.json"
    raw = read_json(legacy, None)
    if not isinstance(raw, dict) or "dpapi" not in raw:
        return None
    try:
        plain = dpapi.unprotect(base64.b64decode(raw["dpapi"]), entropy=None)
        value = json.loads(plain.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("Không nhập được credential cũ: %s", exc)
        return None
    if not (isinstance(value, dict) and value.get("agent_id") and value.get("agent_credential")):
        return None
    credential_file.save({"agent_id": value["agent_id"], "agent_credential": value["agent_credential"],
                          "cloud_url": "", "activated_at": None, "imported_from": "legacy"})
    log.info("Đã chuyển credential Agent cũ sang kho DPAPI mới, giữ nguyên agent_id")
    return value
