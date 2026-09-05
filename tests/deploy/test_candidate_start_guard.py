from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "deploy" / "public" / "candidate-start-guard.py"
TXID = "0123456789abcdef0123456789abcdef"
VERSION = "1.2.3"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
SERVICE = "hermes-agent.service"


@dataclass(frozen=True)
class _PrivilegedPython:
    prefix: tuple[str, ...]
    unprivileged_prefix: tuple[str, ...]
    executable: str
    guard: str

    def run_source(
        self, source: str, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
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

    def run_guard(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.prefix, self.executable, "-I", self.guard, *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

    def run_guard_unprivileged(
        self, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                *self.unprivileged_prefix,
                self.executable,
                "-I",
                self.guard,
                *arguments,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )


@dataclass(frozen=True)
class _GuardCase:
    runtime: _PrivilegedPython
    sandbox: str
    marker: str
    lease: str
    target: str
    target_identity: str
    runtime_home: str
    state_directory_identity: str
    installer_pid: int
    installer_starttime: str
    systemctl: str
    systemctl_log: str


_CREATE_CASE = r"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

sandbox = pathlib.Path(
    tempfile.mkdtemp(prefix="hermes-candidate-guard-test-", dir="/tmp")
)
sandbox.chmod(0o755)
target = sandbox / "target"
target.mkdir()
runtime_home = target / "runtime"
runtime_home.mkdir()
state = sandbox / "release-state"
state.mkdir(mode=0o755)
marker = state / "candidate-pending.json"
lease = state / "candidate-start-lease.json"
fake_bin = sandbox / "fake-bin"
fake_bin.mkdir(mode=0o755)
systemctl = fake_bin / "systemctl"
systemctl_log = sandbox / "systemctl.log"
systemctl.write_text(
    f'''#!{sys.executable}
import pathlib
import sys

with pathlib.Path({str(systemctl_log)!r}).open("a", encoding="utf-8") as stream:
    stream.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == ["is-active"]:
    raise SystemExit(3)
''',
    encoding="utf-8",
)
systemctl.chmod(0o755)
os.chown(systemctl, 0, 0)

def identity(path):
    metadata = path.lstat()
    return f"{metadata.st_dev}:{metadata.st_ino}"

installer = subprocess.Popen(
    ["sleep", "300"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
stat_text = pathlib.Path(f"/proc/{installer.pid}/stat").read_text(encoding="ascii")
closing = stat_text.rfind(")")
installer_starttime = stat_text[closing + 2:].split()[19]
payload = {
    "schema": "hermes.release-candidate.v1",
    "phase": "candidate",
    "txid": sys.argv[1],
    "target_root": str(target),
    "target_identity": identity(target),
    "runtime_home": str(runtime_home),
    "runtime_identity": identity(runtime_home),
    "service": sys.argv[2],
    "version": sys.argv[3],
    "commit": sys.argv[4],
}
marker.write_text(
    json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
marker.chmod(0o600)
os.chown(marker, 0, 0)
print(json.dumps({
    "sandbox": str(sandbox),
    "marker": str(marker),
    "lease": str(lease),
    "target": str(target),
    "target_identity": identity(target),
    "runtime_home": str(runtime_home),
    "state_directory_identity": identity(state),
    "installer_pid": installer.pid,
    "installer_starttime": installer_starttime,
    "systemctl": str(systemctl),
    "systemctl_log": str(systemctl_log),
}))
"""


_REMOVE_CASE = r"""
import os
import pathlib
import shutil
import signal
import sys
import time

sandbox = pathlib.Path(sys.argv[1])
pid = int(sys.argv[2])
expected_starttime = sys.argv[3]
if sandbox.parent != pathlib.Path("/tmp") or not sandbox.name.startswith(
    "hermes-candidate-guard-test-"
):
    raise RuntimeError(f"refusing unsafe test cleanup: {sandbox}")
stat_path = pathlib.Path(f"/proc/{pid}/stat")
try:
    stat_text = stat_path.read_text(encoding="ascii")
except FileNotFoundError:
    pass
else:
    closing = stat_text.rfind(")")
    actual_starttime = stat_text[closing + 2:].split()[19]
    if actual_starttime != expected_starttime:
        raise RuntimeError("refusing to signal a reused process id")
    os.kill(pid, signal.SIGKILL)
    for _ in range(100):
        if not stat_path.exists():
            break
        time.sleep(0.01)
holder_file = sandbox / "zombie-holder.json"
if holder_file.is_file():
    import json
    holder = json.loads(holder_file.read_text(encoding="utf-8"))
    holder_pid = int(holder["pid"])
    holder_stat = pathlib.Path(f"/proc/{holder_pid}/stat")
    try:
        holder_text = holder_stat.read_text(encoding="ascii")
    except FileNotFoundError:
        pass
    else:
        closing = holder_text.rfind(")")
        actual = holder_text[closing + 2:].split()[19]
        if actual != holder["starttime"]:
            raise RuntimeError("refusing to signal a reused zombie-holder pid")
        os.kill(holder_pid, signal.SIGKILL)
shutil.rmtree(sandbox)
"""


_MUTATE_CASE = r"""
import json
import os
import pathlib
import signal
import sys
import time

marker = pathlib.Path(sys.argv[1])
lease = pathlib.Path(sys.argv[2])
target = pathlib.Path(sys.argv[3])
runtime_home = pathlib.Path(sys.argv[4])
pid = int(sys.argv[5])
expected_starttime = sys.argv[6]
action = sys.argv[7]

def rewrite(path, field, value):
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = value
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    os.chown(path, 0, 0)

if action == "remove-marker":
    marker.unlink()
elif action == "kill-installer":
    stat_path = pathlib.Path(f"/proc/{pid}/stat")
    stat_text = stat_path.read_text(encoding="ascii")
    closing = stat_text.rfind(")")
    if stat_text[closing + 2:].split()[19] != expected_starttime:
        raise RuntimeError("installer process identity changed")
    os.kill(pid, signal.SIGKILL)
    for _ in range(200):
        if not stat_path.exists():
            break
        time.sleep(0.01)
elif action == "wrong-starttime":
    rewrite(lease, "installer_starttime", str(int(expected_starttime) + 1))
elif action == "wrong-boot-id":
    rewrite(lease, "boot_id", "00000000-0000-0000-0000-000000000000")
elif action == "wrong-lease-mode":
    lease.chmod(0o644)
elif action == "runtime-identity-drift":
    runtime_home.replace(target.parent / "displaced-runtime")
    runtime_home.mkdir()
elif action == "wrong-lease-commit":
    rewrite(lease, "commit", sys.argv[8])
elif action == "state-directory-replaced":
    state = marker.parent
    state.replace(state.parent / "displaced-release-state")
    state.mkdir(mode=0o755)
elif action == "state-directory-missing":
    state = marker.parent
    state.replace(state.parent / "displaced-release-state")
elif action == "zombie-installer":
    import subprocess
    holder_file = target.parent / "zombie-holder.json"
    zombie_file = target.parent / "zombie-child.pid"
    holder_source = '''
import os
import pathlib
import sys
import time

child = os.fork()
if child == 0:
    os._exit(0)
pathlib.Path(sys.argv[1]).write_text(str(child), encoding="ascii")
time.sleep(300)
'''
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_source, str(zombie_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(200):
        if zombie_file.is_file():
            break
        time.sleep(0.01)
    zombie_pid = int(zombie_file.read_text(encoding="ascii"))
    zombie_stat = pathlib.Path(f"/proc/{zombie_pid}/stat").read_text(
        encoding="ascii"
    )
    closing = zombie_stat.rfind(")")
    zombie_fields = zombie_stat[closing + 2:].split()
    if zombie_fields[0] != "Z":
        raise RuntimeError("child did not enter zombie state")
    rewrite(lease, "installer_pid", zombie_pid)
    rewrite(lease, "installer_starttime", zombie_fields[19])
    holder_stat = pathlib.Path(f"/proc/{holder.pid}/stat").read_text(
        encoding="ascii"
    )
    closing = holder_stat.rfind(")")
    holder_file.write_text(
        json.dumps({
            "pid": holder.pid,
            "starttime": holder_stat[closing + 2:].split()[19],
        }),
        encoding="utf-8",
    )
else:
    raise RuntimeError(f"unknown mutation: {action}")
"""


_SNAPSHOT_CASE = r"""
import json
import pathlib
import stat
import sys

marker = pathlib.Path(sys.argv[1])
lease = pathlib.Path(sys.argv[2])

def snapshot(path):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    return {
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
        "identity": f"{metadata.st_dev}:{metadata.st_ino}",
        "payload": json.loads(path.read_text(encoding="utf-8")),
    }

print(json.dumps({"marker": snapshot(marker), "lease": snapshot(lease)}))
"""


_READ_LINES = r"""
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
print(json.dumps(path.read_text(encoding="utf-8").splitlines() if path.exists() else []))
"""


def _require_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stderr or result.stdout


def _wsl_path(path: Path) -> str:
    result = subprocess.run(
        ["wsl.exe", "wslpath", "-a", str(path).replace("\\", "/")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip("WSL path translation is unavailable")
    return result.stdout.strip()


@pytest.fixture(scope="module")
def privileged_posix_python() -> _PrivilegedPython:
    if os.name == "nt":
        wsl = shutil.which("wsl.exe")
        if wsl is None:
            pytest.skip("WSL is required to exercise POSIX start-guard semantics")
        runtime = _PrivilegedPython(
            (wsl, "--exec", "sudo", "-n"),
            (wsl, "--exec", "sudo", "-n", "-u", "nobody"),
            "python3",
            _wsl_path(GUARD),
        )
    elif hasattr(os, "geteuid") and os.geteuid() == 0:
        runtime = _PrivilegedPython(
            (), ("runuser", "-u", "nobody", "--"), sys.executable, str(GUARD)
        )
    else:
        sudo = shutil.which("sudo")
        if sudo is None:
            pytest.skip("root or passwordless sudo is required for start-guard tests")
        runtime = _PrivilegedPython(
            (sudo, "-n"),
            (sudo, "-n", "-u", "nobody"),
            sys.executable,
            str(GUARD),
        )

    probe = runtime.run_source(
        "import os; "
        "raise SystemExit(0 if os.geteuid() == 0 "
        "and hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW') else 1)\n"
    )
    if probe.returncode != 0:
        pytest.skip("a root POSIX Python with secure open flags is required")
    return runtime


@pytest.fixture
def guard_case(privileged_posix_python: _PrivilegedPython):
    created = privileged_posix_python.run_source(
        _CREATE_CASE, TXID, SERVICE, VERSION, COMMIT
    )
    _require_success(created)
    details = json.loads(created.stdout)
    case = _GuardCase(
        runtime=privileged_posix_python,
        sandbox=details["sandbox"],
        marker=details["marker"],
        lease=details["lease"],
        target=details["target"],
        target_identity=details["target_identity"],
        runtime_home=details["runtime_home"],
        state_directory_identity=details["state_directory_identity"],
        installer_pid=details["installer_pid"],
        installer_starttime=details["installer_starttime"],
        systemctl=details["systemctl"],
        systemctl_log=details["systemctl_log"],
    )
    assert case.sandbox.startswith("/tmp/hermes-candidate-guard-test-")
    try:
        yield case
    finally:
        removed = case.runtime.run_source(
            _REMOVE_CASE,
            case.sandbox,
            str(case.installer_pid),
            case.installer_starttime,
        )
        _require_success(removed)


def _base(case: _GuardCase) -> tuple[str, ...]:
    return (
        case.marker,
        case.lease,
        case.target,
        case.target_identity,
        SERVICE,
        case.state_directory_identity,
    )


def _check(case: _GuardCase) -> subprocess.CompletedProcess[str]:
    return case.runtime.run_guard("check", *_base(case))


def _write_lease(case: _GuardCase) -> subprocess.CompletedProcess[str]:
    return case.runtime.run_guard(
        "write-lease",
        *_base(case),
        case.runtime_home,
        TXID,
        VERSION,
        COMMIT,
        str(case.installer_pid),
    )


def _remove_lease(case: _GuardCase) -> subprocess.CompletedProcess[str]:
    return case.runtime.run_guard(
        "remove-lease",
        *_base(case),
        case.runtime_home,
        TXID,
        VERSION,
        COMMIT,
    )


def _spawn_watch(case: _GuardCase) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            *case.runtime.prefix,
            case.runtime.executable,
            "-I",
            case.runtime.guard,
            "watch",
            *_base(case),
            case.runtime_home,
            TXID,
            VERSION,
            COMMIT,
            str(case.installer_pid),
            case.systemctl,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _watch_output(watch: subprocess.Popen[str], timeout: float = 10) -> tuple[str, str]:
    try:
        return watch.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        watch.kill()
        stdout, stderr = watch.communicate(timeout=5)
        pytest.fail(
            f"candidate watchdog did not exit\nstdout:\n{stdout}\nstderr:\n{stderr}"
        )


def _assert_watch_running(watch: subprocess.Popen[str], duration: float) -> None:
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        returncode = watch.poll()
        if returncode is not None:
            stdout, stderr = watch.communicate()
            pytest.fail(
                f"candidate watchdog exited early with {returncode}\n"
                f"stdout:\n{stdout}\n"
                f"stderr:\n{stderr}"
            )
        time.sleep(0.05)


def _mutate(case: _GuardCase, action: str) -> None:
    result = case.runtime.run_source(
        _MUTATE_CASE,
        case.marker,
        case.lease,
        case.target,
        case.runtime_home,
        str(case.installer_pid),
        case.installer_starttime,
        action,
        OTHER_COMMIT,
    )
    _require_success(result)


def _snapshot(case: _GuardCase) -> dict[str, object]:
    result = case.runtime.run_source(
        _SNAPSHOT_CASE,
        case.marker,
        case.lease,
    )
    _require_success(result)
    return json.loads(result.stdout)


def _systemctl_log(case: _GuardCase) -> list[str]:
    result = case.runtime.run_source(_READ_LINES, case.systemctl_log)
    _require_success(result)
    return json.loads(result.stdout)


def test_guard_publishes_and_removes_root_state_durably():
    source = GUARD.read_text(encoding="utf-8")

    writer_start = source.index("def _write_root_json(")
    writer_end = source.index("\ndef _remove_matching_lease(", writer_start)
    writer = source[writer_start:writer_end]
    create = writer.index("os.O_EXCL | os.O_NOFOLLOW")
    ownership = writer.index("os.fchown(descriptor, 0, 0)", create)
    mode = writer.index("os.fchmod(descriptor, 0o600)", ownership)
    file_fsync = writer.index("os.fsync(stream.fileno())", mode)
    replace = writer.index("os.replace(", file_fsync)
    source_directory = writer.index("src_dir_fd=directory_fd", replace)
    destination_directory = writer.index("dst_dir_fd=directory_fd", source_directory)
    parent_fsync = writer.index("os.fsync(directory_fd)", destination_directory)
    assert (
        create
        < ownership
        < mode
        < file_fsync
        < replace
        < source_directory
        < destination_directory
        < parent_fsync
    )

    remover_start = writer_end
    remover_end = source.index("\ndef check(", remover_start)
    remover = source[remover_start:remover_end]
    reread = remover.index("_read_root_json(")
    identity_check = remover.index(
        'raise GuardError("start lease changed before removal")', reread
    )
    unlink = remover.index("os.unlink(name, dir_fd=directory_fd)", identity_check)
    parent_fsync = remover.index("os.fsync(directory_fd)", unlink)
    assert reread < identity_check < unlink < parent_fsync


def test_watch_stops_the_uncommitted_service_when_its_owner_dies(
    guard_case: _GuardCase,
):
    _require_success(_write_lease(guard_case))
    watch = _spawn_watch(guard_case)
    owner_killed = False
    try:
        _mutate(guard_case, "kill-installer")
        owner_killed = True
        stdout, stderr = _watch_output(watch)
        assert watch.returncode == 0, stderr or stdout
        assert stdout == ""
        assert stderr == ""
        assert _systemctl_log(guard_case) == [
            f"stop {SERVICE}",
            f"is-active --quiet {SERVICE}",
        ]
    finally:
        if not owner_killed:
            _mutate(guard_case, "kill-installer")
        if watch.poll() is None:
            _watch_output(watch)


def test_watch_stays_alive_after_commit_until_its_live_owner_exits(
    guard_case: _GuardCase,
):
    _require_success(_write_lease(guard_case))
    watch = _spawn_watch(guard_case)
    owner_killed = False
    try:
        # Let the watcher observe the exact live marker/lease pair before the
        # marker disappears. Once it has attached, marker absence is commit
        # authority but must not make the manager-side BindsTo peer vanish
        # while the committing installer is still alive.
        _assert_watch_running(watch, 1.0)
        _mutate(guard_case, "remove-marker")
        _assert_watch_running(watch, 0.4)
        assert _systemctl_log(guard_case) == []

        _mutate(guard_case, "kill-installer")
        owner_killed = True
        stdout, stderr = _watch_output(watch)
        assert watch.returncode == 0, stderr or stdout
        assert stdout == ""
        assert stderr == ""
        assert _systemctl_log(guard_case) == []
    finally:
        if not owner_killed:
            _mutate(guard_case, "kill-installer")
        if watch.poll() is None:
            _watch_output(watch)


def test_cli_allows_start_when_candidate_marker_is_absent(guard_case: _GuardCase):
    _require_success(_write_lease(guard_case))
    _mutate(guard_case, "remove-marker")
    assert _snapshot(guard_case)["lease"] is not None

    result = _check(guard_case)

    _require_success(result)


def test_cli_allows_exact_live_installer_lease(guard_case: _GuardCase):
    _require_success(_write_lease(guard_case))
    state = _snapshot(guard_case)

    assert state["lease"]["uid"] == 0
    assert state["lease"]["mode"] == 0o600
    assert state["lease"]["payload"]["schema"] == "hermes.release-start-lease.v1"
    assert state["lease"]["payload"]["installer_pid"] == guard_case.installer_pid
    assert (
        state["lease"]["payload"]["installer_starttime"]
        == guard_case.installer_starttime
    )
    _require_success(_check(guard_case))


def test_cli_rejects_dead_installer_pid(guard_case: _GuardCase):
    _require_success(_write_lease(guard_case))
    _mutate(guard_case, "kill-installer")

    result = _check(guard_case)

    assert result.returncode == 1
    assert "candidate-start-guard:" in result.stderr


def test_cli_rejects_wrong_installer_starttime(guard_case: _GuardCase):
    _require_success(_write_lease(guard_case))
    _mutate(guard_case, "wrong-starttime")

    result = _check(guard_case)

    assert result.returncode == 1
    assert "installer process is no longer alive" in result.stderr


def test_cli_rejects_lease_from_another_boot(guard_case: _GuardCase):
    _require_success(_write_lease(guard_case))
    _mutate(guard_case, "wrong-boot-id")

    result = _check(guard_case)

    assert result.returncode == 1
    assert "start lease belongs to another boot" in result.stderr


def test_cli_rejects_unsafe_lease_permissions(guard_case: _GuardCase):
    _require_success(_write_lease(guard_case))
    _mutate(guard_case, "wrong-lease-mode")

    result = _check(guard_case)

    assert result.returncode == 1
    assert "unsafe ownership, mode, type, or size" in result.stderr


def test_cli_rejects_runtime_identity_drift(guard_case: _GuardCase):
    _require_success(_write_lease(guard_case))
    _mutate(guard_case, "runtime-identity-drift")

    result = _check(guard_case)

    assert result.returncode == 1
    assert "runtime home identity changed" in result.stderr


def test_cli_rejects_lease_identity_mismatch_with_marker(guard_case: _GuardCase):
    _require_success(_write_lease(guard_case))
    _mutate(guard_case, "wrong-lease-commit")

    result = _check(guard_case)

    assert result.returncode == 1
    assert "start lease does not match marker field commit" in result.stderr


def test_cli_rejects_target_identity_argument_mismatch(guard_case: _GuardCase):
    _require_success(_write_lease(guard_case))

    result = guard_case.runtime.run_guard(
        "check",
        guard_case.marker,
        guard_case.lease,
        guard_case.target,
        "1:1",
        SERVICE,
        guard_case.state_directory_identity,
    )

    assert result.returncode == 1
    assert "target root or service identity changed" in result.stderr


@pytest.mark.parametrize(
    "mutation",
    ("state-directory-replaced", "state-directory-missing"),
)
def test_cli_rejects_state_directory_identity_loss_even_if_marker_looks_absent(
    guard_case: _GuardCase,
    mutation: str,
):
    _require_success(_write_lease(guard_case))
    _mutate(guard_case, mutation)

    result = _check(guard_case)

    assert result.returncode == 1
    assert "candidate-start-guard:" in result.stderr
    assert (
        "release state directory" in result.stderr
        or "cannot open release state directory" in result.stderr
    )


def test_cli_rejects_a_zombie_installer_process(guard_case: _GuardCase):
    _require_success(_write_lease(guard_case))
    _mutate(guard_case, "zombie-installer")

    result = _check(guard_case)

    assert result.returncode == 1
    assert "installer process is no longer runnable" in result.stderr


def test_root_owned_state_is_unreadable_to_the_service_user(
    guard_case: _GuardCase,
):
    _require_success(_write_lease(guard_case))

    result = guard_case.runtime.run_guard_unprivileged("check", *_base(guard_case))

    assert result.returncode == 1
    assert "candidate-start-guard:" in result.stderr
    assert "Permission denied" in result.stderr


def test_remove_lease_requires_full_marker_identity_and_then_removes_atomically(
    guard_case: _GuardCase,
):
    _require_success(_write_lease(guard_case))
    _mutate(guard_case, "wrong-lease-commit")
    mismatched = _snapshot(guard_case)["lease"]

    rejected = _remove_lease(guard_case)

    assert rejected.returncode == 1
    assert "start lease does not match marker field commit" in rejected.stderr
    assert _snapshot(guard_case)["lease"] == mismatched

    _require_success(_write_lease(guard_case))
    _require_success(_remove_lease(guard_case))
    assert _snapshot(guard_case)["lease"] is None
