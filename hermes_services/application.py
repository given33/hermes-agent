"""Composition root shared by Hermes protocol adapters.

FastAPI, aiohttp, and the local JSON-RPC process intentionally keep their
wire-specific routing. They construct authentication policy, live-session
ownership, and RPC dispatch through this application-layer kernel instead of
assembling unrelated module globals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .http_boundary import HttpBoundaryPolicy
from .http_boundary import HttpBoundaryCompatibilityAdapter, HttpContractMode
from .jsonrpc import JsonRpcMethodRegistry
from .session_registry import LiveSessionRegistry
from .contexts import BoundedContextRegistry


@dataclass(slots=True)
class HermesApplicationKernel:
    """Transport-neutral application services owned by one adapter process."""

    http_boundary: HttpBoundaryPolicy | HttpBoundaryCompatibilityAdapter | None = None
    sessions: LiveSessionRegistry[Any] = field(default_factory=LiveSessionRegistry)
    rpc: JsonRpcMethodRegistry = field(default_factory=JsonRpcMethodRegistry)
    contexts: BoundedContextRegistry = field(default_factory=BoundedContextRegistry)

    @classmethod
    def for_http(
        cls,
        *,
        surface: str,
        bearer_secret: str | None = None,
        allow_unconfigured_bearer: bool = False,
        allowed_origins: tuple[str, ...] = (),
        max_request_bytes: int | None = None,
        compatibility_mode: HttpContractMode = "dual",
    ) -> "HermesApplicationKernel":
        options: dict[str, Any] = {
            "surface": surface,
            "bearer_secret": bearer_secret,
            "allow_unconfigured_bearer": allow_unconfigured_bearer,
            "allowed_origins": allowed_origins,
        }
        if max_request_bytes is not None:
            options["max_request_bytes"] = max_request_bytes
        canonical = HttpBoundaryPolicy(**options)
        legacy = HttpBoundaryPolicy(**options)
        return cls(
            http_boundary=HttpBoundaryCompatibilityAdapter(
                canonical=canonical,
                legacy=legacy,
                mode=compatibility_mode,
            )
        )

    @classmethod
    def for_local_rpc(cls) -> "HermesApplicationKernel":
        return cls()

    def require_http_boundary(
        self,
    ) -> HttpBoundaryPolicy | HttpBoundaryCompatibilityAdapter:
        boundary = self.http_boundary
        if boundary is None:
            raise RuntimeError("application kernel has no HTTP boundary")
        return boundary


__all__ = ["HermesApplicationKernel"]
