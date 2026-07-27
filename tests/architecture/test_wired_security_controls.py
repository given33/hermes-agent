"""Ratchet (d): security controls must be wired — or loudly marked UNWIRED.

The audit's worst pattern was not a missing control but a control that
*exists and is silently unused*: a reviewer greps, finds the symbol, and
concludes the property holds, while nothing ever calls it. This test keeps a
registry of security-relevant symbols in one of two honest states:

- ``wired``   — at least one NON-TEST call site actually uses the symbol
  (resolved via AST by ``archlint.find_any_symbol_references``; a dangling
  import does not count as wiring). Both cross-module callers and call sites
  inside the defining module count: a module-private guard called only by its
  siblings is wired, and judging it by imports alone would call it dead.
- ``unwired`` — ZERO non-test call sites, AND the gap is declared with a
  greppable ``UNWIRED`` marker either in the symbol's own docstring or in a
  module docstring that names the symbol. Nobody can mistake it for active.

Failure modes and their fixes:

- A ``wired`` control loses its last caller  -> the control was silently
  disabled; re-wire it, or (if genuinely deliberate) add the UNWIRED
  docstring marker and flip the registry entry in the same change.
- An ``unwired`` control gains a caller      -> good news; remove the
  UNWIRED marker and flip the registry entry to ``wired`` so the ratchet
  now protects the new call site.
- An ``unwired`` control loses its marker    -> the gap became invisible
  again; restore the marker (see gateway/relay/auth.py for the shape).

The registry is seeded from measured reality at the time of writing (all
verified via ``archlint.find_symbol_references``), not from aspiration —
see each entry's comment for where its callers live.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Tuple

import pytest

from tests.architecture import archlint


@dataclass(frozen=True)
class Control:
    module: str            # dotted module path, e.g. "gateway.relay.auth"
    symbol: str            # the security-relevant callable/constant
    expected: str          # "wired" | "unwired"
    why: str               # one line: what property this symbol enforces


REGISTRY: Tuple[Control, ...] = (
    # ── gateway/relay/auth.py — connector⇄gateway HMAC schemes ──────────────
    Control(
        module="gateway.relay.auth",
        symbol="verify_delivery_signature",
        expected="unwired",
        # No inbound HTTP delivery route exists yet; events arrive over the
        # upgrade-authenticated WS. Whoever adds the HTTP endpoint MUST call
        # this and flip the entry to "wired" (module docstring, scheme 2).
        why="inbound connector→gateway delivery HMAC (reserved, no route yet)",
    ),
    Control(
        module="gateway.relay.auth",
        symbol="verify_token",
        expected="unwired",
        # Deliberately caller-less in Python: the gateway only MINTS the
        # upgrade token; the connector's TypeScript verifies it. Exists to
        # mirror the wire contract for conformance tests (module docstring).
        why="upgrade-token verify half; connector-side verification mirrors it",
    ),
    Control(
        module="gateway.relay.auth",
        symbol="make_upgrade_token",
        expected="wired",
        # Callers: gateway/relay/ws_transport.py, gateway/relay/__init__.py.
        why="WS-upgrade bearer token minting for the /relay connection",
    ),
    # ── utils.py — secret-file hygiene ───────────────────────────────────────
    Control(
        module="utils",
        symbol="write_secret_file",
        expected="wired",
        # Callers: plugins/memory/hindsight/__init__.py,
        # plugins/memory/mem0/_setup.py. TOCTOU-safe 0o600-from-birth writes.
        why="secrets are never on disk with looser than owner-only perms",
    ),
    # ── approval / redaction gates ───────────────────────────────────────────
    Control(
        module="tools.write_approval",
        symbol="write_approval_enabled",
        expected="wired",
        # Callers: gateway/slash_commands.py,
        # hermes_cli/write_approval_commands.py.
        why="gates whether account-write operations require human approval",
    ),
    Control(
        module="hermes_runtime.redaction",
        symbol="redact_sensitive_text",
        expected="wired",
        # 30+ callers across agent/, gateway/, hermes_cli/, plugins/.
        why="strips secrets from text before display/logging/upload",
    ),
    # ── controls added by the audit remediation ──────────────────────────────
    # Registered here so a later refactor cannot quietly drop the call site and
    # leave the symbol behind — which is the exact shape of every "declared but
    # not wired" finding the audit raised.
    Control(
        module="hermes_cli.dashboard_auth.client_ip",
        symbol="client_ip",
        expected="wired",
        # Callers: dashboard_auth/{middleware,routes,token_auth}.py. Thin
        # request-scoped wrapper over resolve_client_ip.
        why="rate-limit/audit identity comes from the trusted-proxy walk, not raw XFF",
    ),
    Control(
        module="hermes_cli.dashboard_auth.client_ip",
        symbol="resolve_client_ip",
        expected="wired",
        # Internal caller: client_ip() at client_ip.py:192. Zero cross-module
        # callers BY DESIGN — the wrapper is the public surface. Only visible
        # via find_internal_symbol_references; this entry is why that exists.
        why="right-to-left XFF walk; ignores XFF entirely unless proxies are configured",
    ),
    Control(
        module="hermes_secret_compare",
        symbol="constant_time_equals",
        expected="wired",
        # Callers: gateway/platforms/bluebubbles.py, hermes_cli/web_server.py.
        why="secret comparison cannot leak length/prefix through timing",
    ),
    Control(
        module="hermes_secret_compare",
        symbol="bearer_matches",
        expected="wired",
        # Callers: gateway/platforms/api_server.py, hermes_cli/web_server.py.
        why="bearer-token check is constant-time and fails closed on an empty secret",
    ),
    Control(
        module="hermes_cli.account_write_approvals",
        symbol="_derive_payload_summary",
        expected="wired",
        # Internal caller: account_write_approvals.py:338 (the recorder).
        why="approval UI can show a summary derived from the payload, not agent prose",
    ),
    Control(
        module="plugins.platforms.slack.adapter",
        symbol="_check_slack_download_url",
        expected="wired",
        # Internal callers: slack/adapter.py:4743, :4810 (both download paths).
        why="pins Slack download hosts so the bot token is never sent off-host (SSRF)",
    ),
    Control(
        module="agent.auxiliary_client",
        symbol="neuter_async_httpx_del",
        expected="wired",
        # Internal caller: auxiliary_client.py:4663, in the async client
        # factory. This one shipped with ZERO call sites — the eviction fix
        # above it was unsafe in gateway processes until it was wired.
        why="defuses httpx __del__ so client eviction cannot raise on a dead loop",
    ),
    Control(
        module="hermes_runtime.config",
        symbol="read_raw_config_strict",
        expected="wired",
        # Callers: gateway/slash_commands.py (both /model global-persist sites),
        # plus hermes_cli/config.py:_persist_migration internally.
        why="read-modify-write refuses a corrupt config instead of replacing it",
    ),
    Control(
        module="hermes_runtime.config",
        symbol="require_readable_config_before_write",
        expected="wired",
        # Callers: hermes_cli/{auth,credential_lifecycle,xai_retirement}.py,
        # plus atomic_config_write internally.
        why="never overwrite a config.yaml that exists but cannot be read",
    ),
)


def _module_source_file(module: str):
    path = archlint.REPO_ROOT / (module.replace(".", "/") + ".py")
    if not path.exists():
        path = archlint.REPO_ROOT / module.replace(".", "/") / "__init__.py"
    return path if path.exists() else None


def _symbol_exists(module: str, symbol: str) -> bool:
    """True when ``module`` defines ``symbol`` (def/class/assignment). AST,
    not import — importing gateway/agent modules pulls heavy dependencies."""
    path = _module_source_file(module)
    if path is None:
        return False
    tree = archlint.parse_file(path)
    if tree is None:
        return False
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) and node.name == symbol:
            return True
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == symbol:
                    return True
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == symbol:
                return True
    return False


def _has_unwired_marker(module: str, symbol: str) -> bool:
    """Greppable declaration of the gap: 'UNWIRED' in the symbol's own
    docstring, or in a module docstring that names the symbol (the
    gateway/relay/auth.py shape, where one module docstring narrates the
    status of several related symbols)."""
    fn_doc, mod_doc = archlint.symbol_docstrings(module, symbol)
    if fn_doc and "UNWIRED" in fn_doc:
        return True
    return bool(mod_doc and "UNWIRED" in mod_doc and symbol in mod_doc)


@pytest.mark.parametrize(
    "control", REGISTRY, ids=[f"{c.module}.{c.symbol}" for c in REGISTRY]
)
def test_security_control_state_matches_registry(control: Control):
    assert _symbol_exists(control.module, control.symbol), (
        f"{control.module}.{control.symbol} no longer exists. It enforced: "
        f"{control.why}. If it moved, update this registry entry; if it was "
        "removed, its property is now unenforced — that needs a deliberate "
        "decision, not a silent deletion."
    )

    # Cross-module AND internal call sites: a module-private guard invoked
    # only by its own siblings (the SSRF pin, the confused-deputy summary) is
    # every bit as wired as a public one, and counting only imports would
    # report it dead. See archlint.find_internal_symbol_references.
    refs = archlint.find_any_symbol_references(control.module, control.symbol)

    if control.expected == "wired":
        assert refs, (
            f"{control.module}.{control.symbol} is registered WIRED but now "
            f"has ZERO non-test call sites — the control ({control.why}) has "
            "been silently disabled. Re-wire it, or add an 'UNWIRED' "
            "docstring marker and flip this registry entry to 'unwired' in "
            "the same change so the gap stays visible."
        )
    else:
        assert control.expected == "unwired", (
            f"registry bug: unknown expected state {control.expected!r}"
        )
        assert not refs, (
            f"{control.module}.{control.symbol} is registered UNWIRED but "
            f"now has call site(s): {', '.join(refs)}. Good — now make it "
            "official: remove the UNWIRED docstring marker and flip this "
            "registry entry to 'wired' so the ratchet protects the new "
            "call site."
        )
        assert _has_unwired_marker(control.module, control.symbol), (
            f"{control.module}.{control.symbol} has no callers AND no "
            "greppable 'UNWIRED' docstring marker — a reviewer grepping for "
            "the symbol will assume it is active. Add the marker (see "
            "gateway/relay/auth.py for the shape) or wire it."
        )


def test_internal_reference_detection_ignores_prose_mentions():
    """Load-bearing property: only real ``ast.Name`` loads count as wiring.

    Every UNWIRED entry declares its gap in a docstring that NAMES the symbol.
    If the detector ever degraded to a text search, those docstrings would
    satisfy themselves — every unwired control would report "wired" and the
    ratchet would certify exactly the dead controls it exists to catch. So
    pin the distinction rather than trusting it.

    ``agent.auxiliary_client`` is the natural fixture: it names
    ``neuter_async_httpx_del`` in two prose comments/docstrings (around lines
    3414 and 6081) and calls it exactly once (4663).
    """
    refs = archlint.find_internal_symbol_references(
        "agent.auxiliary_client", "neuter_async_httpx_del"
    )
    assert len(refs) == 1, (
        f"expected exactly the one real call site, got {refs} — if this grew, "
        "the detector started counting prose (or a genuine second call site "
        "appeared, in which case update this test)"
    )

    # The UNWIRED controls must stay at zero even though their own docstrings
    # spell their names out repeatedly.
    for module, symbol in (
        ("gateway.relay.auth", "verify_delivery_signature"),
        ("gateway.relay.auth", "verify_token"),
    ):
        doc, mod_doc = archlint.symbol_docstrings(module, symbol)
        assert (doc and symbol in doc) or (mod_doc and symbol in mod_doc), (
            f"{module}.{symbol} no longer names itself in its own docs, so "
            "this test is no longer exercising the prose-vs-code distinction"
        )
        assert not archlint.find_any_symbol_references(module, symbol), (
            f"{module}.{symbol} is documented-but-unwired, yet the detector "
            "reported a call site — it is counting prose"
        )


def test_registry_covers_relay_auth_delivery_headers():
    """The two delivery-signature header constants share scheme 2's fate:
    they are meaningless unless ``verify_delivery_signature`` is wired, and
    the module docstring's UNWIRED block must keep naming them so a grep for
    either header name lands on the declaration of the gap."""
    _, mod_doc = archlint.symbol_docstrings(
        "gateway.relay.auth", "verify_delivery_signature"
    )
    assert mod_doc and "UNWIRED" in mod_doc, (
        "gateway/relay/auth.py module docstring lost its UNWIRED block"
    )
    for const in ("DELIVERY_TS_HEADER", "DELIVERY_SIG_HEADER"):
        assert const in mod_doc, (
            f"gateway/relay/auth.py UNWIRED module docstring no longer names "
            f"{const}; keep the header constants listed so grepping the "
            "header name reveals that nothing verifies it yet."
        )
