import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace


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

    def test_artifact_delivery_requires_an_explicit_file_deliverable(self):
        module = load_module()

        for request in (
            "帮我做一个季度汇报 PPT",
            "把分析结果导出成 PDF 给我下载",
            "生成一份 Excel 表格和 Word 文档",
            "请压缩成 zip 文件发给我",
        ):
            self.assertTrue(module.requires_artifact_delivery(request), request)

        for request in (
            "检查项目里的文件并运行测试",
            "分析日志，直接告诉我结论",
            "修改代码后汇报结果",
            "搜索网页并总结重点",
            "分析我上传的 PDF，只在会话里告诉我结论",
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
        self.assertEqual(manifest["version"], "2.1.28")
        self.assertEqual(manifest["entry"], "dist/index.js?v=2.1.28")
        self.assertEqual(manifest["css"], "dist/style.css?v=2.1.28")

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
        self.assertIn('collabApi("/route"', bundle)
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
        self.assertIn("buildContinuousPrompt", bundle)
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
        self.assertIn('event.type === "thinking.delta"', bundle)
        self.assertIn('event.type === "tool.progress"', bundle)
        self.assertIn('event.type === "subagent.tool"', bundle)
        self.assertNotIn("hc-streaming-label", bundle)
        self.assertNotIn('if (message.kind === "route") return null;', bundle)
        self.assertIn("hc-route-event", bundle)
        self.assertIn('kind: "route"', bundle)
        self.assertIn('name: route.label', bundle)
        self.assertIn('await record(conversationId, routeMessage)', bundle)
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

    def test_frontend_realtime_reasoning_uses_model_phase_start_boundary(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("let modelPhaseStartedAt = turnStartedAt;", bundle)
        self.assertIn("started_at: modelPhaseStartedAt || Date.now(),", bundle)
        self.assertIn("modelPhaseStartedAt = endedAt;", bundle)

    def test_frontend_workflow_runs_roles_serially_and_publishes_one_final_report(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )
        work_start = bundle.index('if (route.mode === "work")')
        work_end = bundle.index("} else {", work_start)
        workflow = bundle[work_start:work_end]

        self.assertNotIn("await Promise.all(", workflow)
        self.assertIn('roleStage: "worker"', workflow)
        self.assertIn('roleStage: "reviewer"', workflow)
        self.assertIn('roleStage: "reporter"', workflow)
        self.assertIn("collapseActivities: true", workflow)
        self.assertIn("publishAttachments: false", workflow)
        self.assertIn("workerResult.text", workflow)
        self.assertIn("reviewerResult.text", workflow)
        self.assertIn("workerResult.attachments", workflow)
        self.assertIn("你是唯一最终汇报者", workflow)

    def test_frontend_only_collects_outputs_for_explicit_artifact_tasks(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("route.artifact_required", bundle)
        self.assertIn("collectArtifacts: artifactRequired", bundle)
        self.assertIn("本任务未要求交付文件", bundle)
        self.assertIn("不要创建、复制或上传文件", bundle)

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

    def test_mobile_header_hides_route_picker_and_main_nav_hides_files(self):
        bundle = (MODULE_PATH.parent / "dist" / "index.js").read_text(
            encoding="utf-8"
        )
        app_source = (
            MODULE_PATH.parents[3] / "web" / "src" / "App.tsx"
        ).read_text(encoding="utf-8")

        self.assertNotIn('className: "hc-route-select"', bundle)
        self.assertIn('collabApi("/route"', bundle)
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
        self.assertIn("setInterval", bundle)
        self.assertIn("latestAssistantText", bundle)
        self.assertIn("hc-connection-state", bundle)
        self.assertNotIn("reject(new Error(`${profile} 流式连接失败`))", bundle)

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
        self.assertIn("设备离线，等待网络恢复；已提交任务会继续运行", bundle)
        self.assertIn('window.removeEventListener("offline", handleOffline)', bundle)
        self.assertIn('window.removeEventListener("online", handleOnline)', bundle)
        self.assertIn('window.removeEventListener("pageshow", handlePageShow)', bundle)
        self.assertIn(
            'document.removeEventListener("visibilitychange", handleVisibilityChange)',
            bundle,
        )

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


if __name__ == "__main__":
    unittest.main()
