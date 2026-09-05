from unittest.mock import patch

from scripts import upstream_change_report as report


def test_report_counts_all_commits_without_truncating_the_backlog():
    def fake_git(*args):
        if args[0] == "merge-base":
            return "common"
        if args[0] == "diff":
            return ""
        if args[0] == "log":
            return "\n".join(f"{index:040x} change {index}" for index in range(237))
        raise AssertionError(args)

    with patch.object(report, "git", side_effect=fake_git):
        text = report.build_report("product", "official")
    assert "Upstream commits: `237`" in text
    assert "change 199`" in text
    assert "change 200`" not in text


def test_rpc_state_and_shared_transport_changes_require_mobile_review():
    changed = [
        "tui_gateway/methods_profiles.py",
        "apps/shared/src/client.ts",
        "hermes_state_sessions.py",
        "agent/tool_executor.py",
        "cron/jobs.py",
    ]

    def fake_git(*args):
        if args[0] == "merge-base":
            return "common"
        if args[0] == "diff":
            return "\n".join(changed) if args[-1] == "common..official" else ""
        if args[0] == "log":
            return "123456 changed mobile protocol"
        raise AssertionError(args)

    with patch.object(report, "git", side_effect=fake_git):
        text = report.build_report("product", "official")
    mobile_section = text.split("## iOS/API adaptation signals", 1)[1].split("## Deployment", 1)[0]
    assert all(f"`{path}`" in mobile_section for path in changed)
    assert "Manual Codex review required before merge" in text
