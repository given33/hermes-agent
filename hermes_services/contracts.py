"""Transport-neutral response contracts for Hermes service adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ServiceFailure:
    """A framework-independent application failure.

    Adapters retain control of their wire format, but all transports share the
    status, stable code, and public message.  Internal exceptions never belong
    in this object.
    """

    status_code: int
    code: str
    message: str

    def simple_body(self) -> dict[str, str]:
        """Legacy Hermes REST error shape used by cron callbacks."""
        return {"error": self.message}


def openai_error_body(
    message: str,
    *,
    error_type: str = "invalid_request_error",
    param: str | None = None,
    code: str | None = None,
) -> dict[str, dict[str, str | None]]:
    """Canonical OpenAI-compatible error envelope."""
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }


def jsonrpc_result(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical JSON-RPC 2.0 success envelope."""
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """Canonical JSON-RPC 2.0 error envelope."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
