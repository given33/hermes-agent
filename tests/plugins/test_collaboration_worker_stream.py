import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_official_worker_hooks_publish_before_tool_completion_and_replay(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_probe")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1")
    plugin = _load("worker_stream", "plugins/collaboration/worker-stream/__init__.py")
    hooks = {}
    plugin.register(SimpleNamespace(register_hook=lambda name, callback: hooks.setdefault(name, callback)))
    hooks["on_stream_start"](session_id="s", model="configured", provider="custom", iteration=1)
    hooks["on_stream_delta"](delta="Checking ", kind="reasoning", session_id="s", iteration=1)
    hooks["on_stream_delta"](delta="the file", kind="reasoning", session_id="s", iteration=1)
    hooks["pre_tool_call"](tool_name="read_file", args={"path": "report.md"}, tool_call_id="read-1")
    path = tmp_path / "collaboration-streams/t_probe.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [item["type"] for item in records] == ["request.accepted", "reasoning.delta", "reasoning.delta", "tool.start"]

    api = _load("worker_api", "plugins/collaboration/dashboard/plugin_api.py")
    remote = {"id": "r", "profile": "dbb3-worker", "role_stage": "worker:dbb3-worker",
              "status": "running", "claim_token": "claim", "lease_owner": "dbb3-primary",
              "lease_until": 10**15, "started_at": 1000}
    conversation = {"id": "c", "owner_id": "owner", "account_generation": "gen", "messages": [],
                    "hosted_turns": {"t": {"turn_id": "t", "status": "running", "remote_runs": {"worker:dbb3-worker": remote}}}}
    api._HOSTED_LIVE_CONVERSATIONS["c"] = conversation
    monkeypatch.setattr(api, "_require_connector", lambda _: "dbb3-primary")
    monkeypatch.setattr(api, "_require_connector_account_boundary", lambda *args: None)
    monkeypatch.setattr(api, "save_single_state", lambda _: pytest.fail("Live events must not wait for durable writes"))
    events = [{**item, "cursor": index + 1} for index, item in enumerate(records)]
    body = api.ConnectorStreamBody(connector_id="dbb3-primary", claim_token="claim", events=events)
    assert api.connector_stream_run("r", body, SimpleNamespace())["cursor"] == 4
    stream = api._REMOTE_STREAM_STATES["r"]
    assert stream["activities"][0]["output"] == "Checking the file"
    assert stream["activities"][1]["status"] == "running"
    assert not api._remote_execution_stalled(remote, now=stream["updated_at"] + 1000)
    assert api.connector_stream_run("r", body, SimpleNamespace())["cursor"] == 4
    assert stream["activities"][0]["output"] == "Checking the file"

    hooks["post_tool_call"](tool_name="read_file", args={"path": "report.md"},
        tool_call_id="read-1", result=json.dumps({"error": "Permission denied"}))
    final = json.loads(path.read_text().splitlines()[-1])
    api.connector_stream_run("r", api.ConnectorStreamBody(connector_id="dbb3-primary", claim_token="claim",
        events=[{**final, "cursor": 5}]), SimpleNamespace())
    assert stream["activities"][1]["status"] == "failed"
    assert stream["activities"][1]["error"] == "Permission denied"
    with pytest.raises(api.HTTPException) as error:
        api.connector_stream_run("r", body.model_copy(update={"claim_token": "old"}), SimpleNamespace())
    assert error.value.status_code == 409
    api._HOSTED_LIVE_CONVERSATIONS["c"]["hosted_turns"]["t"]["remote_runs"]["worker:dbb3-worker"]["cancel_requested"] = True
    with pytest.raises(api.HTTPException):
        api.connector_stream_run("r", body, SimpleNamespace())


def test_connector_assignment_uses_its_owned_board_without_second_triage():
    from deploy.dbb3.dbb3_cloud_connector import build_root_task_command
    command = build_root_task_command({"profile": "dbb3-worker", "board": "hosted-test", "objective": "Read report"})
    assert "--triage" not in command
    assert command[command.index("kanban") + 1:command.index("kanban") + 3] == ["--board", "hosted-test"]
    selected = build_root_task_command({"profile": "pc-worker", "objective": "Read report",
        "model_override": "selected-model", "provider_override": "custom:configured"})
    assert selected[selected.index("--model") + 1] == "selected-model"
    assert selected[selected.index("--provider") + 1] == "custom:configured"


def test_worker_api_errors_stream_without_provider_secrets_or_fake_reasoning(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_retry")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1")
    plugin = _load("worker_stream_retry", "plugins/collaboration/worker-stream/__init__.py")
    hooks = {}
    plugin.register(SimpleNamespace(register_hook=lambda name, callback: hooks.setdefault(name, callback)))
    hooks["api_request_error"](session_id="s", status_code=404, retry_count=0,
        max_retries=5, retryable=False, error={"message": "secret account details"})
    records = [json.loads(line) for line in (tmp_path / "collaboration-streams/t_retry.jsonl").read_text().splitlines()]
    assert records[0]["type"] == "connection.retry"
    assert records[0]["payload"]["status_code"] == 404
    assert "备用模型" in records[0]["payload"]["message"]
    assert "secret" not in json.dumps(records)
