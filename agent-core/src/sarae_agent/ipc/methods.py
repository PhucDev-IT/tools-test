"""IPC method table: name -> (params model, handler). The GUI can call nothing else.

There is deliberately no method that runs a shell command or opens an arbitrary path for
writing; file paths accepted here are validated by the owning service.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from sarae_agent.errors import AgentError
from sarae_agent.ipc.server import IpcServer
from sarae_agent.logging_setup import event_handler
from sarae_agent.media.cloud_cache import MEDIA_ID
from sarae_agent.playback.engine import PolicyUpdate
from sarae_agent.runtime import AgentRuntime


class Strict(BaseModel):
    model_config = {"extra": "forbid"}


class ConfigSetParams(Strict):
    cloud: dict | None = None
    obs: dict | None = None
    monitor: dict | None = None
    agent: dict | None = None
    obs_password: str | None = Field(default=None, max_length=256)
    clear_obs_password: bool = False


class ActivateParams(Strict):
    activation_key: str = Field(min_length=1, max_length=200)
    cloud_url: str | None = Field(default=None, max_length=300)


class PlayParams(Strict):
    media_id: str = Field(pattern=MEDIA_ID.pattern)  # Media Library id, played from the cloud media cache


class PlaylistParams(Strict):
    media_ids: list[str] = Field(min_length=1, max_length=300)

    @field_validator("media_ids")
    @classmethod
    def _ids(cls, value: list[str]) -> list[str]:
        if any(not MEDIA_ID.fullmatch(m) for m in value):
            raise ValueError("Danh sách media_ids không hợp lệ")
        return value
    loop: bool = True


class CachePrepareParams(Strict):
    session_id: int = Field(ge=1)


class LoopParams(Strict):
    loop: bool


class MonitorConfigParams(Strict):
    interval_seconds: float | None = Field(default=None, ge=180, le=240)
    threshold: float | None = Field(default=None, ge=0.65, le=0.99)
    autostart: bool | None = None


class CustomTemplateParams(Strict):
    name: str = Field(min_length=1, max_length=80)
    image_base64: str = Field(min_length=1, max_length=4_100_000)


class ResetTemplateParams(Strict):
    name: str = Field(min_length=1, max_length=80)


class SpeakParams(Strict):
    text: str = Field(min_length=1, max_length=450)
    voice: str = Field(default="vi-VN-HoaiMyNeural", min_length=1, max_length=100)
    provider: Literal["edge"] = "edge"
    model: str | None = Field(default=None, min_length=1, max_length=100)
    settings: dict = Field(default_factory=dict)
    rate: int = Field(default=0, ge=-30, le=50)
    duck: bool = True
    intro: str | None = Field(default=None, min_length=1, max_length=120)


class AutoPinOpenParams(Strict):
    url: str = Field(min_length=12, max_length=1000)


class AutoPinLoginParams(Strict):
    url: str = Field(default="https://shop.tiktok.com/streamer/live/product/dashboard", min_length=12, max_length=1000)

class AutoPinConfigureParams(Strict):
    products: list[str] = Field(min_length=1, max_length=100)
    interval_seconds: int = Field(ge=5, le=3600)

class LogsParams(Strict):
    limit: int = Field(default=200, ge=1, le=500)


class ObsConnectParams(Strict):
    host: str | None = Field(default=None, min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    password: str | None = Field(default=None, max_length=256)


def register_all(server: IpcServer, rt: AgentRuntime) -> None:
    reg = server.register

    # system
    reg("system.status", lambda _: rt.system_status())
    reg("system.ping", lambda _: {"pong": True})
    reg("system.methods", lambda _: {"methods": server.methods, "cloud_command_kinds": rt.router.kinds})
    reg("system.shutdown", lambda _: {"accepted": True})

    # config
    def config_set(p: ConfigSetParams) -> dict:
        patch = {k: v for k, v in p.model_dump(include={"cloud", "obs", "monitor", "agent"}).items()
                 if v is not None}
        if "cloud" in patch and "api_url" in patch["cloud"] and rt.worker is not None:
            raise AgentError("AGENT_RUNNING", "Dừng Agent trước khi đổi Cloud URL")
        if patch:
            rt.config.update(patch)
        if p.clear_obs_password:
            rt.obs.set_password(None)
        elif p.obs_password:
            rt.obs.set_password(p.obs_password)
        if "obs" in patch or p.clear_obs_password or p.obs_password:
            rt.reload_obs_settings()
        return rt.config_view()

    reg("config.get", lambda _: rt.config_view())
    reg("config.set", config_set, ConfigSetParams)

    # activation / agent lifecycle
    def activate(p: ActivateParams) -> dict:
        if rt.worker is not None:
            raise AgentError("AGENT_RUNNING", "Dừng Agent trước khi kích hoạt")
        if p.cloud_url is not None:
            rt.config.update({"cloud": {"api_url": p.cloud_url}})
        url = rt.config.get().cloud.api_url
        if not url:
            raise AgentError("CLOUD_URL_MISSING", "Nhập Cloud API URL trước khi kích hoạt")
        result = rt.activation.activate(url, p.activation_key)
        rt.emit("agent.status_changed", rt.agent_status())
        return result

    def deactivate(_) -> dict:
        if rt.worker is not None:
            raise AgentError("AGENT_RUNNING", "Dừng Agent trước khi gỡ kích hoạt")
        result = rt.activation.deactivate_local()
        rt.emit("agent.status_changed", rt.agent_status())
        return result

    reg("activation.status", lambda _: rt.activation.status())
    reg("activation.activate", activate, ActivateParams)
    reg("activation.deactivate", deactivate)
    reg("agent.start", lambda _: rt.start_agent())
    reg("agent.stop", lambda _: rt.stop_agent())
    reg("agent.restart", lambda _: rt.restart_agent())
    reg("agent.status", lambda _: {**rt.agent_status(), "cloud": rt.cloud_status()})

    # media: Media Library videos are played from the Agent cloud media cache (no local folders)
    reg("media.cloud_cache.prepare", lambda p: rt.prepare_cloud_cache(p.session_id), CachePrepareParams)
    reg("media.cloud_cache.status", lambda _: rt.cloud_cache_status())

    # obs
    def obs_connect(p: ObsConnectParams) -> dict:
        patch = {k: v for k, v in {"host": p.host, "port": p.port}.items() if v is not None}
        if patch:
            rt.config.update({"obs": patch})
        if p.password is not None:
            rt.obs.set_password(p.password or None)
        rt.reload_obs_settings()
        return rt.obs.connect()

    reg("obs.status", lambda _: rt.obs.inspect())
    reg("obs.connect", obs_connect, ObsConnectParams)
    reg("obs.disconnect", lambda _: rt.obs.disconnect())

    # playback (local operator control; Cloud uses the same engine)
    reg("playback.status", lambda _: rt.playback.status())
    reg("playback.play", lambda p: rt.playback.play(p.media_id), PlayParams)
    reg("playback.playlist", lambda p: rt.playback.play_playlist(p.media_ids, p.loop), PlaylistParams)
    reg("playback.pause", lambda _: rt.playback.pause())
    reg("playback.resume", lambda _: rt.playback.resume())
    reg("playback.stop", lambda _: rt.playback.stop())
    reg("playback.next", lambda _: rt.playback.next())
    reg("playback.previous", lambda _: rt.playback.previous())
    reg("playback.loop", lambda p: rt.playback.set_loop(p.loop), LoopParams)
    reg("playback.policy", lambda p: rt.playback.set_policy(p.session_id, p.policy.model_dump(),
                                                            [item.model_dump() for item in p.lineup]), PolicyUpdate)
    reg("playback.camera", lambda _: rt.playback.use_camera())
    reg("playback.video", lambda _: rt.playback.use_video())

    # CAPTCHA monitor
    reg("monitor.status", lambda _: rt.monitor.status())
    reg("monitor.start", lambda _: rt.monitor.start())
    reg("monitor.stop", lambda _: rt.monitor.stop())
    reg("monitor.config", lambda p: rt.monitor.configure(p.model_dump(exclude_none=True)), MonitorConfigParams)
    reg("monitor.templates.list", lambda _: {"templates": rt.monitor.templates()})
    reg("monitor.templates.set_custom", lambda p: rt.monitor.set_custom_template(p.name, p.image_base64), CustomTemplateParams)
    reg("monitor.templates.reset", lambda p: rt.monitor.reset_custom_template(p.name), ResetTemplateParams)

    # voice
    reg("voice.speak", lambda p: rt.voice.speak(p.text, p.voice, rate=p.rate, duck=p.duck, intro=p.intro,
                                                provider=p.provider, model=p.model, settings=p.settings), SpeakParams)
    reg("voice.clear", lambda _: rt.voice.clear())
    reg("voice.status", lambda _: rt.voice.status())

    # Auto Pin: controlled Chrome profile, local-only IPC (no Cloud command support).
    reg("autopin.status", lambda _: rt.auto_pin.status())
    # Step 1: plain Chrome for a manual TikTok sign-in; step 2 (open/start) lets Playwright use that profile.
    reg("autopin.login", lambda p: rt.auto_pin.login_browser(p.url), AutoPinLoginParams)
    reg("autopin.login_done", lambda _: rt.auto_pin.finish_login())
    reg("autopin.open", lambda p: rt.auto_pin.open_browser(p.url), AutoPinOpenParams)
    reg("autopin.scan", lambda _: rt.auto_pin.scan_products())
    reg("tiktok.products.sync", lambda _: rt.sync_tiktok_products())
    reg("autopin.configure", lambda p: rt.auto_pin.configure(p.products, p.interval_seconds), AutoPinConfigureParams)
    reg("autopin.start", lambda _: rt.auto_pin.start())
    reg("autopin.stop", lambda _: rt.auto_pin.stop())

    # diagnostics
    reg("diagnostics.export", lambda _: rt.diagnostics())
    reg("logs.recent", lambda p: {"entries": list(event_handler.recent)[-p.limit:]}, LogsParams)
