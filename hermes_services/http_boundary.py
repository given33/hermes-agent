"""One framework-neutral HTTP policy object for every Hermes listener."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from .auth import BearerAuthorization, authorize_bearer
from .contracts import ServiceFailure
from .http_policy import (
    DEFAULT_MAX_REQUEST_BYTES,
    cors_headers_for_origin,
    origin_allowed,
    security_headers,
    validate_content_length,
)

HttpSurface = Literal["api", "dashboard"]


@dataclass(frozen=True, slots=True)
class HttpBoundaryPolicy:
    """Shared request/response policy adapted by FastAPI and aiohttp.

    Routing and wire serialization remain transport concerns. Authentication,
    declared body limits, browser-origin decisions and response hardening do
    not, so listeners receive them through this single immutable boundary.
    """

    surface: HttpSurface
    bearer_secret: str | None = None
    allow_unconfigured_bearer: bool = False
    allowed_origins: tuple[str, ...] = ()
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES

    @property
    def response_headers(self) -> Mapping[str, str]:
        return security_headers(self.surface)

    def authorize(self, authorization_header: str | None) -> BearerAuthorization:
        return authorize_bearer(
            authorization_header,
            self.bearer_secret,
            allow_unconfigured=self.allow_unconfigured_bearer,
        )

    def validate_content_length(
        self,
        method: str,
        content_length: str | None,
    ) -> ServiceFailure | None:
        return validate_content_length(
            method,
            content_length,
            max_bytes=self.max_request_bytes,
        )

    def origin_allowed(self, origin: str) -> bool:
        return origin_allowed(origin, self.allowed_origins)

    def cors_headers(self, origin: str) -> dict[str, str] | None:
        return cors_headers_for_origin(origin, self.allowed_origins)


__all__ = ["HttpBoundaryPolicy", "HttpSurface"]
