"""Product invariants for the retired reviewer/supervisor workflow."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest


RETIRED_TOOLS = {"kanban_request_review", "kanban_request_changes"}


def test_worker_schema_has_only_completion_and_block_terminal_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_retired_review_test")
    import tools.kanban_tools  # noqa: F401 - registers the Kanban tools
    from acp_adapter.tools import _POLISHED_TOOLS
    from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    definitions = registry.get_definitions(
        set(resolve_toolset("hermes-cli")), quiet=True
    )
    names = {
        definition["function"]["name"]
        for definition in definitions
        if "function" in definition
    }

    assert RETIRED_TOOLS.isdisjoint(names)
    assert RETIRED_TOOLS.isdisjoint(_POLISHED_TOOLS)
    assert RETIRED_TOOLS.isdisjoint(EXPOSED_TOOLS)
    assert RETIRED_TOOLS.isdisjoint(resolve_toolset("kanban"))
    assert {"kanban_complete", "kanban_block"}.issubset(names)


def test_worker_prompts_and_goal_loop_do_not_offer_review_handoffs() -> None:
    from agent.prompt_builder import KANBAN_GUIDANCE
    from hermes_cli import goals

    prompt_text = "\n".join(
        (
            KANBAN_GUIDANCE,
            goals.KANBAN_GOAL_CONTINUATION_TEMPLATE,
            goals.KANBAN_GOAL_FINALIZE_TEMPLATE,
        )
    )
    assert "kanban_request_review" not in prompt_text
    assert "kanban_request_changes" not in prompt_text
    assert "kanban_complete" in prompt_text
    assert "kanban_block" in prompt_text


def test_cli_and_dispatcher_have_no_reviewer_execution_path() -> None:
    from hermes_cli import kanban, kanban_db
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    cli_help = kanban.run_slash("help")
    assert "request-review" not in cli_help
    assert "request-changes" not in cli_help
    assert "reopen-review" not in cli_help
    assert "review_dispatch" not in DEFAULT_CONFIG["kanban"]

    dispatch_source = inspect.getsource(kanban_db._dispatch_once_locked)
    assert "claim_review_task" not in dispatch_source
    assert "sdlc-review" not in dispatch_source
    assert "status = 'review'" not in dispatch_source


def test_legacy_review_row_migrates_back_to_implementer_queue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from hermes_cli import kanban_db

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kanban_db._INITIALIZED_PATHS.clear()
    kanban_db.init_db()

    with kanban_db.connect() as conn:
        task_id = kanban_db.create_task(
            conn, title="legacy review", assignee="builder"
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        assert kanban_db.request_review(
            conn,
            task_id,
            summary="legacy handoff",
            reviewer="old-reviewer",
            expected_run_id=claimed.current_run_id,
        )
        review_run = kanban_db.claim_review_task(conn, task_id)
        assert review_run is not None

    kanban_db.init_db()

    with kanban_db.connect() as conn:
        task = kanban_db.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.assignee == "builder"
        assert task.current_run_id is None
        latest = kanban_db.latest_run(conn, task_id)
        assert latest is not None
        assert latest.status == "reclaimed"
        assert latest.outcome == "reclaimed"
        assert any(
            event.kind == "review_retired"
            for event in kanban_db.list_events(conn, task_id)
        )
