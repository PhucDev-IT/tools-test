"""Wire format (protocol v1). One ASCII JSON object per line, '\\n' terminated.

GUI -> Core   {"v":1,"type":"request","request_id":"r-1","method":"system.status","params":{}}
Core -> GUI   {"v":1,"type":"response","request_id":"r-1","ok":true,"result":{...},"error":null}
Core -> GUI   {"v":1,"type":"event","event":"core.ready","data":{...},"ts":"2026-..."}

Frames are encoded with ensure_ascii so Windows pipe code pages can never corrupt them.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sarae_agent import IPC_PROTOCOL_VERSION

MAX_FRAME_BYTES = 1_000_000
METHOD_RE = re.compile(r"^[a-z]+(\.[a-z_]+){1,3}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str, request_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


@dataclass(frozen=True)
class Request:
    request_id: str
    method: str
    params: dict


def encode(frame: dict) -> bytes:
    return (json.dumps(frame, ensure_ascii=True, separators=(",", ":"), default=str) + "\n").encode("ascii")


def response_ok(request_id: str, result: Any) -> dict:
    return {"v": IPC_PROTOCOL_VERSION, "type": "response", "request_id": request_id, "ok": True,
            "result": result, "error": None}


def response_error(request_id: str | None, error: dict) -> dict:
    return {"v": IPC_PROTOCOL_VERSION, "type": "response", "request_id": request_id, "ok": False,
            "result": None, "error": error}


def event(name: str, data: Any) -> dict:
    return {"v": IPC_PROTOCOL_VERSION, "type": "event", "event": name, "data": data,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}


def parse_request(line: bytes) -> Request:
    if len(line) > MAX_FRAME_BYTES:
        raise ProtocolError("FRAME_TOO_LARGE", "Thông điệp IPC vượt quá 1 MB")
    text = line.decode("utf-8", errors="strict").lstrip("﻿").strip()
    try:
        frame = json.loads(text)
    except ValueError as exc:
        raise ProtocolError("INVALID_JSON", f"JSON không hợp lệ: {exc}") from exc
    if not isinstance(frame, dict):
        raise ProtocolError("INVALID_FRAME", "Thông điệp IPC phải là JSON object")
    request_id = frame.get("request_id")
    rid = request_id if isinstance(request_id, str) and REQUEST_ID_RE.fullmatch(request_id) else None
    if frame.get("v", IPC_PROTOCOL_VERSION) != IPC_PROTOCOL_VERSION:
        raise ProtocolError("UNSUPPORTED_VERSION", f"Core chỉ hỗ trợ IPC v{IPC_PROTOCOL_VERSION}", rid)
    if frame.get("type", "request") != "request":
        raise ProtocolError("INVALID_FRAME", "Core chỉ nhận thông điệp type=request", rid)
    if rid is None:
        raise ProtocolError("INVALID_REQUEST_ID", "Thiếu hoặc sai request_id")
    method = frame.get("method")
    if not isinstance(method, str) or not METHOD_RE.fullmatch(method):
        raise ProtocolError("METHOD_NOT_FOUND", "Tên method không hợp lệ", rid)
    params = frame.get("params") or {}
    if not isinstance(params, dict):
        raise ProtocolError("INVALID_PARAMS", "params phải là JSON object", rid)
    return Request(rid, method, params)
