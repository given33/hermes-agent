"""Regression tests for local terminal initial cwd normalization."""

import os
import re
from pathlib import Path

from tools.environments.local import LocalEnvironment, _resolve_local_initial_cwd


def _native_cwd_text(text: str) -> str:
    # Normalize a shell pwd output for cross-platform comparison.
    # On Windows the local backend deliberately runs Git Bash (MSYS),
    # whose pwd prints /c/Users/...; convert that to the native drive
    # form so the assertion compares locations, not shell dialects.
    text = text.strip()
    m = re.match(r"^/([a-zA-Z])/(.*)$", text)
    if m and os.name == "nt":
        text = f"{m.group(1).upper()}:/{m.group(2)}"
    return str(Path(text))


def test_relative_initial_cwd_resolves_from_parent(tmp_path, monkeypatch):
    project = tmp_path / "hermes-agent"
    project.mkdir()
    monkeypatch.chdir(tmp_path)

    assert _resolve_local_initial_cwd("hermes-agent") == str(project)


def test_local_environment_keeps_existing_relative_child_cwd(tmp_path, monkeypatch):
    project = tmp_path / "hermes-agent"
    project.mkdir()
    monkeypatch.chdir(tmp_path)

    env = LocalEnvironment(cwd="hermes-agent", timeout=5)
    try:
        result = env.execute("pwd", timeout=5)
    finally:
        env.cleanup()

    assert result["returncode"] == 0
    assert _native_cwd_text(result["output"]) == str(Path(os.path.realpath(project)))
