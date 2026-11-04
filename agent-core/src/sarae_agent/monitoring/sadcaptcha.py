"""SadCaptcha REST analysis provider for Sarae Windows Agent.

Deliberately analysis-only: no UI automation, mouse/keyboard control, or
continuous/background API calls. Call analyze() only from an explicit local
operator action with images cropped to the CAPTCHA itself.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import threading
import time
from pathlib import Path
from typing import Callable, Literal

import httpx

from sarae_agent.activation.secrets import SecretFile
from sarae_agent.config import paths, read_json, write_json
from sarae_agent.errors import AgentError

CaptchaKind = Literal["puzzle", "shapes"]
API_BASE = "https://www.sadcaptcha.com/api/v1"
DEFAULT_BUDGET = 25
MAX_TOTAL_IMAGE_BYTES = 680_000  # keeps the entire IPC frame below 1 MB after Base64 encoding
MAX_IMAGE_BYTES = 680_000
MAX_IMAGE_SIDE = 2200


class SadCaptchaAnalyzer:
    """Locally managed API key, trial request budget and image analysis.

    `sender` is replaceable by a test double; default sends one HTTPS request.
    A trial 'attempt' is counted before making a network request. This is not
    the provider's authoritative credit balance.
    """

    def __init__(self, *, secret_store=None, usage_file: Path | None = None,
                 sender: Callable | None = None, max_attempts: int = DEFAULT_BUDGET):
        self._secret = secret_store if secret_store is not None else SecretFile("sadcaptcha")
        self._usage_file = usage_file if usage_file is not None else paths.root / "sadcaptcha_trial_usage.json"
        self._sender = sender if sender is not None else httpx.post
        self._max_attempts = max_attempts
        self._lock = threading.Lock()

    def set_api_key(self, key: str) -> dict:
        value = key.strip()
        if len(value) < 8 or len(value) > 256 or any(c.isspace() for c in value):
            raise AgentError("SADCAPTCHA_INVALID_KEY", "API key không hợp lệ")
        self._secret.save({"api_key": value})
        return self.status()

    def clear_api_key(self) -> dict:
        self._secret.delete()
        return self.status()

    def _key(self) -> str:
        data = self._secret.load() or {}
        value = data.get("api_key")
        if not isinstance(value, str) or not value:
            raise AgentError("SADCAPTCHA_KEY_MISSING", "Chưa cấu hình SadCaptcha API key")
        return value

    def _usage(self) -> dict:
        data = read_json(self._usage_file, {})
        if not isinstance(data, dict):
            return {}
        return data

    def status(self) -> dict:
        with self._lock:
            usage = self._usage()
            attempts = max(0, int(usage.get("attempts", 0)))
            return {"key_configured": self._secret.exists(), "attempts": attempts,
                    "remaining_local_budget": max(0, self._max_attempts - attempts),
                    "max_attempts": self._max_attempts,
                    "last": usage.get("last")}

    @staticmethod
    def _validate_image(encoded: str) -> tuple[str, bytes]:
        if not isinstance(encoded, str) or len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 20:
            raise AgentError("SADCAPTCHA_IMAGE_INVALID", "Ảnh CAPTCHA quá lớn")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise AgentError("SADCAPTCHA_IMAGE_INVALID", "Ảnh phải là Base64 hợp lệ") from None
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise AgentError("SADCAPTCHA_IMAGE_INVALID", "Ảnh CAPTCHA vượt quá giới hạn")
        if not (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff")):
            raise AgentError("SADCAPTCHA_IMAGE_INVALID", "Chỉ hỗ trợ ảnh PNG/JPEG")
        # Decode the image to reject malformed data and decompression bombs.
        try:
            import cv2
            import numpy as np
            image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None or min(image.shape[:2]) < 20 or max(image.shape[:2]) > MAX_IMAGE_SIDE:
                raise ValueError("image dimensions")
        except (ValueError, cv2.error) as exc:
            raise AgentError("SADCAPTCHA_IMAGE_INVALID", "Ảnh CAPTCHA không đọc được hoặc kích thước sai") from None
        return encoded, raw

    @staticmethod
    def _parse(kind: CaptchaKind, response: dict) -> dict:
        if not isinstance(response, dict):
            raise AgentError("SADCAPTCHA_BAD_RESPONSE", "SadCaptcha không trả về JSON object")
        keys = (["slideXProportion"] if kind == "puzzle" else
                ["pointOneProportionX", "pointOneProportionY", "pointTwoProportionX", "pointTwoProportionY"])
        result = {}
        for key in keys:
            value = response.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not (0 <= value <= 1):
                raise AgentError("SADCAPTCHA_BAD_RESPONSE", f"SadCaptcha trả về {key} không hợp lệ")
            result[key] = float(value)
        return result

    def analyze(self, kind: CaptchaKind, image_base64: str, piece_image_base64: str | None = None) -> dict:
        if kind not in ("puzzle", "shapes"):
            raise AgentError("SADCAPTCHA_UNSUPPORTED", "Chỉ hỗ trợ puzzle và shapes trong bản thử nghiệm")
        if (kind == "puzzle") != bool(piece_image_base64):
            raise AgentError("SADCAPTCHA_IMAGE_MISSING", "Puzzle cần ảnh puzzle và ảnh mảnh ghép riêng")
        image, raw = self._validate_image(image_base64)
        piece, raw_piece = (self._validate_image(piece_image_base64) if piece_image_base64 else (None, b""))
        if len(raw) + len(raw_piece) > MAX_TOTAL_IMAGE_BYTES:
            raise AgentError("SADCAPTCHA_IMAGE_INVALID", "Tổng kích thước ảnh vượt 680 KB")

        key = self._key()
        payload = {"imageB64": image} if kind == "shapes" else {
            "puzzleImageB64": image, "pieceImageB64": piece}
        fingerprint = hashlib.sha256(kind.encode("ascii") + raw + raw_piece).hexdigest()

        # The single Agent core owns this service; serialize requests so duplicate
        # submits cannot spend two trial credits.
        with self._lock:
            usage = self._usage()
            attempts = max(0, int(usage.get("attempts", 0)))
            if usage.get("last_fingerprint") == fingerprint:
                raise AgentError("SADCAPTCHA_DUPLICATE", "Ảnh này đã được gửi; không gọi API lặp để tránh hao credit")
            if attempts >= self._max_attempts:
                raise AgentError("SADCAPTCHA_BUDGET_EXCEEDED", "Đã hết hạn mức gọi API thử nghiệm trong Sarae")
            usage.update(attempts=attempts + 1, last_fingerprint=fingerprint,
                         last={"kind": kind, "state": "REQUESTED", "attempt": attempts + 1})
            write_json(self._usage_file, usage)

            started = time.monotonic()
            try:
                response = self._sender(f"{API_BASE}/{kind}", params={"licenseKey": key},
                                        json=payload, timeout=20.0, follow_redirects=False)
                if not 200 <= response.status_code < 300:
                    raise AgentError("SADCAPTCHA_HTTP_ERROR", f"SadCaptcha HTTP {response.status_code}")
                try:
                    body = response.json()
                except (ValueError, TypeError):
                    raise AgentError("SADCAPTCHA_BAD_RESPONSE", "SadCaptcha trả về JSON không hợp lệ") from None
                result = self._parse(kind, body)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                # Never expose str(exc): it may contain the licenseKey URL.
                raise AgentError("SADCAPTCHA_NETWORK_ERROR", "Lỗi kết nối hoặc timeout tới SadCaptcha") from None
            except httpx.HTTPError:
                raise AgentError("SADCAPTCHA_NETWORK_ERROR", "Không gửi được yêu cầu tới SadCaptcha") from None
            except AgentError as exc:
                usage["last"] = {"kind": kind, "state": "FAILED", "attempt": attempts + 1, "error": exc.code}
                write_json(self._usage_file, usage)
                raise
            duration_ms = int((time.monotonic() - started) * 1000)
            usage["last"] = {"kind": kind, "state": "ANALYSIS_READY", "attempt": attempts + 1,
                             "duration_ms": duration_ms, "result": result}
            write_json(self._usage_file, usage)
            return {**usage["last"], "remaining_local_budget": self._max_attempts - attempts - 1}
