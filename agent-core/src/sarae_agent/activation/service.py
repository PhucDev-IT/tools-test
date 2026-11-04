"""Device activation: one-time key -> permanent agent_id + per-device credential (DPAPI)."""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from sarae_agent.activation.secrets import credential_file, import_legacy_credential
from sarae_agent.cloud.client import CloudClient
from sarae_agent.errors import AgentError
from sarae_agent.windows.win32 import hostname

log = logging.getLogger("sarae.activation")

KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{20,200}$")


@dataclass(frozen=True)
class Identity:
    agent_id: str
    credential: str
    cloud_url: str
    activated_at: str | None


class ActivationService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached: Identity | None = None
        self._load_error: AgentError | None = None
        self._load()

    def _load(self) -> None:
        try:
            data = credential_file.load()
            if data is None:
                legacy = import_legacy_credential()
                data = credential_file.load() if legacy else None
        except AgentError as exc:
            self._load_error = exc
            log.error("Không đọc được credential: %s", exc.message, extra={"code": exc.code})
            return
        if data and data.get("agent_id") and data.get("agent_credential"):
            self._cached = Identity(data["agent_id"], data["agent_credential"],
                                    data.get("cloud_url") or "", data.get("activated_at"))

    @property
    def identity(self) -> Identity | None:
        return self._cached

    def require(self) -> Identity:
        if self._cached is None:
            if self._load_error:
                raise self._load_error
            raise AgentError("AGENT_NOT_ACTIVATED", "Agent chưa được kích hoạt trên máy này")
        return self._cached

    def status(self) -> dict:
        ident = self._cached
        return {
            "activated": ident is not None,
            "agent_id": ident.agent_id if ident else None,
            "activated_cloud_url": ident.cloud_url if ident else None,
            "activated_at": ident.activated_at if ident else None,
            "hostname": hostname(),
            "credential_error": self._load_error.to_dict() if self._load_error else None,
        }

    def activate(self, cloud_url: str, activation_key: str, *, timeout: float = 20.0,
                 client: CloudClient | None = None) -> dict:
        key = activation_key.strip()
        if not KEY_PATTERN.fullmatch(key):
            raise AgentError("ACTIVATION_KEY_INVALID", "Mã kích hoạt không đúng định dạng")
        if not self._lock.acquire(blocking=False):
            raise AgentError("ACTIVATION_IN_PROGRESS", "Đang kích hoạt, vui lòng chờ")
        try:
            if self._cached is not None:
                raise AgentError("ALREADY_ACTIVATED",
                                 "Máy đã kích hoạt. Gỡ kích hoạt cục bộ trước khi dùng mã mới",
                                 {"agent_id": self._cached.agent_id})
            own_client = client is None
            client = client or CloudClient(cloud_url, timeout=timeout)
            try:
                result = client.activate(key, hostname())
            finally:
                if own_client:
                    client.close()
            activated_at = datetime.now(timezone.utc).isoformat()
            credential_file.save({"agent_id": result["agent_id"],
                                  "agent_credential": result["agent_credential"],
                                  "cloud_url": cloud_url, "activated_at": activated_at})
            self._cached = Identity(result["agent_id"], result["agent_credential"], cloud_url, activated_at)
            self._load_error = None
            log.info("Kích hoạt thành công agent_id=%s", result["agent_id"])
            return self.status()
        finally:
            self._lock.release()

    def deactivate_local(self) -> dict:
        """Forget the credential on this PC only. The Cloud record stays until OWNER/ADMIN revokes it."""
        credential_file.delete()
        previous = self._cached.agent_id if self._cached else None
        self._cached = None
        self._load_error = None
        log.warning("Đã xoá credential cục bộ của agent_id=%s", previous)
        return self.status()
