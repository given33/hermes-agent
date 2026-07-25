"""Tests for static context-engineering diagnostics."""

from pathlib import Path

from agent.context_diagnostics import analyze_context_sources


def _codes(report) -> set[str]:
    return {finding.code for finding in report.findings}


def test_reports_project_context_precedence_and_shadowed_files(tmp_path: Path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    workdir = repo / "packages" / "app"
    (repo / ".git").mkdir(parents=True)
    workdir.mkdir(parents=True)
    (repo / ".hermes.md").write_text("Repository-wide guidance", encoding="utf-8")
    (workdir / "AGENTS.md").write_text("Local agent guidance", encoding="utf-8")
    (workdir / "CLAUDE.md").write_text("Legacy Claude guidance", encoding="utf-8")

    report = analyze_context_sources(cwd=workdir, hermes_home=home)

    project_sources = [source for source in report.sources if source.kind == "project"]
    assert [source.path.name for source in project_sources if source.active] == [".hermes.md"]
    assert {source.path.name for source in project_sources if not source.active} == {
        "AGENTS.md",
        "CLAUDE.md",
    }
    finding = next(
        finding
        for finding in report.findings
        if finding.code == "shadowed_project_context"
    )
    assert "Hermes project context wins precedence" in finding.message
    assert {path.name for path in finding.paths} == {"AGENTS.md", "CLAUDE.md"}


def test_empty_higher_priority_context_does_not_shadow_loaded_agents_file(tmp_path: Path):
    home = tmp_path / "home"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    (cwd / ".hermes.md").write_text("\n", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("Loaded project guidance", encoding="utf-8")

    report = analyze_context_sources(cwd=cwd, hermes_home=home)

    project_sources = [source for source in report.sources if source.kind == "project"]
    assert [(source.path.name, source.active) for source in project_sources] == [
        ("AGENTS.md", True)
    ]
    assert "shadowed_project_context" not in _codes(report)


def test_flags_large_always_on_sources_and_exact_cross_layer_repetition(tmp_path: Path):
    home = tmp_path / "home"
    memories = home / "memories"
    cwd = tmp_path / "workspace"
    memories.mkdir(parents=True)
    cwd.mkdir()

    repeated = (
        "Keep deployment identifiers exact and verify every artifact before reporting completion."
    )
    (home / "SOUL.md").write_text(repeated + "\n" + ("s" * 8_100), encoding="utf-8")
    (memories / "MEMORY.md").write_text("m" * 8_100, encoding="utf-8")
    (cwd / "AGENTS.md").write_text(repeated + "\n" + ("p" * 16_100), encoding="utf-8")

    report = analyze_context_sources(cwd=cwd, hermes_home=home)

    assert {
        "large_persona",
        "large_memory",
        "large_project",
        "large_always_on_context",
        "duplicate_guidance",
    } <= _codes(report)
    assert report.always_on_chars > 32_000
    assert report.always_on_estimated_tokens == (report.always_on_chars + 3) // 4


def test_skill_diagnostics_respect_progressive_disclosure_support_dirs(tmp_path: Path):
    home = tmp_path / "home"
    cwd = tmp_path / "workspace"
    skills = tmp_path / "skills"
    skill = skills / "review"
    references = skill / "references"
    cwd.mkdir()
    references.mkdir(parents=True)
    (skill / "SKILL.md").write_text("x" * 20_100, encoding="utf-8")
    (references / "SKILL.md").write_text("archived" * 4_000, encoding="utf-8")

    report = analyze_context_sources(
        cwd=cwd,
        hermes_home=home,
        skill_dirs=(skills,),
    )

    skill_sources = [source for source in report.sources if source.kind == "skill"]
    assert [source.path for source in skill_sources] == [(skill / "SKILL.md").resolve()]
    finding = next(finding for finding in report.findings if finding.code == "large_skill")
    assert "linked support files" in finding.message


def test_small_single_source_context_has_no_findings(tmp_path: Path):
    home = tmp_path / "home"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text("One repository-specific gotcha.", encoding="utf-8")

    report = analyze_context_sources(cwd=cwd, hermes_home=home)

    assert report.always_on_estimated_tokens > 0
    assert report.findings == ()
