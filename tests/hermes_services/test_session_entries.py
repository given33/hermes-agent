from __future__ import annotations

import json

from hermes_services.session_entries import (
    MAX_MESSAGE_DELTA_CHARS,
    SCHEMA_VERSION,
    append_message_stream_entries,
    append_session_entry,
    branch_entries,
    entries_after,
    normalize_session_entries,
)


def _entry(entry_id: str, cursor: int, parent: str = "") -> dict:
    return {
        "entry_id": entry_id,
        "cursor": cursor,
        "parent_entry_id": parent or None,
        "entry_type": "message",
        "occurred_at": cursor,
        "idempotency_key": f"key-{entry_id}",
        "payload": {"message_id": entry_id},
        "schema_version": SCHEMA_VERSION,
    }


def test_malformed_entry_is_isolated_without_losing_later_reachable_history():
    conversation = {
        "session_entries": [
            _entry("entry-1", 1),
            {
                **_entry("broken", 2, "missing-parent"),
                "schema_version": "unknown",
            },
            _entry("entry-3", 3, "entry-1"),
        ],
        "session_entry_cursor": 999,
        "session_entry_leaf_id": "broken",
    }

    diagnostics = normalize_session_entries(conversation)

    assert [item["entry_id"] for item in conversation["session_entries"]] == [
        "entry-1",
        "entry-3",
    ]
    assert conversation["session_entry_cursor"] == 3
    assert conversation["session_entry_leaf_id"] == "entry-3"
    assert diagnostics == [{"index": 1, "reason": "unsupported session entry schema"}]
    assert conversation["session_entry_quarantine"][0]["raw"]["entry_id"] == "broken"
    assert [item["entry_id"] for item in entries_after(conversation, 1)] == [
        "entry-3"
    ]


def test_entry_referencing_an_isolated_parent_is_also_isolated():
    conversation = {
        "session_entries": [
            _entry("entry-1", 1),
            {**_entry("broken", 2, "entry-1"), "payload": "invalid"},
            _entry("entry-3", 3, "broken"),
        ]
    }

    diagnostics = normalize_session_entries(conversation)

    assert [item["entry_id"] for item in conversation["session_entries"]] == [
        "entry-1"
    ]
    assert [item["index"] for item in diagnostics] == [1, 2]
    assert len(conversation["session_entry_quarantine"]) == 2


def test_quarantined_raw_entry_survives_save_and_restart_without_duplication():
    conversation = {"session_entries": [_entry("entry-1", 1), {"broken": True}]}
    normalize_session_entries(conversation)

    restarted = json.loads(json.dumps(conversation))
    normalize_session_entries(restarted)

    assert [item["entry_id"] for item in restarted["session_entries"]] == ["entry-1"]
    assert len(restarted["session_entry_quarantine"]) == 1
    assert restarted["session_entry_quarantine"][0]["raw"] == {"broken": True}


def test_append_cursor_and_incremental_reads_are_monotonic():
    conversation = {}
    first, first_added = append_session_entry(
        conversation, entry_type="message", payload={"text": "one"}, idempotency_key="one"
    )
    second, second_added = append_session_entry(
        conversation, entry_type="message", payload={"text": "two"}, idempotency_key="two"
    )

    assert first_added and second_added
    assert [item["cursor"] for item in entries_after(conversation, 0)] == [1, 2]
    assert [item["entry_id"] for item in entries_after(conversation, 1)] == [second["entry_id"]]
    assert conversation["session_entry_leaf_id"] == second["entry_id"]
    assert first["cursor"] == 1


def test_compaction_is_an_append_only_entry_and_does_not_remove_history():
    conversation = {}
    message, _ = append_session_entry(
        conversation, entry_type="message", payload={"text": "large history"}
    )
    compacted, _ = append_session_entry(
        conversation,
        entry_type="compaction",
        payload={"summary": "bounded model context", "through_cursor": message["cursor"]},
    )

    assert [item["entry_type"] for item in conversation["session_entries"]] == [
        "message",
        "compaction",
    ]
    assert compacted["parent_entry_id"] == message["entry_id"]


def test_branch_materialization_preserves_root_to_selected_leaf():
    conversation = {}
    root, _ = append_session_entry(conversation, entry_type="message", payload={"text": "root"})
    left, _ = append_session_entry(
        conversation, entry_type="message", payload={"text": "left"}, parent_entry_id=root["entry_id"]
    )
    right, _ = append_session_entry(
        conversation, entry_type="message", payload={"text": "right"}, parent_entry_id=root["entry_id"]
    )

    assert [item["entry_id"] for item in branch_entries(conversation, from_entry_id=left["entry_id"])] == [
        root["entry_id"],
        left["entry_id"],
    ]
    assert right["entry_id"] in {item["entry_id"] for item in conversation["session_entries"]}


def test_multi_client_idempotency_replays_same_entry_without_advancing_cursor():
    conversation = {}
    first, added = append_session_entry(
        conversation,
        entry_type="intervention",
        payload={"text": "steer"},
        idempotency_key="client-operation-42",
    )
    replay, replay_added = append_session_entry(
        conversation,
        entry_type="intervention",
        payload={"text": "steer"},
        idempotency_key="client-operation-42",
    )

    assert added is True
    assert replay_added is False
    assert replay == first
    assert conversation["session_entry_cursor"] == 1


def test_streamed_message_uses_bounded_suffix_chunks_and_one_final_replacement():
    conversation = {}
    first = "a" * (MAX_MESSAGE_DELTA_CHARS + 17)
    second = first + ("b" * (MAX_MESSAGE_DELTA_CHARS + 23))

    append_message_stream_entries(
        conversation,
        message_id="message-1",
        previous_content="",
        current_content=first,
        status="streaming",
        role="assistant",
        name="Hermes",
        kind="message",
        turn_id="turn-1",
        role_stage="worker",
    )
    append_message_stream_entries(
        conversation,
        message_id="message-1",
        previous_content=first,
        current_content=second,
        status="streaming",
        role="assistant",
        name="Hermes",
        kind="message",
        turn_id="turn-1",
        role_stage="worker",
    )
    append_message_stream_entries(
        conversation,
        message_id="message-1",
        previous_content=second,
        current_content=second,
        status="completed",
        role="assistant",
        name="Hermes",
        kind="message",
        turn_id="turn-1",
        role_stage="worker",
    )

    payloads = [entry["payload"] for entry in conversation["session_entries"]]
    deltas = [payload for payload in payloads if payload.get("operation") == "append"]
    final = [payload for payload in payloads if payload.get("operation") == "replace"]
    assert max(len(payload["content_delta"]) for payload in deltas) <= MAX_MESSAGE_DELTA_CHARS
    assert "".join(payload["content_delta"] for payload in deltas) == second
    assert len(final) == 1
    assert final[0]["content"] == second
    assert sum(len(payload.get("content", "")) for payload in payloads) == len(second)


def test_stream_rewrite_is_constant_size_reference_until_terminal_content():
    conversation = {}
    append_message_stream_entries(
        conversation,
        message_id="message-rewrite",
        previous_content="draft one",
        current_content="a completely replaced answer",
        status="streaming",
        role="assistant",
        name="Hermes",
        kind="message",
        turn_id="turn-rewrite",
        role_stage="chat",
    )
    reference = conversation["session_entries"][0]["payload"]
    assert reference["operation"] == "reference"
    assert "content" not in reference
    assert "content_delta" not in reference

    append_message_stream_entries(
        conversation,
        message_id="message-rewrite",
        previous_content="a completely replaced answer",
        current_content="a completely replaced answer",
        status="failed",
        role="assistant",
        name="Hermes",
        kind="message",
        turn_id="turn-rewrite",
        role_stage="chat",
    )
    final = conversation["session_entries"][-1]["payload"]
    assert final["operation"] == "replace"
    assert final["content"] == "a completely replaced answer"

    cursor = conversation["session_entry_cursor"]
    append_message_stream_entries(
        conversation,
        message_id="message-rewrite",
        previous_content="a completely replaced answer",
        current_content="a completely replaced answer",
        status="failed",
        role="assistant",
        name="Hermes",
        kind="message",
        turn_id="turn-rewrite",
        role_stage="chat",
    )
    assert conversation["session_entry_cursor"] == cursor
