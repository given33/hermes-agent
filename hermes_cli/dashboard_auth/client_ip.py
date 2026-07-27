"""Trusted-proxy-aware client IP resolution.

Three modules used to carry an identical copy of this logic
(``routes.py``, ``middleware.py``, ``token_auth.py``), and all three
read ``X-Forwarded-For`` unconditionally, returning ``fwd.split(",")[0]``
— the **leftmost** element. That is the wrong end of the chain and it is
attacker-controlled:

  * With the near-universal nginx idiom
    ``proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`` the
    header is *appended* to, so whatever the client sent arrives first
    and the proxy's observation of the real peer arrives last. The
    leftmost element is therefore exactly the segment the attacker
    wrote; the trustworthy one is on the right.
  * With no proxy at all the header is pure client input.

Because the resolved value keys the password-login rate limiter, a
spoofable value meant an attacker could land every guess in a fresh
bucket and brute-force passwords without limit. It is also the ``ip``
field recorded in every audit log entry, so it could be used to frame an
arbitrary address.

Resolution rules, fail-closed by default:

  1. No trusted proxies configured (the default) → ``X-Forwarded-For`` is
     ignored entirely and the transport peer is authoritative. A direct
     deployment cannot be spoofed at all.
  2. Trusted proxies configured but the immediate peer is not one of them
     → the connection did not come through our proxy, so its forwarding
     header carries no authority. Peer wins again.
  3. Peer is a trusted proxy → walk the chain right-to-left and return
     the first entry that is *not* a trusted proxy. That is the append
     semantics above read from the correct end: the last hop a trusted
     proxy actually observed. Anything the client prepended sits further
     left and is never reached.

Configure with ``HERMES_TRUSTED_PROXIES`` (comma-separated IPs and/or
CIDRs) or ``dashboard.trusted_proxies`` in ``config.yaml``. The env var
wins when non-empty, matching the precedence used by
``dashboard_auth.prefix.resolve_public_url``.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from typing import Sequence, Tuple

_log = logging.getLogger(__name__)

TRUSTED_PROXIES_ENV = "HERMES_TRUSTED_PROXIES"

_Network = ipaddress._BaseNetwork  # type: ignore[attr-defined]

# Cache keyed by the raw configured string so a config change is picked
# up without a restart, but the (repeated, per-request) parse is not.
_cache_key: object = object()
_cache_value: Tuple[_Network, ...] = ()


def _parse_networks(raw: str) -> Tuple[_Network, ...]:
    """Parse a comma/whitespace separated list of IPs and CIDRs.

    Bare addresses become single-host networks. Unparseable entries are
    dropped with a warning rather than raising: a typo in one entry must
    not take down authentication, and dropping an entry can only make
    the check *stricter* (an unrecognised proxy is untrusted).
    """
    networks: list[_Network] = []
    for chunk in raw.replace(",", " ").split():
        try:
            networks.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            _log.warning(
                "dashboard-auth.client_ip: ignoring unparseable trusted "
                "proxy entry %r",
                chunk,
            )
    return tuple(networks)


def _configured_raw() -> str:
    """Return the raw trusted-proxy list from env, else config.yaml."""
    env_raw = os.environ.get(TRUSTED_PROXIES_ENV, "").strip()
    if env_raw:
        return env_raw
    try:
        from hermes_cli.dashboard_auth.prefix import _load_dashboard_section
    except Exception:  # noqa: BLE001 — config layer optional at import time
        return ""
    try:
        value = _load_dashboard_section().get("trusted_proxies", "")
    except Exception as exc:  # noqa: BLE001 — never fail auth on config IO
        _log.debug(
            "dashboard-auth.client_ip: reading dashboard.trusted_proxies "
            "raised %s; treating as unset",
            exc,
        )
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value or "")


def trusted_proxy_networks() -> Tuple[_Network, ...]:
    """Return the configured trusted proxy networks (possibly empty)."""
    global _cache_key, _cache_value
    raw = _configured_raw()
    if raw != _cache_key:
        _cache_value = _parse_networks(raw)
        _cache_key = raw
    return _cache_value


def reset_trusted_proxy_cache() -> None:
    """Test-only: drop the parsed trusted-proxy cache."""
    global _cache_key, _cache_value
    _cache_key = object()
    _cache_value = ()


def _coerce_address(raw: str):
    """Parse one chain entry into an address, or None if unusable.

    Tolerates the shapes real proxies emit: a bracketed IPv6 literal, a
    ``host:port`` pair, and surrounding whitespace. Returns ``None`` for
    anything that is not a literal IP — including the ``unknown`` and
    obfuscated identifiers RFC 7239 permits — so such an entry is
    treated as untrusted and terminates the walk.
    """
    text = raw.strip()
    if not text:
        return None
    if text.startswith("["):
        # "[::1]" or "[::1]:8080"
        text = text[1:].split("]", 1)[0]
    elif text.count(":") == 1:
        # IPv4 with a port; a bare IPv6 literal has more than one colon.
        text = text.split(":", 1)[0]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _is_trusted(raw: str, networks: Sequence[_Network]) -> bool:
    address = _coerce_address(raw)
    if address is None:
        return False
    return any(address in network for network in networks)


def resolve_client_ip(peer: str, forwarded_for: str) -> str:
    """Resolve the effective client IP from the peer and the XFF header.

    Split out from the request object so it can be unit-tested directly
    and shared by the FastAPI and aiohttp surfaces.
    """
    networks = trusted_proxy_networks()
    if not networks:
        # Rule 1: no declared proxies — the header has no authority.
        return peer
    if not _is_trusted(peer, networks):
        # Rule 2: this connection did not arrive through our proxy.
        return peer
    # Rule 3: walk right-to-left past our own proxies.
    entries = [chunk.strip() for chunk in forwarded_for.split(",") if chunk.strip()]
    for entry in reversed(entries):
        if not _is_trusted(entry, networks):
            address = _coerce_address(entry)
            # A syntactically invalid entry means the chain is not
            # trustworthy from here leftwards; stop rather than reaching
            # past it for something that looks nicer.
            return str(address) if address is not None else peer
    # Every entry was one of our own proxies (or the header was absent):
    # the peer really is the closest thing to a client we can name.
    return peer


def client_ip(request) -> str:
    """Return the effective client IP for ``request``.

    Accepts anything exposing Starlette's ``.headers`` mapping and
    ``.client.host``; aiohttp requests are handled by
    :func:`resolve_client_ip` directly.
    """
    peer = ""
    client = getattr(request, "client", None)
    if client is not None:
        peer = getattr(client, "host", "") or ""
    forwarded_for = request.headers.get("x-forwarded-for", "") or ""
    return resolve_client_ip(peer, forwarded_for)
