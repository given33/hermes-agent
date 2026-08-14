from __future__ import annotations

import pytest

from hermes_services.hosted_event_protocol import (
    HostedEventProtocolError,
    append_hosted_event,
    validate_event_envelope,
)


def test_runtime_metadata_is_append_only_and_typed() -> None:
    conversation = {
        "id": "chat-runtime",
        "hosted_events": [],
        "hosted_event_sequences": {},
        "hosted_event_terminals": {},
        "hosted_event_cursor": 0,
    }
    result = append_hosted_event(
        conversation,
        conversation_id="chat-runtime",
        turn_id="turn-runtime",
        role_stage="worker",
        event_type="component.active",
        idempotency_key="component-active-1",
        component_id="fiber:worker",
        parent_component_id="fiber:turn",
        provider_refs=("connector:primary",),
        dependency_state={"mcp:search": "satisfied"},
        lifecycle_state="active",
        effect_scope_id="scope:worker",
        plan_node_id="node:worker",
        artifact_refs=("artifact:report",),
        contract_revision="turn-plan:3",
        policy_snapshot_hash="policy:abc",
    )
    envelope = validate_event_envelope(result.event)
    assert envelope["runtime"]["component_id"] == "fiber:worker"
    assert envelope["runtime"]["provider_refs"] == ["connector:primary"]
    assert envelope["runtime"]["dependency_state"] == {"mcp:search": "satisfied"}


def test_runtime_metadata_rejects_untyped_fields() -> None:
    with pytest.raises(HostedEventProtocolError, match="runtime.component_id"):
        validate_event_envelope(
            {
                "event_id": "evt-1",
                "cursor": 1,
                "account_generation": "g1",
                "conversation_id": "chat-1",
                "turn_id": "turn-1",
                "role_stage": "worker",
                "event_type": "component.active",
                "sequence": 1,
                "occurred_at": 1,
                "idempotency_key": "k1",
                "payload": {},
                "schema_version": "hermes.hosted-event.v1",
                "runtime": {"component_id": 12},
            }
        )


def test_runtime_metadata_rejects_illegal_component_transition() -> None:
    conversation = {
        "id": "chat-runtime",
        "hosted_events": [],
        "hosted_event_sequences": {},
        "hosted_event_terminals": {},
        "hosted_event_cursor": 0,
    }
    append_hosted_event(
        conversation,
        conversation_id="chat-runtime",
        turn_id="turn-runtime",
        role_stage="worker",
        event_type="component.active",
        idempotency_key="active-1",
        component_id="fiber:worker",
        lifecycle_state="active",
    )
    with pytest.raises(HostedEventProtocolError, match="illegal lifecycle transition"):
        append_hosted_event(
            conversation,
            conversation_id="chat-runtime",
            turn_id="turn-runtime",
            role_stage="worker",
            event_type="component.activating",
            idempotency_key="activating-2",
            component_id="fiber:worker",
            lifecycle_state="activating",
        )


def test_runtime_metadata_allows_recovering_after_process_failure() -> None:
    conversation = {
        "id": "chat-runtime",
        "hosted_events": [],
        "hosted_event_sequences": {},
        "hosted_event_terminals": {},
        "hosted_event_cursor": 0,
    }
    append_hosted_event(
        conversation,
        conversation_id="chat-runtime",
        turn_id="turn-runtime",
        role_stage="worker",
        event_type="component.active",
        idempotency_key="active-1",
        component_id="fiber:worker",
        lifecycle_state="active",
    )
    recovered = append_hosted_event(
        conversation,
        conversation_id="chat-runtime",
        turn_id="turn-runtime",
        role_stage="worker",
        event_type="component.activating",
        idempotency_key="recovering-2",
        component_id="fiber:worker",
        lifecycle_state="recovering",
    )
    assert recovered.appended is True


def test_runtime_boundary_redacts_credentials_and_host_paths() -> None:
    conversation = {"id": "chat-runtime"}
    result = append_hosted_event(
        conversation,
        conversation_id="chat-runtime",
        turn_id="turn-runtime",
        role_stage="worker",
        event_type="component.active",
        idempotency_key="redaction-1",
        payload={"api_key": "raw-secret", "error": "Bearer abc.def"},
        component_id="fiber:worker",
        lifecycle_state="active",
        dependency_state={"access_token": "raw-token", "mcp": "ready"},
        artifact_refs=(r"C:\Users\given\private-report.md",),
    )
    event = result.event
    assert event["payload"]["api_key"] == "[REDACTED]"
    assert event["payload"]["error"] == "Bearer [REDACTED]"
    assert event["runtime"]["dependency_state"]["access_token"] == "[REDACTED]"
    assert event["runtime"]["artifact_refs"][0].startswith("artifact:")
    assert "private-report" not in event["runtime"]["artifact_refs"][0]


def test_runtime_envelope_rejects_fields_outside_the_allowlist() -> None:
    envelope = append_hosted_event(
        {},
        conversation_id="chat-runtime",
        turn_id="turn-runtime",
        role_stage="worker",
        event_type="component.active",
        idempotency_key="allowlist-1",
        component_id="fiber:worker",
        lifecycle_state="active",
    ).event
    envelope["runtime"]["raw_prompt"] = "do not persist"
    with pytest.raises(HostedEventProtocolError, match="unsupported field"):
        validate_event_envelope(envelope)


def test_duplicate_preterminal_lifecycle_event_is_a_noop_after_completion() -> None:
    conversation = {"id": "chat-runtime"}
    active = append_hosted_event(
        conversation,
        conversation_id="chat-runtime",
        turn_id="turn-runtime",
        role_stage="worker",
        event_type="component.active",
        idempotency_key="active-1",
        component_id="fiber:worker",
        lifecycle_state="active",
    )
    append_hosted_event(
        conversation,
        conversation_id="chat-runtime",
        turn_id="turn-runtime",
        role_stage="worker",
        event_type="component.completed",
        idempotency_key="completed-1",
        component_id="fiber:worker",
        lifecycle_state="completed",
    )
    duplicate = append_hosted_event(
        conversation,
        conversation_id="chat-runtime",
        turn_id="turn-runtime",
        role_stage="worker",
        event_type="component.active",
        idempotency_key="active-1",
        component_id="fiber:worker",
        lifecycle_state="active",
    )
    assert duplicate.appended is False
    assert duplicate.reason == "duplicate"
    assert duplicate.event["event_id"] == active.event["event_id"]


def test_component_terminal_index_does_not_fence_turn_terminal() -> None:
    conversation = {"id": "chat-runtime"}
    append_hosted_event(
        conversation,
        conversation_id="chat-runtime",
        turn_id="turn-runtime",
        role_stage="worker",
        event_type="component.active",
        idempotency_key="component-active-2",
        component_id="fiber:worker",
        lifecycle_state="active",
    )
    component = append_hosted_event(
        conversation,
        conversation_id="chat-runtime",
        turn_id="turn-runtime",
        role_stage="worker",
        event_type="component.completed",
        idempotency_key="component-completed-2",
        component_id="fiber:worker",
        lifecycle_state="completed",
    )
    turn = append_hosted_event(
        conversation,
        conversation_id="chat-runtime",
        turn_id="turn-runtime",
        role_stage="worker",
        event_type="turn.completed",
        idempotency_key="turn-completed-2",
    )
    assert component.appended is True
    assert turn.appended is True
    assert conversation["hosted_event_terminals"]["turn:turn-runtime"] == "turn.completed"
