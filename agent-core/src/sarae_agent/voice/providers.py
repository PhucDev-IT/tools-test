"""Audio generators. Providers write MP3 to disk so OBS can play a completed file."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, Protocol


from sarae_agent.errors import AgentError

FirstAudio = Callable[[], None]


class TTSProvider(Protocol):
    requires_key: bool
    def normalize(self, model: str | None, voice: str, rate: int, settings: dict | None) -> tuple[str, dict]: ...

    def synthesize(self, text: str, voice: str, model: str, settings: dict,
                   output: Path, api_key: str | None, first_audio: FirstAudio) -> None: ...


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, TTSProvider] = {}

    def register(self, name: str, provider: TTSProvider) -> None:
        self._providers[name] = provider

    def get(self, name: str) -> TTSProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise AgentError("TTS_PROVIDER", f"Nhà cung cấp giọng nói không được hỗ trợ: {name}") from exc


class EdgeTTSProvider:
    VOICES = ("vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural")
    requires_key = False

    def normalize(self, model: str | None, voice: str, rate: int, settings: dict | None) -> tuple[str, dict]:
        if voice not in self.VOICES or model not in (None, "edge-neural"):
            raise AgentError("INVALID_PARAMS", "Giọng hoặc model Edge TTS không được hỗ trợ")
        if settings and settings != {"rate": rate}:
            raise AgentError("INVALID_PARAMS", "Edge TTS không nhận voice settings")
        if not -30 <= rate <= 50:
            raise AgentError("INVALID_PARAMS", "Tốc độ đọc từ -30% đến +50%")
        return "edge-neural", {"rate": rate}

    def synthesize(self, text: str, voice: str, model: str, settings: dict,
                   output: Path, api_key: str | None, first_audio: FirstAudio) -> None:
        if voice not in self.VOICES or model != "edge-neural":
            raise AgentError("INVALID_PARAMS", "Giọng hoặc model Edge TTS không được hỗ trợ")
        try:
            import edge_tts
        except ImportError as exc:
            raise AgentError("TTS_UNAVAILABLE", "Thiếu thư viện edge-tts trong Agent Core") from exc

        async def run() -> None:
            for attempt in range(4):
                try:
                    async def stream() -> None:
                        with output.open("wb") as file:
                            async for chunk in edge_tts.Communicate(text, voice, rate=f"{settings.get('rate', 0):+d}%").stream():
                                if chunk["type"] == "audio":
                                    if file.tell() == 0:
                                        first_audio()
                                    file.write(chunk["data"])
                    await asyncio.wait_for(stream(), timeout=25)
                    if output.stat().st_size:
                        return
                    raise edge_tts.exceptions.NoAudioReceived()
                except edge_tts.exceptions.NoAudioReceived:
                    if attempt == 3:
                        raise
                    await asyncio.sleep(0.5 * (attempt + 1))

        try:
            asyncio.run(run())
        except asyncio.TimeoutError as exc:
            raise AgentError("TTS_TIMEOUT", "Tạo giọng nói quá 25 giây", retryable=True) from exc
        except AgentError:
            raise
        except Exception as exc:
            raise AgentError("TTS_FAILED", f"Dịch vụ Edge TTS lỗi: {type(exc).__name__}", retryable=True) from exc


def default_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register("edge", EdgeTTSProvider())
    return registry
