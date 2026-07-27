"""Transport-neutral JSON-RPC method registration and request dispatch."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .contracts import jsonrpc_error, jsonrpc_result

JsonRpcHandler = Callable[[Any, dict[str, Any]], dict[str, Any] | None]
NormalizedJsonRpcRequest = tuple[Any, str, dict[str, Any]]


class JsonRpcMethodRegistry:
    """Own the method table and JSON-RPC 2.0 request boundary.

    TUI stdio/WebSocket code remains responsible for scheduling and writing
    responses. This registry owns the protocol invariants shared by those
    transports: request shape, parameter shape, method lookup and envelopes.
    ``methods`` intentionally remains a mutable dict for plugin registration
    and backwards-compatible test injection.
    """

    def __init__(self) -> None:
        self.methods: dict[str, JsonRpcHandler] = {}

    @staticmethod
    def success(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return jsonrpc_result(request_id, result)

    @staticmethod
    def failure(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return jsonrpc_error(request_id, code, message)

    def method(self, name: str) -> Callable[[JsonRpcHandler], JsonRpcHandler]:
        if not isinstance(name, str) or not name:
            raise ValueError("JSON-RPC method name must be a non-empty string")

        def register(handler: JsonRpcHandler) -> JsonRpcHandler:
            self.methods[name] = handler
            return handler

        return register

    def normalize(
        self,
        request: Any,
    ) -> NormalizedJsonRpcRequest | dict[str, Any]:
        if not isinstance(request, dict):
            return self.failure(None, -32600, "invalid request: expected an object")

        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str) or not method:
            return self.failure(
                request_id,
                -32600,
                "invalid request: method must be a non-empty string",
            )

        params = request.get("params", {})
        if params is None:
            params = {}
        elif not isinstance(params, dict):
            return self.failure(
                request_id,
                -32602,
                "invalid params: expected an object",
            )
        return request_id, method, params

    def handle(self, request: Any) -> dict[str, Any] | None:
        normalized = self.normalize(request)
        if isinstance(normalized, dict):
            return normalized

        request_id, method, params = normalized
        handler = self.methods.get(method)
        if handler is None:
            return self.failure(request_id, -32601, f"unknown method: {method}")
        return handler(request_id, params)


__all__ = [
    "JsonRpcHandler",
    "JsonRpcMethodRegistry",
    "NormalizedJsonRpcRequest",
]
