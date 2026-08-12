from __future__ import annotations

import pytest

from hermes_services.hosted_event_protocol import (
    append_hosted_event,
    hosted_event_page,
)


def _conversation() -> dict:
    return {
        "id": "chat-1",
        "hosted_events": [],
        "hosted_event_sequences": {},
        "hosted_event_terminals": {},
        "hosted_event_cursor": 0,
    }


def test_progress_is_idempotent_and_rejected_after_terminal():
    conversation = _conversation()
    first = append_hosted_event(
        conversation,
        conversation_id="chat-1",
        turn_id="turn-1",
        role_stage="worker",
        event_type="message.delta",
        entity_id="message-1",
        idempotency_key="delta-1",
        payload={"entity_id": "message-1", "content": "a"},
    )
    replay = append_hosted_event(
        conversation,
        conversation_id="chat-1",
        turn_id="turn-1",
        role_stage="worker",
        event_type="message.delta",
        entity_id="message-1",
        idempotency_key="delta-1",
        payload={"entity_id": "message-1", "content": "a"},
    )
    terminal = append_hosted_event(
        conversation,
        conversation_id="chat-1",
        turn_id="turn-1",
        role_stage="worker",
        event_type="message.completed",
        entity_id="message-1",
        idempotency_key="message-1-completed",
        payload={"entity_id": "message-1", "content": "answer"},
    )
    late = append_hosted_event(
        conversation,
        conversation_id="chat-1",
        turn_id="turn-1",
        role_stage="worker",
        event_type="message.delta",
        entity_id="message-1",
        idempotency_key="delta-late",
        payload={"entity_id": "message-1", "content": "late"},
    )

    assert first.appended is True
    assert replay.appended is False
    assert terminal.appended is True
    assert late.appended is False
    assert late.reason == "terminal:message.completed"
    assert conversation["hosted_event_cursor"] == 2


def test_page_reports_retention_gap_and_protocol_cursor():
    conversation = _conversation()
    conversation["hosted_events"] = [
        {"cursor": 7, "sequence": 1},
        {"cursor": 8, "sequence": 2},
    ]
    conversation["hosted_event_cursor"] = 8

    page = hosted_event_page(conversation, 1)

    assert page.has_gap is True
    assert page.min_cursor == 7
    assert page.next_cursor == 8
    assert [event["cursor"] for event in page.events] == [7, 8]


def test_page_explicitly_resets_a_cursor_beyond_the_authoritative_tail():
    conversation = _conversation()
    conversation["hosted_events"] = [{"cursor": 10, "sequence": 1}]
    conversation["hosted_event_cursor"] = 10

    page = hosted_event_page(conversation, 999)

    assert page.events == []
    assert page.next_cursor == 10
    assert page.has_gap is True
    assert page.reset_cursor is True
    assert page.reset_reason == "future_cursor"


def test_turn_completed_fences_all_late_progress_and_conflicting_terminal():
    conversation = _conversation()
    completed = append_hosted_event(
        conversation,
        conversation_id="chat-1",
        turn_id="turn-1",
        role_stage="reporter",
        event_type="turn.completed",
        idempotency_key="turn-terminal",
    )
    replay = append_hosted_event(
        conversation,
        conversation_id="chat-1",
        turn_id="turn-1",
        role_stage="reporter",
        event_type="turn.completed",
        idempotency_key="turn-terminal",
    )
    late_message = append_hosted_event(
        conversation,
        conversation_id="chat-1",
        turn_id="turn-1",
        role_stage="worker",
        event_type="message.delta",
        idempotency_key="late-message",
    )
    late_tool = append_hosted_event(
        conversation,
        conversation_id="chat-1",
        turn_id="turn-1",
        role_stage="worker",
        event_type="tool.started",
        entity_id="tool-1",
        idempotency_key="late-tool",
    )
    conflicting = append_hosted_event(
        conversation,
        conversation_id="chat-1",
        turn_id="turn-1",
        role_stage="reporter",
        event_type="turn.failed",
        idempotency_key="conflicting-terminal",
    )

    assert completed.appended is True
    assert replay.reason == "duplicate"
    assert late_message.reason == "turn_terminal:turn.completed"
    assert late_tool.reason == "turn_terminal:turn.completed"
    assert conflicting.reason == "turn_terminal:turn.completed"
    assert conversation["hosted_event_cursor"] == 1


def test_retained_turn_terminal_repairs_missing_index_before_late_event():
    conversation = _conversation()
    terminal = append_hosted_event(
        conversation,
        conversation_id="chat-1",
        turn_id="turn-index-repair",
        role_stage="chat",
        event_type="turn.completed",
        idempotency_key="turn-index-repair-terminal",
    )
    assert terminal.appended is True
    conversation.pop("hosted_event_terminals")

    late = append_hosted_event(
        conversation,
        conversation_id="chat-1",
        turn_id="turn-index-repair",
        role_stage="chat",
        event_type="message.delta",
        idempotency_key="turn-index-repair-late",
        payload={"entity_id": "message-1", "text": "late"},
    )

    assert late.appended is False
    assert late.reason == "turn_terminal:turn.completed"
    assert conversation["hosted_event_cursor"] == 1


@pytest.mark.parametrize(
    ("terminal_type", "progress_type", "payload_key"),
    [
        ("message.completed", "message.delta", "message_id"),
        ("thinking.completed", "thinking.delta", "thinking_id"),
        ("tool.completed", "tool.progress", "tool_id"),
        ("command.completed", "command.output", "command_id"),
    ],
)
def test_legacy_entity_terminal_repairs_missing_index(
    terminal_type,
    progress_type,
    payload_key,
):
    conversation = _conversation()
    terminal = append_hosted_event(
        conversation,
        conversation_id="chat-1",
        turn_id="turn-entity-repair",
        role_stage="worker",
        event_type=terminal_type,
        idempotency_key=f"terminal-{terminal_type}",
        payload={payload_key: "entity-123"},
    )
    assert terminal.appended is True
    assert terminal.event["entity_id"] == "entity-123"

    # Simulate an older retained envelope that carried the entity only in its
    # type-specific payload field and lost the derived terminal index.
    conversation["hosted_events"][0].pop("entity_id")
    conversation.pop("hosted_event_terminals")

    late = append_hosted_event(
        conversation,
        conversation_id="chat-1",
        turn_id="turn-entity-repair",
        role_stage="worker",
        event_type=progress_type,
        idempotency_key=f"late-{progress_type}",
        payload={payload_key: "entity-123"},
    )

    assert late.appended is False
    assert late.reason == f"terminal:{terminal_type}"
    assert conversation["hosted_event_cursor"] == 1


def test_turn_cancelled_fences_every_later_event():
    conversation = _conversation()
    append_hosted_event(
        conversation,
        conversation_id="chat-1",
        turn_id="turn-2",
        role_stage="chat",
        event_type="turn.cancelled",
        idempotency_key="cancelled",
    )

    for index, event_type in enumerate(
        ("thinking.started", "command.output", "role.handoff", "turn.completed"),
        start=1,
    ):
        result = append_hosted_event(
            conversation,
            conversation_id="chat-1",
            turn_id="turn-2",
            role_stage="worker",
            event_type=event_type,
            idempotency_key=f"late-{index}",
        )
        assert result.appended is False
        assert result.reason == "turn_terminal:turn.cancelled"
    assert conversation["hosted_event_cursor"] == 1


def test_mobile_interactive_card_event_types_are_registered():
    """awaiting/supervisor/rework card events must pass protocol validation.

    These are the only events the iOS client renders as interactive cards
    (decision card, verdict card, rework chips). If they are not registered
    the append raises instead of persisting, and the cards silently never
    appear — regression guard for the C-2 incident.
    """
    conversation = _conversation()
    for event_type in (
        "awaiting.choice",
        "supervisor.verdict",
        "rework.started",
        "rework.dispatched",
    ):
        result = append_hosted_event(
            conversation,
            conversation_id="chat-1",
            turn_id="turn-1",
            role_stage="worker",
            event_type=event_type,
            idempotency_key=f"card-{event_type}",
        )
        assert result.appended is True, event_type
    assert conversation["hosted_event_cursor"] == 4
