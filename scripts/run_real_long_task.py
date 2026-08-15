"""Run a real hosted long task through the collaboration plugin and export events.

This script is a validation harness for the iOS reducer path. It creates a
chat-mode hosted turn in the active collaboration state, starts the real
Hermes workflow (using the configured provider/tools), waits for the turn to
reach a terminal state, and writes the durable hosted events to a JSON file
for the iOS reducer acceptance script.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Make the repo importable when executed as a script.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.collaboration.dashboard import plugin_api  # noqa: E402


def _find_or_create_conversation(state: dict, conversation_id: str) -> dict:
    for conv in state.get("conversations") or []:
        if str(conv.get("id") or "") == conversation_id:
            return conv
    # Create a minimal conversation with the same shape the plugin uses.
    now = int(time.time() * 1000)
    conv = {
        "id": conversation_id,
        "owner_id": "owner-a",
        "account_generation": "",
        "profile": "default",
        "title": "Real long task",
        "created_at": now,
        "updated_at": now,
        "messages": [],
        "hosted_turns": {},
        "hosted_events": [],
        "hosted_event_sequences": {},
        "hosted_event_terminals": {},
        "hosted_event_cursor": 0,
        "hosted_event_min_cursor": 1,
        "runtime_sessions": {},
        "session_entries": [],
        "session_entry_cursor": 0,
        "session_entry_leaf_id": "",
        "runtime_runs": {},
        "pending_turn_cancellations": {},
        "event_cursor": 0,
        "event_updated_at": 0,
    }
    state.setdefault("conversations", []).append(conv)
    return conv


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--conversation-id", default="chat_real_long_task")
    parser.add_argument("--turn-id", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument(
        "--prompt",
        default=(
            "请完成一个真实的 Windows 本地文件工具长任务：\n"
            "1. 使用 search_files 或 terminal 列出 C:\\Users\\given\\hermes-audit\\hermes-agent\\tests\\tools "
            "下所有 test_local_env_*.py 文件；\n"
            "2. 使用 read_file 读取其中至少 3 个文件的完整内容；\n"
            "3. 统计每个文件的行数、函数定义数量（以 def 开头的行为准）和文件大小；\n"
            "4. 用 write_file 或 terminal 把统计结果写入 "
            "C:\\Users\\given\\hermes-audit\\validation-targets\\long-task-report.md；\n"
            "5. 最后在回答中给出报告摘要。请尽量多调用文件工具，确保这是一个真实、多步骤的长任务。"
        ),
    )
    args = parser.parse_args()

    conversation_id = args.conversation_id
    turn_id = args.turn_id or f"turn-long-{int(time.time() * 1000)}"
    output = Path(args.output).expanduser().resolve() if args.output else (
        REPO_ROOT.parent / "validation-targets" / f"{turn_id}-events.json"
    )

    state = plugin_api.load_single_state()
    conversation = _find_or_create_conversation(state, conversation_id)
    if not conversation.get("account_generation"):
        conversation["account_generation"] = plugin_api._account_generation_for_owner(
            str(conversation.get("owner_id") or "owner-a")
        )
    run = plugin_api.create_hosted_turn_record(
        conversation,
        turn_id=turn_id,
        content=args.prompt,
        title="Real Windows file-tool long task",
        profiles=["default"],
        artifact_required=False,
        mode="chat",
        delivery_context=(
            "Use real local file tools. Write the report file and then summarize it."
        ),
    )
    plugin_api.save_single_state(state)
    print(f"created turn {turn_id} in {conversation_id} status={run.get('status')}")

    thread = plugin_api.start_hosted_workflow(conversation_id, turn_id)
    print(f"workflow thread started: {thread.name}")

    deadline = time.monotonic() + args.timeout
    terminal_statuses = {"completed", "failed", "cancelled"}
    last_status = ""
    while time.monotonic() < deadline:
        time.sleep(3.0)
        state = plugin_api.load_single_state()
        conv = next(
            (c for c in state.get("conversations") or [] if str(c.get("id") or "") == conversation_id),
            None,
        )
        if conv is None:
            break
        current = (conv.get("hosted_turns") or {}).get(turn_id)
        if not isinstance(current, dict):
            break
        status = str(current.get("status") or "")
        stage = str(current.get("stage") or "")
        if status != last_status:
            print(f"[{time.strftime('%H:%M:%S')}] turn status={status} stage={stage}")
            last_status = status
        if status in terminal_statuses:
            break
    else:
        print("timed out waiting for hosted turn")
        return 3

    state = plugin_api.load_single_state()
    conv = next(
        (c for c in state.get("conversations") or [] if str(c.get("id") or "") == conversation_id),
        None,
    )
    if conv is None:
        print("conversation missing after run")
        return 4
    current = (conv.get("hosted_turns") or {}).get(turn_id)
    events = [
        ev for ev in (conv.get("hosted_events") or [])
        if isinstance(ev, dict) and str(ev.get("turn_id") or "") == turn_id
    ]
    payload = {
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "status": (current or {}).get("status"),
        "stage": (current or {}).get("stage"),
        "error": (current or {}).get("error", ""),
        "event_count": len(events),
        "events": events,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "status": payload["status"],
        "stage": payload["stage"],
        "error": payload["error"],
        "event_count": payload["event_count"],
        "output": str(output),
    }, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
