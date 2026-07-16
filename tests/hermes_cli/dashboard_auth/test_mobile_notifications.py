from __future__ import annotations

import json

from hermes_cli.dashboard_auth.mobile_notifications import (
    _MAX_PAYLOAD_BYTES,
    build_task_completion_payload,
)


def test_task_completion_payload_contains_deep_link_and_server_result():
    payload = build_task_completion_payload(
        conversation_id="conversation-1",
        turn_id="turn-1",
        status="completed",
        result="任务已完成，完整结果在服务器会话中。",
    )

    assert payload["aps"]["alert"]["title"] == "Hermes 任务已完成"
    assert payload["hermes"] == {
        "conversation_id": "conversation-1",
        "deep_link": "hermes-agent://conversation/conversation-1?turn=turn-1",
        "result": "任务已完成，完整结果在服务器会话中。",
        "status": "completed",
        "turn_id": "turn-1",
    }


def test_oversized_results_keep_a_valid_apns_payload_and_deep_link():
    payload = build_task_completion_payload(
        conversation_id="conversation-1",
        turn_id="turn-1",
        status="failed",
        result="结果 " * 20_000,
    )
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()

    assert len(encoded) <= _MAX_PAYLOAD_BYTES
    assert payload["aps"]["alert"]["body"] == "打开 Hermes 查看完整结果。"
    assert payload["hermes"]["deep_link"].startswith("hermes-agent://conversation/")
    assert payload["hermes"]["status"] == "failed"


def test_multibyte_results_are_truncated_by_final_utf8_payload_size():
    payload = build_task_completion_payload(
        conversation_id="conversation/with spaces",
        turn_id="turn?unsafe=value",
        status="completed",
        result="完成🚀" * 20_000,
    )
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()

    assert len(encoded) <= _MAX_PAYLOAD_BYTES
    assert payload["hermes"]["result"]
    assert payload["hermes"]["deep_link"] == (
        "hermes-agent://conversation/conversation%2Fwith%20spaces"
        "?turn=turn%3Funsafe%3Dvalue"
    )
