"""Local paths and versioned configuration for the Agent Core.

All state lives under %LOCALAPPDATA%\\SaraeAgent (override with SARAE_AGENT_HOME for tests).
Nothing depends on the working directory, the EXE folder or Program Files.
Secrets (Cloud credential, OBS password) are NOT stored here; see activation/secrets.py.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger("sarae.config")

CONFIG_VERSION = 2


def data_root() -> Path:
    override = os.environ.get("SARAE_AGENT_HOME")
    if override:
        root = Path(override)
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        root = Path(base) / "SaraeAgent"
    root.mkdir(parents=True, exist_ok=True)
    return root


class Paths:
    """Resolved on demand so tests can redirect SARAE_AGENT_HOME before use."""

    @property
    def root(self) -> Path:
        return data_root()

    def _dir(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def config_file(self) -> Path:
        return self.root / "config.json"

    @property
    def secrets_dir(self) -> Path:
        return self._dir("secrets")

    @property
    def playback_state(self) -> Path:
        return self.root / "playback_state.json"

    @property
    def command_journal(self) -> Path:
        return self.root / "command_journal.json"

    @property
    def command_lock(self) -> Path:
        """Same file the retired backend/agent locked, so it and this Core never both take Cloud commands."""
        path = self.legacy_data
        path.mkdir(parents=True, exist_ok=True)
        return path / "agent_commands.lock"

    @property
    def logs(self) -> Path:
        return self._dir("logs")

    @property
    def diagnostics(self) -> Path:
        return self._dir("diagnostics")

    @property
    def monitor_templates(self) -> Path:
        path = self.root / "monitor" / "templates"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def monitor_captures(self) -> Path:
        path = self.root / "monitor" / "captures"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def voice(self) -> Path:
        return self._dir("voice")

    @property
    def overlay_cache(self) -> Path:
        return self._dir("overlay-cache")

    @property
    def legacy_data(self) -> Path:
        """Folder used by the retired backend/agent runtime (PyInstaller build of backend)."""
        return self.root / "data"


paths = Paths()


class CloudConfig(BaseModel):
    api_url: str = ""
    # Idle heartbeat; while a video plays the worker reports every 2 s (Cloud marks an Agent offline after 35 s).
    heartbeat_seconds: float = Field(default=5.0, ge=3, le=30)
    # Long-poll window of GET /agents/commands/{agent_id}/next (Cloud maximum 25 s).
    poll_seconds: float = Field(default=25.0, ge=1, le=25)
    request_timeout_seconds: float = Field(default=12.0, ge=3, le=60)


class ObsConfig(BaseModel):
    host: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    port: int = Field(default=4455, ge=1, le=65535)
    media_input: str = Field(default="Sarae Video", min_length=1, max_length=200)
    voice_input: str = Field(default="Sarae Voice", min_length=1, max_length=100)
    auto_connect: bool = True
    # Dual A/B Media Sources (Cloud `obs_configure`); empty names fall back to "<media_input> A/B".
    dual_mode: bool = False
    source_a: str = Field(default="", max_length=200)
    source_b: str = Field(default="", max_length=200)
    camera_input: str = Field(default="", max_length=200)


class MonitorConfig(BaseModel):
    """Same field names/limits as the Cloud `monitor_config` command payload."""
    interval_seconds: float = Field(default=240.0, ge=180, le=240)
    threshold: float = Field(default=0.82, ge=0.65, le=0.99)
    autostart: bool = False


class AgentOptions(BaseModel):
    autostart: bool = False


class TimelineConfig(BaseModel):
    """Single Video Timeline pins (sarae_agent/timeline). Dry-run by default: the schedule runs and is reported, but
    nothing is clicked on TikTok until the operator turns it off on this PC."""
    dry_run: bool = True
    # Dry-run only: treat the broadcast as LIVE without reading the Console (rehearsal without going LIVE).
    assume_live_in_dry_run: bool = True


class OverlayConfig(BaseModel):
    """LIVE overlays (sarae_agent/overlay): the Agent serves the renderer on this PC and keeps one OBS Browser Source."""
    enabled: bool = True
    # Local renderer server (127.0.0.1 only); the OBS Browser Source loads http://127.0.0.1:<port>/overlay/.
    port: int = Field(default=17891, ge=1024, le=65535)
    source_name: str = Field(default="Sarae Overlay", min_length=1, max_length=200)
    # The Browser Source this Agent created, per OBS scene collection (obs-websocket inputUuid): only those inputs are
    # ever changed; a source of the same name made by the operator is left alone.
    input_uuids: dict[str, str] = Field(default_factory=dict)
    # Secret in the Browser Source URL, so other local pages cannot read or drive the renderer.
    token: str = Field(default="", max_length=64)


class AgentConfig(BaseModel):
    # Unknown keys are ignored, so config.json files written by older Agents (with the retired
    # "media": {"folders", "rescan_minutes"} section) still load; the next save drops them.
    version: int = CONFIG_VERSION
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    obs: ObsConfig = Field(default_factory=ObsConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    agent: AgentOptions = Field(default_factory=AgentOptions)
    timeline: TimelineConfig = Field(default_factory=TimelineConfig)
    overlay: OverlayConfig = Field(default_factory=OverlayConfig)


class ConfigError(ValueError):
    code = "INVALID_CONFIG"


def normalize_cloud_url(raw: str) -> str:
    """Accept https://host[/api] (or http on localhost) and return the canonical .../api base."""
    value = (raw or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError("Cloud URL không được chứa tài khoản, query hoặc fragment")
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme == "https" and parsed.hostname:
        pass
    elif parsed.scheme == "http" and local:
        pass
    else:
        raise ConfigError("Cloud URL phải dùng HTTPS (HTTP chỉ cho localhost)")
    path = parsed.path.rstrip("/")
    if path in ("", "/"):
        path = "/api"
    if not path.endswith("/api"):
        raise ConfigError("Cloud URL phải trỏ tới gốc API, ví dụ https://sarae.vn/api")
    host = parsed.netloc
    return f"{parsed.scheme}://{host}{path}"


def _atomic_write(path: Path, data: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(data, encoding="utf-8")
    os.replace(temp, path)


def write_json(path: Path, value: Any) -> None:
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2))


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log.warning("Không đọc được %s: %s", path.name, exc)
        return default


def _migrate(raw: dict) -> dict:
    """Upgrade older config shapes in place. Version 1 = flat legacy GUI settings."""
    version = int(raw.get("version") or 1)
    if version < 2:
        upgraded: dict = {"version": 2, "cloud": {}}
        if isinstance(raw.get("cloud_url"), str):
            upgraded["cloud"]["api_url"] = raw["cloud_url"]
        raw = upgraded
    raw["version"] = CONFIG_VERSION
    return raw


def _import_legacy() -> dict | None:
    """First run after upgrading from the backend-embedded agent: keep the Cloud URL."""
    gui = read_json(paths.legacy_data / "agent_gui_settings.json", None)
    if not isinstance(gui, dict):
        return None
    raw: dict = {"version": 1, "cloud_url": gui.get("cloud_url", "")}
    log.info("Đã nhập cấu hình từ Agent phiên bản cũ")
    return raw


class ConfigStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._config = self._load()

    def _load(self) -> AgentConfig:
        raw = read_json(paths.config_file, None)
        migrated = False
        if raw is None:
            raw = _import_legacy() or {}
            migrated = bool(raw)
        if not isinstance(raw, dict):
            raw = {}
        if int(raw.get("version") or 1) != CONFIG_VERSION:
            raw = _migrate(raw)
            migrated = True
        # Older installations scanned every few seconds. Preserve other settings while
        # moving that obsolete interval to the new safe default.
        monitor = raw.get("monitor")
        if isinstance(monitor, dict):
            for obsolete in ("telegram_enabled", "telegram_chat_id"):
                if obsolete in monitor:
                    monitor.pop(obsolete)
                    migrated = True
            interval = monitor.get("interval_seconds")
            if isinstance(interval, (int, float)) and not isinstance(interval, bool) and interval < 180:
                monitor["interval_seconds"] = 240.0
                migrated = True
        try:
            config = AgentConfig.model_validate(raw)
        except ValidationError as exc:
            log.error("Cấu hình hỏng, dùng mặc định: %s", exc.errors()[:3])
            backup = paths.config_file.with_suffix(".invalid.json")
            if paths.config_file.exists():
                os.replace(paths.config_file, backup)
            config = AgentConfig()
            migrated = True
        if config.cloud.api_url:
            try:
                config.cloud.api_url = normalize_cloud_url(config.cloud.api_url)
            except ConfigError:
                config.cloud.api_url = ""
        if migrated or not paths.config_file.exists():
            write_json(paths.config_file, config.model_dump())
        return config

    def get(self) -> AgentConfig:
        with self._lock:
            return self._config.model_copy(deep=True)

    def update(self, patch: dict) -> AgentConfig:
        """Deep-merge a partial dict, validate the full result and persist atomically."""
        with self._lock:
            current = self._config.model_dump()
            for section, values in patch.items():
                if section == "version":
                    continue
                if section not in current or not isinstance(values, dict):
                    raise ConfigError(f"Mục cấu hình không hợp lệ: {section}")
                current[section].update(values)
            if "cloud" in patch and "api_url" in patch["cloud"]:
                current["cloud"]["api_url"] = normalize_cloud_url(current["cloud"]["api_url"])
            try:
                updated = AgentConfig.model_validate(current)
            except ValidationError as exc:
                first = exc.errors()[0]
                field = ".".join(str(p) for p in first.get("loc", ()))
                raise ConfigError(f"{field}: {first.get('msg')}") from exc
            write_json(paths.config_file, updated.model_dump())
            self._config = updated
            return updated.model_copy(deep=True)
