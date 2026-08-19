#!/usr/bin/env python3
"""Measure the official stdio gateway without the collaboration state layer."""

from __future__ import annotations

import os
from pathlib import Path
import time
import json
import uuid

from plugins.collaboration.dashboard.hosted_tui_runtime import (
    release_hosted_gateway_conversation,
    run_hosted_gateway_turn,
)


def main() -> None:
    runtime_home = os.environ["HERMES_BENCHMARK_RUNTIME_HOME"]
    owner_id = os.environ["HERMES_BENCHMARK_OWNER_ID"]
    account_generation = os.environ["HERMES_BENCHMARK_ACCOUNT_GENERATION"]
    artifact_root = os.environ.get("HERMES_BENCHMARK_ARTIFACT_ROOT", runtime_home)
    prompt = os.environ.get(
        "HERMES_BENCHMARK_PROMPT",
        "请用一句话解释什么是 SSE。",
    )
    if os.environ.get("HERMES_BENCHMARK_WRAP_HOSTED") == "1":
        prompt = (
            "Planning behavior: for a request with 3 or more actionable steps, or multiple "
            "tasks, call the todo tool before execution. Keep exactly one item in_progress "
            "and update the list immediately after each step. Do not create a todo list for "
            "ordinary single-step chat.\n\n"
            "你正在 Hermes 官方 WebUI 单聊中。\n"
            "当前 Hermes Profile：default\n"
            "请使用简体中文直接回答并执行用户请求。你仍可使用该 Profile 已配置的"
            "模型、Skill、MCP、记忆和工具。回复应清晰说明结果、关键过程与错误。\n\n"
            f"最近对话：\nYou: {prompt}\n\n"
            f"用户的新消息：\n{prompt}"
        )
    import_root = os.environ.get(
        "HERMES_BENCHMARK_IMPORT_ROOT",
        str(Path(__file__).resolve().parents[1]),
    )
    conversation_id = f"direct_{uuid.uuid4().hex}"
    turn_id = f"turn_{uuid.uuid4().hex}"
    started = time.perf_counter()
    turn_started = started
    seen: set[str] = set()

    def elapsed() -> float:
        return time.perf_counter() - started

    def on_event(event: dict[str, object]) -> None:
        event_type = str(event.get("type") or "")
        if event_type in seen and event_type.endswith(".delta"):
            return
        seen.add(event_type)
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        preview = {
            key: str(payload[key])[:120]
            for key in ("text", "delta", "status", "model", "provider")
            if payload.get(key) not in (None, "")
        }
        turn_elapsed = time.perf_counter() - turn_started
        print(
            f"{elapsed():.3f} TURN+{turn_elapsed:.3f} EVENT {event_type} "
            f"{json.dumps(preview, ensure_ascii=False)}",
            flush=True,
        )

    try:
        for turn_number in (1, 2):
            turn_started = time.perf_counter()
            seen.clear()
            result = run_hosted_gateway_turn(
                prompt,
                runtime_home=runtime_home,
                owner_id=owner_id,
                account_generation=account_generation,
                conversation_id=conversation_id,
                turn_id=f"{turn_id}_{turn_number}",
                profile="default",
                artifact_root=artifact_root,
                import_root=import_root,
                event_callback=on_event,
                timeout=90.0,
                extra_env={
                    "HERMES_API_MAX_RETRIES": "5",
                    "HERMES_API_RETRY_CLIENT_ERRORS": "1",
                    "HERMES_API_RETRY_DELAY_SECONDS": "15",
                    "HERMES_API_RETRY_STATUS_LIVE": "1",
                    "HERMES_STARTUP_PROFILE": "1",
                },
            )
            print(
                f"{elapsed():.3f} TURN+{time.perf_counter() - turn_started:.3f} "
                f"RESULT {turn_number} {result}",
                flush=True,
            )
    finally:
        release_hosted_gateway_conversation(
            conversation_id,
            owner_id=owner_id,
            account_generation=account_generation,
        )


if __name__ == "__main__":
    main()
