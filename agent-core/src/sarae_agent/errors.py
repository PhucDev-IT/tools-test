"""Typed errors with stable codes shared by IPC responses, Cloud ACKs and logs."""
from __future__ import annotations


class AgentError(Exception):
    """Business error. `code` is a stable UPPER_SNAKE identifier documented in WINDOWS_AGENT_IPC.md."""

    def __init__(self, code: str, message: str, details: dict | None = None, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable

    def to_dict(self) -> dict:
        data = {"code": self.code, "message": self.message}
        if self.details:
            data["details"] = self.details
        return data


def as_agent_error(exc: BaseException) -> AgentError:
    if isinstance(exc, AgentError):
        return exc
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.isupper():
        return AgentError(code, str(exc))
    if isinstance(exc, (ValueError, TypeError)):
        return AgentError("INVALID_REQUEST", str(exc) or type(exc).__name__)
    if isinstance(exc, TimeoutError):
        return AgentError("TIMEOUT", str(exc) or "Hết thời gian chờ", retryable=True)
    return AgentError("INTERNAL_ERROR", f"{type(exc).__name__}: {str(exc)[:300]}")
