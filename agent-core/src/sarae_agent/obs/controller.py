"""Single persistent obs-websocket connection shared by playback and TTS.

* One lock serialises every request (the websocket client is not thread-safe).
* Any transport failure drops the client; the next call reconnects (rate-limited).
* Success is only reported after OBS answers the request; settings changes are read back.
* PlaybackEngine talks to OBS through ObsRequests (obsws-python ReqClient-style calls) on this
  same connection, and keeps its settings in config.json + the DPAPI secret via ObsSettingsStore.
"""
from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable

from sarae_agent.activation.secrets import obs_secret_file
from sarae_agent.config import ConfigStore
from sarae_agent.errors import AgentError

log = logging.getLogger("sarae.obs")

MEDIA_ACTIONS = {
    "play": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY",
    "pause": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE",
    "stop": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP",
    "restart": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART",
}
ENDED_STATES = {"OBS_MEDIA_STATE_ENDED", "OBS_MEDIA_STATE_STOPPED", "OBS_MEDIA_STATE_NONE"}


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"


class ObsController:
    RECONNECT_MIN_SECONDS = 3.0

    def __init__(self, config: ConfigStore, emit: Callable[[str, dict], None], client_factory=None):
        self._config = config
        self._emit = emit
        self._lock = threading.RLock()
        self._client = None
        self._last_attempt = 0.0
        self._factory = client_factory
        self._status: dict = {"connected": False, "state": "disconnected", "error": None,
                              "obs_version": None, "websocket_version": None}
        self._password_cache: str | None = None
        self.wanted = config.get().obs.auto_connect  # keep reconnecting in the background

    # --- secrets -------------------------------------------------------------------------
    @staticmethod
    def password_set() -> bool:
        return obs_secret_file.exists()

    def _password(self) -> str:
        # Cached: the playback watchdog asks for a client every second; DPAPI decrypts only after a change.
        if self._password_cache is None:
            try:
                data = obs_secret_file.load() or {}
            except AgentError:
                data = {}
            self._password_cache = str(data.get("password") or "")
        return self._password_cache

    def set_password(self, password: str | None) -> None:
        if password:
            obs_secret_file.save({"password": password})
        else:
            obs_secret_file.delete()
        self._password_cache = None

    # --- connection ----------------------------------------------------------------------
    def _set_status(self, **changes: Any) -> None:
        before = (self._status.get("connected"), self._status.get("state"), self._status.get("error"))
        self._status.update(changes)
        after = (self._status.get("connected"), self._status.get("state"), self._status.get("error"))
        if before != after:
            self._emit("obs.status_changed", self.status())

    def _make_client(self):
        cfg = self._config.get().obs
        if self._factory:
            return self._factory(cfg, self._password())
        import obsws_python as obs
        return obs.ReqClient(host=cfg.host, port=cfg.port, password=self._password(), timeout=4)

    def _drop(self, reason: str | None) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._set_status(connected=False, state="disconnected" if reason is None else "error", error=reason)

    def connect(self, *, force: bool = True) -> dict:
        with self._lock:
            if self._client is not None and not force:
                return self.status()
            self._drop(None)
            self.wanted = True
            self._last_attempt = time.monotonic()
            self._set_status(state="connecting", error=None)
            try:
                self._client = self._make_client()
                version = self._client.send("GetVersion", raw=True) or {}
            except Exception as exc:  # noqa: BLE001
                self._client = None
                code, message = self._classify_connect(exc)
                self._set_status(connected=False, state="error", error={"code": code, "message": message})
                raise AgentError(code, message) from exc
            self._set_status(connected=True, state="connected", error=None,
                             obs_version=version.get("obsVersion"),
                             websocket_version=version.get("obsWebSocketVersion"))
            log.info("Đã kết nối OBS %s (websocket %s)", version.get("obsVersion"),
                     version.get("obsWebSocketVersion"))
            return self.status()

    def disconnect(self) -> dict:
        with self._lock:
            self.wanted = False
            self._drop(None)
            return self.status()

    @staticmethod
    def _classify_connect(exc: BaseException) -> tuple[str, str]:
        text = str(exc).lower()
        if isinstance(exc, ConnectionRefusedError) or "refused" in text or "10061" in text:
            return "OBS_NOT_RUNNING", "OBS chưa chạy hoặc chưa bật WebSocket Server (Tools → WebSocket Server Settings)"
        if "authentication" in text or "auth" in text or "4009" in text:
            return "OBS_AUTH_FAILED", "Sai mật khẩu OBS WebSocket"
        if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text:
            return "OBS_TIMEOUT", "OBS không phản hồi (kiểm tra host/port)"
        return "OBS_CONNECT_FAILED", f"Không kết nối được OBS: {_short(exc)}"

    def _call(self, request: str, data: dict | None = None) -> dict:
        with self._lock:
            if self._client is None:
                if time.monotonic() - self._last_attempt < self.RECONNECT_MIN_SECONDS and self._status.get("error"):
                    raise AgentError("OBS_DISCONNECTED", "Chưa kết nối OBS", {"last_error": self._status.get("error")})
                self.connect()
            try:
                return self._client.send(request, data, raw=True) or {}
            except Exception as exc:  # noqa: BLE001
                code = getattr(exc, "code", None)
                if isinstance(code, int):  # OBSSDKRequestError: OBS answered with a failure status
                    if code == 600:
                        name = (data or {}).get("inputName", "")
                        raise AgentError("OBS_INPUT_MISSING",
                                         f"OBS không có nguồn '{name}'. Hãy tạo Media Source đúng tên này",
                                         {"input": name}) from exc
                    raise AgentError("OBS_REQUEST_FAILED", f"OBS từ chối {request} (mã {code})",
                                     {"obs_code": code}) from exc
                self._drop({"code": "OBS_DISCONNECTED", "message": _short(exc)})
                log.warning("Mất kết nối OBS: %s", _short(exc), extra={"code": "OBS_DISCONNECTED"})
                raise AgentError("OBS_DISCONNECTED", "Mất kết nối OBS", {"reason": _short(exc)},
                                 retryable=True) from exc

    def request(self, request: str, data: dict | None = None) -> dict:
        """One obs-websocket request on the shared connection (overlay source management)."""
        return self._call(request, data)

    def ensure_connected(self) -> bool:
        """Background reconnect used by the watchdog; never raises."""
        with self._lock:
            if self._client is not None:
                try:
                    self._client.send("GetVersion", raw=True)
                    return True
                except Exception as exc:  # noqa: BLE001
                    self._drop({"code": "OBS_DISCONNECTED", "message": _short(exc)})
            if not self.wanted or time.monotonic() - self._last_attempt < 10:
                return False
            try:
                self.connect()
                return True
            except AgentError:
                return False

    # --- queries -------------------------------------------------------------------------
    def status(self) -> dict:
        cfg = self._config.get().obs
        return {**self._status, "host": cfg.host, "port": cfg.port, "media_input": cfg.media_input,
                "voice_input": cfg.voice_input, "password_set": self.password_set(), "auto_connect": self.wanted}

    def inspect(self) -> dict:
        """Connection + presence of the configured media/voice inputs (for obs.status)."""
        cfg = self._config.get().obs
        result = self.status()
        if not self._status.get("connected"):
            return result
        for key, name in (("media_input_ok", cfg.media_input), ("voice_input_ok", cfg.voice_input)):
            try:
                self._call("GetInputSettings", {"inputName": name})
                result[key] = True
            except AgentError as exc:
                result[key] = False if exc.code == "OBS_INPUT_MISSING" else None
        try:
            result["media"] = self.media_status(cfg.media_input)
        except AgentError:
            result["media"] = None
        result.update({k: v for k, v in self.status().items() if k in ("connected", "state", "error")})
        return result

    def media_status(self, input_name: str) -> dict:
        data = self._call("GetMediaInputStatus", {"inputName": input_name})
        return {"state": data.get("mediaState"), "duration_ms": data.get("mediaDuration"),
                "cursor_ms": data.get("mediaCursor")}

    # --- commands ------------------------------------------------------------------------
    def play_file(self, input_name: str, path: str, *, unmute: bool = False) -> dict:
        self._call("GetInputSettings", {"inputName": input_name})  # fail fast when missing
        self._call("SetInputSettings", {"inputName": input_name, "overlay": True, "inputSettings": {
            "local_file": path, "is_local_file": True, "looping": False,
            "close_when_inactive": False, "restart_on_activate": False}})
        applied = self._call("GetInputSettings", {"inputName": input_name}).get("inputSettings", {})
        if str(applied.get("local_file", "")).replace("\\", "/").casefold() != path.replace("\\", "/").casefold():
            raise AgentError("OBS_NOT_APPLIED", "OBS không nhận file mới cho nguồn phát", {"input": input_name})
        if unmute:
            self._call("SetInputMute", {"inputName": input_name, "inputMuted": False})
        self._call("TriggerMediaInputAction", {"inputName": input_name, "mediaAction": MEDIA_ACTIONS["restart"]})
        return self.media_status(input_name)

    def input_volume_db(self, input_name: str) -> float:
        return float(self._call("GetInputVolume", {"inputName": input_name}).get("inputVolumeDb", 0.0))

    def set_input_volume_db(self, input_name: str, db: float) -> None:
        # OBS accepts -100..26 dB; -100 is silence.
        self._call("SetInputVolume", {"inputName": input_name, "inputVolumeDb": max(-100.0, min(26.0, db))})

    def media_action(self, input_name: str, action: str) -> dict:
        if action not in MEDIA_ACTIONS:
            raise AgentError("INVALID_REQUEST", f"Hành động media không hợp lệ: {action}")
        self._call("TriggerMediaInputAction", {"inputName": input_name, "mediaAction": MEDIA_ACTIONS[action]})
        return self.media_status(input_name)

    def requests_for(self, config) -> "ObsRequests":
        """Client for PlaybackEngine. The saved connection reuses the shared websocket; another
        host/port/password (obs_inputs with values not saved yet) gets a temporary one."""
        cfg = self._config.get().obs
        if (config.host, config.port) == (cfg.host, cfg.port) and (config.password or "") == self._password():
            return ObsRequests(self._call)
        if self._factory:
            client = self._factory(config, config.password or "")
        else:
            import obsws_python as obs
            client = obs.ReqClient(host=config.host, port=config.port, password=config.password or "", timeout=4)
        return ObsRequests(lambda request, data=None: client.send(request, data, raw=True) or {}, client.disconnect)

    def reconnect_later(self) -> None:
        """Connection settings changed: drop the socket; the next request connects with the new ones."""
        with self._lock:
            if self._client is not None:
                self._drop(None)

    def close(self) -> None:
        with self._lock:
            self.wanted = False
            self._drop(None)


def _ns(**values: Any) -> SimpleNamespace:
    return SimpleNamespace(**values)


class ObsRequests:
    """The obsws-python ReqClient calls PlaybackEngine makes, over a raw ``send(request, data) -> dict``."""

    def __init__(self, send: Callable[..., dict], close: Callable[[], None] | None = None):
        self._send = send
        self._close = close

    def close(self) -> None:
        if self._close is not None:
            self._close()

    def get_version(self):
        data = self._send("GetVersion")
        return _ns(obs_version=data.get("obsVersion"), obs_web_socket_version=data.get("obsWebSocketVersion"))

    def get_current_program_scene(self):
        data = self._send("GetCurrentProgramScene")
        return _ns(current_program_scene_name=data.get("currentProgramSceneName") or data.get("sceneName"))

    def get_scene_item_id(self, scene: str, name: str):
        data = self._send("GetSceneItemId", {"sceneName": scene, "sourceName": name})
        return _ns(scene_item_id=data["sceneItemId"])

    def get_scene_item_list(self, scene: str):
        return _ns(scene_items=self._send("GetSceneItemList", {"sceneName": scene}).get("sceneItems") or [])

    def get_video_settings(self):
        data = self._send("GetVideoSettings")
        return _ns(base_width=data.get("baseWidth"), base_height=data.get("baseHeight"))

    def set_scene_item_transform(self, scene: str, item_id: int, transform: dict):
        self._send("SetSceneItemTransform", {"sceneName": scene, "sceneItemId": item_id,
                                              "sceneItemTransform": transform})

    def get_input_list(self):
        return _ns(inputs=self._send("GetInputList").get("inputs") or [])

    def get_input_settings(self, name: str):
        return _ns(input_settings=self._send("GetInputSettings", {"inputName": name}).get("inputSettings") or {})

    def set_input_settings(self, name: str, settings: dict, overlay: bool = True):
        self._send("SetInputSettings", {"inputName": name, "inputSettings": settings, "overlay": overlay})

    def set_input_mute(self, name: str, muted: bool):
        self._send("SetInputMute", {"inputName": name, "inputMuted": muted})

    def get_input_mute(self, name: str):
        return _ns(input_muted=self._send("GetInputMute", {"inputName": name}).get("inputMuted"))

    def set_scene_item_enabled(self, scene: str, item_id: int, enabled: bool):
        self._send("SetSceneItemEnabled", {"sceneName": scene, "sceneItemId": item_id, "sceneItemEnabled": enabled})

    def trigger_media_input_action(self, name: str, action: str):
        self._send("TriggerMediaInputAction", {"inputName": name, "mediaAction": action})

    def get_media_input_status(self, name: str):
        data = self._send("GetMediaInputStatus", {"inputName": name})
        return _ns(media_state=data.get("mediaState"), media_cursor=data.get("mediaCursor"),
                   media_duration=data.get("mediaDuration"))

    def set_media_input_cursor(self, name: str, ms: int):
        self._send("SetMediaInputCursor", {"inputName": name, "mediaCursor": ms})


class ObsSettingsStore:
    """PlaybackEngine settings kept in config.json (obs section) with the password in the DPAPI secret."""

    def __init__(self, config: ConfigStore, obs: ObsController):
        self._config = config
        self._obs = obs

    def load(self):
        from sarae_agent.playback.engine import ObsConfig as EngineObsConfig
        cfg = self._config.get().obs
        return EngineObsConfig(host=cfg.host, port=cfg.port, password=self._obs._password(),
                               input_name=cfg.media_input, dual_mode=cfg.dual_mode,
                               source_a=cfg.source_a, source_b=cfg.source_b, camera_input=cfg.camera_input)

    def save(self, engine_config) -> None:
        before = self._config.get().obs
        self._config.update({"obs": {"host": engine_config.host, "port": engine_config.port,
                                     "media_input": engine_config.input_name, "dual_mode": engine_config.dual_mode,
                                     "source_a": engine_config.source_a, "source_b": engine_config.source_b,
                                     "camera_input": engine_config.camera_input}})
        password_changed = (engine_config.password or "") != self._obs._password()
        if password_changed:
            self._obs.set_password(engine_config.password or None)
        if password_changed or (before.host, before.port) != (engine_config.host, engine_config.port):
            self._obs.reconnect_later()
