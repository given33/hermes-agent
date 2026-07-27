"""Shared constant-time secret comparison for every HTTP surface.

Hermes exposes three independent HTTP surfaces — ``hermes_cli/web_server.py``
(FastAPI), ``gateway/platforms/api_server.py`` (aiohttp) and
``tui_gateway/server.py`` (bespoke JSON-RPC) — and each grew its own copy of
"compare the presented credential against the configured one". The copies
were kept in agreement by hand, which the code itself admitted: the aiohttp
``_check_auth`` comment says its byte-encoding behaviour *"matches
web_server.py's dashboard-token check"*. Agreement-by-comment is exactly the
failure mode that produced the kanban dashboard outage, where a third-party
consumer's "bespoke check only understood ``_SESSION_TOKEN``" and broke every
gated deployment.

This module is the one implementation they share. It deliberately lives at
the repository root with **no imports beyond the standard library**, so any
package can use it without adding a cross-package dependency edge (see
``docs/architecture/layering.md`` — the six top-level packages already form a
dependency mesh and must not gain new edges).

Three rules, all of which a hand-rolled copy has historically got wrong
somewhere in this repo:

1. **Constant time.** ``hmac.compare_digest``, never ``==``/``!=``. A
   byte-wise early exit is a remote timing oracle that recovers the secret.
2. **Encode both sides.** ``compare_digest`` raises ``TypeError`` on a
   ``str`` containing non-ASCII, and the presented value is raw client
   input — an unencoded compare turns a stray non-ASCII byte into a 500
   instead of a clean 401.
3. **Fail closed on empty.** If the configured secret is missing,
   ``"" != ""`` is false, so a naive compare *authenticates a request that
   presents nothing*. An unconfigured secret must deny everything, never
   admit everyone.
"""

from __future__ import annotations

import hmac

__all__ = [
    "constant_time_equals",
    "extract_bearer_token",
    "bearer_matches",
]


def constant_time_equals(presented: str | bytes | None, expected: str | bytes | None) -> bool:
    """Constant-time comparison that fails closed on an empty side.

    Returns ``False`` whenever either value is empty or ``None`` — an
    unconfigured secret denies every request rather than accepting an
    empty credential. Both sides are UTF-8 encoded first so non-ASCII
    client input cannot raise.
    """
    if not presented or not expected:
        return False
    presented_bytes = presented.encode("utf-8") if isinstance(presented, str) else presented
    expected_bytes = expected.encode("utf-8") if isinstance(expected, str) else expected
    return hmac.compare_digest(presented_bytes, expected_bytes)


def extract_bearer_token(authorization_header: str | None) -> str:
    """Return the token from ``Authorization: Bearer <token>``, else ``""``.

    The scheme match is case-insensitive per RFC 7235. A missing,
    malformed or non-bearer header yields ``""``, which
    :func:`constant_time_equals` then rejects.
    """
    if not authorization_header:
        return ""
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].strip().lower() != "bearer":
        return ""
    return parts[1].strip()


def bearer_matches(authorization_header: str | None, expected: str | None) -> bool:
    """True if the header carries exactly the expected bearer credential."""
    return constant_time_equals(extract_bearer_token(authorization_header), expected)
