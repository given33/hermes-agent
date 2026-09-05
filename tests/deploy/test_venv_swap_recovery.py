from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "deploy" / "public" / "install-collaboration-backend.sh"
TXID = "0123456789abcdef0123456789abcdef"


def _extract_function_python_heredoc(
    shell_source: str, function_name: str
) -> tuple[str, str]:
    """Return the first heredoc body in a named shell function and its opener."""
    lines = shell_source.splitlines(keepends=True)
    function_pattern = re.compile(
        rf"^[ \t]*{re.escape(function_name)}\(\)[ \t]*\{{[ \t]*(?:#.*)?$"
    )
    try:
        function_line = next(
            index
            for index, line in enumerate(lines)
            if function_pattern.fullmatch(line.rstrip("\r\n"))
        )
    except StopIteration as error:
        raise AssertionError(f"shell function not found: {function_name}") from error

    heredoc_pattern = re.compile(
        r"<<(?P<dash>-?)[ \t]*(?P<token>'[^']+'|\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)"
    )
    for opener_index in range(function_line + 1, len(lines)):
        opener = lines[opener_index].rstrip("\r\n")
        match = heredoc_pattern.search(opener)
        if match is None:
            continue
        token = match.group("token")
        delimiter = token[1:-1] if token[:1] in {"'", '"'} else token
        strip_tabs = match.group("dash") == "-"
        body: list[str] = []
        for line in lines[opener_index + 1 :]:
            candidate = line.rstrip("\r\n")
            terminator = candidate.lstrip("\t") if strip_tabs else candidate
            if terminator == delimiter:
                return "".join(body), opener
            body.append(line.lstrip("\t") if strip_tabs else line)
        raise AssertionError(
            f"unterminated {delimiter!r} heredoc in {function_name}"
        )
    raise AssertionError(f"Python heredoc not found in {function_name}")


INSTALLER_SOURCE = INSTALLER.read_text(encoding="utf-8")
RECOVERY_SOURCE, RECOVERY_OPENER = _extract_function_python_heredoc(
    INSTALLER_SOURCE, "recover_venv_swap"
)


@dataclass(frozen=True)
class _PrivilegedPython:
    prefix: tuple[str, ...]
    executable: str

    def run(
        self,
        source: str,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [*self.prefix]
        if environment:
            command.extend(
                ["env", *(f"{key}={value}" for key, value in environment.items())]
            )
        command.extend([self.executable, "-", *arguments])
        return subprocess.run(
            command,
            input=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )


@dataclass(frozen=True)
class _RecoveryCase:
    runtime: _PrivilegedPython
    sandbox: str
    target: str
    journal: str


_CREATE_SANDBOX = r"""
import tempfile

print(tempfile.mkdtemp(prefix="hermes-venv-swap-test-", dir="/tmp"))
"""


_REMOVE_SANDBOX = r"""
import pathlib
import shutil
import sys

path = pathlib.Path(sys.argv[1])
if path.parent != pathlib.Path("/tmp") or not path.name.startswith(
    "hermes-venv-swap-test-"
):
    raise RuntimeError(f"refusing unsafe test cleanup: {path}")
shutil.rmtree(path)
"""


_SETUP_CASE = r"""
import json
import os
import pathlib
import stat
import sys

sandbox = pathlib.Path(sys.argv[1])
phase = sys.argv[2]
position_spec = sys.argv[3]
txid = sys.argv[4]
target = sandbox / "target"
journal = sandbox / "journal" / "venv-swap.json"
target.mkdir()
journal.parent.mkdir()
objects = sandbox / "objects"
objects.mkdir()

paths = {}
identities = {}
for role in ("old", "new"):
    path = objects / role
    path.mkdir()
    (path / "marker").write_text(role, encoding="utf-8")
    binary = path / "bin" / "python"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    metadata = path.lstat()
    identities[role] = f"{metadata.st_dev}:{metadata.st_ino}"
    paths[role] = path

position_names = {
    "live": ".venv",
    "candidate": f".venv.candidate.{txid}",
    "rollback": f".venv.rollback-{txid}",
}
if position_spec:
    for assignment in position_spec.split(","):
        position, role = assignment.split(":", 1)
        os.replace(paths[role], target / position_names[position])

root_metadata = target.stat(follow_symlinks=False)
payload = {
    "schema": "hermes.venv-swap.v1",
    "phase": phase,
    "txid": txid,
    "target_root": str(target),
    "target_identity": f"{root_metadata.st_dev}:{root_metadata.st_ino}",
    "live": ".venv",
    "old_identity": identities["old"],
    "candidate": position_names["candidate"],
    "new_identity": identities["new"],
    "rollback": position_names["rollback"],
}
journal.write_text(
    json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
journal.chmod(0o600)
os.chown(journal, 0, 0)
print(json.dumps({"target": str(target), "journal": str(journal)}))
"""


_MUTATE_CASE = r"""
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
journal = pathlib.Path(sys.argv[2])
action = sys.argv[3]
txid = sys.argv[4]

if action == "corrupt-journal":
    journal.write_text("{not-json\n", encoding="utf-8")
    journal.chmod(0o600)
elif action == "wrong-journal-mode":
    journal.chmod(0o644)
elif action == "extra-artifact":
    extra = target / ".venv.failed.unrecorded"
    extra.mkdir()
    (extra / "marker").write_text("extra", encoding="utf-8")
elif action == "candidate-inode-mismatch":
    candidate = target / f".venv.candidate.{txid}"
    displaced = target.parent / "displaced-candidate"
    candidate.replace(displaced)
    candidate.mkdir()
    (candidate / "marker").write_text("replacement", encoding="utf-8")
elif action == "remove-journal":
    journal.unlink()
else:
    raise RuntimeError(f"unknown mutation: {action}")
"""


_SNAPSHOT_CASE = r"""
import json
import pathlib
import stat
import sys

target = pathlib.Path(sys.argv[1])
journal = pathlib.Path(sys.argv[2])

entries = {}
for entry in sorted(target.iterdir(), key=lambda path: path.name):
    metadata = entry.lstat()
    marker = entry / "marker"
    binary = entry / "bin" / "python"
    entries[entry.name] = {
        "identity": f"{metadata.st_dev}:{metadata.st_ino}",
        "kind": (
            "directory"
            if stat.S_ISDIR(metadata.st_mode)
            else "symlink"
            if stat.S_ISLNK(metadata.st_mode)
            else "other"
        ),
        "marker": marker.read_text(encoding="utf-8") if marker.is_file() else None,
        "mode": stat.S_IMODE(metadata.st_mode),
        "runtime_python": binary.is_file() and bool(stat.S_IMODE(binary.stat().st_mode) & 0o111),
    }

journal_state = None
if journal.exists() or journal.is_symlink():
    metadata = journal.lstat()
    journal_state = {
        "identity": f"{metadata.st_dev}:{metadata.st_ino}",
        "kind": (
            "file"
            if stat.S_ISREG(metadata.st_mode)
            else "symlink"
            if stat.S_ISLNK(metadata.st_mode)
            else "other"
        ),
        "mode": stat.S_IMODE(metadata.st_mode),
        "text": journal.read_text(encoding="utf-8", errors="replace"),
        "uid": metadata.st_uid,
    }

print(json.dumps({"entries": entries, "journal": journal_state}, sort_keys=True))
"""


_KILL_RECOVERY = f"""
import os
import signal
import subprocess
import sys

recovery_source = {RECOVERY_SOURCE!r}
environment = os.environ.copy()
environment["HERMES_VENV_RECOVERY_KILL_PHASE"] = sys.argv[3]
completed = subprocess.run(
    [sys.executable, "-", sys.argv[1], sys.argv[2]],
    input=recovery_source,
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    timeout=15,
    env=environment,
    check=False,
)
sys.stdout.write(completed.stdout)
sys.stderr.write(completed.stderr)
if completed.returncode != -signal.SIGKILL:
    raise RuntimeError(
        f"recovery kill point was not reached; return code={{completed.returncode}}"
    )
raise SystemExit(128 + signal.SIGKILL)
"""


def _require_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.fixture(scope="module")
def privileged_posix_python() -> _PrivilegedPython:
    if os.name == "nt":
        wsl = shutil.which("wsl.exe")
        if wsl is None:
            pytest.skip("WSL is required to exercise POSIX venv recovery semantics")
        runtime = _PrivilegedPython((wsl, "--exec", "sudo", "-n"), "python3")
    elif hasattr(os, "geteuid") and os.geteuid() == 0:
        runtime = _PrivilegedPython((), sys.executable)
    else:
        sudo = shutil.which("sudo")
        if sudo is None:
            pytest.skip("root or passwordless sudo is required for root-owned journals")
        runtime = _PrivilegedPython((sudo, "-n"), sys.executable)

    probe = runtime.run(
        "import os, sys; "
        "raise SystemExit(0 if os.geteuid() == 0 and hasattr(os, 'O_DIRECTORY') else 1)\n"
    )
    if probe.returncode != 0:
        pytest.skip("a root POSIX Python with O_DIRECTORY is required")
    return runtime


@pytest.fixture
def posix_sandbox(privileged_posix_python: _PrivilegedPython):
    created = privileged_posix_python.run(_CREATE_SANDBOX)
    _require_success(created)
    sandbox = created.stdout.strip()
    assert sandbox.startswith("/tmp/hermes-venv-swap-test-")
    try:
        yield privileged_posix_python, sandbox
    finally:
        removed = privileged_posix_python.run(_REMOVE_SANDBOX, sandbox)
        _require_success(removed)


def _make_case(
    posix_sandbox: tuple[_PrivilegedPython, str],
    phase: str,
    positions: str,
) -> _RecoveryCase:
    runtime, sandbox = posix_sandbox
    result = runtime.run(_SETUP_CASE, sandbox, phase, positions, TXID)
    _require_success(result)
    paths = json.loads(result.stdout)
    return _RecoveryCase(runtime, sandbox, paths["target"], paths["journal"])


def _mutate(case: _RecoveryCase, action: str) -> None:
    result = case.runtime.run(_MUTATE_CASE, case.target, case.journal, action, TXID)
    _require_success(result)


def _snapshot(case: _RecoveryCase) -> dict[str, object]:
    result = case.runtime.run(_SNAPSHOT_CASE, case.target, case.journal)
    _require_success(result)
    return json.loads(result.stdout)


def _recover(
    case: _RecoveryCase, kill_phase: str | None = None
) -> subprocess.CompletedProcess[str]:
    if kill_phase:
        return case.runtime.run(
            _KILL_RECOVERY, case.target, case.journal, kill_phase
        )
    return case.runtime.run(RECOVERY_SOURCE, case.target, case.journal)


def _assert_recovered(case: _RecoveryCase, expected_marker: str) -> None:
    state = _snapshot(case)
    entries = state["entries"]
    assert list(entries) == [".venv"]
    assert entries[".venv"]["kind"] == "directory"
    assert entries[".venv"]["marker"] == expected_marker
    assert entries[".venv"]["runtime_python"] is True
    assert state["journal"] is None


@pytest.mark.parametrize(
    "positions",
    (
        pytest.param("live:old,candidate:new", id="before-old-move"),
        pytest.param("candidate:new,rollback:old", id="after-old-move"),
        pytest.param("live:new,rollback:old", id="after-candidate-publish"),
        pytest.param("live:old", id="rollback-already-restored"),
    ),
)
def test_prepared_recovery_covers_every_durable_rename_state(
    posix_sandbox, positions: str
):
    case = _make_case(posix_sandbox, "prepared", positions)

    result = _recover(case)

    _require_success(result)
    _assert_recovered(case, "old")


@pytest.mark.parametrize("phase", ("candidate", "committed"))
@pytest.mark.parametrize(
    "positions",
    (
        pytest.param("live:new,rollback:old", id="rollback-present"),
        pytest.param("live:new", id="rollback-already-removed"),
    ),
)
def test_authoritative_recovery_keeps_new_environment(
    posix_sandbox, phase: str, positions: str
):
    case = _make_case(posix_sandbox, phase, positions)

    result = _recover(case)

    _require_success(result)
    assert result.stdout.strip() == phase
    _assert_recovered(case, "new")


@pytest.mark.parametrize(
    ("phase", "positions", "kill_phase", "interrupted_positions", "final_marker"),
    (
        pytest.param(
            "prepared",
            "live:old,candidate:new",
            "after-candidate-delete",
            {".venv": "old"},
            "old",
            id="prepared-after-candidate-delete",
        ),
        pytest.param(
            "prepared",
            "candidate:new,rollback:old",
            "after-old-restore",
            {".venv": "old", f".venv.candidate.{TXID}": "new"},
            "old",
            id="prepared-after-old-restore",
        ),
        pytest.param(
            "prepared",
            "live:new,rollback:old",
            "after-live-quarantine",
            {
                f".venv.candidate.{TXID}": "new",
                f".venv.rollback-{TXID}": "old",
            },
            "old",
            id="prepared-after-live-quarantine",
        ),
        pytest.param(
            "candidate",
            "live:new,rollback:old",
            "after-rollback-delete",
            {".venv": "new"},
            "new",
            id="candidate-after-rollback-delete",
        ),
        pytest.param(
            "committed",
            "live:new,rollback:old",
            "after-rollback-delete",
            {".venv": "new"},
            "new",
            id="committed-after-rollback-delete",
        ),
    ),
)
def test_recovery_is_idempotent_after_kill_at_each_mutation_boundary(
    posix_sandbox,
    phase: str,
    positions: str,
    kill_phase: str,
    interrupted_positions: dict[str, str],
    final_marker: str,
):
    case = _make_case(posix_sandbox, phase, positions)

    interrupted = _recover(case, kill_phase)

    assert interrupted.returncode != 0
    interrupted_state = _snapshot(case)
    assert {
        name: metadata["marker"]
        for name, metadata in interrupted_state["entries"].items()
    } == interrupted_positions
    assert interrupted_state["journal"] is not None

    resumed = _recover(case)

    _require_success(resumed)
    _assert_recovered(case, final_marker)


@pytest.mark.parametrize(
    ("mutation", "error_fragment"),
    (
        pytest.param(
            "corrupt-journal", "journal cannot be parsed", id="corrupt-journal"
        ),
        pytest.param(
            "wrong-journal-mode",
            "journal ownership or mode is invalid",
            id="wrong-journal-mode",
        ),
        pytest.param(
            "extra-artifact",
            "unrecorded swap artifacts exist",
            id="extra-artifact",
        ),
        pytest.param(
            "candidate-inode-mismatch",
            "candidate environment has an unexpected identity",
            id="inode-mismatch",
        ),
    ),
)
def test_unsafe_journal_or_artifact_state_fails_closed_without_mutation(
    posix_sandbox, mutation: str, error_fragment: str
):
    case = _make_case(
        posix_sandbox, "prepared", "live:old,candidate:new"
    )
    _mutate(case, mutation)
    before = _snapshot(case)

    result = _recover(case)

    assert result.returncode != 0
    assert error_fragment in result.stderr
    assert _snapshot(case) == before


def test_rollback_artifact_without_journal_fails_closed(posix_sandbox):
    case = _make_case(
        posix_sandbox, "prepared", "live:new,rollback:old"
    )
    _mutate(case, "remove-journal")
    before = _snapshot(case)

    result = _recover(case)

    assert result.returncode != 0
    assert "rollback artifacts exist without a journal" in result.stderr
    assert _snapshot(case) == before


def test_missing_live_environment_is_recovered_before_runtime_python_check(
    posix_sandbox,
):
    managed_branch = INSTALLER_SOURCE.index(
        'if [[ "${runtime_python}" == "${runtime_venv}/bin/python" ]]; then'
    )
    recovery_call = INSTALLER_SOURCE.index(
        "  recover_venv_swap \\", managed_branch
    )
    runtime_python_check = INSTALLER_SOURCE.index(
        '[[ -x "${runtime_python}" ]]', recovery_call
    )
    assert '"${bootstrap_python_resolved}"' in RECOVERY_OPENER
    assert recovery_call < runtime_python_check

    case = _make_case(
        posix_sandbox, "prepared", "candidate:new,rollback:old"
    )
    assert ".venv" not in _snapshot(case)["entries"]

    result = _recover(case)

    _require_success(result)
    _assert_recovered(case, "old")
