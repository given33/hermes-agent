from __future__ import annotations

import importlib.util
from collections import Counter
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any
import uuid

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from hermes_cli import managed_installations
from hermes_services.behavior_eval import (
    EvalEventPage,
    RecoverableHostedEvalError,
    TerminalHostedEvalError,
    run_hosted_behavior_scenario,
)
from hermes_services.hosted_event_protocol import validate_event_envelope
from hermes_services.tool_output_artifacts import EncryptedToolArtifactStore


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "collaboration"
    / "dashboard"
    / "plugin_api.py"
)
API_PREFIX = "/api/plugins/collaboration"
OWNER_ID = "p7-product-owner"
PROVIDER_NAME = "fake-provider"
MODEL_NAME = "fake-model"


def _load_collaboration_module():
    module_name = f"collaboration_p7_product_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _client(module: Any, owner_id: str) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def attach_identity(request: Request, call_next):
        request.state.session = SimpleNamespace(user_id=owner_id)
        return await call_next(request)

    app.include_router(module.router, prefix=API_PREFIX)
    return TestClient(app)


def _review_control(verdict: str = "PASS") -> str:
    passing = verdict == "PASS"
    return json.dumps(
        {
            "protocol": "hermes.review.v1",
            "verdict": verdict,
            "checks": {
                "requirements_met": passing,
                "evidence_verified": passing,
                "tests_passed": passing,
                "risks_resolved": passing,
            },
            "blockers": [] if passing else ["验收证据缺失"],
            "findings": [] if passing else ["测试结果尚未提交"],
            "required_actions": [] if passing else ["补齐测试证据并重新验收"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _supervision_control(verdict: str = "PASS") -> str:
    passing = verdict == "PASS"
    return json.dumps(
        {
            "protocol": "hermes.supervision.v1",
            "verdict": verdict,
            "checks": {
                "role_boundaries_respected": passing,
                "task_coverage_complete": passing,
                "evidence_sufficient": passing,
                "process_compliant": passing,
            },
            "blockers": [] if passing else ["Manager 遗漏验收步骤"],
            "findings": [] if passing else ["计划没有独立的测试验收项"],
            "required_actions": (
                []
                if passing
                else ["@Hermes Manager 补充测试验收步骤后重新提交监督检查"]
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


class DeterministicProvider:
    """The sole test double: deterministic responses at the provider boundary."""

    def __init__(
        self,
        *,
        scenario: str,
        mode: str,
        failures: list[Exception] | None = None,
        max_attempts: int = 2,
    ) -> None:
        self.scenario = scenario
        self.mode = mode
        self.failures = list(failures or [])
        self.max_attempts = max(1, int(max_attempts))
        self.classifier_calls = 0
        self.calls: list[tuple[str, str]] = []
        self.call_counts: Counter[str] = Counter()
        self.manager_profile = "hermes-manager"
        self.reporter_prompts: list[str] = []
        self.workflow_errors: list[str] = []
        self.evidence: dict[str, Any] = {}
        self.uploaded: dict[str, Any] | None = None
        self.tool_started = threading.Event()
        self.tool_release = threading.Event()
        self.intervention_queued = False
        self.intervention_reply_sent = False
        self._lock = threading.Lock()

    def profiles(self) -> list[dict[str, Any]]:
        return [
            {
                "name": profile,
                "provider": PROVIDER_NAME,
                "model": MODEL_NAME,
            }
            for profile in (
                "default",
                "dbb3-manager",
                "dbb3-worker",
                "reviewer",
                "supervisor",
            )
        ]

    def call_auxiliary(self, *, task: str, **_kwargs: Any) -> Any:
        assert task == "kanban_decomposer"
        self.call_counts["kanban-decomposer"] += 1
        content = json.dumps(
            {
                "fanout": False,
                "rationale": "the hosted manager already owns the durable plan",
                "title": f"P7 {self.scenario}",
                "body": "Execute the hosted manager plan and preserve its evidence.",
                "assignee": None,
            },
            separators=(",", ":"),
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

    def classify_intent(self, _content: str, **_kwargs: Any) -> dict[str, Any]:
        self.classifier_calls += 1
        return {
            "mode": self.mode,
            "confidence": 0.99,
            "reason": "deterministic provider classification",
            "profiles": [] if self.mode == "chat" else ["dbb3-worker"],
            "targets": [] if self.mode == "chat" else ["dbb3"],
            "needs_execution": self.mode == "work",
            "needs_tools": self.mode == "work",
            "mutates_state": self.mode == "work",
            "artifact": {"decision": "none", "types": [], "reason": ""},
        }

    def _manager_plan(self) -> str:
        if self.scenario == "05-supervisor-intervention":
            steps = [
                {
                    "id": "step-implement",
                    "title": "implement",
                    "objective": "implement the requested change",
                    "assignee": "dbb3-worker",
                    "depends_on": [],
                }
            ]
        else:
            steps = [
                {
                    "id": "step-inspect",
                    "title": "inspect",
                    "objective": "inspect the authoritative state",
                    "assignee": "dbb3-worker",
                    "depends_on": [],
                },
                {
                    "id": "step-implement",
                    "title": "implement",
                    "objective": "perform the requested work",
                    "assignee": "dbb3-worker",
                    "depends_on": ["step-inspect"],
                },
                {
                    "id": "step-test",
                    "title": "test",
                    "objective": "capture verification evidence",
                    "assignee": "dbb3-worker",
                    "depends_on": ["step-implement"],
                },
            ]
        return json.dumps(
            {
                "difficulty": "medium",
                "reason": "one bounded execution lane is sufficient",
                "workers": ["dbb3-worker"],
                "reviewer_target": "dbb3",
                "plan": steps,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _complete_installation(self, artifact_context: dict[str, str]) -> None:
        owner_id = artifact_context["owner_id"]
        generation = artifact_context["account_generation"]
        db_path = managed_installations.managed_installations_db_path()
        operation = managed_installations.create_managed_installation(
            kind="skill",
            identifier="example-product-skill",
            request_id=f"install-{self.scenario}",
            targets=["server"],
            db_path=db_path,
            owner_id=owner_id,
            account_generation=generation,
        )
        claimed = managed_installations._claim_target(
            db_path,
            now=time.time(),
            lease_seconds=30,
        )
        assert claimed is not None
        try:
            assert managed_installations._finish_target(
                db_path,
                claimed,
                state="completed",
                detail={
                    "proof_schema": 1,
                    "proof_source": "local_filesystem",
                    "version": "1.0.0",
                },
            )
        finally:
            managed_installations._release_execution_fence(claimed)
        self.evidence["installation"] = managed_installations.get_managed_installation(
            operation["id"],
            db_path=db_path,
            owner_id=owner_id,
            account_generation=generation,
        )
        self.evidence["resource_catalog"] = (
            managed_installations.list_managed_resources(
                db_path=db_path,
                owner_id=owner_id,
                account_generation=generation,
            )
        )

    def _store_tool_artifact(
        self,
        artifact_context: dict[str, str],
        *,
        tool_call_id: str,
    ) -> dict[str, Any]:
        store = EncryptedToolArtifactStore(Path(artifact_context["root"]))
        artifact = store.put(
            owner_id=artifact_context["owner_id"],
            account_generation=artifact_context["account_generation"],
            conversation_id=artifact_context["conversation_id"],
            turn_id=artifact_context["turn_id"],
            tool_call_id=tool_call_id,
            tool_name="terminal",
            content="complete tool output",
        )
        self.evidence["artifact"] = artifact
        return artifact

    def _run_worker(
        self,
        call_number: int,
        *,
        event_callback: Any,
        artifact_context: dict[str, str],
    ) -> str:
        if self.scenario == "09-targeted-intervention":
            if self.intervention_queued and not self.intervention_reply_sent:
                self.intervention_reply_sent = True
                return "已接受定向调整，并将从安全检查点继续。"

        tool_id = f"{self.scenario}:worker:{call_number}"
        event_callback({
            "type": "tool.start",
            "payload": {
                "tool_id": tool_id,
                "name": "terminal",
                "args": {"command": "verify-product-chain"},
            },
        })
        attachments: list[dict[str, Any]] = []
        if self.scenario in {"07-ios-background", "09-targeted-intervention"}:
            self.tool_started.set()
            if not self.tool_release.wait(timeout=30):
                raise TimeoutError("product-chain tool gate timed out")
        if self.scenario == "11-resource-refresh" and (
            "installation" not in self.evidence
        ):
            self._complete_installation(artifact_context)
        if self.scenario == "12-file-artifact-deletion" and call_number == 1:
            artifact = self._store_tool_artifact(
                artifact_context,
                tool_call_id=tool_id,
            )
            assert self.uploaded is not None
            attachments = [self.uploaded, artifact]
        event_callback({
            "type": "tool.complete",
            "payload": {
                "tool_id": tool_id,
                "name": "terminal",
                "result_text": "verified worker evidence",
                "duration_s": 0.01,
                "attachments": attachments,
            },
        })
        return f"verified worker evidence {call_number}"

    def run(
        self,
        profile: str,
        prompt: str,
        *,
        event_callback: Any = None,
        artifact_context: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> str:
        with self._lock:
            self.call_counts[profile] += 1
            call_number = self.call_counts[profile]
            self.calls.append((profile, prompt))
        if event_callback is not None:
            event_callback({
                "type": "session.info",
                "payload": {
                    "provider": PROVIDER_NAME,
                    "model": MODEL_NAME,
                },
            })
        if profile == "default" and self.mode == "chat" and self.failures:
            raise self.failures.pop(0)
        if profile in {"dbb3-manager", self.manager_profile}:
            if call_number == 1:
                return self._manager_plan()
            return json.dumps(
                {"suggested_conclusion": "all verified handoff data is ready"},
                separators=(",", ":"),
            )
        if profile == "supervisor":
            if self.scenario == "05-supervisor-intervention" and call_number == 1:
                return _supervision_control("CORRECTIVE_ACTION")
            return _supervision_control()
        if profile == "dbb3-worker":
            assert event_callback is not None
            assert artifact_context is not None
            return self._run_worker(
                call_number,
                event_callback=event_callback,
                artifact_context=artifact_context,
            )
        if profile == "reviewer":
            if self.scenario == "04-review-rework" and call_number == 1:
                return _review_control("REWORK")
            return _review_control()
        if self.mode == "chat":
            return "你好" if self.scenario == "01-simple-chat" else "recovered response"
        self.reporter_prompts.append(prompt)
        return "verified report from authoritative handoff"


class ProductChainAdapter:
    """In-process client of the real collaboration router and hosted consumer."""

    def __init__(
        self,
        root: Path,
        *,
        module: Any,
        provider: DeterministicProvider,
        page_size: int = 500,
    ) -> None:
        self.root = root
        self.module = module
        self.provider = provider
        self.page_size = page_size
        self.owner_id = OWNER_ID
        self.client = _client(module, self.owner_id)
        self.conversation_id = ""
        self.turn_id = ""
        self.account_generation = ""
        self.requested_cursors: list[int] = []
        self.returned_cursors: list[int] = []
        self.evidence: dict[str, Any] = {}
        self._workflow_thread: threading.Thread | None = None
        self._intervention_sent = False
        self._uploaded: dict[str, Any] | None = None

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()

    def close(self) -> None:
        self.provider.tool_release.set()
        self.wait_for_workflow()
        self.client.close()
        sys.modules.pop(self.module.__name__, None)

    def _response_json(self, response: Any) -> dict[str, Any]:
        if response.status_code >= 500:
            raise RecoverableHostedEvalError(
                str(response.text),
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise TerminalHostedEvalError(
                str(response.text),
                status_code=response.status_code,
            )
        value = response.json()
        assert isinstance(value, dict)
        return value

    def _conversation(self) -> dict[str, Any]:
        conversation = self.module._live_conversation_snapshot(
            self.conversation_id,
            self.owner_id,
        )
        if conversation is not None:
            return conversation
        with self.module._STATE_LOCK:
            state = self.module.load_single_state()
            return next(
                conversation
                for conversation in state["conversations"]
                if conversation["id"] == self.conversation_id
            )

    def _upload_fixture(self) -> None:
        if self._uploaded is not None:
            return
        response = self.client.post(
            f"{API_PREFIX}/single/conversations/{self.conversation_id}/attachments",
            content=b"uploaded account data",
            headers={
                "x-filename": "upload.txt",
                "content-type": "text/plain",
                "x-turn-id": self.turn_id,
                "x-upload-id": "p7-upload-0001",
            },
        )
        self._uploaded = self._response_json(response)["attachment"]
        self.provider.uploaded = self._uploaded

    def enqueue(
        self,
        *,
        provider: str,
        model: str,
        scenario_id: str,
        prompt: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity = idempotency_key.rsplit(":", 1)[-1]
        self.conversation_id = f"chat_p7_{identity}"
        self.turn_id = f"turn-p7-{identity}"
        created = self._response_json(
            self.client.post(
                f"{API_PREFIX}/single/conversations",
                json={
                    "profile": "default",
                    "client_id": self.conversation_id,
                    "title": f"P7 {scenario_id}",
                },
            )
        )["conversation"]
        self.account_generation = str(created.get("account_generation") or "")
        if scenario_id == "12-file-artifact-deletion":
            self._upload_fixture()
        body = {
            "request_id": idempotency_key,
            "turn_id": self.turn_id,
            "message": {
                "id": f"message-{identity}",
                "role": "user",
                "name": "Behavior Eval",
                "content": prompt,
                "status": "completed",
                "kind": "eval",
            },
            "recent_messages": [],
            "profiles": ["default"],
            "attachment_ids": (
                [self._uploaded["id"]] if self._uploaded is not None else []
            ),
            "attachment_context": "",
            "delivery_context": "P7 deterministic product-chain evaluation",
            "required_provider": provider,
            "required_model": model,
        }
        accepted = self._response_json(
            self.client.post(
                f"{API_PREFIX}/single/conversations/{self.conversation_id}/enqueue",
                json=body,
            )
        )
        self._workflow_thread = self.module._HOSTED_THREADS.get(self.conversation_id)
        if scenario_id == "07-ios-background" and not self.evidence:
            assert self.provider.tool_started.wait(timeout=10)
            running = self._conversation()["hosted_turns"][self.turn_id]
            self.evidence["background_status_before_stream"] = running["status"]
            self.evidence["background_cursor_before_stream"] = int(
                self._conversation().get("hosted_event_cursor") or 0
            )
            self.provider.tool_release.set()
        return {
            **accepted,
            "account_generation": self.account_generation,
        }

    def _send_targeted_intervention(self) -> None:
        if self._intervention_sent:
            return
        if not self.provider.tool_started.wait(timeout=10):
            raise RecoverableHostedEvalError(
                "worker tool did not reach its atomic boundary"
            )
        self.provider.intervention_queued = True
        try:
            response = self.client.post(
                f"{API_PREFIX}/single/conversations/{self.conversation_id}"
                f"/hosted-turns/{self.turn_id}/interventions",
                json={
                    "content": "@Worker change direction after the current tool call",
                    "message_id": "p7-targeted-intervention",
                    "delivery": "steer",
                    "queue_mode": "one_at_a_time",
                },
            )
            self.evidence["intervention_response"] = self._response_json(response)
            self.evidence[
                "intervention_reached_blocked_boundary"
            ] = not self.provider.tool_release.is_set()
            self._intervention_sent = True
        finally:
            self.provider.tool_release.set()

    def events_after(self, *, conversation_id: str, cursor: int) -> EvalEventPage:
        assert conversation_id == self.conversation_id
        self.requested_cursors.append(cursor)
        if self.provider.scenario == "09-targeted-intervention":
            self._send_targeted_intervention()
        thread = self._workflow_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.01)
        if (
            thread is not None
            and not thread.is_alive()
            and self.provider.workflow_errors
        ):
            raise TerminalHostedEvalError(
                "hosted workflow crashed: " + "; ".join(self.provider.workflow_errors),
                error_code="workflow_crashed",
            )

        # The production SSE endpoint deliberately reads its immutable live
        # projection without the account-state lock. Exercise that same path:
        # polling full multi-megabyte JSON every 50 ms starves hosted workers.
        conversation = self.module._live_conversation_snapshot(
            conversation_id,
            self.owner_id,
        )
        if conversation is None:
            with self.module._STATE_LOCK:
                state = self.module.load_single_state()
                conversation = next(
                    item
                    for item in state["conversations"]
                    if item["id"] == conversation_id
                )
        envelope, _has_more = self.module._hosted_event_stream_frame(
            conversation,
            delivered_cursor=cursor,
            include_snapshot=cursor == 0,
            limit=self.page_size,
        )
        next_cursor = int(envelope["cursor"])
        self.returned_cursors.append(next_cursor)
        snapshot = envelope.get("conversation")
        return EvalEventPage(
            events=[dict(item) for item in envelope["events"]],
            cursor=next_cursor,
            min_cursor=int(envelope["min_cursor"]),
            has_gap=bool(envelope["has_gap"]),
            reset_cursor=bool(envelope["reset_cursor"]),
            reset_reason=str(envelope["reset_reason"]),
            snapshot=dict(snapshot) if isinstance(snapshot, dict) else None,
            account_generation=str(envelope["account_generation"]),
        )

    def snapshot(self, *, conversation_id: str) -> dict[str, Any]:
        assert conversation_id == self.conversation_id
        conversation = self._conversation()
        run = conversation["hosted_turns"][self.turn_id]
        return {
            "status": str(run["status"]),
            "hosted_event_cursor": int(conversation.get("hosted_event_cursor") or 0),
            "account_generation": str(conversation["account_generation"]),
            "hosted_turn": run,
        }

    def wait_for_workflow(self) -> None:
        thread = self._workflow_thread
        if thread is not None:
            thread.join(timeout=15)
            assert not thread.is_alive(), "hosted workflow did not terminate"

    def persisted_conversation(self) -> dict[str, Any]:
        self.wait_for_workflow()
        return self._conversation()


def _build_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scenario: str,
    mode: str,
    page_size: int = 500,
    failures: list[Exception] | None = None,
    max_attempts: int = 2,
) -> ProductChainAdapter:
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    module = _load_collaboration_module()
    module.get_hermes_home = lambda: root
    provider = DeterministicProvider(
        scenario=scenario,
        mode=mode,
        failures=failures,
        max_attempts=max_attempts,
    )
    from agent import auxiliary_client

    monkeypatch.setattr(auxiliary_client, "call_llm", provider.call_auxiliary)
    module.available_profiles = provider.profiles
    module.classify_intent_with_context_model = provider.classify_intent
    # Prewarm owns the real TUI subprocess just like the provider runner does.
    # Leaving it production-backed starts an unrelated model process that can
    # hold the account lifecycle lock while the deterministic chain runs.
    module.prewarm_hosted_gateway = lambda **_kwargs: None
    execute_product_workflow = module.execute_hosted_workflow

    def execute_with_provider(conversation_id: str, turn_id: str) -> None:
        try:
            execute_product_workflow(
                conversation_id,
                turn_id,
                runner=provider.run,
                manager_runner=provider.run,
                provider_max_attempts=provider.max_attempts,
                provider_retry_sleeper=lambda _seconds: None,
            )
        except Exception as exc:
            provider.workflow_errors.append(str(exc))
            raise

    # start_hosted_workflow remains production code. This replacement only
    # supplies its one external dependency, the provider call.
    module.execute_hosted_workflow = execute_with_provider
    return ProductChainAdapter(
        root,
        module=module,
        provider=provider,
        page_size=page_size,
    )


PRODUCT_SCENARIOS = [
    ("01-simple-chat", "你好", "chat", "completed"),
    (
        "02-lift-exactly-once",
        "分析仓库，修复问题，运行测试并生成发布报告",
        "work",
        "completed",
    ),
    (
        "03-manager-decompose-dispatch",
        "分析仓库，修改代码，运行测试，代码审查并汇报交付结果",
        "work",
        "completed",
    ),
    (
        "04-review-rework",
        "实现功能，审阅证据，不通过则返工并复测",
        "work",
        "completed",
    ),
    (
        "05-supervisor-intervention",
        "监督调度员、Worker 和审阅员完成全部验收项",
        "work",
        "completed",
    ),
    (
        "06-reporter-verified-only",
        "审阅完成后只根据已验证证据生成最终报告",
        "work",
        "completed",
    ),
    (
        "07-ios-background",
        "执行一个退出 iOS 后仍由服务器继续的复杂任务",
        "work",
        "completed",
    ),
    (
        "08-cursor-reconnect",
        "执行复杂任务并支持按游标恢复完整进度",
        "work",
        "completed",
    ),
    (
        "09-targeted-intervention",
        "执行工具时接收用户针对 Worker 的方向调整",
        "work",
        "completed",
    ),
    (
        "10-provider-recovery",
        "模型连接失败五次后恢复并继续回复",
        "chat",
        "completed",
    ),
    (
        "11-resource-refresh",
        "安装 skill 并让资源目录实时刷新",
        "work",
        "completed",
    ),
    (
        "12-file-artifact-deletion",
        "上传文件，保存工具产物，下载验证并删除账户数据",
        "work",
        "completed",
    ),
]


def _events(conversation: dict[str, Any], event_type: str) -> list[dict[str, Any]]:
    return [
        event
        for event in conversation["hosted_events"]
        if event["event_type"] == event_type
    ]


def _parse_sse_frames(payload: bytes) -> list[dict[str, Any]]:
    """Parse complete SSE frames emitted by the public hosted-events route."""

    frames: list[dict[str, Any]] = []
    fields: dict[str, list[str]] = {}
    for raw_line in payload.decode("utf-8").splitlines():
        if not raw_line:
            if fields.get("data"):
                frames.append({
                    "id": "\n".join(fields.get("id") or []),
                    "event": "\n".join(fields.get("event") or []),
                    "data": json.loads("\n".join(fields["data"])),
                })
            fields = {}
            continue
        if raw_line.startswith(":"):
            continue
        name, separator, value = raw_line.partition(":")
        if not separator:
            continue
        fields.setdefault(name, []).append(value.lstrip(" "))
    return frames


def test_product_chain_hosted_events_public_sse_stream(tmp_path, monkeypatch):
    """The production GET route, not its frame helper, is the client contract."""

    with _build_adapter(
        tmp_path,
        monkeypatch,
        scenario="01-simple-chat",
        mode="chat",
    ) as adapter:
        summary = run_hosted_behavior_scenario(
            adapter,
            provider=PROVIDER_NAME,
            model=MODEL_NAME,
            scenario_id="01-simple-chat",
            prompt="stream public hosted events",
            eval_run_id="integration-public-sse",
            code_revision="integration-revision",
            sleep=lambda _seconds: None,
        )
        persisted = adapter.persisted_conversation()

        original_is_disconnected = Request.is_disconnected
        sse_polls = 0

        async def disconnect_after_first_frame(request: Request) -> bool:
            nonlocal sse_polls
            if request.url.path.endswith("/hosted-events"):
                sse_polls += 1
                return sse_polls > 1
            return await original_is_disconnected(request)

        monkeypatch.setattr(Request, "is_disconnected", disconnect_after_first_frame)
        response = adapter.client.get(
            f"{API_PREFIX}/single/conversations/{adapter.conversation_id}"
            "/hosted-events?cursor=0"
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _parse_sse_frames(response.content)
    assert len(frames) == 1
    frame = frames[0]
    assert frame["event"] == "conversation"
    assert int(frame["id"]) == persisted["hosted_event_cursor"]
    envelope = frame["data"]
    assert envelope["cursor"] == summary["cursor"]
    assert envelope["account_generation"] == summary["account_generation"]
    assert envelope["conversation"]["id"] == adapter.conversation_id
    assert envelope["events"] == persisted["hosted_events"]
    assert all(validate_event_envelope(event) for event in envelope["events"])
    assert any(event["event_type"] == "turn.completed" for event in envelope["events"])


@pytest.mark.parametrize(
    "scenario,prompt,mode,expected_outcome",
    PRODUCT_SCENARIOS,
    ids=[item[0] for item in PRODUCT_SCENARIOS],
)
def test_product_backed_behavior_matrix(
    tmp_path,
    monkeypatch,
    scenario,
    prompt,
    mode,
    expected_outcome,
):
    failures = (
        [RuntimeError("HTTP 503 temporarily unavailable") for _ in range(5)]
        if scenario == "10-provider-recovery"
        else []
    )
    max_attempts = 6 if scenario == "10-provider-recovery" else 2
    page_size = 1 if scenario == "08-cursor-reconnect" else 500
    with _build_adapter(
        tmp_path,
        monkeypatch,
        scenario=scenario,
        mode=mode,
        page_size=page_size,
        failures=failures,
        max_attempts=max_attempts,
    ) as adapter:
        summary = run_hosted_behavior_scenario(
            adapter,
            provider=PROVIDER_NAME,
            model=MODEL_NAME,
            scenario_id=scenario,
            prompt=prompt,
            eval_run_id=f"integration-{scenario}",
            code_revision="integration-revision",
            sleep=lambda _seconds: None,
        )
        assert summary["outcome"] == expected_outcome, json.dumps(
            summary, ensure_ascii=False, sort_keys=True
        )
        assert summary["runtime_binding_verified"] is True
        assert summary["actual_provider"] == PROVIDER_NAME
        assert summary["actual_model"] == MODEL_NAME
        persisted = adapter.persisted_conversation()
        run = persisted["hosted_turns"][adapter.turn_id]
        assert run["status"] == expected_outcome
        assert persisted["account_generation"] == summary["account_generation"]
        assert persisted["hosted_event_cursor"] == summary["cursor"]
        assert adapter.module.single_state_path().is_file()
        assert all(
            validate_event_envelope(event) for event in persisted["hosted_events"]
        )
        assert len(_events(persisted, f"turn.{expected_outcome}")) == 1

        if scenario == "01-simple-chat":
            assert run["route_metadata"]["mode"] == "chat"
            assert adapter.provider.call_counts == Counter({"default": 1})
            assert not [
                event
                for event in _events(persisted, "role.handoff")
                if event["payload"].get("action") == "collaboration_lift"
            ]
            assert any(
                event.get("role") == "assistant" and event.get("content") == "你好"
                for event in summary["events"]
                if event["type"] == "message"
            )
        elif scenario == "02-lift-exactly-once":
            replay = adapter.enqueue(
                provider=PROVIDER_NAME,
                model=MODEL_NAME,
                scenario_id=scenario,
                prompt=prompt,
                idempotency_key=summary["idempotency_key"],
            )
            assert replay["replayed"] is True
            replayed = adapter.persisted_conversation()
            lifts = [
                event
                for event in _events(replayed, "role.handoff")
                if event["payload"].get("action") == "collaboration_lift"
            ]
            assert len(lifts) == 1
            assert adapter.provider.classifier_calls == 0
        elif scenario == "03-manager-decompose-dispatch":
            plan = run["manager_plan"]
            assert plan["workers"] == ["dbb3-worker"]
            assert [step["id"] for step in plan["plan"]] == [
                "step-inspect",
                "step-implement",
                "step-test",
            ]
            assert run["task_id"]
            assert run["manager_handoff"]["plan"] == plan["plan"]
            assert adapter.provider.call_counts["hermes-manager"] == 1
            assert any(
                message.get("kind") == "workflow" for message in persisted["messages"]
            )
        elif scenario == "04-review-rework":
            # Reviewer/supervisor LLM gates are retired. Verified worker
            # evidence is accepted by the deterministic server-side gate.
            assert run["rework_round"] == 0
            assert run["validation_verdicts"]["final_report"] == "pass"
            assert adapter.provider.call_counts["dbb3-worker"] == 3
            assert "reviewer" not in adapter.provider.call_counts
        elif scenario == "05-supervisor-intervention":
            assert run["status"] == "completed"
            assert run["validation_verdicts"]["final_report"] == "pass"
            assert "supervisor" not in adapter.provider.call_counts
            assert adapter.provider.call_counts["dbb3-worker"] == 1
        elif scenario == "06-reporter-verified-only":
            handoff = run["manager_handoff"]
            assert handoff["task_goal"] == prompt
            assert handoff["worker_results"] == run["worker_results"]
            assert "服务器确定性校验通过" in handoff["review_verdict"]
            assert handoff["failures"] == []
            assert "verified worker evidence" in str(handoff["worker_results"])
            assert not adapter.provider.reporter_prompts
            final_reports = [
                message
                for message in persisted["messages"]
                if (message.get("meta") or {}).get("final_report")
            ]
            assert len(final_reports) == 1
            assert (
                final_reports[0]["content"]
                != ""
            )
        elif scenario == "07-ios-background":
            assert adapter.evidence["background_status_before_stream"] == "running"
            assert adapter.evidence["background_cursor_before_stream"] > 0
            assert run["status"] == "completed"
        elif scenario == "08-cursor-reconnect":
            assert adapter.requested_cursors[0] == 0
            assert len(adapter.requested_cursors) > 3
            assert all(
                current >= previous
                for previous, current in zip(
                    adapter.requested_cursors,
                    adapter.requested_cursors[1:],
                )
            )
            assert any(cursor > 0 for cursor in adapter.requested_cursors[1:])
            assert adapter.returned_cursors[-1] == persisted["hosted_event_cursor"]
        elif scenario == "09-targeted-intervention":
            tool_id = f"{scenario}:worker:1"
            started = next(
                event
                for event in _events(persisted, "tool.started")
                if event["payload"].get("tool_id") == tool_id
            )
            tool_completed = next(
                event
                for event in _events(persisted, "tool.completed")
                if event["payload"].get("tool_id") == tool_id
            )
            queued = _events(persisted, "intervention.queued")[0]
            claimed = _events(persisted, "intervention.claimed")[0]
            replied = _events(persisted, "intervention.replied")[0]
            completed = _events(persisted, "intervention.completed")[0]
            assert started["cursor"] < tool_completed["cursor"]
            assert queued["cursor"] < claimed["cursor"] < replied["cursor"]
            assert replied["cursor"] < completed["cursor"]
            assert adapter.evidence["intervention_reached_blocked_boundary"] is True
            intervention = run["interventions"][0]
            assert intervention["status"] == "completed"
            assert intervention["targets"] == ["worker"]
            checkpoint_activity = intervention["checkpoint"]["activities"][0]
            assert checkpoint_activity["id"] == tool_id
            assert checkpoint_activity["status"] == "completed"
            assert (
                checkpoint_activity["started_at"]
                <= intervention["created_at"]
                <= checkpoint_activity["ended_at"]
                <= intervention["claimed_at"]
            )
            assert replied["payload"]["profile"] == "dbb3-worker"
            assert summary["tool_count"] >= 2
        elif scenario == "10-provider-recovery":
            retries = _events(persisted, "connection.retry_started")
            assert [event["payload"]["attempt"] for event in retries] == [2, 3, 4, 5, 6]
            assert summary["model_retries"] == 5
            assert summary["transport_retries"] == 0
            assert summary["retries"] == 5
            assert adapter.provider.call_counts["default"] == 6
        elif scenario == "11-resource-refresh":
            catalog_response = adapter.client.get(f"{API_PREFIX}/managed-resources")
            catalog = adapter._response_json(catalog_response)
            assert catalog == adapter.provider.evidence["resource_catalog"]
            installation = adapter.provider.evidence["installation"]
            assert installation["state"] == "completed"
            assert installation["targets"][0]["state"] == "completed"
            assert catalog["cursor"] == 1
            assert catalog["resources"][0]["health"] == "healthy"
            assert catalog["resources"][0]["trust_state"] == "approved"
            assert catalog["resources"][0]["enabled"] is True
            assert catalog["resources"][0]["loaded_nodes"] == ["server"]
            assert managed_installations.managed_installations_db_path().is_file()
        elif scenario == "12-file-artifact-deletion":
            uploaded = adapter._uploaded
            artifact = adapter.provider.evidence["artifact"]
            assert uploaded is not None
            file_download = adapter.client.get(
                f"{API_PREFIX}/files/{uploaded['id']}/download"
            )
            artifact_download = adapter.client.get(
                f"{API_PREFIX}/tool-output-artifacts/{artifact['id']}/download"
            )
            assert file_download.status_code == 200
            assert file_download.content == b"uploaded account data"
            assert artifact_download.status_code == 200
            assert artifact_download.content == b"complete tool output"
            assert (
                len([
                    event
                    for event in summary["events"]
                    if event["type"] == "attachment"
                ])
                == 2
            )
            cleanup = adapter.module.delete_owner_account_data(
                adapter.owner_id,
                account_generation=adapter.account_generation,
            )
            assert cleanup["conversations"] == 1
            assert cleanup["files"]["files"] == 1
            assert cleanup["files"]["object_buckets"] == 1
            assert cleanup["tool_output_artifacts"] == {"artifacts": 1}
            remaining = adapter.module.load_single_state()["conversations"]
            assert not any(
                item.get("id") == adapter.conversation_id for item in remaining
            )
            assert adapter.client.get(
                f"{API_PREFIX}/files/{uploaded['id']}/download"
            ).status_code in {404, 410}
            assert adapter.client.get(
                f"{API_PREFIX}/tool-output-artifacts/{artifact['id']}/download"
            ).status_code in {404, 410}


PROVIDER_FAILURES = [
    (
        "model-unconfigured",
        "model is not configured",
        1,
        "failed",
        0,
        "model_request_failed",
    ),
    ("http-401", "HTTP 401 unauthorized", 1, "failed", 0, "http_401"),
    ("http-429", "HTTP 429 rate limited", 2, "completed", 1, ""),
    ("http-503", "HTTP 503 unavailable", 2, "completed", 1, ""),
    ("timeout", "socket timeout", 2, "completed", 1, ""),
    ("disconnect", "connection reset by peer", 2, "completed", 1, ""),
]


@pytest.mark.parametrize(
    "case_id,error_message,max_attempts,expected_outcome,expected_retries,error_code",
    PROVIDER_FAILURES,
    ids=[item[0] for item in PROVIDER_FAILURES],
)
def test_product_chain_provider_failure_matrix(
    tmp_path,
    monkeypatch,
    case_id,
    error_message,
    max_attempts,
    expected_outcome,
    expected_retries,
    error_code,
):
    scenario = f"failure-{case_id}"
    with _build_adapter(
        tmp_path,
        monkeypatch,
        scenario=scenario,
        mode="chat",
        failures=[RuntimeError(error_message)],
        max_attempts=max_attempts,
    ) as adapter:
        summary = run_hosted_behavior_scenario(
            adapter,
            provider=PROVIDER_NAME,
            model=MODEL_NAME,
            scenario_id=scenario,
            prompt="验证模型失败分类和恢复",
            eval_run_id=f"integration-{scenario}",
            code_revision="integration-revision",
            sleep=lambda _seconds: None,
        )

        persisted = adapter.persisted_conversation()
        run = persisted["hosted_turns"][adapter.turn_id]
        assert summary["outcome"] == expected_outcome
        assert summary["transport_retries"] == 0
        assert summary["model_retries"] == expected_retries
        assert summary["retries"] == expected_retries
        assert len(_events(persisted, "connection.retry_started")) == expected_retries
        assert run["status"] == expected_outcome
        assert run.get("error_code", "") == error_code
        assert adapter.provider.call_counts["default"] == max_attempts
