"""Doctor output for context-engineering diagnostics."""

from pathlib import Path

from hermes_cli.doctor import _check_context_engineering


def test_doctor_surfaces_project_context_precedence(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    home = tmp_path / "home"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    (cwd / ".hermes.md").write_text("Primary project context", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("Shadowed project context", encoding="utf-8")
    monkeypatch.setattr("agent.skill_utils.get_all_skills_dirs", lambda: [])

    _check_context_engineering(home, cwd)

    output = capsys.readouterr().out
    assert "Context Engineering" in output
    assert "Always-on file context" in output
    assert "Hermes project context wins precedence" in output
    assert "AGENTS.md" in output


def test_doctor_reports_clean_small_context(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    home = tmp_path / "home"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text("One project gotcha", encoding="utf-8")
    monkeypatch.setattr("agent.skill_utils.get_all_skills_dirs", lambda: [])

    _check_context_engineering(home, cwd)

    output = capsys.readouterr().out
    assert "Context sources are right-sized" in output
