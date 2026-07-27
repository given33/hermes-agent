"""Trusted-proxy-aware client IP resolution.

Regression cover for the login rate-limit bypass: ``_client_ip`` used to
return ``X-Forwarded-For``'s **leftmost** element unconditionally, so an
attacker could send a different forged value on every request, land each
password guess in a fresh rate-limit bucket, and brute-force the
dashboard password without limit. The same value keys every audit log
entry, so it could also be used to attribute an attack to an arbitrary
address.

The leftmost element is specifically the wrong end: with the standard
nginx idiom ``proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for``
the header is appended to, so the client's own bytes stay on the left and
the proxy's observation of the real peer lands on the right.
"""

from __future__ import annotations

import pytest

from hermes_cli.dashboard_auth.client_ip import (
    TRUSTED_PROXIES_ENV,
    reset_trusted_proxy_cache,
    resolve_client_ip,
)


@pytest.fixture
def proxies(monkeypatch):
    """Set the trusted-proxy list for one test and drop the parse cache."""

    def _apply(value: str) -> None:
        monkeypatch.setenv(TRUSTED_PROXIES_ENV, value)
        reset_trusted_proxy_cache()

    _apply("")
    yield _apply
    reset_trusted_proxy_cache()


class TestNoTrustedProxies:
    """Default posture: the header carries no authority at all."""

    def test_forwarded_for_is_ignored(self, proxies):
        # THE fix. Previously returned "1.2.3.4".
        assert resolve_client_ip("203.0.113.9", "1.2.3.4") == "203.0.113.9"

    def test_each_forged_value_resolves_to_the_same_peer(self, proxies):
        # The bypass worked by getting a distinct rate-limit key per
        # request. Every forgery must now collapse to one key.
        forged = [f"10.{n}.{n}.{n}" for n in range(1, 20)]
        resolved = {resolve_client_ip("203.0.113.9", value) for value in forged}
        assert resolved == {"203.0.113.9"}

    def test_missing_peer_yields_empty(self, proxies):
        assert resolve_client_ip("", "1.2.3.4") == ""


class TestWithTrustedProxies:
    def test_peer_not_a_trusted_proxy_ignores_header(self, proxies):
        # Direct connection from an attacker: the header is theirs, so it
        # must not be honoured even though proxies are configured.
        proxies("10.0.0.0/8")
        assert resolve_client_ip("203.0.113.9", "9.9.9.9, 8.8.8.8") == "203.0.113.9"

    def test_rightmost_untrusted_entry_wins(self, proxies):
        # Append semantics: real client first, then each hop. Reading the
        # right end is what makes the value trustworthy.
        proxies("10.0.0.0/8")
        assert (
            resolve_client_ip("10.0.0.1", "1.2.3.4, 203.0.113.9") == "203.0.113.9"
        )

    def test_client_cannot_prepend_a_forgery(self, proxies):
        # Attacker sends "X-Forwarded-For: 66.66.66.66"; the edge proxy
        # appends what it actually saw. The forgery sits to the left and
        # is never reached.
        proxies("10.0.0.0/8")
        assert (
            resolve_client_ip("10.0.0.1", "66.66.66.66, 203.0.113.9")
            == "203.0.113.9"
        )

    def test_chained_trusted_hops_are_skipped(self, proxies):
        proxies("10.0.0.0/8")
        assert (
            resolve_client_ip("10.0.0.1", "203.0.113.9, 10.0.0.5, 10.0.0.6")
            == "203.0.113.9"
        )

    def test_all_entries_trusted_falls_back_to_peer(self, proxies):
        proxies("10.0.0.0/8")
        assert resolve_client_ip("10.0.0.1", "10.0.0.5") == "10.0.0.1"

    def test_absent_header_falls_back_to_peer(self, proxies):
        proxies("10.0.0.0/8")
        assert resolve_client_ip("10.0.0.1", "") == "10.0.0.1"

    def test_unparseable_entry_stops_the_walk(self, proxies):
        # RFC 7239 permits "unknown" and obfuscated identifiers. We must
        # not step past one to grab a nicer-looking value further left,
        # because that value is exactly what an attacker would plant.
        proxies("10.0.0.0/8")
        assert resolve_client_ip("10.0.0.1", "203.0.113.9, unknown") == "10.0.0.1"


class TestAddressShapes:
    def test_ipv4_port_is_stripped(self, proxies):
        proxies("10.0.0.0/8")
        assert resolve_client_ip("10.0.0.1", "203.0.113.9:41234") == "203.0.113.9"

    def test_bracketed_ipv6_with_port(self, proxies):
        proxies("10.0.0.0/8")
        assert resolve_client_ip("10.0.0.1", "[2001:db8::1]:443") == "2001:db8::1"

    def test_bare_ipv6_is_not_mistaken_for_host_port(self, proxies):
        proxies("10.0.0.0/8")
        assert resolve_client_ip("10.0.0.1", "2001:db8::1") == "2001:db8::1"

    def test_ipv6_trusted_network(self, proxies):
        proxies("2001:db8::/32")
        assert resolve_client_ip("2001:db8::1", "203.0.113.9") == "203.0.113.9"

    def test_single_host_entry_without_cidr(self, proxies):
        proxies("10.0.0.1")
        assert resolve_client_ip("10.0.0.1", "203.0.113.9") == "203.0.113.9"
        assert resolve_client_ip("10.0.0.2", "203.0.113.9") == "10.0.0.2"

    def test_unparseable_config_entry_is_dropped_not_fatal(self, proxies):
        # A typo must not take down authentication, and dropping an entry
        # can only make the check stricter.
        proxies("not-an-ip, 10.0.0.0/8")
        assert resolve_client_ip("10.0.0.1", "203.0.113.9") == "203.0.113.9"

    def test_whitespace_and_mixed_separators(self, proxies):
        proxies("10.0.0.0/8  192.168.0.0/16")
        assert (
            resolve_client_ip("192.168.1.1", "203.0.113.9, 10.0.0.5")
            == "203.0.113.9"
        )
