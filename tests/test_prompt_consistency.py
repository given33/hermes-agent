"""Prompt-consistency regression tests.

Every agent-team capability must be described consistently across the four
surface texts the model actually reads:

  1. KANBAN_GUIDANCE (agent/prompt_builder.py) — worker protocol
  2. _manager_plan_prompt (collaboration plugin) — manager/planner
  3. build_group_prompt worker instruction (collaboration plugin) — group chat
  4. DELEGATE_TASK_SCHEMA (tools/delegate_tool.py) — the tool schema

When you add a new capability (tool, parameter, handoff field), add the
capability keyword below and ensure all four surfaces mention it — this test
then fails on drift instead of the model silently forgetting the feature.
"""

from __future__ import annotations

import pytest

from agent.prompt_builder import KANBAN_GUIDANCE
from plugins.collaboration.dashboard import plugin_api
from tools.delegate_tool import DELEGATE_TASK_SCHEMA

# capability -> expected keyword(s). Each tuple means: ANY of the keywords
# must be present (synonyms allowed).
CAPABILITIES: dict[str, tuple[str, ...]] = {
    "subagent naming": ("name",),
    "expected output contract": ("expected_output",),
    "acceptance criteria": ("acceptance_criteria",),
    "selective inheritance": ("inherit_turns",),
    "shared context variables": ("context_variables",),
    "subagent list": ("subagent_list",),
    "subagent steering": ("subagent_send",),
    "subagent kill": ("subagent_kill",),
    "needs_input block": ("needs_input",),
    "choice option format": ("选项", "A. ", "A、"),
}


def _surfaces() -> dict[str, str]:
    """Return the four model-facing prompt surfaces as text blobs."""
    manager_prompt = plugin_api._manager_plan_prompt(
        content="测试任务",
        fallback_workers=["dbb3-worker"],
        attachment_context="",
        artifact_required=False,
    )
    group_worker = plugin_api.build_group_prompt(
        room={"name": "测试群聊", "profiles": ["worker1"], "messages": []},
        profile="worker1",
        user_message="测试",
    )
    schema_text = str(DELEGATE_TASK_SCHEMA)
    return {
        "KANBAN_GUIDANCE": KANBAN_GUIDANCE,
        "manager_prompt": manager_prompt,
        "group_worker_prompt": group_worker,
        "DELEGATE_TASK_SCHEMA": schema_text,
    }


@pytest.mark.parametrize("capability", sorted(CAPABILITIES))
def test_kanban_guidance_mentions_capability(capability: str) -> None:
    keywords = CAPABILITIES[capability]
    assert any(keyword in KANBAN_GUIDANCE for keyword in keywords), (
        f"KANBAN_GUIDANCE missing capability '{capability}' "
        f"(expected one of {keywords})"
    )


@pytest.mark.parametrize("capability", sorted(CAPABILITIES))
def test_manager_prompt_mentions_capability(capability: str) -> None:
    keywords = CAPABILITIES[capability]
    manager_prompt = _surfaces()["manager_prompt"]
    assert any(keyword in manager_prompt for keyword in keywords), (
        f"manager prompt missing capability '{capability}' "
        f"(expected one of {keywords})"
    )


@pytest.mark.parametrize("capability", sorted(CAPABILITIES))
def test_group_worker_prompt_mentions_capability(capability: str) -> None:
    keywords = CAPABILITIES[capability]
    group_worker = _surfaces()["group_worker_prompt"]
    assert any(keyword in group_worker for keyword in keywords), (
        f"group worker prompt missing capability '{capability}' "
        f"(expected one of {keywords})"
    )


# The tool schema only needs to carry the parameters the model passes to
# delegate_task — prompts are for guidance, the schema is for mechanics.
SCHEMA_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "subagent naming": ("name",),
    "expected output contract": ("expected_output",),
    "acceptance criteria": ("acceptance_criteria",),
    "selective inheritance": ("inherit_turns",),
    "shared context variables": ("context_variables",),
}


@pytest.mark.parametrize("capability", sorted(SCHEMA_CAPABILITIES))
def test_delegate_schema_carries_capability(capability: str) -> None:
    keywords = SCHEMA_CAPABILITIES[capability]
    schema_text = _surfaces()["DELEGATE_TASK_SCHEMA"]
    assert any(keyword in schema_text for keyword in keywords), (
        f"DELEGATE_TASK_SCHEMA missing parameter for '{capability}' "
        f"(expected one of {keywords})"
    )


def test_parse_slash_directive():
    directive, argument, content = plugin_api._parse_slash_directive(
        "/plan 调查 DBB3 的内存瓶颈"
    )
    assert directive == "plan"
    assert argument == "调查 DBB3 的内存瓶颈"
    assert content == argument
    directive, argument, content = plugin_api._parse_slash_directive(
        "/GOAL 完成三端部署"
    )
    assert directive == "goal"
    assert argument == "完成三端部署"
    directive, argument, content = plugin_api._parse_slash_directive("普通任务描述")
    assert directive == "" and argument == "" and content == "普通任务描述"
    # Bare directive keeps the original text as the task content.
    directive, argument, content = plugin_api._parse_slash_directive("/plan")
    assert directive == "plan" and argument == "" and content == "/plan"


def test_slash_directives_inject_into_manager_prompt():
    plan_prompt = plugin_api._manager_plan_prompt(
        content="测试任务",
        fallback_workers=["dbb3-worker"],
        attachment_context="",
        artifact_required=False,
        plan_only=True,
    )
    assert "/plan" in plan_prompt
    assert "绝不派发执行节点" in plan_prompt
    goal_prompt = plugin_api._manager_plan_prompt(
        content="测试任务",
        fallback_workers=["dbb3-worker"],
        attachment_context="",
        artifact_required=False,
        goal_override="以目标为准",
    )
    assert "/goal" in goal_prompt
    assert "最高优先级" in goal_prompt
    assert "以目标为准" in goal_prompt


def test_render_manager_plan_report():
    report = plugin_api._render_manager_plan_report(
        {
            "difficulty": "medium",
            "reason": "需要核实",
            "approach": "分三步推进",
            "task_requirements": "逐条要求",
            "acceptance_criteria": ["条件一", "条件二"],
            "test_plan": "验证动作",
            "flow": ["步骤一", "步骤二"],
            "plan": [
                {
                    "id": "step-1",
                    "title": "第一步",
                    "objective": "完成第一步目标",
                    "assignee": "dbb3-worker",
                }
            ],
        },
        "原始任务",
        "方案主题",
    )
    assert report.startswith("# 执行方案：方案主题")
    assert "## 整体方案" in report
    assert "## 验收标准" in report and "条件一" in report
    assert "## 执行步骤" in report and "dbb3-worker" in report
