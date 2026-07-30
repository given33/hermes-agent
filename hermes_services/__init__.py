"""Framework-neutral application services shared by Hermes transports.

FastAPI, aiohttp, and the local JSON-RPC gateway are protocol adapters.  This
package owns the security and application contracts that must behave the same
on every surface.  Importing it must not start a server or mutate process-wide
state.
"""

from .contracts import (
    ServiceFailure,
    jsonrpc_error,
    jsonrpc_result,
    openai_error_body,
)
from .auth import (
    AuthError,
    BearerAuthorization,
    CODEX_RATE_LIMITED_CODE,
    authorize_bearer,
    has_usable_secret,
    is_rate_limited_auth_error,
)
from .application import HermesApplicationKernel
from .contexts import BoundedContext, BoundedContextRegistry
from .cron_fire import CronFireAcceptance, accept_cron_fire_request
from .http_boundary import (
    HttpBoundaryCompatibilityAdapter,
    HttpBoundaryPolicy,
    HttpContractMode,
)
from .jsonrpc import JsonRpcMethodRegistry
from .session_registry import LiveSessionRegistry
from .middleware import (
    MiddlewareBackend,
    RequestMiddlewareResult,
    install_middleware_backend,
    restore_middleware_backend,
)
from .http_policy import (
    API_SECURITY_HEADERS,
    DASHBOARD_SECURITY_HEADERS,
    DEFAULT_MAX_REQUEST_BYTES,
    LOCAL_DASHBOARD_CORS_ORIGIN_REGEX,
    cors_headers_for_origin,
    origin_allowed,
    security_headers,
    validate_content_length,
)

__all__ = [
    "ServiceFailure",
    "AuthError",
    "BearerAuthorization",
    "CODEX_RATE_LIMITED_CODE",
    "CronFireAcceptance",
    "HermesApplicationKernel",
    "BoundedContext",
    "BoundedContextRegistry",
    "JsonRpcMethodRegistry",
    "LiveSessionRegistry",
    "MiddlewareBackend",
    "RequestMiddlewareResult",
    "HttpBoundaryPolicy",
    "HttpBoundaryCompatibilityAdapter",
    "HttpContractMode",
    "API_SECURITY_HEADERS",
    "DASHBOARD_SECURITY_HEADERS",
    "DEFAULT_MAX_REQUEST_BYTES",
    "LOCAL_DASHBOARD_CORS_ORIGIN_REGEX",
    "accept_cron_fire_request",
    "authorize_bearer",
    "has_usable_secret",
    "is_rate_limited_auth_error",
    "cors_headers_for_origin",
    "jsonrpc_error",
    "jsonrpc_result",
    "openai_error_body",
    "origin_allowed",
    "install_middleware_backend",
    "restore_middleware_backend",
    "security_headers",
    "validate_content_length",
]
