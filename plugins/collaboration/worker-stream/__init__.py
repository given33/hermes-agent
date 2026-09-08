"""Local append-only observer output; credentials remain in the connector."""

import json
import os
from pathlib import Path
import re
import threading
import time

_lock = threading.Lock()
_sequence = 0
_tools = {}
_stream_directory = None


def _bounded(value, limit=20000):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[-limit:]


def _emit(event_type, payload):
    global _sequence, _stream_directory
    task = os.environ.get("HERMES_KANBAN_TASK", "")
    run = os.environ.get("HERMES_KANBAN_RUN_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task) or not run.isdigit():
        return
    from hermes_constants import get_hermes_home
    directory = Path(get_hermes_home()) / "collaboration-streams"
    path = directory / f"{task}.jsonl"
    with _lock:
        if _stream_directory != directory:
            directory.mkdir(mode=0o700, exist_ok=True)
            _stream_directory = directory
        _sequence += 1
        record = {"run_id": run, "sequence": _sequence, "type": event_type,
                  "payload": {**payload, "timestamp": int(time.time() * 1000)}}
        encoded = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "ab", buffering=0) as stream:
            stream.write(encoded)


def _start(session_id="", model="", provider="", iteration=0, **_):
    _emit("request.accepted", {"session_id": session_id, "model": model,
                               "provider": provider, "iteration": iteration})


def _delta(delta="", kind="text", session_id="", iteration=0, **_):
    if delta:
        for offset in range(0, len(delta), 8000):
            _emit("reasoning.delta" if kind == "reasoning" else "message.delta",
                  {"text": delta[offset:offset + 8000], "session_id": session_id,
                   "entity_id": f"{session_id}:{iteration}:{kind}"})


def _api_error(session_id="", status_code=None, retry_count=None, max_retries=None, retryable=None, reason=None, **_):
    # Observe the official error hook immediately. Raw provider errors may
    # contain credentials or account data; emit only bounded status metadata.
    labels = {401: "模型账号未激活或认证失败", 402: "模型账户额度不足",
              403: "模型访问被拒绝", 404: "模型或接口不可用", 429: "模型请求受到限流"}
    code = status_code if isinstance(status_code, int) else 0
    label = labels.get(code, "模型请求暂时失败")
    _emit("connection.retry", {"session_id": session_id,
        "attempt": max(1, int(retry_count or 0) + 1), "max_attempts": max(1, int(max_retries or 1)),
        "retryable": retryable, "status_code": code,
        "message": label + ("，正在尝试已配置的备用模型" if retryable is False else "，正在恢复连接")})


def _tool_start(tool_name="", args=None, tool_call_id="", session_id="", task_id="", **_):
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return
    key = tool_call_id or f"{session_id}:{task_id}:{tool_name}"
    started = int(time.time() * 1000)
    with _lock:
        _tools[key] = started
    _emit("tool.start", {"name": tool_name, "args": _bounded(args or {}, 8000), "tool_id": key,
                         "session_id": session_id, "started_at": started})


def _tool_complete(tool_name="", args=None, result=None, tool_call_id="", session_id="", task_id="", status="", **_):
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return
    key = tool_call_id or f"{session_id}:{task_id}:{tool_name}"
    ended = int(time.time() * 1000)
    with _lock:
        started = _tools.pop(key, ended)
    parsed = result
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (ValueError, TypeError):
            pass
    error = parsed.get("error") if isinstance(parsed, dict) else ""
    if not error and status in {"failed", "error", "cancelled"}:
        error = status
    _emit("tool.complete", {"name": tool_name, "args": _bounded(args or {}, 8000), "tool_id": key,
                            "result": _bounded(result), "error": _bounded(error or "", 4000), "session_id": session_id,
                            "started_at": started, "ended_at": ended,
                            "duration_s": (ended - started) / 1000})


def register(ctx):
    # Registration can happen in an idle worker; each callback binds the
    # actual task at invocation and emits nothing before an assignment.
    ctx.register_hook("on_stream_start", _start)
    ctx.register_hook("on_stream_delta", _delta)
    ctx.register_hook("api_request_error", _api_error)
    ctx.register_hook("pre_tool_call", _tool_start)
    ctx.register_hook("post_tool_call", _tool_complete)
