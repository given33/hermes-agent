from hermes_runtime.trajectory import project_hosted_trajectory


def _event(cursor, event_type, payload=None, event_id=None):
    return {
        "event_id": event_id or f"event-{cursor}",
        "cursor": cursor,
        "turn_id": "turn-1",
        "event_type": event_type,
        "occurred_at": 1_700_000_000_000 + cursor,
        "payload": payload or {},
        "runtime": {
            "component_id": "component-1",
            "provider_refs": ["provider-1@2"],
            "artifact_refs": ["artifact:sha256"],
            "lifecycle_state": "active",
        },
    }


def test_trajectory_is_bounded_and_summary_redacts_sensitive_content():
    result = project_hosted_trajectory(
        [
            _event(2, "tool.started", {"tool_name": "shell", "text": "api_key=secret"}),
            _event(1, "turn.started"),
            _event(3, "tool.failed", {"error": "Bearer abc123"}),
            _event(3, "tool.failed", {"error": "duplicate"}, event_id="event-3"),
        ],
        session_id="turn-1",
        title="  A   task ",
        detail_level="summary",
        max_records=50,
    )

    assert result["schemaVersion"] == 1
    assert result["session"]["title"] == "A task"
    assert result["stats"]["records"] == 3
    assert len(result["records"]) == 3
    serialized = str(result)
    assert "secret" not in serialized
    assert "abc123" not in serialized
    assert result["records"][0]["metadata"] == {}


def test_full_trajectory_keeps_runtime_metadata_but_not_raw_payloads():
    item = _event(
        1,
        "subagent.progress",
        {
            "summary": "worker update",
            "prompt": "private prompt",
            "result": "private result",
            "provider_id": "provider-1",
            "duration_ms": 12.4,
        },
    )
    item["runtime"]["provider_refs"] = [{"provider": "provider-1", "apiKey": "nested-secret"}]
    result = project_hosted_trajectory(
        [item],
        session_id="turn-1",
        detail_level="full",
        max_records=50,
    )

    record = result["records"][0]
    assert record["kind"] == "subagent"
    assert record["durationMs"] == 12
    assert record["metadata"]["component_id"] == "component-1"
    assert "private prompt" not in str(result)
    assert "private result" not in str(result)
    assert "nested-secret" not in str(result)
