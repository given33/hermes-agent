import asyncio
import importlib.util
import json
import os
import subprocess
import threading
import time
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "collaboration"
    / "dashboard"
    / "plugin_api.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("collaboration_plugin_api", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class CollaborationDashboardTests(unittest.TestCase):
    def test_short_chinese_greeting_keeps_its_first_character_in_title(self):
        module = load_module()

        self.assertEqual(module.summarize_task_title("你好"), "你好")
        self.assertEqual(module.summarize_task_title("你好吗"), "你好吗")
        self.assertEqual(module.summarize_task_title("你帮我检查服务"), "检查服务")

    def test_profile_toolsets_connect_mcp_before_resolving_agent_snapshot(self):
        module = load_module()
        calls = []
        config = {"mcp_servers": {"ios-location": {"enabled": True}}}

        with patch(
            "tools.mcp_tool.discover_mcp_tools",
            side_effect=lambda: calls.append("discover") or ["current_location"],
        ), patch(
            "hermes_cli.tools_config._get_platform_tools",
            side_effect=lambda current, platform: (
                calls.append(("resolve", current, platform)) or {"ios-location"}
            ),
        ):
            resolved = module._discover_profile_toolsets(config)

        self.assertEqual(resolved, ["ios-location"])
        self.assertEqual(calls[0], "discover")
        self.assertEqual(calls[1], ("resolve", config, "cli"))

    def test_room_store_round_trip(self):
        module = load_module()
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "rooms.json"
            room = module.create_room_record("研发讨论", ["default", "pc-worker"])
            module.save_state({"rooms": [room]}, state_path)

            loaded = module.load_state(state_path)

        self.assertEqual(loaded["rooms"][0]["name"], "研发讨论")
        self.assertEqual(loaded["rooms"][0]["profiles"], ["default", "pc-worker"])
        self.assertEqual(loaded["rooms"][0]["messages"], [])

    def test_single_store_recovers_atomic_backup_and_quarantines_corruption(self):
        module = load_module()

        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "single.json"
            first = module.create_single_conversation("default", "first")
            second = module.create_single_conversation("default", "second")
            module.save_single_state({"conversations": [first]}, state_path)
            module.save_single_state({"conversations": [second]}, state_path)

            backup_path = state_path.with_name("single.json.bak")
            self.assertEqual(
                json.loads(backup_path.read_text(encoding="utf-8"))["conversations"][0]["id"],
                first["id"],
            )
            state_path.write_text('{"conversations": [', encoding="utf-8")

            recovered = module.load_single_state(state_path)
            quarantines = list(Path(tmp).glob("single.json.corrupt.*"))

            self.assertEqual(recovered["conversations"][0]["id"], first["id"])
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["conversations"][0]["id"],
                first["id"],
            )
            self.assertEqual(len(quarantines), 1)
            self.assertEqual(
                quarantines[0].read_text(encoding="utf-8"),
                '{"conversations": [',
            )

    def test_corrupt_store_without_backup_remains_blocked_after_isolation(self):
        module = load_module()

        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "single.json"
            state_path.write_text('{"conversations": "not-a-list"}', encoding="utf-8")

            with self.assertRaises(module.StateStoreError):
                module.load_single_state(state_path)
            quarantines = list(Path(tmp).glob("single.json.corrupt.*"))
            self.assertEqual(len(quarantines), 1)
            self.assertFalse(state_path.exists())

            with self.assertRaises(module.StateStoreError):
                module.load_single_state(state_path)
            with self.assertRaises(module.StateStoreError):
                module.save_single_state({"conversations": []}, state_path)
            self.assertFalse(state_path.exists())
            self.assertEqual(
                quarantines[0].read_text(encoding="utf-8"),
                '{"conversations": "not-a-list"}',
            )

    def test_room_store_read_error_recovers_backup_without_returning_empty(self):
        module = load_module()

        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "rooms.json"
            first = module.create_room_record("first", ["default"])
            second = module.create_room_record("second", ["default"])
            module.save_state({"rooms": [first]}, state_path)
            module.save_state({"rooms": [second]}, state_path)
            original_reader = module._read_state_document

            def fail_primary_once(target, collection_key):
                if target == state_path:
                    raise OSError("simulated primary read failure")
                return original_reader(target, collection_key)

            with patch.object(module, "_read_state_document", fail_primary_once):
                recovered = module.load_state(state_path)

            self.assertEqual(recovered["rooms"][0]["id"], first["id"])
            self.assertEqual(len(list(Path(tmp).glob("rooms.json.corrupt.*"))), 1)
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["rooms"][0]["id"],
                first["id"],
            )

    def test_atomic_primary_replace_failure_keeps_previous_store_and_backup(self):
        module = load_module()

        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "single.json"
            first = module.create_single_conversation("default", "first")
            second = module.create_single_conversation("default", "second")
            module.save_single_state({"conversations": [first]}, state_path)
            original_replace = module.os.replace

            def fail_primary_replace(source, destination):
                if Path(destination) == state_path:
                    raise OSError("simulated atomic replace failure")
                return original_replace(source, destination)

            with patch.object(module.os, "replace", fail_primary_replace):
                with self.assertRaises(OSError):
                    module.save_single_state({"conversations": [second]}, state_path)

            self.assertEqual(
                module.load_single_state(state_path)["conversations"][0]["id"],
                first["id"],
            )
            self.assertEqual(
                json.loads(
                    state_path.with_name("single.json.bak").read_text(encoding="utf-8")
                )["conversations"][0]["id"],
                first["id"],
            )
            self.assertEqual(list(Path(tmp).glob(".*.tmp")), [])

    def test_profile_turn_uses_argument_array_without_shell(self):
        module = load_module()
        captured = {}

        def runner(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="执行完成", stderr="")

        response = module.run_profile_turn(
            "pc-worker",
            "检查本地电脑",
            runner=runner,
            hermes_bin="/usr/local/bin/hermes",
            kanban_task_id="t_worker_child",
        )

        self.assertEqual(response, "执行完成")
        self.assertEqual(
            captured["args"],
            [
                "/usr/local/bin/hermes",
                "-p",
                "pc-worker",
                "chat",
                "-Q",
                "-q",
                "检查本地电脑",
                "--source",
                "dashboard-group",
                "--max-turns",
                "45",
            ],
        )
        self.assertFalse(captured["kwargs"]["shell"])
        self.assertEqual(captured["kwargs"]["timeout"], 600)
        self.assertEqual(
            captured["kwargs"]["env"]["HERMES_KANBAN_TASK"],
            "t_worker_child",
        )

    def test_single_chat_store_and_prompt_keep_conversation_context(self):
        module = load_module()
        from tempfile import TemporaryDirectory

        conversation = module.create_single_conversation("default")
        self.assertEqual(conversation["runtime_sessions"], {})
        module.set_conversation_runtime_session(
            conversation,
            "default",
            "session_primary",
        )
        self.assertEqual(
            conversation["runtime_sessions"]["default"],
            "session_primary",
        )
        conversation["messages"] = [
            {"role": "user", "name": "用户", "content": "先检查服务"},
            {"role": "assistant", "name": "default", "content": "服务正常"},
        ]

        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "single.json"
            module.save_single_state({"conversations": [conversation]}, state_path)
            loaded = module.load_single_state(state_path)

        prompt = module.build_single_prompt(
            loaded["conversations"][0],
            "default",
            "继续检查网络",
        )
        self.assertIn("Hermes 官方 WebUI 单聊", prompt)
        self.assertIn("用户: 先检查服务", prompt)
        self.assertIn("default: 服务正常", prompt)
        self.assertIn("继续检查网络", prompt)

    def test_runtime_run_reconciles_background_result_into_original_conversation(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        module.mark_conversation_runtime_run(
            conversation,
            "default",
            "session-background-1",
            baseline_message_count=2,
            started_at=1000,
        )

        changed = module.reconcile_conversation_runtime_results(
            conversation,
            loader=lambda _profile, _session_id: [
                {"role": "user", "content": "旧问题"},
                {"role": "assistant", "content": "旧回答"},
                {"role": "user", "content": "后台任务"},
                {"role": "assistant", "content": "后台任务已经完成"},
            ],
            now_ms=2000,
        )

        self.assertTrue(changed)
        self.assertEqual(conversation["messages"][-1]["content"], "后台任务已经完成")
        self.assertEqual(conversation["messages"][-1]["status"], "completed")
        self.assertEqual(
            conversation["messages"][-1]["meta"]["runtime_session_id"],
            "session-background-1",
        )
        self.assertTrue(conversation["messages"][-1]["meta"]["recovered"])
        self.assertEqual(
            conversation["runtime_runs"]["default"]["status"],
            "completed",
        )

        self.assertFalse(
            module.reconcile_conversation_runtime_results(
                conversation,
                loader=lambda _profile, _session_id: [],
                now_ms=3000,
            )
        )
        self.assertEqual(
            [m["content"] for m in conversation["messages"]].count(
                "后台任务已经完成"
            ),
            1,
        )

    def test_runtime_run_stays_pending_until_assistant_result_exists(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        module.mark_conversation_runtime_run(
            conversation,
            "default",
            "session-background-2",
            baseline_message_count=1,
        )

        changed = module.reconcile_conversation_runtime_results(
            conversation,
            loader=lambda _profile, _session_id: [
                {"role": "user", "content": "旧消息"},
                {"role": "user", "content": "任务已提交"},
                {"role": "tool", "content": "仍在执行"},
            ],
        )

        self.assertFalse(changed)
        self.assertEqual(
            conversation["runtime_runs"]["default"]["status"],
            "running",
        )
        self.assertEqual(conversation["messages"], [])

    def test_same_runtime_session_keeps_each_completed_turn(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None

        for turn_id, content in (
            ("turn-one", "第一轮回答"),
            ("turn-two", "第二轮回答"),
        ):
            module.record_single_message(
                conversation["id"],
                SimpleNamespace(
                    role="assistant",
                    name="default",
                    content=content,
                    status="completed",
                    kind="message",
                    meta={
                        "runtime_session_id": "shared-session",
                        "runtime_turn_id": turn_id,
                    },
                ),
            )

        self.assertEqual(
            [message["content"] for message in conversation["messages"]],
            ["第一轮回答", "第二轮回答"],
        )

    def test_runtime_recovery_matches_turn_id_not_reused_session_id(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        conversation["messages"].append(
            {
                "id": "old-answer",
                "role": "assistant",
                "name": "default",
                "content": "旧轮次回答",
                "status": "completed",
                "kind": "message",
                "created_at": 1000,
                "meta": {
                    "runtime_session_id": "shared-session",
                    "runtime_turn_id": "turn-old",
                },
            }
        )
        module.mark_conversation_runtime_run(
            conversation,
            "default",
            "shared-session",
            turn_id="turn-new",
            baseline_message_count=2,
            started_at=1500,
        )

        changed = module.reconcile_conversation_runtime_results(
            conversation,
            loader=lambda _profile, _session_id: [
                {"role": "user", "content": "旧问题"},
                {"role": "assistant", "content": "旧轮次回答"},
                {"role": "user", "content": "新问题"},
                {"role": "assistant", "content": "新轮次回答"},
            ],
            now_ms=2000,
        )

        self.assertTrue(changed)
        self.assertEqual(
            [message["content"] for message in conversation["messages"]],
            ["旧轮次回答", "新轮次回答"],
        )
        self.assertEqual(
            conversation["messages"][-1]["meta"]["runtime_turn_id"],
            "turn-new",
        )

    def test_mapped_runtime_session_backfills_missing_assistant_turns(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        module.set_conversation_runtime_session(
            conversation,
            "default",
            "shared-session",
        )
        conversation["messages"] = [
            {
                "id": "user-one",
                "role": "user",
                "name": "你",
                "content": "第一个问题",
                "created_at": 1000,
                "meta": {},
            },
            {
                "id": "user-two",
                "role": "user",
                "name": "你",
                "content": "第二个问题",
                "created_at": 2000,
                "meta": {},
            },
            {
                "id": "answer-two",
                "role": "assistant",
                "name": "default",
                "content": "第二个回答",
                "created_at": 2500,
                "meta": {"runtime_session_id": "shared-session"},
            },
        ]
        runtime_messages = [
            {"role": "user", "content": "第一个问题", "timestamp": 1.0},
            {"role": "assistant", "content": "第一个回答", "timestamp": 1.5},
            {"role": "user", "content": "第二个问题", "timestamp": 2.0},
            {"role": "assistant", "content": "第二个回答", "timestamp": 2.5},
        ]

        changed = module.reconcile_conversation_mapped_sessions(
            conversation,
            loader=lambda _profile, _session_id: runtime_messages,
        )

        self.assertTrue(changed)
        self.assertEqual(
            [message["content"] for message in conversation["messages"]],
            ["第一个问题", "第一个回答", "第二个问题", "第二个回答"],
        )
        self.assertEqual(
            conversation["messages"][1]["meta"]["runtime_session_id"],
            "shared-session",
        )
        self.assertFalse(
            module.reconcile_conversation_mapped_sessions(
                conversation,
                loader=lambda _profile, _session_id: runtime_messages,
            )
        )

    def test_runtime_activity_timeline_restores_reasoning_and_tool_details(self):
        module = load_module()
        messages = [
            {
                "role": "assistant",
                "reasoning_content": "先检查本地服务状态。",
                "tool_calls": [
                    {
                        "id": "call-terminal",
                        "function": {
                            "name": "terminal",
                            "arguments": '{"command":"systemctl status hermes"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-terminal",
                "content": '{"output":"active","exit_code":0}',
            },
            {
                "role": "assistant",
                "reasoning_content": "服务正常，继续查询知识库。",
                "tool_calls": [
                    {
                        "id": "call-mcp",
                        "function": {
                            "name": "mcp__knowledge__kb_search",
                            "arguments": '{"query":"Hermes"}',
                        },
                    },
                    {
                        "id": "call-skill",
                        "function": {
                            "name": "skill_manage",
                            "arguments": '{"action":"view","name":"network"}',
                        },
                    },
                    {
                        "id": "call-web",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query":"Hermes Agent docs"}',
                        },
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-mcp",
                "content": '{"results":["record"]}',
            },
        ]

        activities = module.build_runtime_activity_timeline(messages)

        self.assertEqual(
            [activity["kind"] for activity in activities],
            ["reasoning", "tool", "reasoning", "tool", "tool", "tool"],
        )
        tools = [activity for activity in activities if activity["kind"] == "tool"]
        self.assertEqual(
            [tool["category"] for tool in tools],
            ["command", "mcp", "skill", "web"],
        )
        self.assertIn("systemctl status hermes", tools[0]["input"])
        self.assertIn("active", tools[0]["output"])
        self.assertEqual(tools[0]["status"], "completed")

    def test_runtime_activity_timeline_drops_reasoning_repeated_by_final_answer(self):
        module = load_module()
        activities = module.build_runtime_activity_timeline(
            [
                {
                    "role": "assistant",
                    "reasoning_content": "先检查服务，再整理结果。",
                    "timestamp": 10.0,
                },
                {
                    "role": "assistant",
                    "content": "服务已经恢复。",
                    "reasoning_content": "\n服务已经恢复。\n",
                    "timestamp": 12.0,
                },
                {
                    "role": "assistant",
                    "content": "本地电脑已经连接，处理器监控也已经恢复。",
                    "reasoning_content": "本地电脑已经连接，",
                    "timestamp": 14.0,
                },
            ]
        )

        self.assertEqual(
            [item["output"] for item in activities if item["kind"] == "reasoning"],
            ["先检查服务，再整理结果。"],
        )

    def test_provider_html_error_is_replaced_with_concise_chinese_status(self):
        module = load_module()
        raw_error = (
            "API call failed after 3 retries: HTTP 502: "
            "<html><head><title>502 Bad Gateway</title></head>"
            "<body><h1>502 Bad Gateway</h1><center>nginx</center></body></html>"
        )

        cleaned = module.sanitize_runtime_error(raw_error)

        self.assertEqual(
            cleaned,
            "模型服务暂时繁忙（HTTP 502），已保留当前进度。",
        )
        self.assertNotIn("<html>", cleaned.lower())
        self.assertNotIn("nginx", cleaned.lower())

    def test_profile_event_stream_uses_structured_json_and_keeps_tool_details(self):
        module = load_module()
        events = []
        response = module.consume_profile_event_stream(
            [
                "not a json log line\n",
                json.dumps(
                    {
                        "type": "tool.start",
                        "payload": {
                            "tool_id": "tool-1",
                            "name": "mcp__memory__search",
                            "args": {"query": "Hermes"},
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                json.dumps(
                    {
                        "type": "tool.complete",
                        "payload": {
                            "tool_id": "tool-1",
                            "name": "mcp__memory__search",
                            "result_text": "找到 2 条记录",
                            "duration_s": 1.25,
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                json.dumps(
                    {
                        "type": "message.complete",
                        "payload": {"text": "任务完成", "status": "completed"},
                    },
                    ensure_ascii=False,
                )
                + "\n",
            ],
            events.append,
        )

        self.assertEqual(response, "任务完成")
        self.assertEqual([event["type"] for event in events], [
            "tool.start",
            "tool.complete",
            "message.complete",
        ])
        self.assertEqual(events[1]["payload"]["duration_s"], 1.25)

    def test_reasoning_duration_uses_previous_message_as_model_start_boundary(self):
        module = load_module()
        activities = module.build_runtime_activity_timeline(
            [
                {"role": "user", "content": "check", "timestamp": 10.0},
                {
                    "role": "assistant",
                    "reasoning_content": "first pass",
                    "timestamp": 12.0,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {"name": "terminal", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "content": "ok",
                    "timestamp": 13.0,
                },
                {
                    "role": "assistant",
                    "reasoning_content": "second pass",
                    "timestamp": 15.0,
                },
            ]
        )

        reasoning = [item for item in activities if item["kind"] == "reasoning"]
        self.assertEqual(
            [
                (item["started_at"], item["ended_at"], item["duration_ms"])
                for item in reasoning
            ],
            [(10_000, 12_000, 2_000), (13_000, 15_000, 2_000)],
        )

    def test_reasoning_without_start_boundary_is_not_recorded_as_zero_ms(self):
        module = load_module()
        activities = module.build_runtime_activity_timeline(
            [
                {
                    "role": "assistant",
                    "reasoning_content": "restored thought",
                    "timestamp": 12.0,
                }
            ]
        )

        reasoning = activities[0]
        self.assertIsNone(reasoning["started_at"])
        self.assertEqual(reasoning["ended_at"], 12_000)
        self.assertNotIn("duration_ms", reasoning)

    def test_old_standalone_tool_messages_are_folded_into_assistant_activity(self):
        module = load_module()
        messages = [
            {"role": "user", "name": "user", "content": "检查服务"},
            {"role": "assistant", "name": "default", "content": "正在检查"},
            {
                "role": "tool",
                "name": "terminal",
                "content": '{"output":"active"}',
                "status": "completed",
            },
        ]

        normalized = module.normalize_stored_conversation_messages(messages)

        self.assertEqual([item["role"] for item in normalized], ["user", "assistant"])
        activities = normalized[-1]["meta"]["activities"]
        self.assertEqual(activities[0]["category"], "command")
        self.assertIn("active", activities[0]["output"])

    def test_attachment_names_are_confined_to_the_conversation_workspace(self):
        module = load_module()

        self.assertEqual(
            module.safe_attachment_name("../../季度汇报.pptx"),
            "季度汇报.pptx",
        )
        with self.assertRaises(ValueError):
            module.safe_attachment_name("..")

    def test_adopted_official_session_keeps_history_and_runtime_id(self):
        module = load_module()

        conversation = module.create_adopted_single_conversation(
            "default",
            "stored-session-1",
            "历史会话",
            [
                {
                    "role": "user",
                    "content": "继续之前的任务",
                    "timestamp": 123.5,
                },
                {
                    "role": "assistant",
                    "content": "之前的进度",
                    "timestamp": 124,
                },
            ],
        )

        self.assertEqual(
            conversation["runtime_sessions"]["default"],
            "stored-session-1",
        )
        self.assertEqual(
            [message["content"] for message in conversation["messages"]],
            ["继续之前的任务", "之前的进度"],
        )
        self.assertEqual(conversation["messages"][0]["created_at"], 123500)

    def test_adopted_official_session_keeps_more_than_two_hundred_messages(self):
        module = load_module()
        source = [
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"message-{index}",
                "timestamp": index + 1,
            }
            for index in range(250)
        ]

        conversation = module.create_adopted_single_conversation(
            "default",
            "stored-session-long",
            "完整历史",
            source,
        )

        self.assertEqual(len(conversation["messages"]), 250)
        self.assertEqual(conversation["messages"][0]["content"], "message-0")
        self.assertEqual(conversation["messages"][-1]["content"], "message-249")

    def test_deleting_adopted_conversation_removes_mapped_official_session(self):
        module = load_module()
        conversation = module.create_single_conversation("default", "待删除")
        conversation["runtime_sessions"] = {
            "default": "official-session-1",
            "worker": "official-session-2",
        }
        state = {"conversations": [conversation]}
        deleted = []
        saved = []
        module.load_single_state = lambda: state
        module.save_single_state = lambda value: saved.append(value)
        module._delete_runtime_session = (
            lambda profile, session_id: deleted.append((profile, session_id))
        )

        response = module.delete_single_conversation(conversation["id"])

        self.assertEqual(response, {"ok": True})
        self.assertEqual(
            deleted,
            [
                ("default", "official-session-1"),
                ("worker", "official-session-2"),
            ],
        )
        self.assertEqual(saved[-1]["conversations"], [])

    def test_single_turn_uses_official_profile_and_dashboard_source(self):
        module = load_module()
        captured = {}

        def runner(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="单聊回复", stderr="")

        response = module.run_single_turn(
            "default",
            "你好",
            runner=runner,
            hermes_bin="/usr/local/bin/hermes",
        )

        self.assertEqual(response, "单聊回复")
        self.assertEqual(
            captured["args"],
            [
                "/usr/local/bin/hermes",
                "-p",
                "default",
                "chat",
                "-Q",
                "-q",
                "你好",
                "--source",
                "dashboard-single",
                "--max-turns",
                "45",
            ],
        )
        self.assertFalse(captured["kwargs"]["shell"])

    def test_intent_router_separates_chat_from_work_and_selects_profiles(self):
        module = load_module()

        chat = module.classify_user_intent("你好，今天怎么样？")
        work = module.classify_user_intent(
            "帮我在本地 Windows 电脑检查项目、修改代码并运行测试"
        )

        self.assertEqual(chat["mode"], "chat")
        self.assertEqual(chat["label"], "简单任务")
        self.assertGreaterEqual(chat["confidence"], 0.8)
        self.assertEqual(chat["profiles"], ["default"])
        self.assertEqual(work["mode"], "work")
        self.assertEqual(work["label"], "群聊 + 工作流")
        self.assertGreaterEqual(work["confidence"], 0.8)
        self.assertIn("default", work["profiles"])
        self.assertIn("pc-worker", work["profiles"])
        self.assertIn("reviewer", work["profiles"])

    def test_worker_target_constraints_honor_only_and_negative_wording(self):
        module = load_module()

        pc_requests = (
            "On the local Windows WSL PC only. Do not run this worker step on DBB3.",
            "只在 WSL 本地电脑执行，不要在 DBB3 运行。",
            "请在 PC 完成，别在网关执行。",
        )
        for request in pc_requests:
            routed = module._rule_based_user_intent(
                f"Implement, test, and deploy this multi-step task. {request}"
            )
            self.assertEqual(
                routed["profiles"],
                ["default", "pc-worker", "reviewer"],
                request,
            )
            self.assertEqual(routed["targets"], ["pc"], request)
            self.assertIn("dbb3", routed["target_constraints"]["excluded"])

        dbb3_requests = (
            "Run this deployment on DBB3 only, not on the local PC.",
            "仅在 DBB3 执行，不在本地电脑运行。",
            "只用网关处理，别在 WSL 执行。",
        )
        for request in dbb3_requests:
            routed = module._rule_based_user_intent(
                f"Implement, test, and deploy this multi-step task. {request}"
            )
            self.assertEqual(
                routed["profiles"],
                ["default", "dbb3-worker", "reviewer"],
                request,
            )
            self.assertEqual(routed["targets"], ["dbb3"], request)
            self.assertIn("pc", routed["target_constraints"]["excluded"])

        for request in (
            "Do not run on DBB3 or the local PC.",
            "不要在 DBB3 执行，也不要在本地电脑或 WSL 执行。",
        ):
            workers, constraints = module._constrained_worker_profiles(request)
            self.assertEqual(workers, [], request)
            self.assertEqual(set(constraints["excluded"]), {"dbb3", "pc"})
            with self.assertRaises(module.HTTPException) as raised:
                module._hosted_route_parameters(
                    route_metadata={"mode": "work"},
                    content=request,
                    requested_mode="work",
                )
            self.assertEqual(raised.exception.status_code, 422)

    def test_explicit_worker_constraint_overrides_model_profiles(self):
        module = load_module()

        pc_only = module.classify_user_intent(
            "On the local Windows WSL PC only. Do not run this worker step on DBB3.",
            model_classifier=lambda _text: {
                "mode": "work",
                "confidence": 0.99,
                "profiles": ["dbb3-worker"],
                "targets": ["dbb3"],
                "artifact": {"decision": "none"},
            },
        )
        self.assertEqual(pc_only["profiles"], ["default", "pc-worker", "reviewer"])
        self.assertEqual(pc_only["targets"], ["pc"])

        dbb3_only = module.classify_user_intent(
            "仅在 DBB3 完成部署，不要在本地电脑或 WSL 执行。",
            model_classifier=lambda _text: {
                "mode": "work",
                "confidence": 0.99,
                "profiles": ["pc-worker"],
                "targets": ["pc"],
                "artifact": {"decision": "none"},
            },
        )
        self.assertEqual(
            dbb3_only["profiles"],
            ["default", "dbb3-worker", "reviewer"],
        )
        self.assertEqual(dbb3_only["targets"], ["dbb3"])

    def test_hosted_route_reapplies_worker_target_constraint(self):
        module = load_module()

        route, mode, profiles, artifact_required = module._hosted_route_parameters(
            route_metadata={
                "mode": "work",
                "profiles": ["default", "dbb3-worker", "reviewer"],
                "targets": ["dbb3"],
            },
            content="Only execute on the local PC; do not run on DBB3.",
            requested_mode="work",
            requested_profiles=["default", "dbb3-worker", "reviewer"],
        )

        self.assertEqual(mode, "work")
        self.assertEqual(profiles, ["default", "pc-worker", "reviewer"])
        self.assertEqual(route["profiles"], profiles)
        self.assertEqual(route["targets"], ["pc"])
        self.assertFalse(artifact_required)

    def test_hosted_chat_preserves_one_valid_selected_profile(self):
        module = load_module()
        module.available_profiles = lambda: [
            {"name": "default"},
            {"name": "reviewer"},
        ]

        route, mode, profiles, artifact_required = module._hosted_route_parameters(
            route_metadata={"mode": "chat", "profiles": ["reviewer"]},
            content="继续之前的审阅会话",
            requested_mode="chat",
            requested_profiles=["reviewer"],
        )

        self.assertEqual(mode, "chat")
        self.assertEqual(profiles, ["reviewer"])
        self.assertEqual(route["profiles"], ["reviewer"])
        self.assertFalse(artifact_required)

    def test_artifact_delivery_requires_an_explicit_file_deliverable(self):
        module = load_module()

        for request in (
            "帮我做一个季度汇报 PPT",
            "把分析结果导出成 PDF 给我下载",
            "生成一份 Excel 表格和 Word 文档",
            "请压缩成 zip 文件发给我",
            "Create and deliver a UTF-8 text file named result.txt",
        ):
            self.assertTrue(module.requires_artifact_delivery(request), request)

        for request in (
            "检查项目里的文件并运行测试",
            "分析日志，直接告诉我结论",
            "修改代码后汇报结果",
            "搜索网页并总结重点",
            "分析我上传的 PDF，只在会话里告诉我结论",
            "Inspect the uploaded file and summarize it in chat",
            "Do not create, upload, or deliver a file; report only in chat.",
            "不要创建、上传或交付文件，只在会话中汇报。",
        ):
            self.assertFalse(module.requires_artifact_delivery(request), request)

    def test_collaboration_execution_order_ends_with_single_reporter(self):
        module = load_module()

        ordered = module.collaboration_execution_order(
            ["default", "dbb3-worker", "reviewer"]
        )

        self.assertEqual(ordered, ["dbb3-worker", "reviewer", "default"])
        self.assertEqual(module.collaboration_role("dbb3-worker"), "worker")
        self.assertEqual(module.collaboration_role("reviewer"), "reviewer")
        self.assertEqual(module.collaboration_role("default"), "reporter")

    def test_ambiguous_intent_uses_model_classifier_and_keeps_rule_fallback(self):
        module = load_module()
        calls = []

        routed = module.classify_user_intent(
            "这件事你看着办",
            model_classifier=lambda text: calls.append(text) or {
                "mode": "work",
                "confidence": 0.86,
                "reason": "需要持续执行并交付结果。",
            },
        )

        self.assertEqual(calls, ["这件事你看着办"])
        self.assertEqual(routed["mode"], "work")
        self.assertEqual(routed["source"], "model")
        self.assertEqual(routed["label"], "群聊 + 工作流")

        fallback = module.classify_user_intent(
            "这件事你看着办",
            model_classifier=lambda _text: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        self.assertEqual(fallback["source"], "rules")

    def test_room_prompt_contains_recent_context_and_profile_role(self):
        module = load_module()
        room = module.create_room_record("协作室", ["default", "reviewer"])
        room["messages"] = [
            {"role": "user", "name": "用户", "content": "分析问题"},
            {"role": "assistant", "name": "default", "content": "初步分析"},
        ]

        prompt = module.build_group_prompt(room, "reviewer", "请继续复核")

        self.assertIn("你正在 Hermes 官方 WebUI 的多智能体群聊中", prompt)
        self.assertIn("当前身份：reviewer", prompt)
        self.assertIn("用户: 分析问题", prompt)
        self.assertIn("default: 初步分析", prompt)
        self.assertIn("请继续复核", prompt)

    def test_group_prompts_enforce_distinct_worker_reviewer_reporter_roles(self):
        module = load_module()
        room = module.create_room_record(
            "交付协作", ["default", "dbb3-worker", "reviewer"]
        )

        worker = module.build_group_prompt(
            room,
            "dbb3-worker",
            "检查服务并汇报",
            artifact_required=False,
        )
        reviewer = module.build_group_prompt(
            room,
            "reviewer",
            "检查服务并汇报",
            artifact_required=False,
        )
        reporter = module.build_group_prompt(
            room,
            "default",
            "检查服务并汇报",
            artifact_required=False,
        )

        self.assertIn("你是执行者", worker)
        self.assertIn("不要向用户做最终总结", worker)
        self.assertIn("不得创建或上传交付文件", worker)
        self.assertIn("你是审阅者", reviewer)
        self.assertIn("不要重复执行者的工作", reviewer)
        self.assertIn("不得创建或上传交付文件", reviewer)
        self.assertIn("你是唯一最终汇报者", reporter)
        self.assertIn("综合执行者和审阅者", reporter)

    def test_manifest_registers_one_official_collaboration_tab(self):
        manifest_path = MODULE_PATH.parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "collaboration")
        self.assertEqual(manifest["tab"]["path"], "/collaboration")
        self.assertTrue(manifest["tab"]["hidden"])
        self.assertEqual(manifest["api"], "plugin_api.py")
        self.assertIn("chat:top", manifest["slots"])
        version = manifest["version"]
        self.assertEqual(len(version.split(".")), 3)
        self.assertTrue(all(part.isdigit() for part in version.split(".")))
        self.assertEqual(manifest["entry"], f"dist/index.js?v={version}")
        self.assertEqual(manifest["css"], f"dist/style.css?v={version}")

    def test_dbb3_release_installer_uses_private_snapshot_and_health_files(self):
        installer = (
            MODULE_PATH.parents[3]
            / "deploy"
            / "dbb3"
            / "install-collaboration-release.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("health_file=\"$(mktemp", installer)
        self.assertIn(
            'release_snapshot="$(mktemp -d /run/hermes-collaboration-release.',
            installer,
        )
        self.assertIn(
            "/usr/bin/setpriv",
            installer,
        )
        self.assertIn(
            "--reuid=hermes --regid=hermes --init-groups --",
            installer,
        )
        self.assertIn(
            '${release_snapshot}/plugin/plugin_api.py',
            installer,
        )
        self.assertIn(
            'install -d -o root -g root -m 0755 "${web_target}/assets"',
            installer,
        )
        self.assertIn(
            'find "${web_target}/assets" -type f -exec chmod 0644 {} +',
            installer,
        )
        self.assertNotIn(
            'install -m 0755 "${stage}/plugin/plugin_api.py"',
            installer,
        )
        self.assertNotIn(">/tmp/hermes-dashboard-status.json", installer)

        sudoers = (
            MODULE_PATH.parents[3]
            / "deploy"
            / "dbb3"
            / "hermes-collaboration-deploy.sudoers"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            sudoers.strip(),
            "hermes ALL=(root) NOPASSWD: "
            "/usr/local/sbin/hermes-install-collaboration-release",
        )
        self.assertNotIn("NOPASSWD: ALL", sudoers)

    def test_frontend_exposes_unified_streaming_chat_and_workflow_router(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("Hermes Agent", bundle)
        self.assertIn("自动判断", bundle)
        self.assertIn("模型与工具", bundle)
        self.assertIn(
            'registry.registerSlot("collaboration", "chat:top", ChatTopSlot)',
            bundle,
        )
        self.assertIn("/api/plugins/collaboration", bundle)
        self.assertIn('collabApi("/rooms"', bundle)
        self.assertIn('collabApi("/single/conversations"', bundle)
        self.assertIn("submitBrowserEnqueue", bundle)
        self.assertIn('"/enqueue"', bundle)
        self.assertIn("hc-single-chat", bundle)
        self.assertIn('placeholder: "输入消息"', bundle)
        self.assertIn('SDK.buildWsUrl("/api/ws")', bundle)
        self.assertIn('"message.delta"', bundle)
        self.assertIn('"tool.start"', bundle)
        self.assertIn('"message.complete"', bundle)
        self.assertIn("/api/plugins/kanban", bundle)
        self.assertIn('kanbanApi("/tasks"', bundle)
        self.assertIn("/decompose", bundle)
        self.assertIn("sessionStorage", bundle)
        self.assertIn("recent_messages: messages.slice(-20)", bundle)
        self.assertIn("existingSessionId", bundle)
        self.assertIn("runtimeSessionsRef", bundle)
        self.assertIn("/runtime-session", bundle)
        self.assertIn('"session.resume"', bundle)
        self.assertIn("stored_session_id", bundle)
        self.assertIn("storedSessionId", bundle)
        self.assertIn("close_on_disconnect: false", bundle)
        self.assertNotIn('request("session.close"', bundle)
        self.assertIn("hermes:open-model-tools", bundle)
        self.assertIn("hermes:open-navigation", bundle)
        self.assertIn("hermes:new-unified-conversation", bundle)
        self.assertIn("hermes:resume-unified-session", bundle)
        self.assertIn("hermes.unified.pendingStoredSession", bundle)
        self.assertIn("pendingStoredSessionId", bundle)
        self.assertIn("/api/sessions/", bundle)
        self.assertIn("/single/conversations/adopt", bundle)
        self.assertIn('accept: "image/*,.pdf,.ppt,.pptx,.doc,.docx,.xls,.xlsx,.csv,.txt,.md,.zip"', bundle)
        self.assertNotIn("new FormData()", bundle)
        self.assertIn('"X-Filename": encodeURIComponent(file.name)', bundle)
        self.assertIn("body: file", bundle)
        self.assertIn("/attachments", bundle)
        self.assertIn("hc-attachment-list", bundle)
        self.assertIn("hc-attachment-preview-modal", bundle)
        self.assertIn('"预览"', bundle)
        self.assertIn('"下载"', bundle)
        self.assertIn("hc-nav-toggle", bundle)
        self.assertIn('src: "/hermes-official.png"', bundle)
        self.assertIn('className: "hc-official-avatar"', bundle)
        self.assertIn("selectConversation", bundle)
        self.assertIn("buildActivityTimeline", bundle)
        self.assertIn("mergeConversationIndex", bundle)
        self.assertIn("official_session_id", bundle)
        self.assertIn(
            '"/api/sessions?limit=50&offset=0&order=recent"',
            bundle,
        )
        self.assertIn("hc-activity-timeline", bundle)
        self.assertIn("hc-activity-card", bundle)
        self.assertNotIn('event.type === "thinking.delta"', bundle)
        self.assertIn('event.type === "tool.progress"', bundle)
        self.assertIn('event.type === "subagent.tool"', bundle)
        self.assertNotIn("hc-streaming-label", bundle)
        self.assertNotIn('if (message.kind === "route") return null;', bundle)
        self.assertIn("hc-route-event", bundle)
        self.assertIn('kind: "route"', bundle)
        self.assertIn('name: route.label', bundle)
        self.assertIn("const route = enqueued.route || {};", bundle)
        self.assertNotIn('await record(conversationId, routeMessage)', bundle)
        self.assertNotIn('className: "hc-header-profile"', bundle)

    def test_frontend_activity_duration_distinguishes_missing_data_from_zero(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )
        start = bundle.index("function formatActivityDuration(activity)")
        end = bundle.index("\n  function ActivityTimeline", start)
        function_source = bundle[start:end]
        script = (
            function_source
            + "\nconsole.log(JSON.stringify(["
            + "formatActivityDuration({kind:'reasoning',status:'completed',duration_ms:null}),"
            + "formatActivityDuration({kind:'reasoning',status:'completed',duration_ms:0}),"
            + "formatActivityDuration({kind:'reasoning',status:'completed',started_at:1000,ended_at:3000})"
            + "]));"
        )
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(
            json.loads(result.stdout),
            ["耗时未记录", "< 1 ms", "2.0 s"],
        )

    def test_frontend_removes_final_answer_prefix_from_reasoning_but_keeps_real_thoughts(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )
        start = bundle.index("function removeDuplicatedFinalReasoning(")
        end = bundle.index("\n  function buildActivityTimeline", start)
        function_source = bundle[start:end]
        script = (
            function_source
            + "\nconsole.log(JSON.stringify([removeDuplicatedFinalReasoning(["
            + "{id:'real',kind:'reasoning',output:'先检查服务'},"
            + "{id:'prefix',kind:'reasoning',output:'服务已经恢复，'},"
            + "{id:'duplicate',kind:'reasoning',output:'  服务已恢复  '},"
            + "{id:'tool',kind:'tool',output:'服务已恢复'}"
            + "], '\\n服务已恢复\\n').map((item) => item.id),"
            + "removeDuplicatedFinalReasoning(["
            + "{id:'prefix',kind:'reasoning',output:'本地电脑已经连接，'},"
            + "{id:'tool',kind:'tool',output:'ok'}"
            + "], '本地电脑已经连接，处理器监控也已经恢复。').map((item) => item.id)"
            + "]));"
        )
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(
            json.loads(result.stdout),
            [["real", "prefix", "tool"], ["tool"]],
        )
        self.assertIn(
            "removeDuplicatedFinalReasoning(activities, source?.content)",
            bundle,
        )

    def test_frontend_sanitizes_stream_error_and_preserves_partial_output(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )
        start = bundle.index("function sanitizeClientError(error)")
        end = bundle.index("\n\n  const collabApi", start)
        helper_source = bundle[start:end]
        script = (
            helper_source
            + "\nconsole.log(JSON.stringify(["
            + "finalizeStreamText({status:'error',text:'HTTP 502: <html><h1>Bad Gateway</h1></html>'}, '已经完成初步检查。'),"
            + "finalizeStreamText({status:'completed',text:'正常结果'}, '流式片段')"
            + "]));"
        )
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [
                "已经完成初步检查。\n\n本阶段未完成：网络或模型服务短暂波动，DBB3 上的任务仍在继续。",
                "正常结果",
            ],
        )
        self.assertIn(
            "const finalText = finalizeStreamText(finalPayload, accumulatedText);",
            bundle,
        )

    def test_frontend_realtime_reasoning_uses_model_phase_start_boundary(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("let modelPhaseStartedAt = turnStartedAt;", bundle)
        self.assertIn("started_at: modelPhaseStartedAt || Date.now(),", bundle)
        self.assertIn("modelPhaseStartedAt = endedAt;", bundle)

    def test_web_chat_uses_one_persistent_atomic_enqueue(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )
        send_start = bundle.index("const send = async () =>")
        send_end = bundle.index("\n    return h(", send_start)
        send_source = bundle[send_start:send_end]

        self.assertIn("BROWSER_ENQUEUE_OUTBOX_PREFIX", bundle)
        self.assertIn("saveBrowserEnqueue(conversationId, enqueuePayload);", send_source)
        self.assertIn("submitBrowserEnqueue(", send_source)
        self.assertIn("currentRequestAccepted = true;", send_source)
        self.assertIn("if (currentRequestAccepted)", send_source)
        self.assertIn('"/enqueue"', bundle)
        self.assertNotIn("await record(conversationId, userMessage)", send_source)
        self.assertNotIn('collabApi("/route"', send_source)
        self.assertNotIn('"/hosted-turns"', send_source)
        self.assertLess(
            send_source.index("saveBrowserEnqueue(conversationId, enqueuePayload);"),
            send_source.index("const enqueued = await submitBrowserEnqueue("),
        )

    def test_browser_room_recovers_pending_send_and_refreshes_hosted_messages(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )
        room_start = bundle.index("function RoomView({ roomId, onBack })")
        room_end = bundle.index("\n  function GroupMode()", room_start)
        room_source = bundle[room_start:room_end]

        self.assertIn("BROWSER_ROOM_OUTBOX_PREFIX", bundle)
        self.assertIn("saveBrowserRoomRequest(roomId, roomRequest);", room_source)
        self.assertIn("submitBrowserRoomRequest(roomId, roomRequest)", room_source)
        self.assertIn("loadBrowserRoomRequest(roomId)", room_source)
        self.assertIn("roomRunning", room_source)
        self.assertIn("currentRequestAccepted = true;", room_source)
        self.assertIn("if (currentRequestAccepted)", room_source)
        self.assertIn("timer = setTimeout(refresh, 900);", room_source)
        self.assertIn('document.addEventListener("visibilitychange"', room_source)
        self.assertLess(
            room_source.index("saveBrowserRoomRequest(roomId, roomRequest);"),
            room_source.index("await submitBrowserRoomRequest(roomId, roomRequest);"),
        )

    def test_hosted_event_reducer_ignores_spinner_text_but_keeps_real_reasoning(self):
        module = load_module()
        state = {"content": "", "status": "streaming", "activities": []}

        module.apply_profile_event(
            state,
            {"type": "thinking.delta", "payload": {"text": "reflecting..."}},
        )
        module.apply_profile_event(
            state,
            {"type": "reasoning.delta", "payload": {"text": "正在检查服务。"}},
        )

        reasoning = [
            item for item in state["activities"] if item["kind"] == "reasoning"
        ]
        self.assertEqual([item["output"] for item in reasoning], ["正在检查服务。"])

    def test_tool_generating_does_not_create_a_duplicate_running_activity(self):
        module = load_module()
        state = {"content": "", "status": "streaming", "activities": []}

        module.apply_profile_event(
            state,
            {"type": "tool.generating", "payload": {"name": "terminal"}},
        )
        module.apply_profile_event(
            state,
            {
                "type": "tool.start",
                "payload": {
                    "tool_id": "call-terminal-1",
                    "name": "terminal",
                    "args": {"command": "hostname"},
                },
            },
        )
        module.apply_profile_event(
            state,
            {
                "type": "tool.complete",
                "payload": {
                    "tool_id": "call-terminal-1",
                    "name": "terminal",
                    "result_text": "ok",
                },
            },
        )

        tools = [item for item in state["activities"] if item["kind"] == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["id"], "call-terminal-1")
        self.assertEqual(tools[0]["status"], "completed")

        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('if (!matched && event.type !== "tool.generating")', bundle)

    def test_backend_hosted_workflow_runs_roles_serially_and_publishes_one_final_report(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None
        self.assertTrue(hasattr(module, "create_hosted_turn_record"))
        module.create_hosted_turn_record(
            conversation,
            turn_id="turn-hosted-1",
            content="检查服务并修复问题",
            title="检查并修复服务",
            profiles=["default", "dbb3-worker", "reviewer"],
            artifact_required=False,
        )
        calls = []
        notifications = []

        def capture_notification(*args):
            persisted = conversation["hosted_turns"]["turn-hosted-1"].get(
                "notification"
            )
            self.assertIsInstance(persisted, dict)
            self.assertEqual(persisted["state"], "queued")
            notifications.append(args)

        module._schedule_mobile_completion_notification = capture_notification

        def runner(profile, prompt):
            calls.append((profile, prompt))
            return {
                "dbb3-worker": "执行完成，服务已恢复",
                "reviewer": "审阅通过，证据完整",
                "default": "最终汇报：任务完成",
            }[profile]

        module.execute_hosted_workflow(
            conversation["id"],
            "turn-hosted-1",
            runner=runner,
            task_creator=lambda **_kwargs: {
                "task_id": "root-1",
                "child_ids": ["child-1"],
                "fanout": True,
            },
        )

        self.assertEqual([profile for profile, _prompt in calls], [
            "dbb3-worker",
            "reviewer",
            "default",
        ])
        assistant_messages = [
            message
            for message in conversation["messages"]
            if message.get("role") == "assistant"
        ]
        self.assertEqual(
            sum(bool(message.get("meta", {}).get("final_report")) for message in assistant_messages),
            1,
        )
        self.assertEqual(assistant_messages[-1]["content"], "最终汇报：任务完成")
        self.assertEqual(
            conversation["hosted_turns"]["turn-hosted-1"]["status"],
            "completed",
        )
        notification = conversation["hosted_turns"]["turn-hosted-1"]["notification"]
        self.assertEqual(notification["state"], "queued")
        self.assertEqual(notification["task_status"], "completed")
        self.assertTrue(notification["collapse_id"].startswith("hermes-turn-"))
        self.assertEqual(
            notifications,
            [(
                conversation["id"],
                "turn-hosted-1",
                "completed",
                "最终汇报：任务完成",
            )],
        )
        worker_prompt = calls[0][1]
        reviewer_prompt = calls[1][1]
        reporter_prompt = calls[2][1]

        self.assertIn("可以使用所有已配置的 Skill、MCP 和工具", worker_prompt)
        self.assertIn("可以读取根任务和已分配工作项", worker_prompt)
        self.assertIn("可以向已分配工作项写入进度、证据和交接评论", worker_prompt)
        self.assertNotIn("不要主动查询或修改 Kanban 内部状态", worker_prompt)

        self.assertIn("独立抽样复核", reviewer_prompt)
        self.assertIn("正常的 Skill、MCP、命令和取证调用不属于过度执行", reviewer_prompt)
        self.assertNotIn("不要主动查询或修改 Kanban 内部状态", reviewer_prompt)
        self.assertIn("不得创建、改派、关闭或删除根任务", reporter_prompt)

    def test_notification_delivery_progress_and_terminal_state_are_persisted(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        conversation["owner_id"] = "owner-a"
        run = module.create_hosted_turn_record(
            conversation,
            turn_id="turn-notification",
            content="finish",
            title="finish",
            profiles=["default"],
            artifact_required=False,
        )
        run.update(
            {
                "status": "completed",
                "notification": module._completion_notification_record(
                    conversation["id"],
                    "turn-notification",
                    "completed",
                    "finished",
                ),
            }
        )
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None
        observed = {}

        def deliver(**kwargs):
            observed.update(kwargs)
            deliveries = {
                "registration-hash": {
                    "state": "delivered",
                    "attempts": 1,
                    "last_error": "",
                    "updated_at": 1234,
                }
            }
            kwargs["progress_callback"](deliveries)
            return {"state": "delivered", "deliveries": deliveries, "error": ""}

        with patch(
            "hermes_cli.dashboard_auth.mobile_notifications.deliver_task_completion_push",
            side_effect=deliver,
        ):
            delay = module._deliver_persisted_completion_notification(
                conversation["id"],
                "turn-notification",
            )

        self.assertIsNone(delay)
        persisted = run["notification"]
        self.assertEqual(persisted["state"], "delivered")
        self.assertEqual(persisted["attempts"], 1)
        self.assertIn("completed_at", persisted)
        self.assertEqual(
            persisted["deliveries"]["registration-hash"]["state"],
            "delivered",
        )
        self.assertEqual(observed["collapse_id"], persisted["collapse_id"])

    def test_startup_replays_a_persisted_terminal_notification_outbox(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        run = module.create_hosted_turn_record(
            conversation,
            turn_id="turn-notification-replay",
            content="finish",
            title="finish",
            profiles=["default"],
            artifact_required=False,
        )
        run.update(
            {
                "status": "completed",
                "notification": {
                    **module._completion_notification_record(
                        conversation["id"],
                        "turn-notification-replay",
                        "completed",
                        "finished",
                    ),
                    "state": "retry",
                },
            }
        )
        hosted_starts = []
        notification_starts = []
        module.start_hosted_workflow = lambda *args: hosted_starts.append(args)
        module._schedule_mobile_completion_notification = (
            lambda *args: notification_starts.append(args)
        )

        module.resume_unfinished_hosted_workflows([conversation])

        self.assertEqual(hosted_starts, [])
        self.assertEqual(
            notification_starts,
            [(
                conversation["id"],
                "turn-notification-replay",
                "completed",
                "finished",
            )],
        )

    def test_persistent_notifications_share_one_process_dispatcher(self):
        module = load_module()
        conversations = []
        for index in range(2):
            conversation = module.create_single_conversation("default")
            conversation["owner_id"] = "owner-a"
            run = module.create_hosted_turn_record(
                conversation,
                turn_id=f"turn-dispatch-{index}",
                content="finish",
                title="finish",
                profiles=["default"],
                artifact_required=False,
            )
            run.update(
                {
                    "status": "completed",
                    "notification": module._completion_notification_record(
                        conversation["id"],
                        f"turn-dispatch-{index}",
                        "completed",
                        "finished",
                    ),
                }
            )
            conversations.append(conversation)
        state = {"conversations": conversations}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None
        created_threads = []

        class FakeThread:
            def __init__(self, *, target, name, daemon):
                self.target = target
                self.name = name
                self.daemon = daemon
                self.started = False
                created_threads.append(self)

            def start(self):
                self.started = True

            def is_alive(self):
                return self.started

        module._MOBILE_NOTIFICATION_DISPATCH_THREAD = None
        module._MOBILE_NOTIFICATION_PENDING.clear()
        try:
            with patch.object(module.threading, "Thread", FakeThread):
                for index, conversation in enumerate(conversations):
                    module._schedule_mobile_completion_notification(
                        conversation["id"],
                        f"turn-dispatch-{index}",
                        "completed",
                        "finished",
                    )

            self.assertEqual(len(created_threads), 1)
            self.assertEqual(created_threads[0].name, "hermes-apns-dispatcher")
            self.assertTrue(created_threads[0].daemon)
            self.assertEqual(len(module._MOBILE_NOTIFICATION_PENDING), 2)
        finally:
            module._MOBILE_NOTIFICATION_DISPATCH_THREAD = None
            module._MOBILE_NOTIFICATION_PENDING.clear()

    def test_hosted_workflow_consumer_preserves_durable_turn_order(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        conversation["owner_id"] = "owner-a"
        newer = module.create_hosted_turn_record(
            conversation,
            turn_id="turn-newer",
            content="newer",
            title="newer",
            profiles=["default"],
            artifact_required=False,
        )
        older = module.create_hosted_turn_record(
            conversation,
            turn_id="turn-older",
            content="older",
            title="older",
            profiles=["default"],
            artifact_required=False,
        )
        newer["created_at"] = 200
        older["created_at"] = 100
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        observed = []

        def execute(conversation_id, turn_id):
            observed.append((conversation_id, turn_id))
            conversation["hosted_turns"][turn_id]["status"] = "completed"

        module.execute_hosted_workflow = execute
        thread = module.start_hosted_workflow(conversation["id"], "turn-newer")
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(
            observed,
            [
                (conversation["id"], "turn-older"),
                (conversation["id"], "turn-newer"),
            ],
        )

    def test_hosted_workflow_consumer_exits_after_conversation_deletion(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state

        state["conversations"] = []
        thread = module.start_hosted_workflow(conversation["id"], "turn-deleted")
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertNotIn(conversation["id"], module._HOSTED_THREADS)

    def test_conversation_index_compacts_hosted_role_event_payloads(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        conversation["hosted_turns"] = {
            "turn-heavy": {
                "turn_id": "turn-heavy",
                "status": "running",
                "stage": "worker",
                "started_at": 1000,
                "updated_at": 2000,
                "task_id": "root-heavy",
                "worker_result": "x" * 20_000,
                "role_events": {
                    "worker": {
                        "activities": [{"result": "y" * 20_000}],
                    },
                },
            },
        }
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None
        module.reconcile_conversation_runtime_results = lambda _conversation: False
        module.compact_conversation_title = lambda _conversation: False
        module.resume_unfinished_hosted_workflows = lambda _conversations: None

        response = module.get_single_conversations()

        summary = response["conversations"][0]
        hosted = summary["hosted_turns"]["turn-heavy"]
        self.assertEqual(hosted["status"], "running")
        self.assertEqual(hosted["stage"], "worker")
        self.assertEqual(hosted["task_id"], "root-heavy")
        self.assertNotIn("worker_result", hosted)
        self.assertNotIn("role_events", hosted)
        self.assertIn("role_events", conversation["hosted_turns"]["turn-heavy"])

    def test_hosted_roles_run_with_non_root_kanban_task_scopes(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None
        module.create_hosted_turn_record(
            conversation,
            turn_id="turn-scoped",
            content="检查设备并验收",
            title="检查设备",
            profiles=["default", "pc-worker", "reviewer"],
            artifact_required=False,
        )
        calls = []

        def runner(profile, _prompt, **kwargs):
            calls.append((profile, kwargs.get("kanban_task_id")))
            return f"{profile} 完成"

        module.execute_hosted_workflow(
            conversation["id"],
            "turn-scoped",
            runner=runner,
            task_creator=lambda **_kwargs: {
                "task_id": "root-scoped",
                "child_ids": ["child-worker", "child-reviewer"],
                "fanout": True,
            },
        )

        self.assertEqual(calls[0], ("pc-worker", "child-worker"))
        self.assertEqual(calls[1], ("reviewer", "child-reviewer"))
        self.assertTrue(calls[2][1].startswith("hosted-reporter-"))
        self.assertNotIn("root-scoped", [scope for _profile, scope in calls])

    def test_hosted_roles_persist_separate_live_messages_with_nested_activities(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None
        module.create_hosted_turn_record(
            conversation,
            turn_id="turn-live-roles",
            content="检查两个设备并汇报",
            title="检查两个设备",
            profiles=["default", "pc-worker", "reviewer"],
            artifact_required=False,
        )
        observed_running_messages = []

        def runner(profile, _prompt, *, event_callback=None):
            event_callback(
                {
                    "type": "session.info",
                    "payload": {"provider": "hubway", "model": "gpt-5.6-sol"},
                }
            )
            event_callback(
                {
                    "type": "reasoning.delta",
                    "payload": {"text": f"{profile} 正在分析。"},
                }
            )
            event_callback(
                {
                    "type": "tool.start",
                    "payload": {
                        "tool_id": f"tool-{profile}",
                        "name": "terminal",
                        "args": {"command": "hostname"},
                        "started_at": 1000,
                    },
                }
            )
            event_callback(
                {
                    "type": "message.delta",
                    "payload": {"text": f"{profile} 已取得第一条结果。"},
                }
            )
            role_message = next(
                message
                for message in conversation["messages"]
                if message.get("meta", {}).get("runtime_turn_id") == "turn-live-roles"
                and message.get("name") == profile
                and message.get("status") == "streaming"
            )
            observed_running_messages.append(role_message["content"])
            event_callback(
                {
                    "type": "tool.complete",
                    "payload": {
                        "tool_id": f"tool-{profile}",
                        "name": "terminal",
                        "result_text": "dbb3-hermes",
                        "duration_s": 0.42,
                    },
                }
            )
            return f"{profile} 阶段完成"

        module.execute_hosted_workflow(
            conversation["id"],
            "turn-live-roles",
            runner=runner,
            task_creator=lambda **_kwargs: {
                "task_id": "root-live",
                "child_ids": ["child-live"],
                "fanout": True,
            },
        )

        role_messages = [
            message
            for message in conversation["messages"]
            if message.get("meta", {}).get("runtime_turn_id") == "turn-live-roles"
            and message.get("meta", {}).get("role_stage")
            in {"dispatch", "worker", "reviewer", "reporter"}
        ]
        self.assertEqual(
            [message["meta"]["role_stage"] for message in role_messages],
            ["dispatch", "worker", "reviewer", "reporter"],
        )
        self.assertEqual(len(observed_running_messages), 3)
        for message in role_messages[1:]:
            self.assertTrue(message["meta"]["collapse_activities"])
            self.assertEqual(message["meta"]["actual_model"], "gpt-5.6-sol")
            self.assertEqual(message["meta"]["activities"][1]["category"], "command")
            self.assertEqual(message["meta"]["activities"][1]["duration_ms"], 420)

    def test_hosted_workflow_retries_transient_502_without_losing_partial_progress(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None
        module.create_hosted_turn_record(
            conversation,
            turn_id="turn-retry-502",
            content="检查服务",
            title="检查服务",
            profiles=["default", "dbb3-worker", "reviewer"],
            artifact_required=False,
        )
        worker_attempts = 0

        def runner(profile, _prompt, *, event_callback=None):
            nonlocal worker_attempts
            if profile == "dbb3-worker":
                worker_attempts += 1
                if worker_attempts == 1:
                    event_callback(
                        {
                            "type": "reasoning.delta",
                            "payload": {"text": "已经完成初步检查。"},
                        }
                    )
                    raise RuntimeError(
                        "HTTP 502: <html><body><h1>Bad Gateway</h1></body></html>"
                    )
                event_callback(
                    {
                        "type": "message.delta",
                        "payload": {"text": "重试后服务恢复。"},
                    }
                )
                return "执行恢复完成"
            return "审阅通过" if profile == "reviewer" else "最终汇报完成"

        module.execute_hosted_workflow(
            conversation["id"],
            "turn-retry-502",
            runner=runner,
            task_creator=lambda **_kwargs: {
                "task_id": "root-retry",
                "child_ids": [],
                "fanout": False,
            },
        )

        worker_message = next(
            message
            for message in conversation["messages"]
            if message.get("meta", {}).get("role_stage") == "worker"
        )
        self.assertEqual(worker_attempts, 2)
        self.assertEqual(worker_message["content"], "执行恢复完成")
        self.assertNotIn("<html>", json.dumps(worker_message, ensure_ascii=False).lower())
        self.assertTrue(
            any(
                activity.get("kind") == "reasoning"
                and "初步检查" in activity.get("output", "")
                for activity in worker_message["meta"]["activities"]
            )
        )

    def test_frontend_submits_chat_and_work_with_file_ids_to_server_hosting(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )
        send_start = bundle.index("const send = async () =>")
        send_end = bundle.index("return h(", send_start)
        workflow = bundle[send_start:send_end]

        self.assertIn("submitBrowserEnqueue(", workflow)
        self.assertIn("request_id: requestId", workflow)
        self.assertIn("turn_id: hostedTurnId", workflow)
        self.assertIn("attachment_ids: attachmentIds", workflow)
        self.assertIn("recent_messages: messages.slice(-20)", workflow)
        self.assertIn("saveBrowserEnqueue(conversationId, enqueuePayload)", workflow)
        self.assertIn("setHostedRunning(true)", workflow)
        self.assertNotIn("await runProfile(", workflow)

    def test_unfinished_hosted_turn_is_resumed_during_dashboard_startup(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        run = module.create_hosted_turn_record(
            conversation,
            turn_id="turn-resume-1",
            content="继续后台任务",
            title="继续后台任务",
            profiles=["default", "dbb3-worker", "reviewer"],
            artifact_required=False,
        )
        run.update({"status": "running", "stage": "reviewer"})
        started = []
        module.start_hosted_workflow = (
            lambda conversation_id, turn_id: started.append((conversation_id, turn_id))
        )
        module.load_single_state = lambda: {"conversations": [conversation]}

        async def run_lifespan():
            async with module.collaboration_dashboard_lifespan(None):
                pass

        asyncio.run(run_lifespan())

        self.assertEqual(started, [(conversation["id"], "turn-resume-1")])
        run["status"] = "completed"
        asyncio.run(run_lifespan())
        self.assertEqual(len(started), 1)

    def test_hosted_workflow_only_publishes_outputs_for_explicit_artifact_tasks(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None
        attachment_lookups = []

        def list_attachments(conversation_id, turn_id, _started_at):
            attachment_lookups.append(conversation_id)
            return [
                {
                    "id": "output-1",
                    "bucket": "outputs",
                    "name": "result.pptx",
                    "turn_id": turn_id,
                }
            ]

        module._hosted_turn_output_attachments = list_attachments
        task_creator = lambda **kwargs: {
            "task_id": f"root-{kwargs['turn_id']}",
            "child_ids": [],
            "fanout": False,
        }
        prompts = []

        def runner(profile, prompt):
            prompts.append((profile, prompt))
            return f"{profile} 完成"

        module.create_hosted_turn_record(
            conversation,
            turn_id="turn-no-file",
            content="检查服务状态",
            title="检查服务状态",
            profiles=["default", "dbb3-worker", "reviewer"],
            artifact_required=False,
        )
        module.execute_hosted_workflow(
            conversation["id"],
            "turn-no-file",
            runner=runner,
            task_creator=task_creator,
        )

        self.assertEqual(attachment_lookups, [])
        self.assertTrue(
            any("不要创建、复制或上传文件" in prompt for _profile, prompt in prompts)
        )

        module.create_hosted_turn_record(
            conversation,
            turn_id="turn-with-file",
            content="制作并交付 PPT",
            title="制作 PPT",
            profiles=["default", "dbb3-worker", "reviewer"],
            artifact_required=True,
            delivery_context="请将 PPT 放入会话输出目录。",
        )
        module.execute_hosted_workflow(
            conversation["id"],
            "turn-with-file",
            runner=runner,
            task_creator=task_creator,
        )

        self.assertEqual(attachment_lookups, [conversation["id"]])
        final_message = next(
            message
            for message in reversed(conversation["messages"])
            if message.get("meta", {}).get("runtime_turn_id") == "turn-with-file"
            and message.get("meta", {}).get("final_report")
        )
        self.assertEqual(
            [item["name"] for item in final_message["meta"]["attachments"]],
            ["result.pptx"],
        )

        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("route.artifact_required", bundle)
        self.assertIn("artifact_required: artifactRequired", bundle)

    def test_hosted_workflow_stops_before_the_next_role_when_cancelled(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None
        module.create_hosted_turn_record(
            conversation,
            turn_id="turn-cancel-1",
            content="执行后续检查",
            title="执行后续检查",
            profiles=["default", "dbb3-worker", "reviewer"],
            artifact_required=False,
        )
        calls = []

        def runner(profile, _prompt):
            calls.append(profile)
            module.request_hosted_turn_cancellation(
                conversation["id"],
                "turn-cancel-1",
                reason="用户取消",
            )
            return "执行者已停止"

        module.execute_hosted_workflow(
            conversation["id"],
            "turn-cancel-1",
            runner=runner,
            task_creator=lambda **_kwargs: {
                "task_id": "root-cancel-1",
                "child_ids": [],
                "fanout": False,
            },
        )

        run = conversation["hosted_turns"]["turn-cancel-1"]
        self.assertEqual(calls, ["dbb3-worker"])
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(run["stage"], "cancelled")
        self.assertEqual(
            sum(
                bool(message.get("meta", {}).get("final_report"))
                for message in conversation["messages"]
            ),
            1,
        )

    def test_frontend_does_not_label_plain_chat_as_a_final_report(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('message.meta?.role_stage || "chat"', bundle)
        self.assertIn('roleStage !== "chat"', bundle)

    def test_frontend_keeps_latest_message_above_the_ios_composer(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )
        stylesheet = (MODULE_PATH.parent / "dist" / "style.css").read_text(
            encoding="utf-8"
        )

        self.assertIn("measureComposerOverlap", bundle)
        self.assertIn("hermes:viewport-change", bundle)
        self.assertIn("--hc-composer-overlap", bundle)
        self.assertIn("hc-stream-end", bundle)
        self.assertIn("hc-role-activity-group", bundle)
        self.assertIn("var(--hc-composer-overlap, 0px)", stylesheet)
        self.assertIn(
            'html[data-hermes-keyboard="open"] .hc-single-composer',
            stylesheet,
        )
        self.assertIn("padding-bottom: 3px !important", stylesheet)

    def test_model_tools_only_keeps_new_chat_model_and_event_status(self):
        chat_page = (
            MODULE_PATH.parents[3] / "web" / "src" / "pages" / "ChatPage.tsx"
        ).read_text(encoding="utf-8")
        chat_sidebar = (
            MODULE_PATH.parents[3]
            / "web"
            / "src"
            / "components"
            / "ChatSidebar.tsx"
        ).read_text(encoding="utf-8")

        self.assertNotIn("ChatSessionList", chat_page)
        self.assertIn("新建对话", chat_sidebar)
        self.assertIn("工具事件流", chat_sidebar)
        self.assertNotIn("重新连接工具事件流", chat_sidebar)

    def test_model_switch_rebinds_the_next_turn_without_replacing_web_history(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )
        chat_sidebar = (
            MODULE_PATH.parents[3]
            / "web"
            / "src"
            / "components"
            / "ChatSidebar.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn('new CustomEvent("hermes:model-changed"', chat_sidebar)
        self.assertGreaterEqual(chat_sidebar.count('new CustomEvent("hermes:model-changed"'), 2)
        self.assertIn('profile: profile || "default"', chat_sidebar)
        self.assertNotIn("setPendingReloadModel", chat_sidebar)
        self.assertNotIn("执行 /new 或刷新页面后应用到当前对话", chat_sidebar)
        self.assertIn('window.addEventListener("hermes:model-changed"', bundle)
        self.assertIn("delete nextRuntimeSessions[changedProfile]", bundle)
        self.assertIn('session_id: ""', bundle)
        self.assertIn('event.type === "session.info"', bundle)
        self.assertIn("actual_model", bundle)
        self.assertIn("actual_provider", bundle)
        self.assertIn("hc-runtime-model", bundle)

    def test_model_picker_is_chinese_and_uses_a_single_column_on_iphone(self):
        picker = (
            MODULE_PATH.parents[3]
            / "web"
            / "src"
            / "components"
            / "ModelPickerDialog.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn('import { useI18n } from "@/i18n"', picker)
        self.assertIn('title: "切换模型"', picker)
        self.assertIn('filter: "筛选提供方和模型…"', picker)
        self.assertIn('refresh: "刷新模型"', picker)
        self.assertIn('switchModel: "切换"', picker)
        self.assertIn("grid-rows-[minmax(110px,0.7fr)_minmax(160px,1.3fr)]", picker)
        self.assertIn("sm:grid-cols-[200px_1fr]", picker)
        self.assertIn("max-h-[calc(var(--hermes-viewport-height,100dvh)-1rem)]", picker)

    def test_unified_sidebar_merges_official_sessions_before_restoring_selection(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )
        load_index = bundle.index("const loadIndex = useCallback")
        merge_index = bundle.index(
            "nextConversations = mergeConversationIndex(", load_index
        )
        remembered_index = bundle.index(
            "const rememberedId = loadRememberedConversationId()", load_index
        )

        self.assertLess(merge_index, remembered_index)

    def test_stale_background_refresh_cannot_overwrite_a_new_conversation(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('const activeConversationRef = useRef("");', bundle)
        self.assertIn("const conversationLoadSequenceRef = useRef(0);", bundle)
        self.assertIn(
            "const loadSequence = ++conversationLoadSequenceRef.current;",
            bundle,
        )
        self.assertIn(
            "conversationId !== activeConversationRef.current ||",
            bundle,
        )
        self.assertIn(
            "loadSequence !== conversationLoadSequenceRef.current",
            bundle,
        )
        create_start = bundle.index("const createConversation = useCallback")
        create_end = bundle.index("const selectConversation = useCallback", create_start)
        create_source = bundle[create_start:create_end]
        self.assertIn(
            "activeConversationRef.current = data.conversation.id;",
            create_source,
        )
        self.assertIn("conversationLoadSequenceRef.current += 1;", create_source)

    def test_mobile_header_hides_route_picker_and_main_nav_hides_files(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )
        app_source = (
            MODULE_PATH.parents[3] / "web" / "src" / "App.tsx"
        ).read_text(encoding="utf-8")

        self.assertNotIn('className: "hc-route-select"', bundle)
        self.assertIn('"/enqueue"', bundle)
        self.assertNotIn('{ path: "/files",', app_source)

    def test_frontend_recovers_transient_stream_disconnects_without_resubmitting(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("STREAM_RECONNECT_MAX_ATTEMPTS = 12", bundle)
        self.assertIn("STREAM_CONNECT_TIMEOUT_MS = 12000", bundle)
        self.assertIn("scheduleReconnect", bundle)
        self.assertIn('type: "connection.reconnecting"', bundle)
        self.assertIn('type: "connection.restored"', bundle)
        self.assertIn("if (submitted)", bundle)
        self.assertIn('request(activeSocket, connectionPending, "session.resume"', bundle)
        self.assertIn("const submission = request(", bundle)
        self.assertIn("submitted = true", bundle)
        self.assertIn('type: "session.ready"', bundle)
        self.assertIn("await onEvent", bundle)
        self.assertIn('status: "running"', bundle)
        self.assertIn("runtime_session_id", bundle)
        self.assertGreaterEqual(bundle.count("runtime_turn_id: streamId"), 2)
        self.assertIn("error.submitted = submitted", bundle)
        self.assertIn("err.submitted && err.stored_session_id", bundle)
        self.assertIn("hostedRunning", bundle)
        self.assertIn("representedTurnIds", bundle)
        self.assertIn("DBB3 服务端持续执行", bundle)
        self.assertIn("任务已由 DBB3 托管", bundle)
        self.assertIn("new EventSource", bundle)
        self.assertNotIn("setInterval(refreshNow, 3000)", bundle)
        self.assertIn("latestAssistantText", bundle)
        self.assertIn("hc-connection-state", bundle)
        self.assertNotIn("reject(new Error(`${profile} 流式连接失败`))", bundle)

    def test_frontend_recovery_never_reuses_an_answer_before_the_current_turn(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("function latestAssistantTextAfter(", bundle)
        start = bundle.index("function latestAssistantTextAfter(")
        end = bundle.index("\n  async function streamProfileTurn", start)
        function_source = bundle[start:end]
        script = (
            function_source
            + "\nconsole.log(JSON.stringify(["
            + "latestAssistantTextAfter(["
            + "{role:'user',content:'旧问题'},"
            + "{role:'assistant',content:'旧回答'},"
            + "{role:'user',content:'当前问题'}"
            + "],2),"
            + "latestAssistantTextAfter(["
            + "{role:'user',content:'旧问题'},"
            + "{role:'assistant',content:'旧回答'},"
            + "{role:'user',content:'当前问题'},"
            + "{role:'assistant',content:'当前回答'}"
            + "],2)"
            + "]));"
        )
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(json.loads(result.stdout), ["", "当前回答"])

    def test_frontend_pauses_retries_offline_and_wakes_after_ios_resume(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("STREAM_RECONNECT_MAX_ATTEMPTS = 12", bundle)
        self.assertIn("navigator.onLine === false", bundle)
        self.assertIn('type: "connection.waiting"', bundle)
        self.assertIn('window.addEventListener("offline", handleOffline)', bundle)
        self.assertIn('window.addEventListener("online", handleOnline)', bundle)
        self.assertIn('window.addEventListener("pageshow", handlePageShow)', bundle)
        self.assertIn(
            'document.addEventListener("visibilitychange", handleVisibilityChange)',
            bundle,
        )
        self.assertIn("STREAM_BACKGROUND_STALE_MS", bundle)
        self.assertIn("appBackgrounded", bundle)
        self.assertIn("pendingForegroundReconnect", bundle)
        self.assertIn("document.hidden || appBackgrounded", bundle)
        self.assertIn('window.addEventListener("hermes:app-background"', bundle)
        self.assertIn('window.addEventListener("hermes:app-resume", refreshNow)', bundle)
        self.assertIn('window.removeEventListener("hermes:app-resume", refreshNow)', bundle)
        self.assertIn("设备离线，等待网络恢复；已提交任务会继续运行", bundle)
        self.assertIn('window.removeEventListener("offline", handleOffline)', bundle)
        self.assertIn('window.removeEventListener("online", handleOnline)', bundle)
        self.assertIn('window.removeEventListener("pageshow", handlePageShow)', bundle)
        self.assertIn(
            'document.removeEventListener("visibilitychange", handleVisibilityChange)',
            bundle,
        )
        self.assertNotIn("setInterval(refreshNow, 3000)", bundle)

    def test_frontend_css_constrains_group_chat_on_mobile(self):
        stylesheet = (MODULE_PATH.parent / "dist" / "style.css").read_text(
            encoding="utf-8"
        )

        self.assertIn(".hc-single-chat", stylesheet)
        self.assertIn("body:has(.hc-shell) {", stylesheet)
        self.assertNotIn("\nbody {\n", stylesheet)
        self.assertIn("overflow-wrap: anywhere", stylesheet)
        self.assertIn("min-width: 0", stylesheet)
        self.assertIn("max-width: 100%", stylesheet)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", stylesheet)
        self.assertIn(".hc-profile-copy {\n  width: 0;", stylesheet)
        self.assertIn(
            'body:has([data-chat-active="true"] .hc-single-chat) '
            'header[role="banner"]',
            stylesheet,
        )
        self.assertIn(
            'div[data-layout-variant]:has('
            '[data-chat-active="true"] .hc-single-chat) > header',
            stylesheet,
        )
        self.assertNotIn(
            'body:has(.hc-single-chat) header[role="banner"]',
            stylesheet,
        )
        self.assertIn(".hc-message.is-user .hc-message-body", stylesheet)
        self.assertIn("@media (display-mode: standalone)", stylesheet)
        self.assertIn("env(safe-area-inset-top, 0px)", stylesheet)
        self.assertIn(
            "height: var(--hermes-viewport-height, 100dvh)",
            stylesheet,
        )
        self.assertIn("position: fixed", stylesheet)
        self.assertIn("inset: 0", stylesheet)
        self.assertIn("grid-template-rows: auto minmax(0, 1fr) auto", stylesheet)
        self.assertIn(
            ".hc-single-input-shell textarea {\n    font-size: 16px;",
            stylesheet,
        )
        self.assertIn("overflow-anchor: none", stylesheet)
        self.assertIn("background: var(--background-base)", stylesheet)
        self.assertIn(".hc-system-event.is-workflow", stylesheet)
        self.assertIn(".hc-attachment-list", stylesheet)
        self.assertIn(".hc-attachment-preview", stylesheet)
        self.assertIn(".hc-activity-timeline", stylesheet)
        self.assertIn(".hc-activity-card", stylesheet)
        self.assertIn(".hc-activity-detail", stylesheet)

    def test_release_installer_keeps_existing_hashed_assets_during_deploy(self):
        installer = (
            MODULE_PATH.parents[3]
            / "deploy"
            / "dbb3"
            / "install-collaboration-release.sh"
        ).read_text(encoding="utf-8")

        self.assertNotIn('mv "${web_target}/assets"', installer)
        self.assertIn('install -d -o root -g root -m 0755 "${web_target}/assets"', installer)
        self.assertIn('"${release_snapshot}/web/assets/." "${web_target}/assets/"', installer)

    def test_official_chat_shell_routes_new_session_to_unified_chat(self):
        repo_root = MODULE_PATH.parents[3]
        chat_page = (repo_root / "web" / "src" / "pages" / "ChatPage.tsx").read_text(
            encoding="utf-8"
        )
        index_html = (repo_root / "web" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("hermes:resume-unified-session", chat_page)
        self.assertIn("unifiedChatActive", chat_page)
        self.assertNotIn("新建统一会话", chat_page)
        self.assertIn("apple-mobile-web-app-capable", index_html)
        self.assertIn("apple-mobile-web-app-status-bar-style", index_html)
        self.assertIn('rel="manifest"', index_html)

    def test_pwa_assets_are_public_before_login(self):
        repo_root = MODULE_PATH.parents[3]
        middleware = (
            repo_root / "hermes_cli" / "dashboard_auth" / "middleware.py"
        ).read_text(encoding="utf-8")
        login_page = (
            repo_root / "hermes_cli" / "dashboard_auth" / "login_page.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"/manifest.webmanifest"', middleware)
        self.assertIn('"/apple-touch-icon.png"', middleware)
        self.assertIn('"/hermes-official.png"', middleware)
        self.assertIn("apple-mobile-web-app-capable", login_page)
        self.assertIn("apple-mobile-web-app-status-bar-style", login_page)

        index_css = (repo_root / "web" / "src" / "index.css").read_text(
            encoding="utf-8"
        )
        app_shell = (repo_root / "web" / "src" / "App.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn("@media (display-mode: standalone)", index_css)
        self.assertIn("safe-area-inset-top", app_shell)
        self.assertIn("safe-area-inset-bottom", app_shell)

        plugin_api = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("request.stream()", plugin_api)
        self.assertNotIn("UploadFile", plugin_api)
        self.assertNotIn("File(...)", plugin_api)

        sessions_page = (
            repo_root / "web" / "src" / "pages" / "SessionsPage.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("queueUnifiedSessionResume", sessions_page)
        self.assertIn("onResume={() => resumeSessionInChat(s.id)}", sessions_page)

        chat_page = (
            repo_root / "web" / "src" / "pages" / "ChatPage.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("window.sessionStorage.getItem(", chat_page)
        self.assertIn("PENDING_UNIFIED_SESSION_KEY", chat_page)


    def test_single_conversation_rename_updates_the_persisted_record(self):
        module = load_module()
        conversation = module.create_single_conversation("default", "Old title")
        state = {"conversations": [conversation]}
        saved = []
        module.load_single_state = lambda: state
        module.save_single_state = lambda value: saved.append(value)

        result = module.rename_single_conversation(
            conversation["id"],
            module.RenameSingleConversationBody(title="  New   title  "),
        )

        self.assertEqual(result["conversation"]["title"], "New title")
        self.assertEqual(saved[-1]["conversations"][0]["title"], "New title")

    def test_hosted_chat_uses_selected_profile_and_secure_file_ids_without_kanban(self):
        module = load_module()
        conversation = module.create_single_conversation("reviewer")
        conversation["owner_id"] = "owner-a"
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None
        module._schedule_mobile_completion_notification = lambda *_args: None
        module.create_hosted_turn_record(
            conversation,
            turn_id="turn-chat-hosted",
            content="你好",
            title="你好",
            profiles=["reviewer"],
            artifact_required=False,
            attachment_ids=["file_report"],
            attachment_context="客户端伪造路径：/tmp/not-authoritative.pdf",
            mode="chat",
            route_metadata={"mode": "chat", "confidence": 0.98},
        )
        persisted_attachment = (
            Path.cwd() / "account-files" / "report.pdf"
        ).resolve()
        module._file_library = lambda: SimpleNamespace(
            resolve_download=lambda owner_id, file_id: (
                {
                    "id": file_id,
                    "mime_type": "application/pdf",
                    "name": "report.pdf",
                    "owner_id": owner_id,
                    "size": 4096,
                    "status": "available",
                },
                persisted_attachment,
            )
        )
        calls = []

        def runner(profile, prompt, **kwargs):
            calls.append((profile, prompt, kwargs))
            return "你好，我在。"

        module.execute_hosted_workflow(
            conversation["id"],
            "turn-chat-hosted",
            runner=runner,
            task_creator=lambda **_kwargs: self.fail("chat must not create Kanban tasks"),
        )

        run = conversation["hosted_turns"]["turn-chat-hosted"]
        self.assertEqual([profile for profile, _prompt, _kwargs in calls], ["reviewer"])
        self.assertNotIn("kanban_task_id", calls[0][2])
        self.assertIn("report.pdf", calls[0][1])
        self.assertIn(str(persisted_attachment), calls[0][1])
        self.assertIn("file_report", calls[0][1])
        self.assertNotIn("/tmp/not-authoritative.pdf", calls[0][1])
        self.assertEqual(run["mode"], "chat")
        self.assertEqual(run["status"], "completed")
        final = next(
            message
            for message in conversation["messages"]
            if message.get("meta", {}).get("role_stage") == "chat"
        )
        self.assertEqual(final["role"], "assistant")
        self.assertEqual(final["sender_role"], "hermes")
        self.assertEqual(final["profile"], "reviewer")
        self.assertIn("created_at", final)
        self.assertIn("completed_at", final)

    def test_hosted_chat_reuses_and_updates_the_profile_runtime_session(self):
        module = load_module()
        conversation = module.create_single_conversation("reviewer")
        conversation["owner_id"] = "owner-a"
        conversation["runtime_sessions"] = {"reviewer": "session-existing"}
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None
        module._schedule_mobile_completion_notification = lambda *_args: None
        module.create_hosted_turn_record(
            conversation,
            turn_id="turn-session-continuity",
            content="继续",
            title="继续",
            profiles=["reviewer"],
            artifact_required=False,
            mode="chat",
            route_metadata={"mode": "chat"},
        )
        calls = []

        def runner(profile, prompt, *, event_callback=None, session_id=""):
            calls.append((profile, prompt, session_id))
            event_callback(
                {
                    "type": "session.info",
                    "payload": {
                        "session_id": "session-resolved-tip",
                        "model": "model-a",
                        "provider": "provider-a",
                    },
                }
            )
            event_callback(
                {
                    "type": "message.complete",
                    "payload": {
                        "text": "连续回复",
                        "status": "completed",
                        "session_id": "session-resolved-tip",
                    },
                }
            )
            return "连续回复"

        module.execute_hosted_chat(
            conversation["id"],
            "turn-session-continuity",
            runner=runner,
        )

        self.assertEqual(calls[0][0], "reviewer")
        self.assertEqual(calls[0][2], "session-existing")
        self.assertNotIn("最近对话：\n你:", calls[0][1])
        self.assertEqual(
            conversation["runtime_sessions"]["reviewer"],
            "session-resolved-tip",
        )

    def test_completed_hosted_chat_role_is_not_executed_again_after_restart(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None
        module._schedule_mobile_completion_notification = lambda *_args: None
        run = module.create_hosted_turn_record(
            conversation,
            turn_id="turn-completed-role",
            content="执行一次",
            title="执行一次",
            profiles=["default"],
            artifact_required=False,
            mode="chat",
            route_metadata={"mode": "chat"},
        )
        run["status"] = "running"
        run["role_events"] = {
            "chat": {
                "profile": "default",
                "content": "已经完成",
                "status": "completed",
                "activities": [],
                "runtime_session_id": "session-completed",
                "started_at": 1000,
                "completed_at": 2000,
            }
        }

        module.execute_hosted_chat(
            conversation["id"],
            "turn-completed-role",
            runner=lambda *_args, **_kwargs: self.fail("completed role reran"),
        )

        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["chat_result"], "已经完成")
        self.assertEqual(
            conversation["runtime_sessions"]["default"],
            "session-completed",
        )

    def test_simple_chat_file_delivery_is_in_prompt_and_published(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        conversation["owner_id"] = "owner-a"
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None
        module._schedule_mobile_completion_notification = lambda *_args: None
        output_dir = module._hosted_turn_output_dir(
            conversation["id"],
            "turn-chat-file",
        )
        delivery_context = (
            f"Absolute output directory: `{output_dir.resolve()}`.\n"
            "Write every generated deliverable to this exact directory."
        )
        run = module.create_hosted_turn_record(
            conversation,
            turn_id="turn-chat-file",
            content="生成一个 PDF",
            title="生成 PDF",
            profiles=["default"],
            artifact_required=True,
            delivery_context=delivery_context,
            mode="chat",
            route_metadata={"mode": "chat", "artifact_required": True},
            output_dir=str(output_dir.resolve()),
        )
        prompts = []

        def runner(_profile, prompt, **_kwargs):
            prompts.append(prompt)
            (output_dir / "report.pdf").write_bytes(b"%PDF-fixture")
            return "已生成 report.pdf"

        module.execute_hosted_chat(
            conversation["id"],
            "turn-chat-file",
            runner=runner,
        )

        self.assertIn(str(output_dir.resolve()), prompts[0])
        self.assertEqual(run["status"], "completed")
        final = next(
            message
            for message in conversation["messages"]
            if message.get("meta", {}).get("message_key")
            == "turn-chat-file:chat:completed"
        )
        self.assertEqual([item["name"] for item in final["meta"]["attachments"]], ["report.pdf"])
        self.assertNotIn("path", final["meta"]["attachments"][0])

    def test_two_hosted_workers_run_concurrently_before_reviewer_and_reporter(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None
        module._schedule_mobile_completion_notification = lambda *_args: None
        module.create_hosted_turn_record(
            conversation,
            turn_id="turn-dual-workers",
            content="在 DBB3 部署，并在本地电脑验证",
            title="部署与验证",
            profiles=["default", "dbb3-worker", "pc-worker", "reviewer"],
            artifact_required=False,
            mode="work",
            route_metadata={
                "mode": "work",
                "profiles": ["dbb3-worker", "pc-worker"],
                "targets": ["dbb3", "pc"],
            },
        )
        barrier = threading.Barrier(2, timeout=2)
        worker_finished = set()
        calls = []
        call_lock = threading.Lock()

        def runner(profile, _prompt, *, event_callback=None, kanban_task_id=None):
            with call_lock:
                calls.append((profile, "start", time.monotonic(), kanban_task_id))
            if profile in {"dbb3-worker", "pc-worker"}:
                barrier.wait()
                event_callback(
                    {
                        "type": "session.info",
                        "payload": {"model": f"model-{profile}", "provider": "test-provider"},
                    }
                )
                event_callback(
                    {
                        "type": "tool.start",
                        "payload": {
                            "tool_id": f"tool-{profile}",
                            "name": "terminal",
                            "args": {"command": "hostname"},
                            "started_at": 1000,
                        },
                    }
                )
                time.sleep(0.05)
                event_callback(
                    {
                        "type": "tool.complete",
                        "payload": {
                            "tool_id": f"tool-{profile}",
                            "name": "terminal",
                            "result_text": "ok",
                            "duration_s": 0.05,
                        },
                    }
                )
                with call_lock:
                    worker_finished.add(profile)
                return f"{profile} 完成"
            if profile == "reviewer":
                self.assertEqual(worker_finished, {"dbb3-worker", "pc-worker"})
                return "全部执行结果审阅通过"
            self.assertEqual(worker_finished, {"dbb3-worker", "pc-worker"})
            return "最终汇报完成"

        module.execute_hosted_workflow(
            conversation["id"],
            "turn-dual-workers",
            runner=runner,
            task_creator=lambda **_kwargs: {
                "task_id": "root-dual",
                "child_ids": ["child-dbb3", "child-pc", "child-review"],
                "fanout": True,
            },
        )

        run = conversation["hosted_turns"]["turn-dual-workers"]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(set(run["worker_results"]), {"dbb3-worker", "pc-worker"})
        self.assertTrue(all(value == "completed" for value in run["worker_statuses"].values()))
        start_profiles = [profile for profile, phase, _at, _scope in calls if phase == "start"]
        self.assertEqual(set(start_profiles[:2]), {"dbb3-worker", "pc-worker"})
        self.assertEqual(start_profiles[2:], ["reviewer", "default"])

        worker_messages = [
            message
            for message in conversation["messages"]
            if message.get("sender_role") == "worker"
        ]
        self.assertEqual(
            {message["profile"] for message in worker_messages},
            {"dbb3-worker", "pc-worker"},
        )
        self.assertGreaterEqual(len(worker_messages), 4)
        self.assertEqual(
            len({message["meta"]["message_key"] for message in worker_messages}),
            len(worker_messages),
        )
        final_workers = [
            message
            for message in worker_messages
            if message.get("meta", {}).get("phase") == "handoff"
        ]
        self.assertEqual(len(final_workers), 2)
        for message in final_workers:
            self.assertEqual(message["role"], "assistant")
            self.assertEqual(message["handoff_to"], ["reviewer"])
            self.assertEqual(message["activity_count"], 1)
            self.assertEqual(message["activities"][0]["tool_name"], "terminal")
            self.assertEqual(message["activities"][0]["duration_ms"], 50)
            self.assertTrue(message["model"].startswith("model-"))
            self.assertEqual(message["provider"], "test-provider")
        self.assertEqual(
            sum(bool(message.get("meta", {}).get("final_report")) for message in conversation["messages"]),
            1,
        )

    def test_reviewer_rework_runs_workers_again_before_final_report(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None
        module._schedule_mobile_completion_notification = lambda *_args: None
        module.create_hosted_turn_record(
            conversation,
            turn_id="turn-review-rework",
            content="修复并验证部署",
            title="修复部署",
            profiles=["default", "dbb3-worker", "reviewer"],
            artifact_required=False,
            mode="work",
            route_metadata={"mode": "work", "targets": ["dbb3"]},
        )
        calls = []
        worker_attempt = 0
        reviewer_attempt = 0

        def runner(profile, prompt, **_kwargs):
            nonlocal worker_attempt, reviewer_attempt
            calls.append(profile)
            if profile == "dbb3-worker":
                worker_attempt += 1
                if worker_attempt == 2:
                    self.assertIn("审阅者退回意见", prompt)
                return f"执行结果 {worker_attempt}"
            if profile == "reviewer":
                reviewer_attempt += 1
                if reviewer_attempt == 1:
                    return "证据不足，需要返工。\nHERMES_REVIEW: REWORK"
                self.assertIn("返工后的执行者提交", prompt)
                return "证据已补齐，审阅通过。\nHERMES_REVIEW: PASS"
            self.assertEqual(reviewer_attempt, 2)
            return "最终汇报"

        module.execute_hosted_workflow(
            conversation["id"],
            "turn-review-rework",
            runner=runner,
            task_creator=lambda **_kwargs: {
                "task_id": "root-rework",
                "child_ids": ["child-worker", "child-review"],
                "fanout": True,
            },
        )

        run = conversation["hosted_turns"]["turn-review-rework"]
        self.assertEqual(
            calls,
            ["dbb3-worker", "reviewer", "dbb3-worker", "reviewer", "default"],
        )
        self.assertEqual(run["rework_round"], 1)
        self.assertEqual(run["status"], "completed")
        self.assertIn("HERMES_REVIEW: PASS", run["reviewer_result"])
        rework_request = next(
            message
            for message in conversation["messages"]
            if message.get("meta", {}).get("role_stage")
            == "reviewer:rework-request:1"
        )
        self.assertEqual(rework_request["handoff_to"], ["dbb3-worker"])
        self.assertTrue(
            any(
                message.get("meta", {}).get("role_stage")
                == "worker:dbb3-worker:rework:1"
                for message in conversation["messages"]
            )
        )

    def test_intent_classifier_is_model_first_and_adjudicates_low_confidence(self):
        module = load_module()
        calls = []
        answers = [
            {"mode": "chat", "confidence": 0.4, "reason": "uncertain"},
            {
                "mode": "work",
                "confidence": 0.92,
                "reason": "requires execution",
                "profiles": ["pc-worker"],
                "targets": ["pc"],
                "artifact": {"decision": "none", "types": [], "reason": "repo edit"},
            },
        ]

        routed = module.classify_user_intent(
            "你好",
            model_classifier=lambda text: calls.append(text) or answers.pop(0),
        )

        self.assertEqual(calls, ["你好", "你好"])
        self.assertEqual(routed["mode"], "work")
        self.assertEqual(routed["source"], "model")
        self.assertEqual(routed["profiles"], ["default", "pc-worker", "reviewer"])
        self.assertFalse(routed["artifact_required"])

    def test_hosted_turn_record_persists_route_contract(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        route_metadata = {
            "mode": "work",
            "reason": "needs both targets",
            "confidence": 0.94,
            "source": "model",
            "profiles": ["dbb3-worker", "pc-worker"],
            "artifact": {
                "decision": "required",
                "types": ["ipa"],
                "producer_profiles": ["pc-worker"],
            },
        }

        record = module.create_hosted_turn_record(
            conversation,
            turn_id="turn-route-contract",
            content="构建 IPA",
            title="构建 IPA",
            profiles=["default", "dbb3-worker", "pc-worker", "reviewer"],
            artifact_required=True,
            mode="work",
            route_metadata=route_metadata,
            delivery_context="Absolute output directory: C:/outputs",
            output_dir="C:/outputs",
        )

        self.assertEqual(record["mode"], "work")
        self.assertEqual(record["route_metadata"], route_metadata)
        self.assertEqual(record["artifact"]["decision"], "required")
        self.assertEqual(record["artifact_producer_profiles"], ["pc-worker"])
        self.assertIn("C:/outputs", record["delivery_context"])
        self.assertEqual(record["output_dir"], "C:/outputs")
        self.assertEqual(
            module._artifact_producer_profiles(
                {"artifact": {"decision": "required"}},
                ["dbb3-worker", "pc-worker"],
                required=True,
            ),
            ["dbb3-worker"],
        )

    def test_hosted_kanban_decompose_persists_profile_task_assignments(self):
        with TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "HERMES_HOME": tmp,
                "HERMES_KANBAN_DB": str(Path(tmp) / "kanban.db"),
                "HERMES_KANBAN_WORKSPACES_ROOT": str(Path(tmp) / "workspaces"),
            },
        ):
            module = load_module()
            from hermes_cli import kanban_db, kanban_decompose

            def fake_decompose(task_id, *, author=None):
                with kanban_db.connect_closing() as conn:
                    child_ids = kanban_db.decompose_triage_task(
                        conn,
                        task_id,
                        root_assignee="default",
                        author=author,
                        children=[
                            {"title": "DBB3", "body": "server", "assignee": "dbb3-worker"},
                            {"title": "PC", "body": "local", "assignee": "pc-worker"},
                            {
                                "title": "Review",
                                "body": "review both",
                                "assignee": "reviewer",
                                "parents": [0, 1],
                            },
                        ],
                    )
                return SimpleNamespace(
                    fanout=True,
                    child_ids=child_ids,
                    reason="planned",
                )

            with patch.object(kanban_decompose, "decompose_task", fake_decompose):
                result = module.create_hosted_kanban_task(
                    conversation_id="conversation-kanban",
                    turn_id="turn-kanban",
                    title="Deploy and verify",
                    content="Do the work",
                    profiles=["dbb3-worker", "pc-worker", "reviewer"],
                    output_dir="C:/absolute/output",
                )

            self.assertEqual(
                set(result["profile_task_ids"]),
                {"dbb3-worker", "pc-worker", "reviewer"},
            )
            with kanban_db.connect_closing() as conn:
                root = kanban_db.get_task(conn, result["task_id"])
            self.assertEqual(root.session_id, "conversation-kanban")
            self.assertIn("Required execution lanes", root.body)
            self.assertNotIn("C:/absolute/output", root.body)
            self.assertNotIn("Absolute output directory", root.body)

    def test_hosted_role_keeps_natural_mid_task_milestone_as_separate_message(self):
        module = load_module()
        conversation = module.create_single_conversation("default")
        state = {"conversations": [conversation]}
        module.load_single_state = lambda: state
        module.save_single_state = lambda _state: None
        module._schedule_mobile_completion_notification = lambda *_args: None
        module.create_hosted_turn_record(
            conversation,
            turn_id="turn-milestone",
            content="检查并修复服务",
            title="修复服务",
            profiles=["default", "dbb3-worker", "reviewer"],
            artifact_required=False,
            mode="work",
        )

        def runner(profile, _prompt, *, event_callback=None, **_kwargs):
            if profile == "dbb3-worker":
                event_callback(
                    {
                        "type": "message.delta",
                        "payload": {"text": "我已完成环境检查，发现两处配置问题。"},
                    }
                )
                event_callback(
                    {
                        "type": "tool.start",
                        "payload": {"tool_id": "fix-1", "name": "terminal"},
                    }
                )
                event_callback(
                    {
                        "type": "tool.complete",
                        "payload": {"tool_id": "fix-1", "name": "terminal", "result_text": "ok"},
                    }
                )
                return "配置修复和验证已经完成。"
            return "审阅通过。" if profile == "reviewer" else "最终汇报。"

        module.execute_hosted_workflow(
            conversation["id"],
            "turn-milestone",
            runner=runner,
            task_creator=lambda **_kwargs: {
                "task_id": "root-milestone",
                "child_ids": ["worker-milestone", "review-milestone"],
                "fanout": True,
            },
        )

        worker_messages = [
            message
            for message in conversation["messages"]
            if message.get("sender_role") == "worker"
        ]
        phases = [message.get("meta", {}).get("phase") for message in worker_messages]
        self.assertIn("opening", phases)
        self.assertIn("milestone", phases)
        self.assertIn("handoff", phases)
        milestone = next(
            message
            for message in worker_messages
            if message.get("meta", {}).get("phase") == "milestone"
        )
        self.assertEqual(
            milestone["content"],
            "我已完成环境检查，发现两处配置问题。",
        )


if __name__ == "__main__":
    unittest.main()
