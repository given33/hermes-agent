"""Official dashboard collaboration plugin.

Group chat turns execute the existing Hermes CLI against named profiles, so
responses use the same model, skills, MCP servers, memory, and session store as
ordinary official WebUI chats. Workflow rendering delegates to the bundled
Kanban dashboard API rather than introducing a second scheduler.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from hermes_cli.config import get_hermes_home
from hermes_cli.profiles import list_profiles


router = APIRouter()
_STATE_LOCK = threading.RLock()
_MAX_MESSAGES = 200
_PROMPT_HISTORY = 24
_MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
_MAX_CONVERSATION_TITLE_CHARS = 18
_ATTACHMENT_BUCKETS = {"uploads", "outputs"}
_WORK_MARKERS = (
    "完成",
    "执行",
    "修改",
    "合并",
    "创建",
    "开发",
    "部署",
    "安装",
    "配置",
    "修复",
    "优化",
    "测试",
    "检查",
    "调试",
    "运行",
    "整理",
    "生成",
    "写一个",
    "做一个",
    "workflow",
    "deploy",
    "build",
    "fix",
    "implement",
)
_PC_MARKERS = ("本地电脑", "windows", "wsl", "pc", "桌面", "处理器", "gpu")
_DBB3_MARKERS = ("dbb3", "linux", "armbian", "网关", "gateway")
_COMPLEX_WORK_MARKERS = (
    "修改代码",
    "修复",
    "部署",
    "安装",
    "配置",
    "运行测试",
    "工作流",
    "拆分任务",
    "协作",
    "上传文件",
    "生成ppt",
    "生成 ppt",
    "写ppt",
    "写 ppt",
    "交付",
    "全部完成",
    "多步骤",
    "数据库",
    "服务器",
    "本地电脑",
    "dbb3",
)
_SIMPLE_CHAT_MARKERS = (
    "你好",
    "谢谢",
    "在吗",
    "怎么样",
    "是什么",
    "为什么",
    "解释",
    "介绍",
    "总结一下",
    "聊聊",
)
_MULTI_STEP_MARKERS = ("然后", "接着", "并且", "同时", "最后", "之后", "以及")


def state_path() -> Path:
    return Path(get_hermes_home()) / "collaboration" / "rooms.json"


def single_state_path() -> Path:
    return Path(get_hermes_home()) / "collaboration" / "single.json"


def safe_attachment_name(filename: str) -> str:
    name = Path(str(filename or "").replace("\x00", "")).name.strip()
    if name in {"", ".", ".."}:
        raise ValueError("附件文件名无效")
    return name


def conversation_files_root(conversation_id: str) -> Path:
    return (
        Path(get_hermes_home())
        / "collaboration"
        / "files"
        / conversation_id
    )


def _conversation_file_dir(conversation_id: str, bucket: str) -> Path:
    if bucket not in _ATTACHMENT_BUCKETS:
        raise HTTPException(status_code=404, detail="附件目录不存在")
    target = conversation_files_root(conversation_id) / bucket
    target.mkdir(parents=True, exist_ok=True)
    return target


def _attachment_record(
    conversation_id: str,
    bucket: str,
    path: Path,
) -> dict[str, Any]:
    stat = path.stat()
    relative_path = path.relative_to(_conversation_file_dir(conversation_id, bucket))
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded_relative = "/".join(
        quote(part, safe="")
        for part in relative_path.parts
    )
    return {
        "id": f"{bucket}:{relative_path.as_posix()}",
        "name": path.name,
        "bucket": bucket,
        "relative_path": relative_path.as_posix(),
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mime_type": mime_type,
        "updated_at": int(stat.st_mtime * 1000),
        "download_url": (
            "/api/plugins/collaboration/single/conversations/"
            f"{conversation_id}/attachments/{bucket}/{encoded_relative}"
        ),
    }


def _list_conversation_attachments(conversation_id: str) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for bucket in sorted(_ATTACHMENT_BUCKETS):
        root = _conversation_file_dir(conversation_id, bucket)
        for path in sorted(root.rglob("*")):
            if path.is_file():
                attachments.append(
                    _attachment_record(conversation_id, bucket, path)
                )
    return attachments


def load_state(path: Optional[Path] = None) -> dict[str, Any]:
    target = path or state_path()
    if not target.exists():
        return {"rooms": []}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"rooms": []}
    rooms = data.get("rooms") if isinstance(data, dict) else None
    return {"rooms": rooms if isinstance(rooms, list) else []}


def load_single_state(path: Optional[Path] = None) -> dict[str, Any]:
    target = path or single_state_path()
    if not target.exists():
        return {"conversations": []}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"conversations": []}
    conversations = data.get("conversations") if isinstance(data, dict) else None
    normalized_conversations = conversations if isinstance(conversations, list) else []
    for conversation in normalized_conversations:
        if not isinstance(conversation, dict):
            continue
        messages = conversation.get("messages")
        if isinstance(messages, list):
            conversation["messages"] = normalize_stored_conversation_messages(
                messages
            )
    return {"conversations": normalized_conversations}


def summarize_task_title(content: str) -> str:
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    if not text:
        return "新对话"
    text = text.split("本会话交付文件目录：", 1)[0].strip()
    reply_match = re.search(
        r"(?:^|[，,。；;])\s*只回复\s*[：:]?\s*([A-Za-z0-9][A-Za-z0-9_. -]{1,40})",
        text,
    )
    if reply_match:
        return reply_match.group(1).strip()[:_MAX_CONVERSATION_TITLE_CHARS]
    clauses = [
        clause.strip(" ：:，,。；;！？!?\"'`()[]{}")
        for clause in re.split(r"[\n，,。；;！？!?:：]+", text)
        if clause.strip()
    ] or [text]
    action_weights = {
        "修复": 10,
        "优化": 10,
        "部署": 10,
        "配置": 10,
        "创建": 10,
        "生成": 10,
        "安装": 9,
        "更新": 9,
        "删除": 9,
        "验收": 9,
        "验证": 8,
        "测试": 8,
        "继续": 8,
        "接着": 8,
        "检查": 7,
        "总结": 7,
        "整理": 7,
        "说明": 7,
        "解释": 7,
        "精炼": 7,
        "运行": 5,
        "调用": 4,
        "完成": 2,
    }
    topic_markers = (
        "Hermes",
        "hermes",
        "DBB3",
        "dbb3",
        "本地电脑",
        "iOS",
        "ios",
        "后台托管",
        "会话",
        "标题",
        "看板",
        "网络",
        "输入框",
        "定时任务",
        "自我进化",
        "terminal",
    )
    negative_markers = ("不要", "不调用", "无需", "禁止", "只回复")
    focused_clauses = [
        clause
        for clause in clauses
        if not any(marker in clause for marker in negative_markers)
    ]
    if focused_clauses:
        clauses = focused_clauses

    def score(clause: str) -> tuple[int, int]:
        value = sum(weight for marker, weight in action_weights.items() if marker in clause)
        value += sum(2 for marker in topic_markers if marker in clause)
        if any(marker in clause for marker in negative_markers):
            value -= 20
        return value, -len(clause)

    title = max(clauses, key=score)
    title = re.sub(
        r"^(?:请用[一二三四五六七八九十\d]+句话|请(?:你)?|麻烦(?:你)?|帮我|你(?:去|帮我)?|我(?:希望|想要|要)|能不能|可以)+",
        "",
        title,
    ).strip()
    title = re.sub(r"(?:已)?完成$", "", title).strip()
    if not title:
        title = clauses[0]
    if len(title) > _MAX_CONVERSATION_TITLE_CHARS:
        title = title[: _MAX_CONVERSATION_TITLE_CHARS - 1].rstrip() + "…"
    return title or "新对话"


def compact_conversation_title(conversation: dict[str, Any]) -> bool:
    current = str(conversation.get("title") or "").strip()
    poor_title = any(
        marker in current
        for marker in ("不要", "不调用", "无需", "禁止", "只回复")
    ) or current.startswith("回复 ")
    if len(current) <= _MAX_CONVERSATION_TITLE_CHARS and not poor_title:
        return False
    first_user_message = next(
        (
            str(message.get("content") or "").strip()
            for message in conversation.get("messages") or []
            if message.get("role") == "user" and message.get("content")
        ),
        current,
    )
    compacted = summarize_task_title(first_user_message)
    if compacted == current:
        return False
    conversation["title"] = compacted
    return True


def save_state(state: dict[str, Any], path: Optional[Path] = None) -> None:
    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, target)


def save_single_state(state: dict[str, Any], path: Optional[Path] = None) -> None:
    target = path or single_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, target)


def create_room_record(name: str, profiles: list[str]) -> dict[str, Any]:
    now = int(time.time() * 1000)
    return {
        "id": f"room_{uuid.uuid4().hex[:12]}",
        "name": name.strip() or "新群聊",
        "profiles": list(dict.fromkeys(p.strip() for p in profiles if p.strip())),
        "messages": [],
        "created_at": now,
        "updated_at": now,
    }


def create_single_conversation(
    profile: str,
    title: str = "新对话",
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    return {
        "id": f"chat_{uuid.uuid4().hex[:12]}",
        "title": title.strip() or "新对话",
        "profile": profile.strip() or "default",
        "runtime_sessions": {},
        "runtime_runs": {},
        "messages": [],
        "created_at": now,
        "updated_at": now,
    }


def create_adopted_single_conversation(
    profile: str,
    session_id: str,
    title: str,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    conversation = create_single_conversation(profile, title)
    set_conversation_runtime_session(conversation, profile, session_id)
    pending_assistant: list[dict[str, Any]] = []

    def flush_assistant_turn() -> None:
        if not pending_assistant:
            return
        content = next(
            (
                text
                for text in reversed(
                    [_message_content_text(item) for item in pending_assistant]
                )
                if text
            ),
            "",
        )
        activities = build_runtime_activity_timeline(pending_assistant)
        source = next(
            (
                item
                for item in reversed(pending_assistant)
                if str(item.get("role") or "").lower() == "assistant"
            ),
            pending_assistant[-1],
        )
        source_meta = source.get("meta")
        meta = dict(source_meta) if isinstance(source_meta, dict) else {}
        if activities:
            meta["activities"] = activities
        if content or activities:
            message = _append_message(
                conversation,
                role="assistant",
                name=str(source.get("name") or profile).strip() or profile,
                content=content,
                status=str(source.get("status") or "completed"),
                kind="message",
                meta=meta,
            )
            timestamp = source.get("timestamp")
            if isinstance(timestamp, (int, float)) and timestamp > 0:
                message["created_at"] = int(timestamp * 1000)
        pending_assistant.clear()

    for source in messages[-_MAX_MESSAGES:]:
        role = str(source.get("role") or "assistant").strip().lower()
        if role == "user":
            flush_assistant_turn()
            content = _message_content_text(source)
            if not content:
                continue
            message = _append_message(
                conversation,
                role="user",
                name="user",
                content=content,
                status=str(source.get("status") or "completed"),
                kind="message",
                meta=(
                    source.get("meta")
                    if isinstance(source.get("meta"), dict)
                    else None
                ),
            )
            timestamp = source.get("timestamp")
            if isinstance(timestamp, (int, float)) and timestamp > 0:
                message["created_at"] = int(timestamp * 1000)
        elif role in {"assistant", "tool"}:
            pending_assistant.append(source)
    flush_assistant_turn()
    if conversation["messages"]:
        conversation["updated_at"] = max(
            message["created_at"]
            for message in conversation["messages"]
        )
    return conversation


def set_conversation_runtime_session(
    conversation: dict[str, Any],
    profile: str,
    session_id: str,
) -> dict[str, str]:
    profile_name = profile.strip() or "default"
    runtime_sessions = conversation.get("runtime_sessions")
    if not isinstance(runtime_sessions, dict):
        runtime_sessions = {}
        conversation["runtime_sessions"] = runtime_sessions
    normalized_session_id = session_id.strip()
    if normalized_session_id:
        runtime_sessions[profile_name] = normalized_session_id
    else:
        runtime_sessions.pop(profile_name, None)
    conversation["updated_at"] = int(time.time() * 1000)
    return runtime_sessions


def mark_conversation_runtime_run(
    conversation: dict[str, Any],
    profile: str,
    session_id: str,
    *,
    baseline_message_count: int = 0,
    started_at: Optional[int] = None,
) -> dict[str, Any]:
    """Register a gateway-owned turn before its prompt is submitted.

    The record lets the dashboard process recover a completed answer from the
    profile's persistent SessionDB even when iOS has discarded the WebView.
    """
    profile_name = profile.strip() or "default"
    normalized_session_id = session_id.strip()
    set_conversation_runtime_session(
        conversation,
        profile_name,
        normalized_session_id,
    )
    runtime_runs = conversation.get("runtime_runs")
    if not isinstance(runtime_runs, dict):
        runtime_runs = {}
        conversation["runtime_runs"] = runtime_runs
    now = int(time.time() * 1000) if started_at is None else int(started_at)
    runtime_runs[profile_name] = {
        "session_id": normalized_session_id,
        "status": "running",
        "baseline_message_count": max(0, int(baseline_message_count or 0)),
        "started_at": now,
        "updated_at": now,
    }
    conversation["updated_at"] = now
    return runtime_runs[profile_name]


def _load_runtime_messages(profile: str, session_id: str) -> list[dict[str, Any]]:
    from hermes_cli.profiles import get_profile_dir
    from hermes_state import SessionDB

    db_path = get_profile_dir(profile) / "state.db"
    if not db_path.exists():
        return []
    db = SessionDB(db_path=db_path, read_only=True)
    try:
        resolved = db.resolve_session_id(session_id)
        if not resolved:
            return []
        resolved = db.resolve_resume_session_id(resolved)
        return db.get_messages(resolved)
    finally:
        db.close()


def _message_content_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if not isinstance(text, str):
                    text = item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part.strip() for part in parts if part.strip()).strip()
    if isinstance(content, dict):
        for key in ("text", "content", "output", "result"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        try:
            return json.dumps(content, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return ""
    return ""


def _structured_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(value).strip()
    return str(value).strip()


def _reasoning_text(message: dict[str, Any]) -> str:
    for key in ("reasoning_content", "reasoning", "thinking"):
        text = _structured_text(message.get(key))
        if text:
            return text
    meta = message.get("meta")
    if isinstance(meta, dict):
        return _structured_text(meta.get("reasoning"))
    return ""


def _tool_category(name: str) -> str:
    lowered = name.strip().lower()
    if lowered.startswith("mcp__") or lowered.startswith("mcp_"):
        return "mcp"
    if "skill" in lowered:
        return "skill"
    if any(marker in lowered for marker in ("web_search", "search_web", "browse", "browser")):
        return "web"
    if any(marker in lowered for marker in ("terminal", "shell", "command", "exec", "bash", "powershell")):
        return "command"
    if any(marker in lowered for marker in ("read_file", "write_file", "patch", "filesystem", "glob", "search_files")):
        return "file"
    if any(marker in lowered for marker in ("delegate", "subagent", "spawn_agent")):
        return "subagent"
    return "other"


def _timestamp_ms(message: dict[str, Any]) -> Optional[int]:
    value = message.get("timestamp")
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value if value > 10_000_000_000 else value * 1000)


def build_runtime_activity_timeline(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild reasoning phases and tool calls from SessionDB messages."""
    activities: list[dict[str, Any]] = []
    tools_by_id: dict[str, dict[str, Any]] = {}
    sequence = 0

    for message in messages:
        role = str(message.get("role") or "").lower()
        if role == "assistant":
            reasoning = _reasoning_text(message)
            if reasoning:
                sequence += 1
                activities.append(
                    {
                        "id": f"reasoning-{sequence}",
                        "kind": "reasoning",
                        "category": "reasoning",
                        "name": "模型思考",
                        "input": "",
                        "output": reasoning,
                        "status": "completed",
                        "started_at": _timestamp_ms(message),
                        "ended_at": _timestamp_ms(message),
                    }
                )
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                function = function if isinstance(function, dict) else {}
                sequence += 1
                tool_id = str(call.get("id") or f"tool-{sequence}")
                name = str(function.get("name") or call.get("name") or "tool")
                activity = {
                    "id": tool_id,
                    "kind": "tool",
                    "category": _tool_category(name),
                    "name": name,
                    "input": _structured_text(
                        function.get("arguments", call.get("arguments"))
                    ),
                    "output": "",
                    "status": "running",
                    "started_at": _timestamp_ms(message),
                    "ended_at": None,
                }
                activities.append(activity)
                tools_by_id[tool_id] = activity
        elif role == "tool":
            tool_id = str(
                message.get("tool_call_id")
                or message.get("id")
                or ""
            )
            activity = tools_by_id.get(tool_id)
            if activity is None:
                sequence += 1
                name = str(message.get("name") or "tool")
                activity = {
                    "id": tool_id or f"tool-result-{sequence}",
                    "kind": "tool",
                    "category": _tool_category(name),
                    "name": name,
                    "input": "",
                    "output": "",
                    "status": "running",
                    "started_at": None,
                    "ended_at": None,
                }
                activities.append(activity)
            output = _message_content_text(message)
            activity["output"] = output
            activity["status"] = (
                "failed"
                if message.get("error") or str(message.get("status") or "").lower() in {"error", "failed"}
                else "completed"
            )
            activity["ended_at"] = _timestamp_ms(message)
            if activity.get("started_at") and activity.get("ended_at"):
                activity["duration_ms"] = max(
                    0,
                    activity["ended_at"] - activity["started_at"],
                )

    return activities


def normalize_stored_conversation_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fold legacy standalone tool rows into their assistant response."""
    normalized: list[dict[str, Any]] = []
    for source in messages:
        if not isinstance(source, dict):
            continue
        role = str(source.get("role") or "assistant").lower()
        if role != "tool":
            message = dict(source)
            content = _message_content_text(message)
            if content or not message.get("content"):
                message["content"] = content
            normalized.append(message)
            continue

        target = next(
            (
                item
                for item in reversed(normalized)
                if str(item.get("role") or "").lower() == "assistant"
            ),
            None,
        )
        if target is None:
            target = {
                "id": f"recovered-tool-{uuid.uuid4().hex[:10]}",
                "role": "assistant",
                "name": str(source.get("name") or "default"),
                "content": "",
                "status": str(source.get("status") or "completed"),
                "kind": "message",
                "created_at": source.get("created_at") or int(time.time() * 1000),
                "meta": {},
            }
            normalized.append(target)
        meta = target.get("meta")
        meta = dict(meta) if isinstance(meta, dict) else {}
        activities = meta.get("activities")
        activities = list(activities) if isinstance(activities, list) else []
        activities.extend(build_runtime_activity_timeline([source]))
        meta["activities"] = activities
        target["meta"] = meta
    return normalized


def reconcile_conversation_runtime_results(
    conversation: dict[str, Any],
    *,
    loader: Callable[[str, str], list[dict[str, Any]]] = _load_runtime_messages,
    now_ms: Optional[int] = None,
) -> bool:
    """Persist completed detached turns back into their original conversation."""
    runtime_runs = conversation.get("runtime_runs")
    if not isinstance(runtime_runs, dict):
        return False
    changed = False
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    for profile, run in runtime_runs.items():
        if not isinstance(run, dict) or run.get("status") != "running":
            continue
        session_id = str(run.get("session_id") or "").strip()
        if not session_id:
            continue
        try:
            messages = loader(str(profile), session_id)
        except Exception:
            continue
        baseline = max(0, int(run.get("baseline_message_count") or 0))
        candidates = [
            (_message_content_text(message), message)
            for message in messages[baseline:]
            if str(message.get("role") or "").lower() == "assistant"
        ]
        final_text = next(
            (text for text, _message in reversed(candidates) if text),
            "",
        )
        if not final_text:
            continue
        existing = next(
            (
                message
                for message in conversation.get("messages") or []
                if isinstance(message.get("meta"), dict)
                and message["meta"].get("runtime_session_id") == session_id
            ),
            None,
        )
        if existing is None:
            activities = build_runtime_activity_timeline(messages[baseline:])
            recovered = _append_message(
                conversation,
                role="assistant",
                name=str(profile),
                content=final_text,
                status="completed",
                meta={
                    "runtime_session_id": session_id,
                    "recovered": True,
                    "activities": activities,
                },
            )
            recovered["created_at"] = now
        run["status"] = "completed"
        run["completed_at"] = now
        run["updated_at"] = now
        conversation["updated_at"] = now
        changed = True
    return changed


def available_profiles() -> list[dict[str, Any]]:
    return [
        {
            "name": profile.name,
            "description": profile.description or "",
            "model": profile.model or "",
            "provider": profile.provider or "",
            "gateway_running": bool(profile.gateway_running),
        }
        for profile in list_profiles()
    ]


def _work_profiles(lowered: str) -> list[str]:
    profiles = ["default"]
    if any(marker in lowered for marker in _PC_MARKERS):
        profiles.append("pc-worker")
    elif any(marker in lowered for marker in _DBB3_MARKERS):
        profiles.append("dbb3-worker")
    else:
        profiles.append("dbb3-worker")
    profiles.append("reviewer")
    return list(dict.fromkeys(profiles))


def _rule_based_user_intent(content: str) -> dict[str, Any]:
    text = content.strip()
    lowered = text.lower()
    title = summarize_task_title(text)
    matched = [marker for marker in _WORK_MARKERS if marker in lowered]
    complex_matches = [
        marker for marker in _COMPLEX_WORK_MARKERS if marker in lowered
    ]
    device_matches = [
        marker
        for marker in (*_PC_MARKERS, *_DBB3_MARKERS)
        if marker in lowered
    ]
    multi_step_count = sum(
        lowered.count(marker) for marker in _MULTI_STEP_MARKERS
    )
    score = min(6, len(complex_matches) * 2)
    score += min(3, len(device_matches) * 2)
    score += min(3, multi_step_count)
    score += 1 if matched else 0
    score += 2 if len(text) >= 80 else 0
    if any(marker in lowered for marker in _SIMPLE_CHAT_MARKERS) and len(text) < 30:
        score -= 3

    if score < 4:
        if any(marker in lowered for marker in _SIMPLE_CHAT_MARKERS):
            confidence = 0.96
        elif not matched and len(text) <= 12:
            confidence = 0.62
        elif score <= 0:
            confidence = 0.82
        else:
            confidence = 0.68
        return {
            "mode": "chat",
            "label": "简单任务",
            "title": title,
            "reason": "当前请求可由一个 Hermes 直接回答或完成，不需要创建群聊工作流。",
            "confidence": confidence,
            "source": "rules",
            "profiles": ["default"],
        }

    return {
        "mode": "work",
        "label": "群聊 + 工作流",
        "title": title,
        "reason": "检测到多步骤、设备操作、代码修改或交付要求，将创建工作流并启动多 Profile 协作。",
        "confidence": 0.95 if score >= 7 else 0.86,
        "source": "rules",
        "profiles": _work_profiles(lowered),
    }


def classify_intent_with_model(content: str) -> Optional[dict[str, Any]]:
    """Ask the configured auxiliary model only for ambiguous routing cases."""
    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    response = call_llm(
        task="intent_routing",
        messages=[
            {
                "role": "system",
                "content": (
                    "你是 Hermes 任务路由器。判断用户请求应走 simple 还是 workflow。"
                    "simple 表示普通聊天、问答、总结、搜索或一个 Hermes 可直接完成的单步任务；"
                    "workflow 表示多步骤执行、设备协作、修改代码、部署、测试、生成交付文件或长期任务。"
                    "只输出 JSON：{\"mode\":\"chat|work\",\"confidence\":0到1,"
                    "\"reason\":\"一句中文理由\"}。"
                ),
            },
            {"role": "user", "content": content.strip()},
        ],
        temperature=0,
        max_tokens=160,
        timeout=15,
    )
    raw = extract_content_or_reasoning(response)
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        return None
    parsed = json.loads(match.group(0))
    mode = str(parsed.get("mode") or "").strip().lower()
    if mode not in {"chat", "work"}:
        return None
    return {
        "mode": mode,
        "confidence": max(
            0.0,
            min(1.0, float(parsed.get("confidence") or 0.75)),
        ),
        "reason": str(
            parsed.get("reason") or "模型根据任务复杂度完成判断。"
        )[:180],
    }


def classify_user_intent(
    content: str,
    *,
    model_classifier: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    routed = _rule_based_user_intent(content)
    if routed["confidence"] >= 0.75 or model_classifier is None:
        return routed
    try:
        model_result = model_classifier(content.strip())
    except Exception:
        return routed
    if not isinstance(model_result, dict):
        return routed
    mode = str(model_result.get("mode") or "").strip().lower()
    if mode not in {"chat", "work"}:
        return routed
    routed.update(
        {
            "mode": mode,
            "label": "群聊 + 工作流" if mode == "work" else "简单任务",
            "reason": str(
                model_result.get("reason") or "模型根据任务复杂度完成判断。"
            )[:180],
            "confidence": max(
                0.0,
                min(1.0, float(model_result.get("confidence") or 0.75)),
            ),
            "source": "model",
            "profiles": (
                _work_profiles(content.lower()) if mode == "work" else ["default"]
            ),
        }
    )
    return routed


def _message_line(message: dict[str, Any]) -> str:
    name = str(message.get("name") or message.get("role") or "成员").strip()
    content = str(message.get("content") or "").strip()
    return f"{name}: {content}"


def build_group_prompt(room: dict[str, Any], profile: str, user_message: str) -> str:
    history = room.get("messages") if isinstance(room.get("messages"), list) else []
    recent = "\n".join(_message_line(item) for item in history[-_PROMPT_HISTORY:])
    members = "、".join(str(item) for item in room.get("profiles") or [])
    return (
        "你正在 Hermes 官方 WebUI 的多智能体群聊中。\n"
        f"群聊名称：{room.get('name') or '群聊'}\n"
        f"当前身份：{profile}\n"
        f"参与 Profiles：{members}\n"
        "请使用简体中文回复。结合已有讨论推进任务，明确你的判断、动作和风险；"
        "不要机械重复其他成员。如果需要其他执行端协作，请明确指出。\n\n"
        f"最近讨论：\n{recent or '暂无'}\n\n"
        f"用户的新消息：\n{user_message.strip()}"
    )


def build_single_prompt(
    conversation: dict[str, Any],
    profile: str,
    user_message: str,
) -> str:
    history = (
        conversation.get("messages")
        if isinstance(conversation.get("messages"), list)
        else []
    )
    recent = "\n".join(_message_line(item) for item in history[-_PROMPT_HISTORY:])
    return (
        "你正在 Hermes 官方 WebUI 单聊中。\n"
        f"当前 Hermes Profile：{profile}\n"
        "请使用简体中文直接回答并执行用户请求。你仍可使用该 Profile 已配置的"
        "模型、Skill、MCP、记忆和工具。回复应清晰说明结果、关键过程与错误。\n\n"
        f"最近对话：\n{recent or '暂无'}\n\n"
        f"用户的新消息：\n{user_message.strip()}"
    )


def run_profile_turn(
    profile: str,
    prompt: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    hermes_bin: str = "/usr/local/bin/hermes",
) -> str:
    command = [
        hermes_bin,
        "-p",
        profile,
        "chat",
        "-Q",
        "-q",
        prompt,
        "--source",
        "dashboard-group",
        "--max-turns",
        "45",
    ]
    result = runner(
        command,
        shell=False,
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, "HOME": os.environ.get("HOME", "/home/hermes")},
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "Hermes profile execution failed").strip()
        raise RuntimeError(error[-2000:])
    response = (result.stdout or "").strip()
    if not response:
        raise RuntimeError("Hermes profile returned an empty response")
    return response


def run_single_turn(
    profile: str,
    prompt: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    hermes_bin: str = "/usr/local/bin/hermes",
) -> str:
    command = [
        hermes_bin,
        "-p",
        profile,
        "chat",
        "-Q",
        "-q",
        prompt,
        "--source",
        "dashboard-single",
        "--max-turns",
        "45",
    ]
    result = runner(
        command,
        shell=False,
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, "HOME": os.environ.get("HOME", "/home/hermes")},
    )
    if result.returncode != 0:
        error = (
            result.stderr
            or result.stdout
            or "Hermes profile execution failed"
        ).strip()
        raise RuntimeError(error[-2000:])
    response = (result.stdout or "").strip()
    if not response:
        raise RuntimeError("Hermes profile returned an empty response")
    return response


def _room_by_id(state: dict[str, Any], room_id: str) -> dict[str, Any]:
    for room in state.get("rooms") or []:
        if room.get("id") == room_id:
            return room
    raise HTTPException(status_code=404, detail="群聊不存在")


def _conversation_by_id(
    state: dict[str, Any],
    conversation_id: str,
) -> dict[str, Any]:
    for conversation in state.get("conversations") or []:
        if conversation.get("id") == conversation_id:
            return conversation
    raise HTTPException(status_code=404, detail="单聊会话不存在")


def _append_message(
    room: dict[str, Any],
    *,
    role: str,
    name: str,
    content: str,
    status: str = "completed",
    kind: str = "message",
    meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    message = {
        "id": f"msg_{uuid.uuid4().hex[:14]}",
        "role": role,
        "name": name,
        "content": content,
        "status": status,
        "kind": kind,
        "created_at": int(time.time() * 1000),
    }
    if meta:
        message["meta"] = meta
    messages = room.setdefault("messages", [])
    messages.append(message)
    if len(messages) > _MAX_MESSAGES:
        del messages[:-_MAX_MESSAGES]
    room["updated_at"] = message["created_at"]
    return message


class CreateRoomBody(BaseModel):
    name: str = "新群聊"
    profiles: list[str] = Field(default_factory=list)


class SendMessageBody(BaseModel):
    content: str
    profiles: Optional[list[str]] = None


class CreateSingleConversationBody(BaseModel):
    profile: str = "default"
    title: str = "新对话"


class AdoptSingleConversationBody(BaseModel):
    profile: str = "default"
    session_id: str
    title: str = "Imported session"
    messages: list[dict[str, Any]] = Field(default_factory=list)


class SendSingleMessageBody(BaseModel):
    content: str


class RouteMessageBody(BaseModel):
    content: str
    mode: str = "auto"


class RecordMessageBody(BaseModel):
    role: str
    name: str
    content: str
    status: str = "completed"
    kind: str = "message"
    meta: dict[str, Any] = Field(default_factory=dict)


class RuntimeSessionBody(BaseModel):
    profile: str = "default"
    session_id: str
    status: str = "running"


@router.get("/profiles")
def get_profiles():
    return {"profiles": available_profiles()}


@router.post("/route")
def route_message(payload: RouteMessageBody):
    mode = payload.mode.strip().lower()
    if mode in {"chat", "work"}:
        routed = classify_user_intent(payload.content)
        routed["mode"] = mode
        routed["label"] = "简单任务" if mode == "chat" else "群聊 + 工作流"
        routed["confidence"] = 1.0
        routed["source"] = "manual"
        routed["reason"] = (
            "用户手动选择普通对话。"
            if mode == "chat"
            else "用户手动选择工作任务。"
        )
        if mode == "chat":
            routed["profiles"] = ["default"]
        elif routed.get("profiles") == ["default"]:
            routed["profiles"] = ["default", "dbb3-worker", "reviewer"]
        return routed
    return classify_user_intent(
        payload.content,
        model_classifier=classify_intent_with_model,
    )


@router.get("/single/conversations")
def get_single_conversations():
    with _STATE_LOCK:
        state = load_single_state()
        conversations = state.get("conversations") or []
        changed = False
        for conversation in conversations:
            changed = reconcile_conversation_runtime_results(conversation) or changed
            changed = compact_conversation_title(conversation) or changed
        if changed:
            save_single_state(state)
        summaries = [
            {
                **conversation,
                "messages": (conversation.get("messages") or [])[-1:],
                "message_count": len(conversation.get("messages") or []),
            }
            for conversation in conversations
        ]
    return {"conversations": summaries}


@router.post("/single/conversations")
def create_single_chat(payload: CreateSingleConversationBody):
    known = {item["name"] for item in available_profiles()}
    if payload.profile not in known:
        raise HTTPException(status_code=400, detail="Hermes Profile 不存在")
    with _STATE_LOCK:
        state = load_single_state()
        conversation = create_single_conversation(payload.profile, payload.title)
        state["conversations"].insert(0, conversation)
        save_single_state(state)
    return {"conversation": conversation}


@router.post("/single/conversations/adopt")
def adopt_single_chat(payload: AdoptSingleConversationBody):
    session_id = payload.session_id.strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="Session ID is required")
    known = {item["name"] for item in available_profiles()}
    if payload.profile not in known:
        raise HTTPException(status_code=400, detail="Hermes Profile does not exist")
    with _STATE_LOCK:
        state = load_single_state()
        for conversation in state.get("conversations") or []:
            if session_id in (
                conversation.get("runtime_sessions") or {}
            ).values():
                return {"conversation": conversation, "created": False}
        conversation = create_adopted_single_conversation(
            payload.profile,
            session_id,
            payload.title,
            payload.messages,
        )
        state["conversations"].insert(0, conversation)
        save_single_state(state)
    return {"conversation": conversation, "created": True}


@router.get("/single/conversations/{conversation_id}")
def get_single_conversation(conversation_id: str):
    with _STATE_LOCK:
        state = load_single_state()
        conversation = _conversation_by_id(state, conversation_id)
        changed = reconcile_conversation_runtime_results(conversation)
        changed = compact_conversation_title(conversation) or changed
        if changed:
            save_single_state(state)
        return {"conversation": conversation}


@router.get("/single/conversations/{conversation_id}/attachments")
def get_conversation_attachments(conversation_id: str):
    with _STATE_LOCK:
        _conversation_by_id(load_single_state(), conversation_id)
    uploads_dir = _conversation_file_dir(conversation_id, "uploads")
    outputs_dir = _conversation_file_dir(conversation_id, "outputs")
    return {
        "attachments": _list_conversation_attachments(conversation_id),
        "uploads_dir": str(uploads_dir.resolve()),
        "output_dir": str(outputs_dir.resolve()),
    }


@router.post("/single/conversations/{conversation_id}/attachments")
async def upload_conversation_attachment(
    conversation_id: str,
    request: Request,
):
    with _STATE_LOCK:
        _conversation_by_id(load_single_state(), conversation_id)
    try:
        filename = safe_attachment_name(
            unquote(request.headers.get("x-filename", ""))
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    target = _conversation_file_dir(conversation_id, "uploads") / filename
    total = 0
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.upload")
    try:
        with temp.open("wb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MAX_ATTACHMENT_BYTES:
                    raise HTTPException(status_code=413, detail="附件不能超过 64 MB")
                handle.write(chunk)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return {
        "attachment": _attachment_record(
            conversation_id,
            "uploads",
            target,
        ),
        "output_dir": str(
            _conversation_file_dir(conversation_id, "outputs").resolve()
        ),
    }


@router.get(
    "/single/conversations/{conversation_id}/attachments/"
    "{bucket}/{relative_path:path}"
)
def download_conversation_attachment(
    conversation_id: str,
    bucket: str,
    relative_path: str,
):
    with _STATE_LOCK:
        _conversation_by_id(load_single_state(), conversation_id)
    root = _conversation_file_dir(conversation_id, bucket).resolve()
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(status_code=403, detail="附件路径越界")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="附件不存在")
    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type=mimetypes.guess_type(target.name)[0]
        or "application/octet-stream",
    )


@router.post("/single/conversations/{conversation_id}/record")
def record_single_message(
    conversation_id: str,
    payload: RecordMessageBody,
):
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")
    with _STATE_LOCK:
        state = load_single_state()
        conversation = _conversation_by_id(state, conversation_id)
        runtime_session_id = str(
            payload.meta.get("runtime_session_id") or ""
        ).strip()
        message = None
        if payload.role == "assistant" and runtime_session_id:
            message = next(
                (
                    item
                    for item in conversation.get("messages") or []
                    if isinstance(item.get("meta"), dict)
                    and item["meta"].get("runtime_session_id")
                    == runtime_session_id
                ),
                None,
            )
        if message is None:
            message = _append_message(
                conversation,
                role=payload.role,
                name=payload.name,
                content=payload.content.strip(),
                status=payload.status,
                kind=payload.kind,
                meta=payload.meta,
            )
        else:
            message.update(
                {
                    "role": payload.role,
                    "name": payload.name,
                    "content": payload.content.strip(),
                    "status": payload.status,
                    "kind": payload.kind,
                    "meta": {**message.get("meta", {}), **payload.meta},
                    "created_at": int(time.time() * 1000),
                }
            )
            conversation["updated_at"] = message["created_at"]
        if runtime_session_id:
            for run in (conversation.get("runtime_runs") or {}).values():
                if run.get("session_id") == runtime_session_id:
                    run["status"] = "completed"
                    run["completed_at"] = message["created_at"]
                    run["updated_at"] = message["created_at"]
        if (
            payload.role == "user"
            and conversation.get("title") in {"", "新对话", None}
        ):
            conversation["title"] = summarize_task_title(payload.content)
        save_single_state(state)
    return {"message": message}


@router.post("/single/conversations/{conversation_id}/runtime-session")
def save_runtime_session(
    conversation_id: str,
    payload: RuntimeSessionBody,
):
    with _STATE_LOCK:
        state = load_single_state()
        conversation = _conversation_by_id(state, conversation_id)
        if payload.status == "running":
            try:
                baseline_message_count = len(
                    _load_runtime_messages(payload.profile, payload.session_id)
                )
            except Exception:
                baseline_message_count = 0
            mark_conversation_runtime_run(
                conversation,
                payload.profile,
                payload.session_id,
                baseline_message_count=baseline_message_count,
            )
        else:
            set_conversation_runtime_session(
                conversation,
                payload.profile,
                payload.session_id,
            )
        runtime_sessions = conversation.get("runtime_sessions") or {}
        save_single_state(state)
    return {"runtime_sessions": runtime_sessions}


@router.delete("/single/conversations/{conversation_id}")
def delete_single_conversation(conversation_id: str):
    with _STATE_LOCK:
        state = load_single_state()
        before = len(state.get("conversations") or [])
        state["conversations"] = [
            conversation
            for conversation in state.get("conversations") or []
            if conversation.get("id") != conversation_id
        ]
        if len(state["conversations"]) == before:
            raise HTTPException(status_code=404, detail="单聊会话不存在")
        save_single_state(state)
    return {"ok": True}


@router.post("/single/conversations/{conversation_id}/messages")
async def send_single_message(
    conversation_id: str,
    payload: SendSingleMessageBody,
):
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="消息不能为空")

    with _STATE_LOCK:
        state = load_single_state()
        conversation = _conversation_by_id(state, conversation_id)
        profile = str(conversation.get("profile") or "default")
        _append_message(
            conversation,
            role="user",
            name="用户",
            content=content,
        )
        if conversation.get("title") in {"", "新对话", None}:
            conversation["title"] = summarize_task_title(content)
        prompt = build_single_prompt(conversation, profile, content)
        save_single_state(state)

    try:
        reply = await asyncio.to_thread(run_single_turn, profile, prompt)
        status = "completed"
    except Exception as exc:
        reply = f"执行失败：{str(exc)}"
        status = "failed"

    with _STATE_LOCK:
        state = load_single_state()
        conversation = _conversation_by_id(state, conversation_id)
        message = _append_message(
            conversation,
            role="assistant",
            name=profile,
            content=reply,
            status=status,
        )
        save_single_state(state)
    return {"ok": status == "completed", "message": message}


@router.get("/rooms")
def get_rooms():
    with _STATE_LOCK:
        rooms = load_state().get("rooms") or []
        summaries = [
            {
                **room,
                "messages": (room.get("messages") or [])[-1:],
                "message_count": len(room.get("messages") or []),
            }
            for room in rooms
        ]
    return {"rooms": summaries}


@router.post("/rooms")
def create_room(payload: CreateRoomBody):
    known = {item["name"] for item in available_profiles()}
    selected = [name for name in payload.profiles if name in known]
    if not selected:
        raise HTTPException(status_code=400, detail="至少选择一个 Hermes Profile")
    with _STATE_LOCK:
        state = load_state()
        room = create_room_record(payload.name, selected)
        state["rooms"].insert(0, room)
        save_state(state)
    return {"room": room}


@router.get("/rooms/{room_id}")
def get_room(room_id: str):
    with _STATE_LOCK:
        room = _room_by_id(load_state(), room_id)
        return {"room": room}


@router.delete("/rooms/{room_id}")
def delete_room(room_id: str):
    with _STATE_LOCK:
        state = load_state()
        before = len(state.get("rooms") or [])
        state["rooms"] = [
            room for room in state.get("rooms") or [] if room.get("id") != room_id
        ]
        if len(state["rooms"]) == before:
            raise HTTPException(status_code=404, detail="群聊不存在")
        save_state(state)
    return {"ok": True}


@router.post("/rooms/{room_id}/messages")
async def send_message(room_id: str, payload: SendMessageBody):
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="消息不能为空")

    with _STATE_LOCK:
        state = load_state()
        room = _room_by_id(state, room_id)
        known_profiles = set(room.get("profiles") or [])
        requested = payload.profiles or list(room.get("profiles") or [])
        targets = [name for name in requested if name in known_profiles]
        if not targets:
            raise HTTPException(status_code=400, detail="没有可执行的群聊成员")
        _append_message(room, role="user", name="用户", content=content)
        save_state(state)

    responses = []
    for profile in targets:
        with _STATE_LOCK:
            state = load_state()
            room = _room_by_id(state, room_id)
            prompt = build_group_prompt(room, profile, content)
        try:
            reply = await asyncio.to_thread(run_profile_turn, profile, prompt)
            status = "completed"
        except Exception as exc:
            reply = f"执行失败：{str(exc)}"
            status = "failed"
        with _STATE_LOCK:
            state = load_state()
            room = _room_by_id(state, room_id)
            message = _append_message(
                room,
                role="assistant",
                name=profile,
                content=reply,
                status=status,
            )
            save_state(state)
        responses.append(message)

    return {"ok": True, "messages": responses}
