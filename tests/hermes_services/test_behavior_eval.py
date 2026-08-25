from __future__ import annotations

import io
import json
import os
import threading
import time
from urllib.error import HTTPError, URLError

import pytest

from hermes_services.behavior_eval import (
    EvalEventPage,
    HttpHostedEvalAdapter,
    RecoverableHostedEvalError,
    TerminalHostedEvalError,
    run_hosted_behavior_scenario,
)


class _HttpResponse(io.BytesIO):
    def close(self):
        super().close()


def test_http_adapter_calls_real_mobile_contract_with_header_auth():
    calls = []
    responses = [
        {"conversation": {"id": "chat_eval_test"}, "created": True},
        {
            "accepted": True,
            "conversation_id": "chat_eval_test",
            "runtime_binding": {
                "profile": "default",
                "provider": "test-provider",
                "model": "test-model",
                "verified": True,
            },
        },
        (
            "id: 1\n"
            "event: conversation\n"
            'data: {"cursor":1,"min_cursor":1,"has_gap":false,'
            '"reset_cursor":false,"account_generation":"gen-1",'
            '"events":[{"cursor":1,"event_id":"e1",'
            '"account_generation":"gen-1",'
            '"event_type":"turn.completed","role_stage":"chat",'
            '"payload":{}}]}\n\n'
        ),
        {"conversation": {"account_generation": "gen-1", "hosted_turns": {}}},
    ]

    def open_request(request, timeout):
        calls.append((request, timeout))
        payload = responses.pop(0)
        raw = (
            payload.encode("utf-8")
            if isinstance(payload, str)
            else json.dumps(payload).encode("utf-8")
        )
        return _HttpResponse(raw)

    adapter = HttpHostedEvalAdapter(
        base_url="https://hermes.example.test",
        bearer_token="test-bearer",
        profile="default",
        request_open=open_request,
    )
    accepted = adapter.enqueue(
        provider="test-provider",
        model="test-model",
        scenario_id="simple-chat",
        prompt="hello",
        idempotency_key="eval:simple-chat",
    )
    events = adapter.events_after(conversation_id="chat_eval_test", cursor=0)
    snapshot = adapter.snapshot(conversation_id="chat_eval_test")

    assert accepted["accepted"] is True
    assert events.events[0]["event_type"] == "turn.completed"
    assert events.account_generation == "gen-1"
    assert snapshot["status"] == "running"
    assert all(
        call[0].headers["Authorization"] == "Bearer test-bearer" for call in calls
    )
    assert all("test-bearer" not in call[0].full_url for call in calls)
    assert calls[0][0].full_url.endswith(
        "/api/plugins/collaboration/single/conversations"
    )
    assert "/enqueue" in calls[1][0].full_url
    assert "hosted-events?cursor=0" in calls[2][0].full_url
    enqueue_body = json.loads(calls[1][0].data.decode("utf-8"))
    assert enqueue_body["required_provider"] == "test-provider"
    assert enqueue_body["required_model"] == "test-model"


def test_http_adapter_rejects_cleartext_non_loopback_origin():
    with pytest.raises(ValueError, match="HTTPS"):
        HttpHostedEvalAdapter(
            base_url="http://hermes.example.test",
            bearer_token="token",
        )


@pytest.mark.parametrize(
    "status,error_type",
    [
        (401, TerminalHostedEvalError),
        (403, TerminalHostedEvalError),
        (404, TerminalHostedEvalError),
        (409, TerminalHostedEvalError),
        (422, TerminalHostedEvalError),
        (429, RecoverableHostedEvalError),
        (503, RecoverableHostedEvalError),
    ],
)
def test_http_status_classification_is_deterministic(status, error_type):
    def open_request(request, timeout):
        del timeout
        raise HTTPError(
            request.full_url,
            status,
            "failure",
            hdrs=None,
            fp=io.BytesIO(b'{"detail":"provider rejected request"}'),
        )

    adapter = HttpHostedEvalAdapter(
        base_url="https://hermes.example.test",
        bearer_token="test-bearer",
        request_open=open_request,
    )
    with pytest.raises(error_type) as raised:
        adapter._request("/status")
    assert raised.value.status_code == status
    assert f"HTTP {status}" in str(raised.value)
    assert "provider rejected request" in str(raised.value)


def test_http_transport_failure_is_retryable_and_precise():
    def open_request(_request, timeout):
        del timeout
        raise URLError("network offline")

    adapter = HttpHostedEvalAdapter(
        base_url="https://hermes.example.test",
        bearer_token="test-bearer",
        request_open=open_request,
    )
    with pytest.raises(RecoverableHostedEvalError, match="network offline"):
        adapter._request("/offline")


@pytest.mark.skipif(
    not (
        os.getenv("HERMES_BEHAVIOR_EVAL_URL")
        and os.getenv("HERMES_BEHAVIOR_EVAL_TOKEN")
    ),
    reason="explicit live hosted-eval server and token are not configured",
)
def test_opt_in_live_hosted_behavior_chain():
    summary = run_hosted_behavior_scenario(
        HttpHostedEvalAdapter(
            base_url=os.environ["HERMES_BEHAVIOR_EVAL_URL"],
            bearer_token=os.environ["HERMES_BEHAVIOR_EVAL_TOKEN"],
            profile=os.getenv("HERMES_BEHAVIOR_EVAL_PROFILE", "default"),
            timeout_seconds=30,
        ),
        provider=os.environ.get("HERMES_BEHAVIOR_EVAL_PROVIDER", "explicit-live"),
        model=os.environ.get("HERMES_BEHAVIOR_EVAL_MODEL", "explicit-live"),
        scenario_id="live-simple-chat",
        prompt="Reply with exactly: hosted eval ready",
        timeout_seconds=180,
    )

    assert summary["outcome"] == "completed"


class SequenceAdapter:
    def __init__(
        self,
        events: list[dict],
        snapshot_status: str = "running",
        *,
        generation: str = "",
    ) -> None:
        self.events = events
        self.snapshot_status = snapshot_status
        self.generation = generation or next(
            (
                str(event.get("account_generation") or "")
                for event in events
                if str(event.get("account_generation") or "")
            ),
            "generation-1",
        )

    def enqueue(self, **_kwargs):
        return {"accepted": True, "conversation_id": "conversation-1"}

    def events_after(self, *, conversation_id: str, cursor: int):
        assert conversation_id == "conversation-1"
        events = [event for event in self.events if int(event["cursor"]) > cursor]
        for event in events:
            event.setdefault("account_generation", self.generation)
        return EvalEventPage(
            events=events,
            cursor=max([cursor, *[int(event["cursor"]) for event in events]]),
            account_generation=self.generation,
        )

    def snapshot(self, *, conversation_id: str):
        assert conversation_id == "conversation-1"
        return {
            "status": self.snapshot_status,
            "account_generation": self.generation,
        }


class IdentityAdapter(SequenceAdapter):
    def __init__(self, *, fail_first_enqueue: bool = False) -> None:
        super().__init__([
            {
                "cursor": 1,
                "event_id": "terminal",
                "event_type": "turn.completed",
                "role_stage": "chat",
                "payload": {},
            }
        ])
        self.keys: list[str] = []
        self.fail_first_enqueue = fail_first_enqueue

    def enqueue(self, **kwargs):
        self.keys.append(kwargs["idempotency_key"])
        if self.fail_first_enqueue and len(self.keys) == 1:
            raise RecoverableHostedEvalError("temporary enqueue disconnect")
        return {
            "accepted": True,
            "conversation_id": "conversation-1",
            "account_generation": self.generation,
        }


def test_eval_run_identity_is_unique_across_runs_and_stable_within_retry():
    first = IdentityAdapter(fail_first_enqueue=True)
    first_summary = run_hosted_behavior_scenario(
        first,
        provider="provider",
        model="model",
        scenario_id="identity",
        prompt="same prompt",
        eval_run_id="run-one",
        code_revision="revision-a",
        sleep=lambda _seconds: None,
    )
    second = IdentityAdapter()
    second_summary = run_hosted_behavior_scenario(
        second,
        provider="provider",
        model="model",
        scenario_id="identity",
        prompt="same prompt",
        eval_run_id="run-two",
        code_revision="revision-a",
        sleep=lambda _seconds: None,
    )

    assert first.keys[0] == first.keys[1] == first_summary["idempotency_key"]
    assert second.keys == [second_summary["idempotency_key"]]
    assert first_summary["idempotency_key"] != second_summary["idempotency_key"]
    assert first_summary["retries"] == 1
    assert first_summary["attempts"] == 2


class FailingAdapter(IdentityAdapter):
    def __init__(self, errors: list[Exception]) -> None:
        super().__init__()
        self.errors = list(errors)

    def events_after(self, *, conversation_id: str, cursor: int):
        if self.errors:
            raise self.errors.pop(0)
        return super().events_after(conversation_id=conversation_id, cursor=cursor)


def test_terminal_auth_failure_returns_one_precise_failed_summary():
    adapter = FailingAdapter([
        TerminalHostedEvalError(
            "hosted eval HTTP 401: invalid token",
            status_code=401,
            error_code="http_401",
        )
    ])
    summary = run_hosted_behavior_scenario(
        adapter,
        provider="provider",
        model="model",
        scenario_id="terminal-auth",
        prompt="run",
        sleep=lambda _seconds: None,
    )

    assert summary["outcome"] == "failed"
    assert summary["retries"] == 0
    assert summary["attempts"] == 1
    assert summary["last_status_code"] == 401
    assert summary["last_error"] == "hosted eval HTTP 401: invalid token"


def test_retryable_failures_are_bounded_and_last_error_is_preserved_after_recovery():
    adapter = FailingAdapter([
        RecoverableHostedEvalError("rate limited", status_code=429),
        RecoverableHostedEvalError("service unavailable", status_code=503),
        RecoverableHostedEvalError("socket timeout"),
    ])
    summary = run_hosted_behavior_scenario(
        adapter,
        provider="provider",
        model="model",
        scenario_id="recover",
        prompt="run",
        max_retries=5,
        sleep=lambda _seconds: None,
    )

    assert summary["outcome"] == "completed"
    assert summary["retries"] == 3
    assert summary["attempts"] == 4
    assert summary["last_error"] == "socket timeout"


def test_retry_exhaustion_finishes_instead_of_polling_until_deadline():
    adapter = FailingAdapter([RecoverableHostedEvalError("offline") for _ in range(10)])
    summary = run_hosted_behavior_scenario(
        adapter,
        provider="provider",
        model="model",
        scenario_id="offline",
        prompt="run",
        max_retries=2,
        sleep=lambda _seconds: None,
    )

    assert summary["outcome"] == "failed"
    assert summary["retries"] == 2
    assert summary["attempts"] == 3
    assert summary["last_error"] == "offline"


def test_noop_poll_sleep_yields_to_background_workflow(monkeypatch):
    yielded = threading.Event()
    completed = threading.Event()

    class BackgroundAdapter:
        def enqueue(self, **_kwargs):
            def finish_after_scheduler_yield():
                if yielded.wait(timeout=1):
                    completed.set()

            threading.Thread(target=finish_after_scheduler_yield, daemon=True).start()
            return {
                "accepted": True,
                "conversation_id": "conversation-background",
                "account_generation": "generation-1",
            }

        def events_after(self, *, conversation_id: str, cursor: int):
            assert conversation_id == "conversation-background"
            if completed.is_set():
                return EvalEventPage(
                    events=[
                        {
                            "cursor": 1,
                            "event_id": "terminal-background",
                            "event_type": "turn.completed",
                            "role_stage": "worker",
                            "account_generation": "generation-1",
                            "payload": {},
                        }
                    ],
                    cursor=1,
                    account_generation="generation-1",
                )
            return EvalEventPage(
                events=[],
                cursor=cursor,
                account_generation="generation-1",
            )

        def snapshot(self, *, conversation_id: str):
            assert conversation_id == "conversation-background"
            return {
                "status": "completed" if completed.is_set() else "running",
                "hosted_event_cursor": 1 if completed.is_set() else 0,
                "account_generation": "generation-1",
            }

    real_sleep = time.sleep

    def observe_scheduler_yield(delay: float):
        if delay == 0.001:
            yielded.set()
        real_sleep(delay)

    monkeypatch.setattr(
        "hermes_services.behavior_eval.time.sleep",
        observe_scheduler_yield,
    )
    summary = run_hosted_behavior_scenario(
        BackgroundAdapter(),
        provider="provider",
        model="model",
        scenario_id="background-yield",
        prompt="run",
        timeout_seconds=1,
        poll_interval=0.01,
        sleep=lambda _seconds: None,
    )

    assert summary["outcome"] == "completed"
    assert yielded.is_set()


def test_unconfigured_model_rejection_is_a_terminal_summary():
    class RejectedAdapter(IdentityAdapter):
        def enqueue(self, **_kwargs):
            return {
                "accepted": False,
                "conversation_id": "conversation-1",
                "status_code": 422,
                "error_code": "model_not_configured",
                "error": "No model is configured for this Profile",
            }

    summary = run_hosted_behavior_scenario(
        RejectedAdapter(),
        provider="provider",
        model="model",
        scenario_id="unconfigured",
        prompt="run",
        sleep=lambda _seconds: None,
    )

    assert summary["outcome"] == "failed"
    assert summary["last_status_code"] == 422
    assert summary["last_error"] == "No model is configured for this Profile"
    assert summary["retries"] == 0


def test_tool_progress_updates_do_not_create_duplicate_tool_calls():
    events = [
        {
            "cursor": 1,
            "event_id": "event-1",
            "event_type": "tool.started",
            "role_stage": "worker",
            "payload": {"entity_id": "tool-1", "name": "shell", "args": {}},
        },
        {
            "cursor": 2,
            "event_id": "event-2",
            "event_type": "tool.progress",
            "role_stage": "worker",
            "payload": {"entity_id": "tool-1", "percent": 25},
        },
        {
            "cursor": 3,
            "event_id": "event-3",
            "event_type": "tool.progress",
            "role_stage": "worker",
            "payload": {"entity_id": "tool-1", "percent": 80},
        },
        {
            "cursor": 4,
            "event_id": "event-4",
            "event_type": "tool.completed",
            "role_stage": "worker",
            "payload": {"entity_id": "tool-1", "name": "shell", "result": "ok"},
        },
        {
            "cursor": 5,
            "event_id": "event-5",
            "event_type": "turn.completed",
            "role_stage": "worker",
            "payload": {"status": "completed"},
        },
    ]

    summary = run_hosted_behavior_scenario(
        SequenceAdapter(events),
        provider="provider",
        model="model",
        scenario_id="tool-progress",
        prompt="run",
        sleep=lambda _seconds: None,
    )

    assert summary["outcome"] == "completed"
    assert summary["tool_count"] == 1
    assert [event["type"] for event in summary["events"]] == [
        "tool_call",
        "tool_result",
        "role_event",
    ]


def test_snapshot_terminal_is_recorded_once_when_stream_has_no_terminal_event():
    summary = run_hosted_behavior_scenario(
        SequenceAdapter([], snapshot_status="completed"),
        provider="provider",
        model="model",
        scenario_id="snapshot-terminal",
        prompt="run",
        sleep=lambda _seconds: None,
    )

    terminals = [
        event
        for event in summary["events"]
        if event["type"] == "role_event" and event["status"] == "completed"
    ]
    assert len(terminals) == 1


def test_runtime_evidence_and_token_usage_are_reported_from_real_events():
    summary = run_hosted_behavior_scenario(
        SequenceAdapter([
            {
                "cursor": 1,
                "event_id": "usage-1",
                "event_type": "message.completed",
                "role_stage": "chat",
                "payload": {
                    "content": "done",
                    "actual_provider": "provider",
                    "actual_model": "model",
                    "usage": {"input_tokens": 17, "output_tokens": 5},
                },
            },
            {
                "cursor": 2,
                "event_id": "terminal-1",
                "event_type": "turn.completed",
                "role_stage": "chat",
                "payload": {},
            },
        ]),
        provider="provider",
        model="model",
        scenario_id="usage-evidence",
        prompt="run",
        sleep=lambda _seconds: None,
    )

    assert summary["actual_provider"] == "provider"
    assert summary["actual_model"] == "model"
    assert summary["input_tokens"] == 17
    assert summary["output_tokens"] == 5
    assert summary["usage_observed"] is True


from hermes_services.hosted_event_protocol import (
    append_hosted_event,
    validate_event_envelope,
)


def _canonical_events(specs: list[tuple[str, str, dict]]) -> list[dict]:
    conversation: dict = {}
    for index, (event_type, role_stage, payload) in enumerate(specs, start=1):
        entity_id = str(payload.get("entity_id") or "")
        result = append_hosted_event(
            conversation,
            account_generation="eval-generation",
            conversation_id="conversation-matrix",
            turn_id="turn-matrix",
            role_stage=role_stage,
            event_type=event_type,
            payload=payload,
            entity_id=entity_id,
            idempotency_key=f"matrix-{index}-{event_type}",
            occurred_at=index,
        )
        assert result.appended is True
    return [validate_event_envelope(item) for item in conversation["hosted_events"]]


class PageSequenceAdapter:
    def __init__(
        self, pages: list[EvalEventPage], *, generation: str = "gen-1"
    ) -> None:
        self.pages = list(pages)
        self.generation = generation
        self.requested_cursors: list[int] = []

    def enqueue(self, **_kwargs):
        return {
            "accepted": True,
            "conversation_id": "conversation-pages",
            "account_generation": self.generation,
        }

    def events_after(self, *, conversation_id: str, cursor: int):
        assert conversation_id == "conversation-pages"
        self.requested_cursors.append(cursor)
        if self.pages:
            return self.pages.pop(0)
        return EvalEventPage(
            events=[],
            cursor=cursor,
            account_generation=self.generation,
        )

    def snapshot(self, *, conversation_id: str):
        assert conversation_id == "conversation-pages"
        return {"status": "running", "account_generation": self.generation}


def test_future_cursor_reset_consumes_snapshot_then_resumes_from_authoritative_cursor():
    terminal = _canonical_events([("turn.completed", "chat", {})])[0]
    terminal["cursor"] = 6
    terminal["account_generation"] = "gen-1"
    adapter = PageSequenceAdapter([
        EvalEventPage(
            events=[],
            cursor=5,
            min_cursor=1,
            has_gap=True,
            reset_cursor=True,
            reset_reason="future_cursor",
            snapshot={
                "messages": [
                    {"id": "snapshot-message", "role": "assistant", "content": "saved"}
                ]
            },
            account_generation="gen-1",
        ),
        EvalEventPage(
            events=[terminal],
            cursor=6,
            min_cursor=1,
            account_generation="gen-1",
        ),
    ])

    summary = run_hosted_behavior_scenario(
        adapter,
        provider="provider",
        model="model",
        scenario_id="future-cursor",
        prompt="run",
        sleep=lambda _seconds: None,
    )

    assert summary["outcome"] == "completed"
    assert summary["cursor_resets"] == 1
    assert summary["replay_gaps"] == 1
    assert summary["snapshot_applied"] is True
    assert adapter.requested_cursors == [0, 5]
    assert [
        event.get("content")
        for event in summary["events"]
        if event["type"] == "message"
    ] == ["saved"]


def test_server_cursor_rollback_replaces_future_cursor_and_replays_from_snapshot():
    before_rollback = {
        "cursor": 5,
        "event_id": "before-rollback",
        "account_generation": "gen-1",
        "event_type": "message.delta",
        "role_stage": "chat",
        "entity_id": "old-message",
        "payload": {"entity_id": "old-message", "delta": "stale"},
    }
    terminal = {
        "cursor": 3,
        "event_id": "after-rollback",
        "account_generation": "gen-1",
        "event_type": "turn.completed",
        "role_stage": "chat",
        "payload": {},
    }
    adapter = PageSequenceAdapter([
        EvalEventPage(
            events=[before_rollback],
            cursor=5,
            account_generation="gen-1",
        ),
        EvalEventPage(
            events=[],
            cursor=2,
            min_cursor=1,
            has_gap=True,
            reset_cursor=True,
            reset_reason="future_cursor",
            snapshot={
                "messages": [
                    {
                        "id": "restored-message",
                        "role": "assistant",
                        "content": "restored",
                    }
                ]
            },
            account_generation="gen-1",
        ),
        EvalEventPage(
            events=[terminal],
            cursor=3,
            account_generation="gen-1",
        ),
    ])

    summary = run_hosted_behavior_scenario(
        adapter,
        provider="provider",
        model="model",
        scenario_id="cursor-rollback",
        prompt="run",
        sleep=lambda _seconds: None,
    )

    assert summary["outcome"] == "completed"
    assert summary["cursor"] == 3
    assert adapter.requested_cursors == [0, 5, 2]
    assert [
        event.get("content")
        for event in summary["events"]
        if event["type"] == "message"
    ] == ["restored"]


def test_stale_generation_injection_rejects_the_entire_page_atomically():
    events = _canonical_events([
        ("message.completed", "chat", {"entity_id": "valid", "content": "valid"}),
        ("turn.completed", "chat", {}),
    ])
    events[0]["account_generation"] = "gen-1"
    events[1]["account_generation"] = "old-generation"
    adapter = PageSequenceAdapter([
        EvalEventPage(
            events=events,
            cursor=2,
            account_generation="gen-1",
        )
    ])

    summary = run_hosted_behavior_scenario(
        adapter,
        provider="provider",
        model="model",
        scenario_id="stale-event",
        prompt="run",
        sleep=lambda _seconds: None,
    )

    assert summary["outcome"] == "failed"
    assert "another account generation" in summary["last_error"]
    assert not [event for event in summary["events"] if event["type"] == "message"]


def test_account_generation_switch_fails_closed_before_new_generation_is_applied():
    adapter = PageSequenceAdapter(
        [EvalEventPage(events=[], cursor=0, account_generation="gen-2")],
        generation="gen-1",
    )
    summary = run_hosted_behavior_scenario(
        adapter,
        provider="provider",
        model="model",
        scenario_id="generation-switch",
        prompt="run",
        sleep=lambda _seconds: None,
    )

    assert summary["outcome"] == "failed"
    assert "generation changed" in summary["last_error"]
    assert summary["account_generation"] == "gen-1"


def test_missing_page_generation_fails_closed_without_advancing_cursor():
    adapter = PageSequenceAdapter(
        [EvalEventPage(events=[], cursor=4, account_generation="")],
        generation="gen-1",
    )

    summary = run_hosted_behavior_scenario(
        adapter,
        provider="provider",
        model="model",
        scenario_id="missing-page-generation",
        prompt="run",
        sleep=lambda _seconds: None,
    )

    assert summary["outcome"] == "failed"
    assert "omitted its account generation" in summary["last_error"]
    assert summary["cursor"] == 0


def test_missing_event_generation_rejects_the_entire_page_atomically():
    events = _canonical_events([
        ("message.completed", "chat", {"entity_id": "message", "content": "stale"}),
        ("turn.completed", "chat", {}),
    ])
    events[0]["account_generation"] = "gen-1"
    events[1].pop("account_generation", None)
    adapter = PageSequenceAdapter([
        EvalEventPage(
            events=events,
            cursor=2,
            account_generation="gen-1",
        )
    ])

    summary = run_hosted_behavior_scenario(
        adapter,
        provider="provider",
        model="model",
        scenario_id="missing-event-generation",
        prompt="run",
        sleep=lambda _seconds: None,
    )

    assert summary["outcome"] == "failed"
    assert "without an account generation" in summary["last_error"]
    assert summary["cursor"] == 0
    assert not [event for event in summary["events"] if event["type"] == "message"]


def test_transcript_aggregates_stream_lifecycles_commands_attachments_and_cancel():
    specs: list[tuple[str, str, dict]] = [
        ("message.started", "worker", {"entity_id": "message-1", "role": "assistant"}),
        ("message.delta", "worker", {"entity_id": "message-1", "delta": "你"}),
        ("message.delta", "worker", {"entity_id": "message-1", "delta": "好"}),
        (
            "message.completed",
            "worker",
            {
                "entity_id": "message-1",
                "attachments": [
                    {
                        "id": "file-1",
                        "sha256": "a" * 64,
                        "disposition": "download",
                    }
                ],
            },
        ),
        ("thinking.started", "worker", {"entity_id": "thinking-1"}),
        ("thinking.delta", "worker", {"entity_id": "thinking-1", "delta": "检查"}),
        ("thinking.completed", "worker", {"entity_id": "thinking-1"}),
        ("command.started", "worker", {"entity_id": "command-1", "command": "echo ok"}),
        ("command.output", "worker", {"entity_id": "command-1", "output": "o"}),
        ("command.output", "worker", {"entity_id": "command-1", "output": "k"}),
        ("command.completed", "worker", {"entity_id": "command-1"}),
    ]
    specs.extend(
        (
            "connection.retry_scheduled",
            "worker",
            {"entity_id": f"retry-{attempt}", "attempt": attempt, "error": "503"},
        )
        for attempt in range(1, 6)
    )
    specs.extend([
        ("tool.started", "worker", {"entity_id": "tool-1", "name": "shell"}),
        ("turn.cancelled", "worker", {}),
    ])
    summary = run_hosted_behavior_scenario(
        SequenceAdapter(_canonical_events(specs)),
        provider="provider",
        model="model",
        scenario_id="lifecycle",
        prompt="run",
        sleep=lambda _seconds: None,
    )

    assert summary["outcome"] == "cancelled"
    messages = [event for event in summary["events"] if event["type"] == "message"]
    assert [event["content"] for event in messages] == ["你好"]
    assert [
        event["summary"] for event in summary["events"] if event["type"] == "thinking"
    ] == ["检查"]
    assert [
        event["id"] for event in summary["events"] if event["type"] == "attachment"
    ] == ["file-1"]
    command_result = next(
        event
        for event in summary["events"]
        if event["type"] == "tool_result" and event.get("command")
    )
    assert command_result["content"] == "ok"
    retry_events = [
        event
        for event in summary["events"]
        if event["type"] == "role_event" and event.get("status") == "retry_scheduled"
    ]
    assert [event["attempt"] for event in retry_events] == [1, 2, 3, 4, 5]
    cancelled = next(
        event
        for event in summary["events"]
        if event["type"] == "tool_result" and event.get("tool_call_id") == "tool-1"
    )
    assert cancelled["cancelled"] is True


class ScenarioAdapter:
    def __init__(
        self,
        events: list[dict],
        *,
        failures: list[int | None] | None = None,
        page_size: int = 100,
        disconnect_client_after_accept: bool = False,
    ) -> None:
        self.events = [validate_event_envelope(item) for item in events]
        self.failures = list(failures or [])
        self.page_size = page_size
        self.disconnect_client_after_accept = disconnect_client_after_accept
        self.client_connected = True
        self.requested_cursors: list[int] = []
        self.enqueue_calls = 0

    def enqueue(self, **kwargs):
        self.enqueue_calls += 1
        assert kwargs["idempotency_key"].startswith("eval:")
        if self.disconnect_client_after_accept:
            self.client_connected = False
        return {
            "accepted": True,
            "conversation_id": "conversation-matrix",
            "account_generation": "eval-generation",
        }

    def events_after(self, *, conversation_id: str, cursor: int):
        assert conversation_id == "conversation-matrix"
        self.requested_cursors.append(cursor)
        if self.failures:
            status = self.failures.pop(0)
            raise RecoverableHostedEvalError(
                "temporary transport failure", status_code=status
            )
        events = [event for event in self.events if int(event["cursor"]) > cursor][
            : self.page_size
        ]
        return EvalEventPage(
            events=events,
            cursor=max([cursor, *[int(event["cursor"]) for event in events]]),
            account_generation="eval-generation",
        )

    def snapshot(self, *, conversation_id: str):
        assert conversation_id == "conversation-matrix"
        return {"status": "running", "account_generation": "eval-generation"}


SCENARIOS = [
    (
        "01-simple-chat",
        [
            ("message.completed", "chat", {"content": "hello"}),
            ("turn.completed", "chat", {}),
        ],
        [],
        "simple",
    ),
    (
        "02-lift-exactly-once",
        [
            (
                "role.handoff",
                "manager",
                {
                    "action": "collaboration_lift",
                    "from_role": "chat",
                    "to_role": "manager",
                },
            ),
            ("turn.completed", "manager", {}),
        ],
        [],
        "lift",
    ),
    (
        "03-manager-decompose-dispatch",
        [
            (
                "role.handoff",
                "manager",
                {
                    "action": "dispatch",
                    "plan_id": "plan-1",
                    "subtasks": ["inspect", "test"],
                    "to_role": "worker",
                },
            ),
            ("message.completed", "manager", {"content": "plan dispatched"}),
            ("turn.completed", "manager", {}),
        ],
        [],
        "dispatch",
    ),
    (
        "04-worker-tool-progress",
        [
            ("tool.started", "worker", {"entity_id": "tool-1", "name": "terminal"}),
            ("tool.progress", "worker", {"entity_id": "tool-1", "percent": 50}),
            (
                "tool.completed",
                "worker",
                {"entity_id": "tool-1", "name": "terminal", "result": "ok"},
            ),
            ("turn.completed", "worker", {}),
        ],
        [],
        "tool",
    ),
    (
        "05-review-rework",
        [
            (
                "role.rework_requested",
                "reviewer",
                {"reason": "missing test", "to_role": "manager"},
            ),
            ("role.handoff", "manager", {"action": "redispatch", "to_role": "worker"}),
            ("message.completed", "worker", {"content": "fixed"}),
            ("turn.completed", "reviewer", {}),
        ],
        [],
        "rework",
    ),
    (
        "06-supervisor-intervention",
        [
            (
                "intervention.replied",
                "supervisor",
                {"target_role": "manager", "content": "complete the split"},
            ),
            ("message.completed", "manager", {"content": "acknowledged"}),
            ("turn.completed", "supervisor", {}),
        ],
        [],
        "supervisor",
    ),
    (
        "07-user-intervention-during-tool",
        [
            ("tool.started", "worker", {"entity_id": "tool-1", "name": "terminal"}),
            (
                "intervention.queued",
                "worker",
                {"entity_id": "int-1", "target_role": "worker"},
            ),
            (
                "intervention.claimed",
                "worker",
                {"entity_id": "int-1", "target_role": "worker"},
            ),
            (
                "intervention.replied",
                "worker",
                {
                    "entity_id": "int-1",
                    "target_role": "worker",
                    "content": "direction updated",
                },
            ),
            (
                "intervention.completed",
                "worker",
                {"entity_id": "int-1", "target_role": "worker"},
            ),
            (
                "tool.completed",
                "worker",
                {"entity_id": "tool-1", "name": "terminal", "result": "adjusted"},
            ),
            ("turn.completed", "worker", {}),
        ],
        [],
        "targeted",
    ),
    (
        "08-reporter-verified-only",
        [
            (
                "role.handoff",
                "reviewer",
                {
                    "action": "review_approved",
                    "to_role": "reporter",
                    "evidence_ids": ["e-1"],
                },
            ),
            (
                "message.completed",
                "reporter",
                {"content": "verified result", "evidence_ids": ["e-1"]},
            ),
            ("turn.completed", "reporter", {}),
        ],
        [],
        "reporter",
    ),
    (
        "09-ios-background-resume",
        [
            ("message.delta", "worker", {"entity_id": "message-1", "content": "part"}),
            (
                "message.completed",
                "worker",
                {"entity_id": "message-1", "content": "whole"},
            ),
            ("turn.completed", "worker", {}),
        ],
        [],
        "background",
    ),
    (
        "10-provider-recovery",
        [
            ("message.completed", "chat", {"content": "recovered"}),
            ("turn.completed", "chat", {}),
        ],
        [429, 503, None, None, None],
        "retry",
    ),
    (
        "11-resource-refresh",
        [
            (
                "role.handoff",
                "system",
                {
                    "action": "resource_refresh",
                    "resource_id": "resource-1",
                    "resource_cursor": 7,
                },
            ),
            ("turn.completed", "system", {}),
        ],
        [],
        "resource",
    ),
    (
        "12-file-artifact-deletion",
        [
            (
                "role.handoff",
                "system",
                {
                    "action": "account_data_deleted",
                    "file_ids": ["file-1"],
                    "artifact_ids": ["artifact-1"],
                },
            ),
            ("turn.completed", "system", {}),
        ],
        [],
        "deletion",
    ),
]


@pytest.mark.parametrize("scenario_id,specs,failures,assertion", SCENARIOS)
def test_minimum_hosted_behavior_scenario_matrix(
    scenario_id, specs, failures, assertion
):
    events = _canonical_events(specs)
    adapter = ScenarioAdapter(
        events,
        failures=failures,
        page_size=1 if assertion == "background" else 100,
        disconnect_client_after_accept=assertion == "background",
    )
    summary = run_hosted_behavior_scenario(
        adapter,
        provider="test-provider",
        model="test-model",
        scenario_id=scenario_id,
        prompt="exercise hosted behavior",
        sleep=lambda _seconds: None,
    )

    assert summary["outcome"] == "completed"
    assert adapter.enqueue_calls == 1
    assert all(validate_event_envelope(item) for item in events)
    if assertion == "simple":
        assert [
            item["content"] for item in summary["events"] if item["type"] == "message"
        ] == ["hello"]
    elif assertion == "lift":
        assert (
            sum(
                item.get("status") == "collaboration_lift" for item in summary["events"]
            )
            == 1
        )
    elif assertion == "dispatch":
        dispatch = next(
            item for item in events if item["payload"].get("action") == "dispatch"
        )
        assert dispatch["payload"]["subtasks"] == ["inspect", "test"]
        assert dispatch["payload"]["to_role"] == "worker"
    elif assertion == "tool":
        assert summary["tool_count"] == 1
    elif assertion == "rework":
        assert sum(item.get("status") == "rework" for item in summary["events"]) == 1
        assert any(item["payload"].get("action") == "redispatch" for item in events)
    elif assertion == "supervisor":
        assert (
            next(
                item for item in events if item["event_type"] == "intervention.replied"
            )["payload"]["target_role"]
            == "manager"
        )
    elif assertion == "targeted":
        started = next(
            item["cursor"] for item in events if item["event_type"] == "tool.started"
        )
        replied = next(
            item["cursor"]
            for item in events
            if item["event_type"] == "intervention.replied"
        )
        completed = next(
            item["cursor"] for item in events if item["event_type"] == "tool.completed"
        )
        assert started < replied < completed
    elif assertion == "reporter":
        approval = next(
            item
            for item in events
            if item["payload"].get("action") == "review_approved"
        )
        report = next(
            item
            for item in events
            if item["role_stage"] == "reporter"
            and item["event_type"] == "message.completed"
        )
        assert approval["cursor"] < report["cursor"]
        assert report["payload"]["evidence_ids"] == approval["payload"]["evidence_ids"]
    elif assertion == "background":
        assert adapter.client_connected is False
        assert adapter.requested_cursors == [0, 1, 2]
        assert [
            item["content"] for item in summary["events"] if item["type"] == "message"
        ] == ["whole"]
    elif assertion == "retry":
        assert summary["retries"] == 5
        assert adapter.requested_cursors[:6] == [0, 0, 0, 0, 0, 0]
    elif assertion == "resource":
        refresh = next(
            item
            for item in events
            if item["payload"].get("action") == "resource_refresh"
        )
        assert (
            refresh["payload"]["resource_id"],
            refresh["payload"]["resource_cursor"],
        ) == ("resource-1", 7)
    elif assertion == "deletion":
        deletion = next(
            item
            for item in events
            if item["payload"].get("action") == "account_data_deleted"
        )
        assert deletion["payload"]["file_ids"] == ["file-1"]
        assert deletion["payload"]["artifact_ids"] == ["artifact-1"]
