from __future__ import annotations

import random

import pytest

from hermes_runtime.capabilities import filter_tools_for_role, role_allows
from hermes_runtime.collaboration import (
    CollaborationDependency,
    DependencyGraph,
    MailboxMessage,
    append_mailbox,
    read_mailbox,
)
from hermes_runtime.evidence import EvidenceArtifact
from hermes_runtime.plugin_compatibility import PluginCompatibility, validate_plugin_compatibility
from hermes_runtime.session_trace import SessionTrace, TraceEvent
from hermes_runtime.tool_execution import (
    ToolExecutionLedger,
    ToolPresentationMeta,
    build_envelope,
    replay_projection,
)
from hermes_runtime.visual_evidence import VisualEvidenceRequest, invoke_visual_provider
from tools.registry import ToolRegistry


def test_tool_execution_ledger_is_parent_child_and_replay_safe():
    ledger = ToolExecutionLedger(max_records=32)
    parent = ledger.start(build_envelope(tool_name="execute_code", args={"code": "..."}, call_id="parent"))
    child = ledger.start(build_envelope(tool_name="read_file", args={"path": "x"}, call_id="child", parent_call_id="parent"))
    ledger.finish("child", "completed", result={"ok": True})
    ledger.finish("parent", "completed", result={"call_tree": ["child"]})
    assert child.call_id in parent.children
    projected = replay_projection(ledger.snapshot()[0])
    assert projected["presentation_meta"]["replayable"] is True
    assert "ok" not in str(projected)


def test_tool_execution_state_machine_rejects_terminal_reuse():
    envelope = build_envelope(tool_name="terminal", args={})
    envelope.transition("failed", error_type="timeout")
    finished_at = envelope.finished_at_ms
    envelope.transition("failed", result={"ignored": True})
    assert envelope.finished_at_ms == finished_at
    with pytest.raises(ValueError):
        envelope.transition("completed")


def test_role_policy_keeps_legacy_tools_but_hides_explicit_sensitive_capabilities():
    entries = [
        {"name": "read_file"},
        {"name": "execute_code", "capability_tags": {"process"}, "allowed_roles": {"dispatcher", "worker"}},
        {"name": "send_email", "capability_tags": {"external_emit"}},
    ]
    assert "read_file" in filter_tools_for_role(entries, "reporter")
    assert "execute_code" not in filter_tools_for_role(entries, "reviewer")
    assert "send_email" not in filter_tools_for_role(entries, "worker")
    assert role_allows("dispatcher", {"process"})


def test_role_policy_is_rechecked_at_dispatch_boundary(monkeypatch):
    import model_tools

    monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: object())
    monkeypatch.setattr(model_tools.registry, "get_tool_names_for_role", lambda role, names: set())
    monkeypatch.setattr(model_tools.registry, "dispatch", lambda *args, **kwargs: pytest.fail("hidden tool dispatched"))
    result = model_tools.handle_function_call("hidden_tool", {}, tool_role="reporter")
    assert "not available for role" in result


def test_dependency_graph_rejects_cycles_and_returns_ready_nodes():
    graph = DependencyGraph([
        CollaborationDependency("a"),
        CollaborationDependency("b", requires=("a",)),
        CollaborationDependency("c", requires=("b",)),
    ])
    assert graph.ready() == ["a"]
    assert graph.ready({"a", "b"}) == ["c"]
    succeeded_graph = DependencyGraph([
        CollaborationDependency("a", state="succeeded"),
        CollaborationDependency("b", requires=("a",)),
    ])
    assert succeeded_graph.ready() == ["b"]
    with pytest.raises(ValueError):
        DependencyGraph([CollaborationDependency("a"), CollaborationDependency("a")])
    with pytest.raises(ValueError):
        DependencyGraph([
            CollaborationDependency("a", requires=("b",)),
            CollaborationDependency("b", requires=("a",)),
        ])


def test_mailbox_is_idempotent_and_generation_bound():
    messages: list[dict] = []
    message = MailboxMessage("worker", "reviewer", {"artifact": "x"}, account_generation="g1")
    first = append_mailbox(messages, message)
    second = append_mailbox(messages, message)
    assert first == second
    assert len(read_mailbox(messages, "reviewer", "g1")) == 1
    assert read_mailbox(messages, "reviewer", "g2") == []


def test_session_trace_requires_exact_workspace_and_generation_boundary():
    trace = SessionTrace(max_events=4)
    trace.append(TraceEvent("workspace-a", "gen-1", "tool.completed", {"call_id": "c1", "prompt": "secret prompt", "result": {"raw": "secret"}}))
    record = trace.read(workspace_id="workspace-a", account_generation="gen-1")[0]
    assert record["payload"] == {"call_id": "c1"}
    assert trace.read(workspace_id="workspace-b", account_generation="gen-1") == []
    assert trace.search("completed", workspace_id="workspace-a", account_generation="gen-1")


def test_visual_evidence_is_digest_only_and_validates_kind():
    class Provider:
        def analyze(self, request):
            return {"provider_id": "vision-sidecar", "provider_generation": 2, "result": {"text": "hello"}, "evidence_refs": ["artifact://screen"], "sensitive": True}

    artifact = invoke_visual_provider(Provider(), VisualEvidenceRequest("ocr", "artifact://screen"))
    assert artifact.digest
    assert "hello" not in str(artifact.as_dict())
    with pytest.raises(ValueError):
        VisualEvidenceRequest("arbitrary", "artifact://screen")


def test_plugin_compatibility_flags_missing_rollback_and_license():
    item = PluginCompatibility("vision", "1.0", "sha256:abc", "AGPL-3.0", rollback_supported=False)
    issues = validate_plugin_compatibility(item, allowed_licenses={"mit"})
    assert "license is not allowlisted" in issues
    assert "unload rollback is not supported" in issues


def test_registry_exposes_capability_contract_and_fingerprint_changes():
    registry = ToolRegistry()
    schema = {"name": "demo", "description": "demo", "parameters": {"type": "object"}}
    registry.register(
        "demo", "demo", schema, lambda args, **kwargs: "ok",
        capability_tags={"read_fs"}, allowed_roles={"reviewer"},
    )
    assert registry.get_tool_names_for_role("reviewer") == {"demo"}
    assert registry.get_tool_names_for_role("reporter") == set()
    assert registry.get_capability_metadata({"demo"})["demo"]["capability_tags"] == ["read_fs"]
    before = registry.registration_fingerprint("demo")
    registry.register(
        "demo", "demo", schema, lambda args, **kwargs: "ok",
        override=True, capability_tags={"write_fs"}, allowed_roles={"reviewer"},
    )
    assert registry.registration_fingerprint("demo") != before
