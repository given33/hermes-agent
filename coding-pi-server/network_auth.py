"""Shared network authentication rules for standalone Coding Pi services."""

from __future__ import annotations

import hmac
import ipaddress
import socket


def _normalized_host(value: str | None) -> str:
    host = (value or "").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.split("%", 1)[0]


def _ip_is_loopback(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def peer_is_loopback(peer_host: str | None) -> bool:
    """Return true only for a concrete loopback socket peer address."""

    return _ip_is_loopback(_normalized_host(peer_host))


def bind_host_is_loopback(bind_host: str | None) -> bool:
    """Resolve a listener host and require every result to be loopback."""

    host = _normalized_host(bind_host)
    if not host or host in {"*", "0.0.0.0", "::"}:
        return False
    if _ip_is_loopback(host):
        return True
    try:
        results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addresses = {_normalized_host(str(item[4][0])) for item in results if item[4]}
    return bool(addresses) and all(_ip_is_loopback(address) for address in addresses)


def bearer_or_loopback_authorized(
    expected_token: str | None,
    received_authorization: str | None,
    peer_host: str | None,
    *,
    allow_unauthenticated_loopback: bool = False,
) -> bool:
    """Require bearer auth when configured, otherwise restrict to loopback."""

    expected = (expected_token or "").strip()
    if expected:
        received = (received_authorization or "").encode("utf-8", errors="surrogatepass")
        wanted = f"Bearer {expected}".encode("utf-8", errors="surrogatepass")
        return hmac.compare_digest(received, wanted)
    return allow_unauthenticated_loopback and peer_is_loopback(peer_host)


def require_safe_listener(bind_host: str | None, token: str | None, service_name: str) -> None:
    if bind_host_is_loopback(bind_host) or (token or "").strip():
        return
    raise RuntimeError(f"{service_name} requires a bearer token when listening outside loopback")


def require_coordinator_token(coordinator_url: str | None, token: str | None) -> None:
    if not (coordinator_url or "").strip() or (token or "").strip():
        return
    raise RuntimeError("Coding Pi coordinator registration requires CODING_PI_COORDINATOR_TOKEN")
