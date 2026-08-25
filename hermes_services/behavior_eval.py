"""Normalized behavioral-evaluation transcript and invariant assertions."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import ipaddress
import os
import time
from typing import Any, Callable, Iterable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
import uuid


NORMALIZED_EVENT_TYPES = frozenset({
    "message",
    "thinking",
    "tool_call",
    "tool_result",
    "role_event",
    "attachment",
})


@dataclass
class EvalRun:
    provider: str
    model: str
    scenario: str
    eval_run_id: str
    idempotency_key: str
    code_revision: str
    prompt_hash: str
    started_at: float = field(default_factory=time.monotonic)
    events: list[dict[str, Any]] = field(default_factory=list)
    retries: int = 0
    model_retries: int = 0
    attempts: int = 0
    last_error: str = ""
    last_status_code: int | None = None
    account_generation: str = ""
    cursor: int = 0
    cursor_resets: int = 0
    replay_gaps: int = 0
    snapshot_applied: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    usage_observed: bool = False
    actual_provider: str = ""
    actual_model: str = ""
    runtime_binding_verified: bool = False
    outcome: str = "running"

    def record(self, event_type: str, **payload: Any) -> dict[str, Any]:
        if event_type not in NORMALIZED_EVENT_TYPES:
            raise ValueError(f"unsupported eval event: {event_type}")
        event = {
            "type": event_type,
            "sequence": len(self.events) + 1,
            "timestamp_ms": int(time.time() * 1000),
            **payload,
        }
        self.events.append(event)
        return event

    def finish(self, outcome: str) -> dict[str, Any]:
        if outcome not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid eval outcome")
        self.outcome = outcome
        return self.summary()

    def summary(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "scenario": self.scenario,
            "eval_run_id": self.eval_run_id,
            "idempotency_key": self.idempotency_key,
            "code_revision": self.code_revision,
            "prompt_hash": self.prompt_hash,
            "latency_ms": round((time.monotonic() - self.started_at) * 1000),
            "retries": self.retries + self.model_retries,
            "transport_retries": self.retries,
            "model_retries": self.model_retries,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "last_status_code": self.last_status_code,
            "account_generation": self.account_generation,
            "cursor": self.cursor,
            "cursor_resets": self.cursor_resets,
            "replay_gaps": self.replay_gaps,
            "snapshot_applied": self.snapshot_applied,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "usage_observed": self.usage_observed,
            "actual_provider": self.actual_provider,
            "actual_model": self.actual_model,
            "runtime_binding_verified": self.runtime_binding_verified,
            "tool_count": sum(1 for item in self.events if item["type"] == "tool_call"),
            "outcome": self.outcome,
            "events": list(self.events),
        }


def assert_event_order(events: Iterable[dict[str, Any]]) -> None:
    sequence = 0
    pending_tools: set[str] = set()
    terminal = False
    for event in events:
        current = int(event.get("sequence") or 0)
        if current <= sequence:
            raise AssertionError("eval event sequence is not strictly increasing")
        sequence = current
        event_type = str(event.get("type") or "")
        if terminal:
            raise AssertionError("event recorded after terminal role_event")
        if event_type == "tool_call":
            call_id = str(event.get("id") or "")
            if not call_id or call_id in pending_tools:
                raise AssertionError("tool_call id must be unique and non-empty")
            pending_tools.add(call_id)
        elif event_type == "tool_result":
            call_id = str(event.get("tool_call_id") or "")
            if call_id not in pending_tools:
                raise AssertionError("tool_result has no matching tool_call")
            pending_tools.remove(call_id)
        elif event_type == "role_event" and str(event.get("status") or "") in {
            "completed",
            "failed",
            "cancelled",
        }:
            terminal = True
    if pending_tools:
        raise AssertionError("eval transcript has unresolved tool calls")


class HostedEvalAdapter(Protocol):
    """Real hosted-chain boundary used by integration and optional live evals."""

    def enqueue(
        self,
        *,
        provider: str,
        model: str,
        scenario_id: str,
        prompt: str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    def events_after(self, *, conversation_id: str, cursor: int) -> EvalEventPage: ...

    def snapshot(self, *, conversation_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class EvalEventPage:
    """One authoritative hosted-event page, including replay fences."""

    events: list[dict[str, Any]]
    cursor: int
    min_cursor: int = 0
    has_gap: bool = False
    reset_cursor: bool = False
    reset_reason: str = ""
    snapshot: dict[str, Any] | None = None
    account_generation: str = ""


class HostedEvalError(RuntimeError):
    """Base error carrying stable retry and diagnostic metadata."""

    retryable = False

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = str(error_code or "")


class RecoverableHostedEvalError(HostedEvalError):
    """Explicit adapter signal for a reconnectable hosted transport failure."""

    retryable = True


class TerminalHostedEvalError(HostedEvalError):
    """Explicit adapter signal for a terminal configuration or request failure."""


class HttpHostedEvalAdapter:
    """Opt-in adapter that evaluates the real authenticated hosted API.

    Fake adapters remain the default for deterministic CI. This adapter is
    deliberately explicit about server, bearer token, Profile and model so a
    live evaluation cannot silently fall back to a different runtime.
    Credentials travel only in the Authorization header.
    """

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        profile: str = "default",
        timeout_seconds: float = 30.0,
        request_open: Callable[..., Any] = urlopen,
    ) -> None:
        parsed = urlparse(str(base_url or "").strip())
        hostname = str(parsed.hostname or "").strip().lower()
        local = hostname == "localhost"
        if hostname:
            try:
                local = local or ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                pass
        if parsed.scheme not in ({"https"} if not local else {"http", "https"}):
            raise ValueError(
                "behavior eval base_url must use HTTPS (HTTP is loopback-only)"
            )
        token = str(bearer_token or "").strip()
        if not token:
            raise ValueError("behavior eval requires an explicit bearer token")
        self.base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        self.bearer_token = token
        self.profile = str(profile or "default").strip() or "default"
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._request_open = request_open
        self._turn_by_conversation: dict[str, str] = {}

    def _request(self, path: str, *, method: str = "GET", payload: Any = None):
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            return self._request_open(request, timeout=self.timeout_seconds)
        except HTTPError as exc:
            status = int(exc.code)
            detail = ""
            try:
                raw_detail = exc.read(8_192)
                decoded = raw_detail.decode("utf-8", errors="replace").strip()
                if decoded:
                    try:
                        parsed_detail = json.loads(decoded)
                    except json.JSONDecodeError:
                        parsed_detail = decoded
                    if isinstance(parsed_detail, dict):
                        detail = str(
                            parsed_detail.get("detail")
                            or parsed_detail.get("error")
                            or parsed_detail.get("message")
                            or ""
                        ).strip()
                    else:
                        detail = str(parsed_detail).strip()
            except (OSError, ValueError):
                detail = ""
            message = f"hosted eval HTTP {status}"
            if detail:
                message += f": {detail[:500]}"
            error_type = (
                RecoverableHostedEvalError
                if status in {408, 425, 429} or 500 <= status <= 599
                else TerminalHostedEvalError
            )
            raise error_type(
                message,
                status_code=status,
                error_code=f"http_{status}",
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            detail = str(getattr(exc, "reason", "") or exc).strip()
            message = "hosted eval transport failure"
            if detail:
                message += f": {detail[:500]}"
            raise RecoverableHostedEvalError(
                message,
                error_code="transport_failure",
            ) from exc

    def _json(
        self, path: str, *, method: str = "GET", payload: Any = None
    ) -> dict[str, Any]:
        response = self._request(path, method=method, payload=payload)
        try:
            raw = response.read()
        finally:
            response.close()
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoverableHostedEvalError(
                "hosted eval returned invalid JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise RecoverableHostedEvalError(
                "hosted eval returned a non-object payload"
            )
        return decoded

    def enqueue(
        self,
        *,
        provider: str,
        model: str,
        scenario_id: str,
        prompt: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity = hashlib.sha256(
            f"{scenario_id}\0{idempotency_key}".encode("utf-8")
        ).hexdigest()[:24]
        conversation_id = f"chat_eval_{identity}"
        created = self._json(
            "/api/plugins/collaboration/single/conversations",
            method="POST",
            payload={
                "profile": self.profile,
                "client_id": conversation_id,
                "title": f"Behavior eval: {scenario_id}"[:100],
            },
        )
        conversation = created.get("conversation")
        if not isinstance(conversation, dict) or not conversation.get("id"):
            raise AssertionError("behavior eval conversation was not created")
        conversation_id = str(conversation["id"])
        turn_id = f"eval-turn-{identity}"
        accepted = self._json(
            "/api/plugins/collaboration/single/conversations/"
            f"{quote(conversation_id, safe='')}/enqueue",
            method="POST",
            payload={
                "request_id": idempotency_key,
                "turn_id": turn_id,
                "message": {
                    "id": f"eval-message-{identity}",
                    "role": "user",
                    "name": "Behavior Eval",
                    "content": prompt,
                    "status": "completed",
                    "kind": "eval",
                },
                "recent_messages": [],
                "profiles": [self.profile],
                "attachment_ids": [],
                "attachment_context": "",
                "delivery_context": "Behavior evaluation; preserve exact errors and evidence.",
                "required_provider": provider,
                "required_model": model,
            },
        )
        binding = accepted.get("runtime_binding")
        if not isinstance(binding, dict) or binding.get("verified") is not True:
            raise AssertionError(
                "hosted eval server did not verify the requested runtime"
            )
        if (
            str(binding.get("provider") or "").casefold() != provider.casefold()
            or str(binding.get("model") or "").casefold() != model.casefold()
        ):
            raise AssertionError(
                "hosted eval server accepted a different provider/model"
            )
        self._turn_by_conversation[conversation_id] = turn_id
        return {
            **accepted,
            "accepted": accepted.get("accepted") is True,
            "conversation_id": conversation_id,
        }

    def events_after(self, *, conversation_id: str, cursor: int) -> EvalEventPage:
        path = (
            "/api/plugins/collaboration/single/conversations/"
            f"{quote(conversation_id, safe='')}/hosted-events?cursor={max(0, int(cursor))}"
        )
        response = self._request(path)
        data_lines: list[str] = []
        try:
            while True:
                raw_line = response.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if not line:
                    if data_lines:
                        break
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
        finally:
            response.close()
        if not data_lines:
            return EvalEventPage(events=[], cursor=max(0, int(cursor)))
        try:
            envelope = json.loads("\n".join(data_lines))
        except json.JSONDecodeError as exc:
            raise RecoverableHostedEvalError(
                "hosted eval returned invalid SSE data"
            ) from exc
        if not isinstance(envelope, dict):
            raise RecoverableHostedEvalError(
                "hosted eval SSE envelope is not an object"
            )
        events = envelope.get("events")
        if not isinstance(events, list):
            raise RecoverableHostedEvalError("hosted eval SSE envelope omitted events")
        snapshot = envelope.get("conversation")
        return EvalEventPage(
            events=[dict(item) for item in events if isinstance(item, dict)],
            cursor=max(0, int(envelope.get("cursor") or cursor)),
            min_cursor=max(0, int(envelope.get("min_cursor") or 0)),
            has_gap=bool(envelope.get("has_gap")),
            reset_cursor=bool(envelope.get("reset_cursor")),
            reset_reason=str(envelope.get("reset_reason") or ""),
            snapshot=dict(snapshot) if isinstance(snapshot, dict) else None,
            account_generation=str(envelope.get("account_generation") or "").strip(),
        )

    def snapshot(self, *, conversation_id: str) -> dict[str, Any]:
        response = self._json(
            "/api/plugins/collaboration/single/conversations/"
            f"{quote(conversation_id, safe='')}"
        )
        conversation = response.get("conversation")
        if not isinstance(conversation, dict):
            raise RecoverableHostedEvalError(
                "hosted eval snapshot omitted conversation"
            )
        turn_id = self._turn_by_conversation.get(conversation_id, "")
        turns = conversation.get("hosted_turns")
        turn = turns.get(turn_id) if isinstance(turns, dict) else None
        evidence = _runtime_evidence(turn or {})
        return {
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "status": str((turn or {}).get("status") or "running"),
            "hosted_event_cursor": max(
                0, int(conversation.get("hosted_event_cursor") or 0)
            ),
            "account_generation": str(
                conversation.get("account_generation") or ""
            ).strip(),
            **evidence,
        }


def _usage_counts(value: Any) -> tuple[int, int, bool]:
    if not isinstance(value, dict):
        return 0, 0, False
    usage = value.get("usage") or value.get("token_usage")
    usage = usage if isinstance(usage, dict) else value
    input_value = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_value = usage.get("output_tokens", usage.get("completion_tokens"))
    observed = input_value is not None or output_value is not None
    try:
        input_tokens = max(0, int(input_value or 0))
        output_tokens = max(0, int(output_value or 0))
    except (TypeError, ValueError):
        return 0, 0, False
    return input_tokens, output_tokens, observed


def _runtime_evidence(value: Any) -> dict[str, Any]:
    provider = ""
    model = ""
    input_tokens = 0
    output_tokens = 0
    usage_observed = False

    def walk(item: Any) -> None:
        nonlocal provider, model, input_tokens, output_tokens, usage_observed
        if isinstance(item, dict):
            provider = provider or str(item.get("actual_provider") or "").strip()
            model = model or str(item.get("actual_model") or "").strip()
            seen_input, seen_output, seen_usage = _usage_counts(item)
            if seen_usage:
                input_tokens = max(input_tokens, seen_input)
                output_tokens = max(output_tokens, seen_output)
                usage_observed = True
            for child in item.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return {
        "actual_provider": provider,
        "actual_model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "usage_observed": usage_observed,
    }


def _apply_runtime_evidence(
    run: EvalRun,
    value: Any,
    *,
    cumulative: bool,
) -> None:
    evidence = _runtime_evidence(value)
    actual_provider = str(evidence.get("actual_provider") or "")
    actual_model = str(evidence.get("actual_model") or "")
    if actual_provider and actual_provider.casefold() != run.provider.casefold():
        raise AssertionError(
            f"hosted eval provider mismatch: expected {run.provider}, got {actual_provider}"
        )
    if actual_model and actual_model.casefold() != run.model.casefold():
        raise AssertionError(
            f"hosted eval model mismatch: expected {run.model}, got {actual_model}"
        )
    run.actual_provider = actual_provider or run.actual_provider
    run.actual_model = actual_model or run.actual_model
    if evidence.get("usage_observed"):
        if cumulative:
            run.input_tokens = max(run.input_tokens, int(evidence["input_tokens"]))
            run.output_tokens = max(run.output_tokens, int(evidence["output_tokens"]))
        else:
            run.input_tokens += int(evidence["input_tokens"])
            run.output_tokens += int(evidence["output_tokens"])
        run.usage_observed = True


def _event_entity_id(event: dict[str, Any], payload: dict[str, Any]) -> str:
    return str(
        payload.get("entity_id")
        or event.get("entity_id")
        or event.get("event_id")
        or ""
    ).strip()


def _payload_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return str(value)
    return ""


@dataclass
class _TranscriptState:
    messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    thinking: dict[str, dict[str, Any]] = field(default_factory=dict)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    commands: dict[str, dict[str, Any]] = field(default_factory=dict)
    attachments: set[str] = field(default_factory=set)
    model_retry_attempts: set[str] = field(default_factory=set)

    def clear(self, run: EvalRun) -> None:
        run.events.clear()
        run.model_retries = 0
        self.messages.clear()
        self.thinking.clear()
        self.tools.clear()
        self.commands.clear()
        self.attachments.clear()
        self.model_retry_attempts.clear()

    def _record_attachments(self, run: EvalRun, payload: dict[str, Any]) -> None:
        candidates: list[Any] = []
        for key in (
            "attachment",
            "attachments",
            "file",
            "files",
            "artifact",
            "artifacts",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
            elif value is not None:
                candidates.append(value)
        for value in candidates:
            if isinstance(value, dict):
                attachment_id = str(
                    value.get("id")
                    or value.get("file_id")
                    or value.get("artifact_id")
                    or value.get("sha256")
                    or ""
                ).strip()
                digest = str(value.get("sha256") or value.get("hash") or "").strip()
                disposition = str(
                    value.get("disposition") or value.get("kind") or "attachment"
                ).strip()
            else:
                attachment_id = str(value or "").strip()
                digest = ""
                disposition = "attachment"
            if not attachment_id or attachment_id in self.attachments:
                continue
            self.attachments.add(attachment_id)
            run.record(
                "attachment",
                id=attachment_id,
                hash=digest,
                disposition=disposition,
            )

    def _finish_pending(self, run: EvalRun, *, cancelled: bool = False) -> None:
        for entity_id, state in self.messages.items():
            if state.get("recorded"):
                continue
            content = "".join(state.get("chunks") or [])
            if content:
                run.record(
                    "message",
                    role=state.get("role") or "assistant",
                    content=content,
                    entity_id=entity_id,
                )
                state["recorded"] = True
        for entity_id, state in self.thinking.items():
            if state.get("recorded"):
                continue
            summary = "".join(state.get("chunks") or []) or "hidden"
            run.record("thinking", summary=summary, entity_id=entity_id)
            state["recorded"] = True
        for entity_id, state in {**self.tools, **self.commands}.items():
            if state.get("started") and not state.get("completed"):
                run.record(
                    "tool_result",
                    tool_call_id=entity_id,
                    name=state.get("name"),
                    content="cancelled" if cancelled else "incomplete",
                    error=True,
                    cancelled=cancelled,
                )
                state["completed"] = True

    def apply_snapshot(self, run: EvalRun, snapshot: dict[str, Any]) -> None:
        messages = snapshot.get("messages")
        if isinstance(messages, list):
            for index, item in enumerate(messages):
                if not isinstance(item, dict):
                    continue
                entity_id = str(item.get("id") or f"snapshot-message-{index}")
                if entity_id in self.messages and self.messages[entity_id].get(
                    "recorded"
                ):
                    continue
                content = str(item.get("content") or "")
                if not content:
                    continue
                self.messages[entity_id] = {
                    "chunks": [content],
                    "role": str(item.get("role") or "assistant"),
                    "recorded": True,
                }
                run.record(
                    "message",
                    role=str(item.get("role") or "assistant"),
                    content=content,
                    entity_id=entity_id,
                    snapshot=True,
                )
                self._record_attachments(run, item)
        run.snapshot_applied = True

    def apply_event(
        self,
        run: EvalRun,
        event: dict[str, Any],
        payload: dict[str, Any],
    ) -> str | None:
        event_type = str(event.get("event_type") or "")
        entity_id = _event_entity_id(event, payload)
        self._record_attachments(run, payload)

        if event_type.startswith("message."):
            state = self.messages.setdefault(
                entity_id,
                {
                    "chunks": [],
                    "role": str(payload.get("role") or "assistant"),
                    "recorded": False,
                },
            )
            if event_type == "message.delta":
                state["chunks"].append(
                    _payload_text(payload, "delta", "content", "text")
                )
            elif event_type == "message.completed" and not state["recorded"]:
                content = _payload_text(payload, "content", "text")
                if not content:
                    content = "".join(state["chunks"])
                run.record(
                    "message",
                    role=state["role"],
                    content=content,
                    entity_id=entity_id,
                )
                state["recorded"] = True
            return None

        if event_type.startswith("thinking."):
            state = self.thinking.setdefault(
                entity_id,
                {"chunks": [], "recorded": False},
            )
            if event_type == "thinking.delta":
                state["chunks"].append(
                    _payload_text(payload, "delta", "summary", "content", "text")
                )
            elif event_type == "thinking.completed" and not state["recorded"]:
                summary = _payload_text(payload, "summary", "content", "text")
                if not summary:
                    summary = "".join(state["chunks"]) or "hidden"
                run.record("thinking", summary=summary, entity_id=entity_id)
                state["recorded"] = True
            return None

        if event_type == "tool.started":
            state = self.tools.setdefault(entity_id, {})
            if not state.get("started"):
                state.update({
                    "started": True,
                    "completed": False,
                    "name": payload.get("name"),
                })
                run.record(
                    "tool_call",
                    id=entity_id,
                    name=payload.get("name"),
                    arguments=payload.get("args") or payload.get("arguments"),
                )
            return None
        if event_type == "tool.progress":
            return None
        if event_type in {"tool.completed", "tool.failed"}:
            state = self.tools.get(entity_id)
            if not state or not state.get("started"):
                raise TerminalHostedEvalError(
                    f"tool result has no retained start: {entity_id}",
                    error_code="orphan_tool_result",
                )
            if not state.get("completed"):
                failed = event_type == "tool.failed"
                run.record(
                    "tool_result",
                    tool_call_id=entity_id,
                    name=payload.get("name") or state.get("name"),
                    content=(
                        _payload_text(payload, "error", "result", "content")
                        if failed
                        else _payload_text(payload, "result", "content", "output")
                    ),
                    error=failed,
                )
                state["completed"] = True
            return None

        if event_type == "command.started":
            state = self.commands.setdefault(entity_id, {})
            if not state.get("started"):
                command = _payload_text(payload, "command", "input")
                state.update({
                    "started": True,
                    "completed": False,
                    "name": payload.get("name") or "command",
                    "chunks": [],
                })
                run.record(
                    "tool_call",
                    id=entity_id,
                    name=state["name"],
                    arguments={"command": command},
                    command=True,
                )
            return None
        if event_type == "command.output":
            state = self.commands.get(entity_id)
            if not state or not state.get("started"):
                raise TerminalHostedEvalError(
                    f"command output has no retained start: {entity_id}",
                    error_code="orphan_command_output",
                )
            state["chunks"].append(_payload_text(payload, "output", "content", "text"))
            return None
        if event_type in {"command.completed", "command.failed"}:
            state = self.commands.get(entity_id)
            if not state or not state.get("started"):
                raise TerminalHostedEvalError(
                    f"command result has no retained start: {entity_id}",
                    error_code="orphan_command_result",
                )
            if not state.get("completed"):
                content = _payload_text(payload, "result", "output", "error")
                if not content:
                    content = "".join(state.get("chunks") or [])
                failed = event_type == "command.failed"
                run.record(
                    "tool_result",
                    tool_call_id=entity_id,
                    name=state.get("name"),
                    content=content,
                    error=failed,
                    command=True,
                )
                state["completed"] = True
            return None

        if event_type.startswith("connection.retry_"):
            attempt_key = str(payload.get("attempt") or entity_id).strip()
            if (
                event_type
                in {"connection.retry_scheduled", "connection.retry_started"}
                and attempt_key
                and attempt_key not in self.model_retry_attempts
            ):
                self.model_retry_attempts.add(attempt_key)
                run.model_retries += 1
            run.record(
                "role_event",
                role_stage=event.get("role_stage") or "connection",
                status=event_type.removeprefix("connection."),
                attempt=payload.get("attempt"),
                error=payload.get("error"),
            )
            return None
        if event_type == "role.rework_requested":
            run.record("role_event", role_stage="reviewer", status="rework")
            return None
        if event_type == "role.handoff":
            run.record(
                "role_event",
                role_stage=event.get("role_stage") or payload.get("to_role"),
                status=str(payload.get("action") or "handoff"),
            )
            return None
        if event_type.startswith("intervention."):
            run.record(
                "role_event",
                role_stage=event.get("role_stage") or payload.get("target_role"),
                status=f"intervention_{event_type.removeprefix('intervention.')}",
            )
            return None
        if event_type.startswith("turn.") or event_type.startswith("role."):
            status = event_type.rsplit(".", 1)[-1]
            if event_type in {"turn.completed", "turn.failed", "turn.cancelled"}:
                self._finish_pending(run, cancelled=event_type == "turn.cancelled")
            run.record(
                "role_event",
                role_stage=event.get("role_stage"),
                status=status,
            )
            if event_type in {"turn.completed", "turn.failed", "turn.cancelled"}:
                return status
        return None


def _coerce_event_page(value: Any, *, requested_cursor: int) -> EvalEventPage:
    if isinstance(value, EvalEventPage):
        return value
    if isinstance(value, list):
        # Compatibility for narrowly scoped adapters. Production and integration
        # adapters must return EvalEventPage so replay fences are not discarded.
        events = [dict(item) for item in value if isinstance(item, dict)]
        next_cursor = max([
            requested_cursor,
            *[int(item.get("cursor") or 0) for item in events],
        ])
        generation = next(
            (
                str(item.get("account_generation") or "").strip()
                for item in events
                if str(item.get("account_generation") or "").strip()
            ),
            "",
        )
        return EvalEventPage(
            events=events,
            cursor=next_cursor,
            account_generation=generation,
        )
    raise RecoverableHostedEvalError(
        "hosted eval adapter returned an invalid event page",
        error_code="invalid_event_page",
    )


def _validate_event_page(
    page: EvalEventPage,
    *,
    requested_cursor: int,
    expected_generation: str,
) -> tuple[list[dict[str, Any]], str]:
    page_generation = str(page.account_generation or "").strip()
    if expected_generation and not page_generation:
        raise TerminalHostedEvalError(
            "hosted eval event page omitted its account generation",
            error_code="account_generation_missing",
        )
    if expected_generation and page_generation != expected_generation:
        raise TerminalHostedEvalError(
            "hosted eval account generation changed during the run",
            error_code="account_generation_changed",
        )
    generation = expected_generation or page_generation
    validated: list[dict[str, Any]] = []
    prior_cursor = -1
    for raw in page.events:
        if not isinstance(raw, dict):
            raise RecoverableHostedEvalError(
                "hosted eval event page contains a non-object event",
                error_code="invalid_event",
            )
        event = dict(raw)
        try:
            event_cursor = int(event.get("cursor") or 0)
        except (TypeError, ValueError) as exc:
            raise RecoverableHostedEvalError(
                "hosted eval event cursor is invalid",
                error_code="invalid_event_cursor",
            ) from exc
        if event_cursor <= 0 or event_cursor <= prior_cursor:
            raise RecoverableHostedEvalError(
                "hosted eval event cursors are not strictly increasing",
                error_code="invalid_event_cursor_order",
            )
        prior_cursor = event_cursor
        event_generation = str(event.get("account_generation") or "").strip()
        if generation and not event_generation:
            raise TerminalHostedEvalError(
                "hosted eval page contains an event without an account generation",
                error_code="event_account_generation_missing",
            )
        if generation and event_generation != generation:
            raise TerminalHostedEvalError(
                "hosted eval page contains an event from another account generation",
                error_code="stale_account_generation_event",
            )
        if not generation and event_generation:
            generation = event_generation
        validated.append(event)
    if validated and int(page.cursor) < int(validated[-1].get("cursor") or 0):
        raise RecoverableHostedEvalError(
            "hosted eval page cursor precedes its final event",
            error_code="invalid_page_cursor",
        )
    if page.reset_cursor and page.snapshot is None:
        raise RecoverableHostedEvalError(
            "hosted eval cursor reset omitted its authoritative snapshot",
            error_code="reset_snapshot_missing",
        )
    if page.reset_cursor and validated:
        raise RecoverableHostedEvalError(
            "hosted eval cursor reset unexpectedly included incremental events",
            error_code="reset_events_present",
        )
    if page.has_gap and not page.snapshot:
        raise RecoverableHostedEvalError(
            "hosted eval replay gap omitted its authoritative snapshot",
            error_code="gap_snapshot_missing",
        )
    if not page.reset_cursor:
        validated = [
            item
            for item in validated
            if int(item.get("cursor") or 0) > requested_cursor
        ]
    return validated, generation


def _eval_identity(
    *,
    provider: str,
    model: str,
    scenario_id: str,
    prompt: str,
    eval_run_id: str,
    code_revision: str,
) -> tuple[str, str]:
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    material = json.dumps(
        {
            "provider": provider.casefold(),
            "model": model.casefold(),
            "scenario": scenario_id,
            "prompt_hash": prompt_hash,
            "eval_run_id": eval_run_id,
            "code_revision": code_revision,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    identity = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return prompt_hash, f"eval:{identity}"


def run_hosted_behavior_scenario(
    adapter: HostedEvalAdapter,
    *,
    provider: str,
    model: str,
    scenario_id: str,
    prompt: str,
    timeout_seconds: float = 30.0,
    poll_interval: float = 0.05,
    max_retries: int = 5,
    eval_run_id: str | None = None,
    code_revision: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Execute one durable hosted scenario with no provider/model fallback."""

    # Transitional Windows calibration: schema initialization, durable JSON
    # checkpoints, and antivirus interception can each add seconds on a loaded
    # Windows host. Keep the functional chain unchanged; widen only the test
    # observation window so it measures completion rather than filesystem noise.
    import sys as _sys

    if _sys.platform == "win32":
        timeout_seconds = max(timeout_seconds, timeout_seconds * 10.0)
    if not provider.strip() or not model.strip():
        raise ValueError("behavior eval requires an explicit provider and model")
    normalized_run_id = str(eval_run_id or f"run_{uuid.uuid4().hex}").strip()
    if not normalized_run_id:
        raise ValueError("behavior eval run id is required")
    revision = str(
        code_revision
        or os.getenv("HERMES_BUILD_REVISION")
        or os.getenv("GITHUB_SHA")
        or "working-tree"
    ).strip()
    prompt_hash, idempotency_key = _eval_identity(
        provider=provider,
        model=model,
        scenario_id=scenario_id,
        prompt=prompt,
        eval_run_id=normalized_run_id,
        code_revision=revision,
    )
    run = EvalRun(
        provider=provider,
        model=model,
        scenario=scenario_id,
        eval_run_id=normalized_run_id,
        idempotency_key=idempotency_key,
        code_revision=revision,
        prompt_hash=prompt_hash,
        attempts=1,
    )
    transcript = _TranscriptState()
    retry_limit = max(0, int(max_retries))
    deadline = time.monotonic() + max(0.1, timeout_seconds)

    def fail(error: HostedEvalError | str, *, status_code: int | None = None):
        message = str(error)
        run.last_error = message
        run.last_status_code = (
            error.status_code if isinstance(error, HostedEvalError) else status_code
        )
        transcript._finish_pending(run, cancelled=True)
        run.record(
            "role_event",
            role_stage="evaluation",
            status="failed",
            error=message,
            status_code=run.last_status_code,
            error_code=(error.error_code if isinstance(error, HostedEvalError) else ""),
        )
        assert_event_order(run.events)
        return run.finish("failed")

    def retry(error: RecoverableHostedEvalError) -> bool:
        run.last_error = str(error)
        run.last_status_code = error.status_code
        if run.retries >= retry_limit or time.monotonic() >= deadline:
            return False
        run.retries += 1
        run.attempts = run.retries + 1
        sleep(max(0.0, poll_interval))
        # Test callers commonly inject a no-op clock to keep deterministic
        # evaluations fast.  Still yield the OS scheduler so a background
        # hosted workflow can publish the retryable result.
        time.sleep(0.001)
        return True

    while True:
        try:
            accepted = adapter.enqueue(
                provider=provider,
                model=model,
                scenario_id=scenario_id,
                prompt=prompt,
                idempotency_key=idempotency_key,
            )
            break
        except TerminalHostedEvalError as exc:
            return fail(exc)
        except RecoverableHostedEvalError as exc:
            if not retry(exc):
                return fail(exc)
    conversation_id = str(accepted.get("conversation_id") or "")
    if not conversation_id or accepted.get("accepted") is not True:
        return fail(
            TerminalHostedEvalError(
                str(accepted.get("error") or "hosted eval was not durably accepted"),
                status_code=(
                    int(accepted["status_code"])
                    if accepted.get("status_code") is not None
                    else None
                ),
                error_code=str(accepted.get("error_code") or "not_accepted"),
            )
        )
    binding = accepted.get("runtime_binding")
    if isinstance(binding, dict) and binding.get("verified") is True:
        run.runtime_binding_verified = True
        run.actual_provider = str(binding.get("provider") or "")
        run.actual_model = str(binding.get("model") or "")
        _apply_runtime_evidence(
            run,
            {
                "actual_provider": run.actual_provider,
                "actual_model": run.actual_model,
            },
            cumulative=True,
        )
    run.account_generation = str(accepted.get("account_generation") or "").strip()
    cursor = 0
    seen_event_ids: set[str] = set()
    while time.monotonic() < deadline:
        try:
            raw_page = adapter.events_after(
                conversation_id=conversation_id,
                cursor=cursor,
            )
            page = _coerce_event_page(raw_page, requested_cursor=cursor)
            events, page_generation = _validate_event_page(
                page,
                requested_cursor=cursor,
                expected_generation=run.account_generation,
            )
        except TerminalHostedEvalError as exc:
            return fail(exc)
        except RecoverableHostedEvalError as exc:
            if not retry(exc):
                return fail(exc)
            continue
        if page_generation:
            run.account_generation = page_generation
        if page.has_gap or page.reset_cursor:
            transcript.clear(run)
            seen_event_ids.clear()
            if page.has_gap:
                run.replay_gaps += 1
            if page.reset_cursor:
                run.cursor_resets += 1
                cursor = max(0, int(page.cursor))
                run.cursor = cursor
            if page.snapshot:
                transcript.apply_snapshot(run, page.snapshot)
                _apply_runtime_evidence(run, page.snapshot, cumulative=True)
        for event in events:
            event_cursor = int(event.get("cursor") or 0)
            event_id = str(event.get("event_id") or "")
            if event_cursor <= cursor or (event_id and event_id in seen_event_ids):
                continue
            if event_id:
                seen_event_ids.add(event_id)
            cursor = max(cursor, event_cursor)
            event_type = str(event.get("event_type") or "")
            raw_payload = event.get("payload")
            payload: dict[str, Any] = (
                dict(raw_payload) if isinstance(raw_payload, dict) else {}
            )
            _apply_runtime_evidence(run, payload, cumulative=False)
            try:
                terminal = transcript.apply_event(run, event, payload)
            except TerminalHostedEvalError as exc:
                return fail(exc)
            if terminal:
                run.cursor = cursor
                assert_event_order(run.events)
                return run.finish(
                    terminal if terminal in {"failed", "cancelled"} else "completed"
                )
        cursor = max(cursor, int(page.cursor))
        run.cursor = cursor
        try:
            snapshot = adapter.snapshot(conversation_id=conversation_id)
        except TerminalHostedEvalError as exc:
            return fail(exc)
        except RecoverableHostedEvalError as exc:
            if not retry(exc):
                return fail(exc)
            continue
        snapshot_generation = str(snapshot.get("account_generation") or "").strip()
        if (
            run.account_generation
            and snapshot_generation
            and snapshot_generation != run.account_generation
        ):
            return fail(
                TerminalHostedEvalError(
                    "hosted eval snapshot account generation changed during the run",
                    error_code="account_generation_changed",
                )
            )
        if snapshot_generation:
            run.account_generation = snapshot_generation
        state = str(snapshot.get("status") or "")
        _apply_runtime_evidence(run, snapshot, cumulative=True)
        if state in {"completed", "failed", "cancelled"}:
            authoritative_cursor = max(
                0, int(snapshot.get("hosted_event_cursor") or cursor)
            )
            if cursor < authoritative_cursor:
                sleep(max(0.0, poll_interval))
                time.sleep(0.001)
                continue
            transcript._finish_pending(run, cancelled=state == "cancelled")
            run.record("role_event", role_stage="snapshot", status=state)
            assert_event_order(run.events)
            return run.finish(state)
        sleep(max(0.0, poll_interval))
        # ``sleep`` may be an injected no-op; preserve a real scheduler yield
        # between polls so the producer thread is not starved.
        time.sleep(0.001)
    return fail(
        TerminalHostedEvalError(
            f"hosted behavior eval timed out: {scenario_id}",
            error_code="evaluation_timeout",
        )
    )
