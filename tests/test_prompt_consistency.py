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
