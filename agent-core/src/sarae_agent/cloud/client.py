"""HTTPS client for the Sarae Cloud Agent API (backend/app/cloud_agents.py, identity.py, agent_media.py).

Outbound only: the Agent never opens an inbound port. Every failure is mapped to a stable
error code so the worker can decide between retry (transient) and stop (requires the user).

    POST /identity/activate                         {activation_key, hostname} -> {agent_id, agent_credential}
    POST /agents/heartbeat                          {agent_id, hostname, verification, obs, local_playback, playback}
    GET  /agents/commands/{agent_id}/next?wait=N    long-poll -> {"command": {command_id, kind, payload, claim_token,
                                                    attempt, expires_at} | null}
    POST /agents/commands/ack                       {agent_id, command_id, claim_token, success, result}
    POST /agents/events                             {agent_id, events: [{seq, type, session_id, product_id, detail}]}
    POST /agents/verification-capture?agent_id=&at=  JPEG of the Studio window for the Telegram alert of detection `at`
    GET  /captcha-templates/agent?agent_id=          {templates: [{id, sha256, bytes}]} (set by SUPER_ADMIN)
    GET  /captcha-templates/agent/{id}/image?agent_id=   PNG/JPEG bytes
Agent endpoints authenticate with the X-Sarae-Agent-Key header.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

from sarae_agent import __version__
from sarae_agent.errors import AgentError

log = logging.getLogger("sarae.cloud")

# Codes that cannot be fixed by retrying; the worker stops and the GUI asks the user to act.
FATAL_CODES = {"AGENT_REVOKED", "LICENSE_EXPIRED", "AGENT_UNAUTHORIZED", "AGENT_NOT_ACTIVATED"}
MAX_WAIT_SECONDS = 25  # Cloud cap for the long-poll
RESULT_MAX_CHARS = 65000  # AckBody.result


class CloudError(AgentError):
    @property
    def fatal(self) -> bool:
        return self.code in FATAL_CODES


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):  # FastAPI validation errors
        return "; ".join(str(item.get("msg")) for item in detail[:3] if isinstance(item, dict))
    return str(detail or body)[:300]


def classify(response: httpx.Response, context: str) -> CloudError:
    status = response.status_code
    message = _detail(response)
    # A Cloud build that sends a machine code wins; otherwise derive it from status + endpoint.
    code = response.headers.get("X-Sarae-Error", "").strip().upper()
    if not code:
        if status == 401:
            code = "AGENT_UNAUTHORIZED"
        elif status == 403 and context == "activate":
            code = "LICENSE_EXPIRED" if "license" in message.lower() else "ACTIVATION_KEY_INVALID"
        elif status == 403:
            # verify_agent: unknown/revoked Agent or OWNER/license not valid; the user must act either way.
            code = "LICENSE_EXPIRED"
        elif status == 409 and context == "activate":
            code = "AGENT_LIMIT_REACHED"
        elif status in (404, 409) and context == "ack":
            # Unknown command, claim token not issued for it, or cancelled: the Cloud will not take this ack.
            code = "ACK_REJECTED"
        elif status == 422:
            code = "CLOUD_VALIDATION_ERROR"
        elif status >= 500:
            code = "CLOUD_SERVER_ERROR"
        else:
            code = f"CLOUD_HTTP_{status}"
    retryable = status >= 500 or status == 429
    return CloudError(code, message or f"Cloud trả về HTTP {status}", {"status": status}, retryable=retryable)


class CloudClient:
    def __init__(self, api_url: str, *, timeout: float = 12.0, transport: httpx.BaseTransport | None = None):
        if not api_url:
            raise AgentError("CLOUD_URL_MISSING", "Chưa cấu hình Cloud API URL")
        self.api_url = api_url.rstrip("/")
        self._timeout = timeout
        # Thread-safe: the command long-poll and heartbeats run concurrently on one connection pool.
        self._http = httpx.Client(
            base_url=self.api_url,
            timeout=httpx.Timeout(timeout, connect=min(6.0, timeout)),
            headers={"User-Agent": f"SaraeAgent/{__version__}", "Accept": "application/json"},
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "CloudClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _send(self, method: str, path: str, context: str, *, key: str | None = None,
              json: Any = None, params: dict | None = None, timeout: float | None = None,
              content: bytes | None = None, content_type: str | None = None) -> httpx.Response:
        headers = {"X-Sarae-Agent-Key": key} if key else {}
        if content_type:
            headers["Content-Type"] = content_type
        extra = {} if timeout is None else {"timeout": httpx.Timeout(timeout, connect=min(6.0, timeout))}
        if content is not None:
            extra["content"] = content
        try:
            response = self._http.request(method, path, json=json, params=params, headers=headers or None, **extra)
        except httpx.TimeoutException as exc:
            raise CloudError("CLOUD_TIMEOUT", f"Cloud không phản hồi ({type(exc).__name__})",
                             retryable=True) from exc
        except httpx.TransportError as exc:
            raise CloudError("CLOUD_UNREACHABLE", f"Không kết nối được Cloud: {str(exc)[:160]}",
                             retryable=True) from exc
        except RuntimeError as exc:  # client closed while a long-poll was waiting (worker stopping)
            raise CloudError("CLOUD_CLIENT_CLOSED", str(exc)[:160], retryable=True) from exc
        if response.status_code >= 400:
            raise classify(response, context)
        return response

    def _request(self, method: str, path: str, context: str, **kwargs: Any) -> dict:
        response = self._send(method, path, context, **kwargs)
        try:
            body = response.json()
        except ValueError as exc:
            raise CloudError("CLOUD_BAD_RESPONSE", "Cloud trả về dữ liệu không phải JSON",
                             retryable=True) from exc
        return body if isinstance(body, dict) else {"data": body}

    # --- Endpoints -----------------------------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/health", "health")

    def activate(self, activation_key: str, hostname: str) -> dict:
        body = self._request("POST", "/identity/activate", "activate",
                             json={"activation_key": activation_key, "hostname": hostname})
        if not body.get("agent_id") or not body.get("agent_credential"):
            raise CloudError("CLOUD_BAD_RESPONSE", "Cloud không trả về agent_id/agent_credential")
        return body

    def heartbeat(self, key: str, report: dict) -> dict:
        return self._request("POST", "/agents/heartbeat", "heartbeat", key=key, json=report)


    def next_command(self, key: str, agent_id: str, wait: float = 0) -> dict | None:
        """Claim the next command; with ``wait`` the Cloud holds the request until one is queued."""
        wait = max(0, min(MAX_WAIT_SECONDS, int(wait)))
        body = self._request("GET", f"/agents/commands/{quote(agent_id, safe='')}/next", "poll", key=key,
                             params={"wait": wait}, timeout=wait + self._timeout)
        command = body.get("command")
        return command if isinstance(command, dict) else None

    def ack(self, key: str, agent_id: str, command: dict, success: bool, result: str) -> dict:
        """Final outcome of a claimed command. ``claim_token`` proves this Agent received that delivery."""
        return self._request("POST", "/agents/commands/ack", "ack", key=key, json={
            "agent_id": agent_id, "command_id": command["command_id"],
            "claim_token": str(command.get("claim_token") or ""), "success": bool(success),
            "result": result[:RESULT_MAX_CHARS]})

    def post_events(self, key: str, agent_id: str, events: list[dict]) -> dict:
        return self._request("POST", "/agents/events", "events", key=key,
                             json={"agent_id": agent_id, "events": events[:200]})

    def upload_verification_capture(self, key: str, agent_id: str, at: str, jpeg: bytes) -> dict:
        return self._request("POST", "/agents/verification-capture", "verification_capture", key=key,
                             params={"agent_id": agent_id, "at": at}, content=jpeg, content_type="image/jpeg",
                             timeout=30.0)

    def sync_tiktok_products(self, key: str, agent_id: str, products: list[dict], account: dict) -> dict:
        return self._request("POST", "/tiktok-products/sync", "tiktok_sync", key=key,
                             json={"agent_id": agent_id, "products": products, "complete": True, "account": account},
                             timeout=max(60.0, self._timeout))

    def captcha_templates(self, key: str, agent_id: str) -> list[dict]:
        body = self._request("GET", "/captcha-templates/agent", "templates", key=key, params={"agent_id": agent_id})
        templates = body.get("templates")
        return templates if isinstance(templates, list) else []

    def captcha_template_image(self, key: str, agent_id: str, template_id: str) -> bytes:
        return self._send("GET", f"/captcha-templates/agent/{quote(template_id, safe='')}/image", "templates",
                          key=key, params={"agent_id": agent_id}).content
