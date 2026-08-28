from __future__ import annotations

import pytest

from hermes_runtime.version import HERMES_RELEASE_DATE, HERMES_VERSION
from hermes_services.hosted_event_protocol import normalize_legacy_profile_event


@pytest.mark.parametrize(
    "event_type",
    [
        "notification.show",
        "notification.clear",
        "gateway.ready",
        "skin.changed",
        "reaction",
        "billing.step_up.verification",
        "voice.status",
        "voice.transcript",
        "wake.detected",
        "dashboard.new_session_requested",
        "browser.progress",
        "gateway.stderr",
        "gateway.start_timeout",
        "gateway.protocol_error",
        "moa.reference",
        "moa.aggregating",
        "moa.progress",
        "moa.phase",
        "clarify.request",
        "approval.request",
        "sudo.request",
        "secret.request",
        "sudo.expire",
        "secret.expire",
        "background.complete",
    ],
)
def test_official_frontend_events_remain_structured(event_type: str):
    canonical, payload, _entity_id = normalize_legacy_profile_event(
        {"type": event_type, "payload": {"text": "visible state"}}
    )

    assert canonical == event_type
    assert payload["source_event_type"] == event_type
    assert "unmapped_frontend_event" not in payload


def test_interim_messages_keep_their_streaming_semantics():
    canonical, payload, _entity_id = normalize_legacy_profile_event(
        {
            "type": "message.interim",
            "payload": {"text": "partial answer", "already_streamed": True},
        }
    )

    assert canonical == "message.interim"
    assert payload["already_streamed"] is True


def test_subagent_spawn_request_keeps_queued_semantics():
    canonical, payload, entity_id = normalize_legacy_profile_event(
        {
            "type": "subagent.spawn_requested",
            "payload": {"child_session_id": "child-1", "profile": "hk-worker"},
        }
    )

    assert canonical == "subagent.queued"
    assert entity_id == "child-1"
    assert payload["source_event_type"] == "subagent.spawn_requested"


def test_unknown_gateway_controls_never_become_assistant_text():
    canonical, payload, _entity_id = normalize_legacy_profile_event(
        {"type": "future.control.event", "payload": {"text": "not chat text"}}
    )

    assert canonical == "command.output"
    assert payload["unmapped_frontend_event"] is True


def test_runtime_identity_matches_the_official_020_baseline():
    assert HERMES_VERSION == "0.20.0"
    assert HERMES_RELEASE_DATE == "2026.8.3"
