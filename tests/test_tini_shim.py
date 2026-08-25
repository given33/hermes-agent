"""Unit tests for docker/tini-shim.sh argument stripping (#66679).

These run without Docker: the shim's HERMES_TINI_SHIM_TARGET /
HERMES_TINI_SHIM_WRAPPER hooks let us record the argv that would be
handed to /init.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIM = REPO_ROOT / "docker" / "tini-shim.sh"


def _working_sh() -> str | None:
    """Locate a POSIX shell that can execute the "#!/bin/sh" shim.

    On Windows, bare "sh" is usually not on PATH at all (and "bash"
    on PATH may be the WSL launcher stub, which exits non-zero when WSL
    has no installed distro), so candidates are validated by execution.
    Git-for-Windows ships a real MSYS sh.exe; prefer it over whatever
    PATH order says.
    """
    candidates: list[str] = []
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", "C:\\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)")
        for base in (pf, pf86):
            if base:
                candidates.append(os.path.join(base, "Git", "bin", "sh.exe"))
                candidates.append(os.path.join(base, "Git", "usr", "bin", "sh.exe"))
                candidates.append(os.path.join(base, "Git", "bin", "bash.exe"))
    for name in ("sh", "bash"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for cand in candidates:
        try:
            probe = subprocess.run([cand, "-c", ":"], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return cand
    return None


_SH = _working_sh()

@pytest.fixture
def recorder(tmp_path: Path) -> tuple[Path, Path]:
    """Fake /init + wrapper that print argv, one token per line."""
    init = tmp_path / "fake-init"
    wrapper = tmp_path / "fake-wrapper"
    init.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    wrapper.write_text("#!/bin/sh\nprintf 'wrapper\\n'\n")
    init.chmod(0o755)
    wrapper.chmod(0o755)
    return init, wrapper


def _run_shim(
    recorder: tuple[Path, Path],
    args: list[str],
) -> subprocess.CompletedProcess[str]:
    init, wrapper = recorder
    env = os.environ.copy()
    env["HERMES_TINI_SHIM_TARGET"] = str(init)
    env["HERMES_TINI_SHIM_WRAPPER"] = str(wrapper)
    assert _SH is not None, "no working POSIX shell found"
    return subprocess.run(
        [_SH, str(SHIM), *args],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        check=False,
    )


def test_shim_script_is_executable_bit_friendly() -> None:
    assert SHIM.is_file()
    text = SHIM.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert "HERMES_TINI_SHIM_TARGET" in text


@pytest.mark.skipif(_SH is None, reason="needs a working POSIX shell (e.g. Git Bash) to execute the shim")
def test_strips_g_and_double_dash(recorder: tuple[Path, Path]) -> None:
    """Legacy `tini -g -- gateway run` must not forward `-g` to /init."""
    r = _run_shim(recorder, ["-g", "--", "gateway", "run"])
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln]
    init, wrapper = recorder
    assert lines[0] == str(wrapper)
    assert lines[1:] == ["gateway", "run"]
    assert "-g" not in lines
    assert "--" not in lines






@pytest.mark.skipif(_SH is None, reason="needs a working POSIX shell (e.g. Git Bash) to execute the shim")
def test_does_not_double_wrap_existing_wrapper(
    recorder: tuple[Path, Path],
) -> None:
    _, wrapper = recorder
    r = _run_shim(recorder, ["-g", "--", str(wrapper), "gateway", "run"])
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln]
    assert lines == [str(wrapper), "gateway", "run"]


@pytest.mark.skipif(_SH is None, reason="needs a working POSIX shell (e.g. Git Bash) to execute the shim")
def test_strips_p_and_e_with_arguments(recorder: tuple[Path, Path]) -> None:
    r = _run_shim(
        recorder,
        ["-p", "SIGKILL", "-e", "143", "-v", "-v", "--", "sleep", "infinity"],
    )
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln]
    _, wrapper = recorder
    assert lines == [str(wrapper), "sleep", "infinity"]
    assert "SIGKILL" not in lines
    assert "143" not in lines
