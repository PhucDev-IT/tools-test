"""Download existing orchestrated jobs. This module never creates a TTS job."""
from __future__ import annotations

import io
import time
import wave
from urllib.parse import quote, urlsplit

import httpx

from sarae_agent.errors import AgentError


class OmniVoiceProvider:
    requires_key = False

    def __init__(self, identity, transport=None):
        self.identity = identity
        self.transport = transport

    def normalize(self, model, voice, rate, settings):
        if model not in (None, "demo_default") or rate or settings:
            raise AgentError("INVALID_PARAMS", "G-OmniVoice chỉ hỗ trợ demo_default")
        return "demo_default", {}

    def synthesize(self, text, voice, model, settings, output, api_key, first_audio):
        raise AgentError("INVALID_PARAMS", "OmniVoice cần job reference do Backend tạo")

    def download(self, ref, output, check, audio_path="relay"):
        identity, base = self.identity()
        root = base.rstrip("/") + "/agents/tts/audio/" + quote(ref["utterance_id"], safe="")
        params = {"agent_id": identity.agent_id, "wait": 20}
        headers = {"X-Sarae-Agent-Key": identity.credential}
        url, path = root, "relay"
        started = time.monotonic()
        with httpx.Client(transport=self.transport, timeout=22, follow_redirects=False) as client:
            check()
            if audio_path == "auto":
                try:
                    response = client.post(root + "/token", params={"agent_id": identity.agent_id}, headers=headers)
                    if response.status_code == 200:
                        token = response.json()
                        if token["job_id"] != ref["job_id"] or urlsplit(token["base_url"]).scheme not in ("http", "https"):
                            raise AgentError("TTS_AUTH", "Thông tin direct không hợp lệ")
                        url = token["base_url"].rstrip("/") + "/v1/tts/jobs/" + quote(ref["job_id"], safe="") + "/audio"
                        headers = {"Authorization": "Bearer " + token["read_token"], "X-Sarae-Agent-Id": identity.agent_id}
                        params, path = {"wait": 20}, "direct"
                    elif response.status_code != 404 or response.headers.get("X-Sarae-Error") != "USE_RELAY":
                        self._error(response)
                except httpx.TransportError:
                    pass
            for _ in range(100):
                check()
                try:
                    with client.stream("GET", url, params=params, headers=headers) as response:
                        if response.status_code == 202:
                            check()
                            time.sleep(.1)
                            continue
                        if response.status_code != 200:
                            self._error(response)
                        download = time.monotonic()
                        data = bytearray()
                        for chunk in response.iter_bytes(65536):
                            check()
                            data.extend(chunk)
                            if len(data) > 20 * 1024 * 1024:
                                raise AgentError("AUDIO_TOO_LARGE", "WAV vượt giới hạn")
                    check()
                    try:
                        with wave.open(io.BytesIO(data)) as wav:
                            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 24000, "NONE"):
                                raise ValueError()
                            frames = wav.getnframes()
                            if not frames or len(wav.readframes(frames)) != frames * 2:
                                raise ValueError()
                            duration = frames / 24
                    except (wave.Error, EOFError, ValueError):
                        raise AgentError("TTS_INVALID_AUDIO", "WAV không phải mono PCM16 24 kHz đầy đủ")
                    output.write_bytes(data)
                    return {"audio_path": path, "cache_hit": False, "duration_ms": duration,
                            "wait_audio_ms": int((download-started)*1000),
                            "download_ms": int((time.monotonic()-download)*1000)}
                except httpx.TransportError as exc:
                    if path == "direct":
                        url, path = root, "relay"
                        params = {"agent_id": identity.agent_id, "wait": 20}
                        headers = {"X-Sarae-Agent-Key": identity.credential}
                        continue
                    raise AgentError("TTS_BACKEND_UNREACHABLE", "Không tải được WAV từ Backend") from exc
        raise AgentError("TTS_TIMEOUT", "Hết số lượt chờ audio")

    @staticmethod
    def _error(response):
        code = {401: "TTS_AUTH", 403: "TTS_AUTH", 409: "TTS_INFERENCE_FAILED", 410: "TTS_EXPIRED",
                429: "TTS_BUSY", 503: "TTS_NOT_READY"}.get(response.status_code, "TTS_BACKEND_UNREACHABLE")
        if response.status_code == 410:
            response.read()
            try:
                if response.json().get("error", {}).get("code") in ("JOB_CANCELLED", "TTS_CANCELLED") or response.headers.get("X-Sarae-Error") == "JOB_CANCELLED":
                    code = "TTS_CANCELLED"
            except (ValueError, AttributeError):
                pass
        raise AgentError(code, f"Tải audio HTTP {response.status_code}")
