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
VERSION = "1.2.3"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
SERVICE = "hermes-agent.service"


def _extract_function_python_heredoc(
    shell_source: str, function_name: str
) -> tuple[str, str]:
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
        raise AssertionError(f"unterminated {delimiter!r} heredoc in {function_name}")
    raise AssertionError(f"Python heredoc not found in {function_name}")


INSTALLER_SOURCE = INSTALLER.read_text(encoding="utf-8")
MARKER_SOURCE, MARKER_OPENER = _extract_function_python_heredoc(
    INSTALLER_SOURCE, "release_candidate_marker_action"
)


@dataclass(frozen=True)
class _PrivilegedPython:
    prefix: tuple[str, ...]
    executable: str

    def run(self, source: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.prefix, self.executable, "-", *arguments],
            input=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )


@dataclass(frozen=True)
class _MarkerCase:
    runtime: _PrivilegedPython
    sandbox: str
    marker: str
    target: str
    target_identity: str
    runtime_home: str


_CREATE_SANDBOX = r"""
import tempfile

print(tempfile.mkdtemp(prefix="hermes-release-marker-test-", dir="/tmp"))
"""


_REMOVE_SANDBOX = r"""
import pathlib
import shutil
import sys

path = pathlib.Path(sys.argv[1])
if path.parent != pathlib.Path("/tmp") or not path.name.startswith(
    "hermes-release-marker-test-"
):
    raise RuntimeError(f"refusing unsafe test cleanup: {path}")
shutil.rmtree(path)
"""


_SETUP_CASE = r"""
import json
import pathlib
import sys

sandbox = pathlib.Path(sys.argv[1])
target = sandbox / "target"
target.mkdir()
runtime_home = target / "runtime"
runtime_home.mkdir()
marker_parent = sandbox / "release-state"
marker_parent.mkdir(mode=0o755)
marker = marker_parent / "candidate-pending.json"
target_metadata = target.stat(follow_symlinks=False)
print(json.dumps({
    "marker": str(marker),
    "target": str(target),
    "target_identity": f"{target_metadata.st_dev}:{target_metadata.st_ino}",
    "runtime_home": str(runtime_home),
}))
"""


_SNAPSHOT_CASE = r"""
import json
import pathlib
import stat
import sys

marker = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
runtime_home = pathlib.Path(sys.argv[3])

def identity(path):
    metadata = path.lstat()
    return f"{metadata.st_dev}:{metadata.st_ino}"

marker_state = None
if marker.exists() or marker.is_symlink():
    metadata = marker.lstat()
    marker_state = {
        "identity": identity(marker),
        "kind": (
            "file"
            if stat.S_ISREG(metadata.st_mode)
            else "symlink"
            if stat.S_ISLNK(metadata.st_mode)
            else "other"
        ),
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
        "text": marker.read_text(encoding="utf-8", errors="replace"),
    }

temporaries = {}
prefix = f".{marker.name}.new-"
for entry in marker.parent.iterdir():
    if entry.name.startswith(prefix):
        metadata = entry.lstat()
        temporaries[entry.name] = {
            "kind": "file" if stat.S_ISREG(metadata.st_mode) else "other",
            "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid,
            "text": entry.read_text(encoding="utf-8", errors="replace"),
        }

print(json.dumps({
    "marker": marker_state,
    "temporaries": temporaries,
    "target_identity": identity(target),
    "runtime_identity": identity(runtime_home),
}, sort_keys=True))
"""


_MUTATE_CASE = r"""
import pathlib
import sys

marker = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
runtime_home = pathlib.Path(sys.argv[3])
action = sys.argv[4]

if action in {"valid-temp", "bad-temp-mode"}:
    temporary = marker.with_name(f".{marker.name}.new-interrupted")
    temporary.write_text("temporary\n", encoding="utf-8")
    temporary.chmod(0o600 if action == "valid-temp" else 0o644)
    import os
    os.chown(temporary, 0, 0)
elif action == "wrong-marker-mode":
    marker.chmod(0o644)
elif action == "replace-runtime":
    runtime_home.replace(target.parent / "displaced-runtime")
    runtime_home.mkdir()
elif action == "replace-target":
    target.replace(target.parent / "displaced-target")
    target.mkdir()
    runtime_home.mkdir()
elif action == "symlink-marker":
    target_marker = marker.parent / "outside-marker"
    target_marker.write_text("not trusted\n", encoding="utf-8")
    marker.unlink()
    marker.symlink_to(target_marker)
else:
    raise RuntimeError(f"unknown mutation: {action}")
"""


def _require_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.fixture(scope="module")
def privileged_posix_python() -> _PrivilegedPython:
    if os.name == "nt":
        wsl = shutil.which("wsl.exe")
        if wsl is None:
            pytest.skip("WSL is required to exercise POSIX marker semantics")
        runtime = _PrivilegedPython((wsl, "--exec", "sudo", "-n"), "python3")
    elif hasattr(os, "geteuid") and os.geteuid() == 0:
        runtime = _PrivilegedPython((), sys.executable)
    else:
        sudo = shutil.which("sudo")
        if sudo is None:
            pytest.skip("root or passwordless sudo is required for marker tests")
        runtime = _PrivilegedPython((sudo, "-n"), sys.executable)

    probe = runtime.run(
        "import os; "
        "raise SystemExit(0 if os.geteuid() == 0 "
        "and hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW') else 1)\n"
    )
    if probe.returncode != 0:
        pytest.skip("a root POSIX Python with secure open flags is required")
    return runtime


@pytest.fixture
def marker_case(privileged_posix_python: _PrivilegedPython):
    created = privileged_posix_python.run(_CREATE_SANDBOX)
    _require_success(created)
    sandbox = created.stdout.strip()
    assert sandbox.startswith("/tmp/hermes-release-marker-test-")
    setup = privileged_posix_python.run(_SETUP_CASE, sandbox)
    _require_success(setup)
    details = json.loads(setup.stdout)
    case = _MarkerCase(
        privileged_posix_python,
        sandbox,
        details["marker"],
        details["target"],
        details["target_identity"],
        details["runtime_home"],
    )
    try:
        yield case
    finally:
        removed = privileged_posix_python.run(_REMOVE_SANDBOX, sandbox)
        _require_success(removed)


def _action(
    case: _MarkerCase,
    action: str,
    *,
    target_identity: str | None = None,
    service: str = SERVICE,
    runtime_home: str | None = None,
    txid: str = TXID,
    version: str = VERSION,
    commit: str = COMMIT,
) -> subprocess.CompletedProcess[str]:
    return case.runtime.run(
        MARKER_SOURCE,
        case.marker,
        action,
        case.target,
        target_identity or case.target_identity,
        service,
        case.runtime_home if runtime_home is None else runtime_home,
        txid,
        version,
        commit,
    )


def _snapshot(case: _MarkerCase) -> dict[str, object]:
    result = case.runtime.run(
        _SNAPSHOT_CASE, case.marker, case.target, case.runtime_home
    )
    _require_success(result)
    return json.loads(result.stdout)


def _mutate(case: _MarkerCase, action: str) -> None:
    result = case.runtime.run(
        _MUTATE_CASE,
        case.marker,
        case.target,
        case.runtime_home,
        action,
    )
    _require_success(result)


def test_marker_shell_protocol_orders_all_durable_release_transitions():
    source = INSTALLER_SOURCE
    function_start = source.index("release_candidate_marker_action() {")
    function_end = source.index("\nPY\n}", function_start)
    function = source[function_start:function_end]

    assert "hermes.release-candidate.v1" in function
    assert 'payload["phase"] != "candidate"' in function
    assert "metadata.st_uid != 0" in function
    assert "stat.S_IMODE(metadata.st_mode) != 0o600" in function
    assert "os.O_EXCL | os.O_NOFOLLOW" in function
    marker_file_fsync = function.index("os.fsync(stream.fileno())")
    marker_replace = function.index("os.replace(temporary, marker)")
    marker_dir_fsync = function.index("fsync_directory(parent)", marker_replace)
    assert marker_file_fsync < marker_replace < marker_dir_fsync
    marker_unlink = function.index("marker.unlink()")
    remove_dir_fsync = function.index("fsync_directory(parent)", marker_unlink)
    assert marker_unlink < remove_dir_fsync

    txid_generated = source.index(
        'release_candidate_txid="$("${bootstrap_python_resolved}" -I -c'
    )
    boundary = source.index("runtime_candidate_started=1", txid_generated)
    marker_write = source.index("release_candidate_marker_action write", boundary)
    # The external-runtime path keeps this generated transaction id; clearing
    # it before marker publication would make the helper reject its own write.
    assert 'release_candidate_txid=""' not in source[txid_generated:marker_write]
    candidate_journal = source.index("write_venv_swap_journal candidate", marker_write)
    ready = source.index("publish_dispatcher_ready", candidate_journal)
    start = source.index('systemctl start "${service}"', ready)
    committed = source.index("write_venv_swap_journal committed", start)
    marker_remove = source.index("release_candidate_marker_action remove", committed)
    installed = source.index("installed=1", marker_remove)
    assert (
        txid_generated
        < boundary
        < marker_write
        < candidate_journal
        < ready
        < start
        < committed
        < marker_remove
        < installed
    )

    marker_recovery = source.index("release_marker_recovery_required=0")
    marker_inspect = source.index(
        "release_candidate_marker_action inspect", marker_recovery
    )
    runtime_check = source.index('[[ -x "${runtime_python}" ]]', marker_inspect)
    assert marker_recovery < marker_inspect < runtime_check
    recovery = source[marker_recovery:marker_inspect]
    assert 'systemctl stop "${service}"' in recovery
    assert ".new-*" in recovery
    assert "early_recovery_restart_allowed=0" in recovery
    assert "present|interrupted" in source[marker_inspect:runtime_check]
    assert "release_retry_stopped=1" in source[marker_inspect:runtime_check]


def test_write_inspect_validate_and_remove_round_trip(marker_case: _MarkerCase):
    written = _action(marker_case, "write")
    _require_success(written)
    assert written.stdout.strip() == "present"

    state = _snapshot(marker_case)
    marker = state["marker"]
    assert marker["kind"] == "file"
    assert marker["uid"] == 0
    assert marker["mode"] == 0o600
    assert state["temporaries"] == {}
    payload = json.loads(marker["text"])
    assert payload == {
        "schema": "hermes.release-candidate.v1",
        "phase": "candidate",
        "txid": TXID,
        "target_root": marker_case.target,
        "target_identity": marker_case.target_identity,
        "runtime_home": marker_case.runtime_home,
        "runtime_identity": state["runtime_identity"],
        "service": SERVICE,
        "version": VERSION,
        "commit": COMMIT,
    }

    inspected = _action(
        marker_case,
        "inspect",
        runtime_home="",
        txid="",
        version="",
        commit="",
    )
    _require_success(inspected)
    assert inspected.stdout.strip() == "present"

    validated = _action(marker_case, "validate")
    _require_success(validated)
    assert validated.stdout.strip() == "present"

    removed = _action(marker_case, "remove")
    _require_success(removed)
    assert removed.stdout.strip() == "absent"
    assert _snapshot(marker_case)["marker"] is None

    absent = _action(
        marker_case,
        "inspect",
        runtime_home="",
        txid="",
        version="",
        commit="",
    )
    _require_success(absent)
    assert absent.stdout.strip() == "absent"


def test_interrupted_temporary_marker_is_reported_then_cleaned_by_write(
    marker_case: _MarkerCase,
):
    _mutate(marker_case, "valid-temp")
    assert _snapshot(marker_case)["temporaries"]

    inspected = _action(
        marker_case,
        "inspect",
        runtime_home="",
        txid="",
        version="",
        commit="",
    )

    _require_success(inspected)
    assert inspected.stdout.strip() == "interrupted"
    assert _snapshot(marker_case)["temporaries"]

    written = _action(marker_case, "write")
    _require_success(written)
    assert written.stdout.strip() == "present"
    assert _snapshot(marker_case)["temporaries"] == {}


def test_unsafe_temporary_marker_fails_closed(marker_case: _MarkerCase):
    _mutate(marker_case, "bad-temp-mode")
    before = _snapshot(marker_case)["temporaries"]

    inspected = _action(
        marker_case,
        "inspect",
        runtime_home="",
        txid="",
        version="",
        commit="",
    )

    assert inspected.returncode != 0
    assert "temporary marker ownership, mode, or type is invalid" in inspected.stderr
    assert _snapshot(marker_case)["temporaries"] == before


def test_unsafe_marker_permissions_fail_closed_without_replacement(
    marker_case: _MarkerCase,
):
    _require_success(_action(marker_case, "write"))
    _mutate(marker_case, "wrong-marker-mode")
    before = _snapshot(marker_case)["marker"]

    inspected = _action(marker_case, "inspect")

    assert inspected.returncode != 0
    assert "ownership, mode, type, or size is invalid" in inspected.stderr
    assert _snapshot(marker_case)["marker"] == before


def test_marker_symlink_fails_closed_without_following_target(marker_case: _MarkerCase):
    _require_success(_action(marker_case, "write"))
    _mutate(marker_case, "symlink-marker")

    inspected = _action(marker_case, "inspect")

    assert inspected.returncode != 0
    assert "ownership, mode, type, or size is invalid" in inspected.stderr
    assert _snapshot(marker_case)["marker"]["kind"] == "symlink"


def test_marker_identity_arguments_are_bound_on_validate_and_remove(
    marker_case: _MarkerCase,
):
    _require_success(_action(marker_case, "write"))

    wrong_version = _action(marker_case, "validate", version="9.9.9")
    assert wrong_version.returncode != 0
    assert "release version changed" in wrong_version.stderr

    wrong_service = _action(marker_case, "remove", service="other.service")
    assert wrong_service.returncode != 0
    assert "target root or service identity changed" in wrong_service.stderr
    assert _snapshot(marker_case)["marker"] is not None


def test_runtime_home_identity_drift_fails_closed(marker_case: _MarkerCase):
    _require_success(_action(marker_case, "write"))
    marker_before = _snapshot(marker_case)["marker"]
    _mutate(marker_case, "replace-runtime")

    inspected = _action(marker_case, "inspect")

    assert inspected.returncode != 0
    assert "runtime home identity changed" in inspected.stderr
    assert _snapshot(marker_case)["marker"] == marker_before


def test_target_root_identity_drift_fails_closed(marker_case: _MarkerCase):
    _require_success(_action(marker_case, "write"))
    marker_before = _snapshot(marker_case)["marker"]
    _mutate(marker_case, "replace-target")
    current_identity = _snapshot(marker_case)["target_identity"]

    inspected = _action(
        marker_case,
        "inspect",
        target_identity=current_identity,
    )

    assert inspected.returncode != 0
    assert "target root or service identity changed" in inspected.stderr
    assert _snapshot(marker_case)["marker"] == marker_before
