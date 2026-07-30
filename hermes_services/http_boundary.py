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
HttpContractMode = Literal["legacy", "dual", "canonical"]


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


@dataclass(frozen=True, slots=True)
class HttpBoundaryCompatibilityAdapter:
    """Compare old and canonical policies while HTTP adapters migrate."""

    canonical: HttpBoundaryPolicy
    legacy: HttpBoundaryPolicy
    mode: HttpContractMode = "dual"

    def __post_init__(self) -> None:
        if self.mode not in {"legacy", "dual", "canonical"}:
            raise ValueError(f"invalid HTTP contract migration mode: {self.mode}")

    @property
    def surface(self) -> HttpSurface:
        return self.canonical.surface

    @property
    def max_request_bytes(self) -> int:
        return int(self._select("max_request_bytes"))

    @property
    def response_headers(self) -> Mapping[str, str]:
        return self._select("response_headers")

    def authorize(self, authorization_header: str | None) -> BearerAuthorization:
        canonical = self.canonical.authorize(authorization_header)
        if self.mode == "canonical":
            return canonical
        legacy = self.legacy.authorize(authorization_header)
        if self.mode == "legacy":
            return legacy
        if canonical != legacy:
            return BearerAuthorization(
                authenticated=False,
                configured=bool(canonical.configured or legacy.configured),
                error_code="http_contract_mismatch",
            )
        return canonical

    def validate_content_length(
        self,
        method: str,
        content_length: str | None,
    ) -> ServiceFailure | None:
        return self._dual_value(
            self.canonical.validate_content_length(method, content_length),
            self.legacy.validate_content_length(method, content_length),
        )

    def origin_allowed(self, origin: str) -> bool:
        return bool(self._dual_value(
            self.canonical.origin_allowed(origin),
            self.legacy.origin_allowed(origin),
        ))

    def cors_headers(self, origin: str) -> dict[str, str] | None:
        return self._dual_value(
            self.canonical.cors_headers(origin),
            self.legacy.cors_headers(origin),
        )

    def _select(self, attribute: str):
        return self._dual_value(
            getattr(self.canonical, attribute),
            getattr(self.legacy, attribute),
        )

    def _dual_value(self, canonical, legacy):
        if self.mode == "canonical":
            return canonical
        if self.mode == "legacy":
            return legacy
        if canonical != legacy:
            raise RuntimeError("legacy and canonical HTTP contracts diverged")
        return canonical


__all__ = [
    "HttpBoundaryCompatibilityAdapter",
    "HttpBoundaryPolicy",
    "HttpContractMode",
    "HttpSurface",
]
