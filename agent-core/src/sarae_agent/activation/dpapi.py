"""Windows DPAPI (CryptProtectData) bound to the current Windows user, via ctypes."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from sarae_agent.errors import AgentError

CRYPTPROTECT_UI_FORBIDDEN = 0x1
# Application entropy: another app running as the same user cannot decrypt with a bare
# CryptUnprotectData call unless it also knows this value.
APP_ENTROPY = b"SaraeAgent/credential/v2"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _require_windows() -> None:
    if os.name != "nt":
        raise AgentError("DPAPI_UNAVAILABLE", "Bảo vệ credential bằng DPAPI chỉ hỗ trợ Windows")


def protect(plaintext: bytes, *, entropy: bytes | None = APP_ENTROPY, description: str = "Sarae Agent") -> bytes:
    _require_windows()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    data_in, _keep_in = _blob(plaintext)
    entropy_blob = None
    if entropy is not None:
        entropy_blob, _keep_entropy = _blob(entropy)
    data_out = DATA_BLOB()
    ok = crypt32.CryptProtectData(ctypes.byref(data_in), ctypes.c_wchar_p(description),
                                  ctypes.byref(entropy_blob) if entropy_blob else None,
                                  None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(data_out))
    if not ok:
        raise AgentError("DPAPI_FAILED", f"CryptProtectData lỗi {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        kernel32.LocalFree(data_out.pbData)


def unprotect(protected: bytes, *, entropy: bytes | None = APP_ENTROPY) -> bytes:
    _require_windows()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    data_in, _keep_in = _blob(protected)
    entropy_blob = None
    if entropy is not None:
        entropy_blob, _keep_entropy = _blob(entropy)
    data_out = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(ctypes.byref(data_in), None,
                                    ctypes.byref(entropy_blob) if entropy_blob else None,
                                    None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(data_out))
    if not ok:
        raise AgentError("DPAPI_FAILED",
                         "Không giải mã được credential (khác Windows user hoặc dữ liệu hỏng)")
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        kernel32.LocalFree(data_out.pbData)
