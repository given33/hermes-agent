#!/usr/bin/env python3
"""Gate systemd starts while a Hermes release candidate is uncommitted.

The public installer keeps a root-owned candidate marker until every release
gate has passed.  While that marker exists, systemd may start the candidate
only when a root-owned lease proves that the exact installer process which
published it is still alive in the same boot.  This closes the crash/reboot
window between publishing the systemd ready sentinel and committing a release.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path


MARKER_KEYS = {
    "schema",
    "phase",
    "txid",
    "target_root",
    "target_identity",
    "runtime_home",
    "runtime_identity",
    "service",
    "version",
    "commit",
}
LEASE_KEYS = MARKER_KEYS | {
    "installer_pid",
    "installer_starttime",
    "boot_id",
}
MAX_STATE_BYTES = 16 * 1024


class GuardError(RuntimeError):
    """A release state file failed strict validation."""


def _identity(metadata: os.stat_result) -> str:
    return f"{metadata.st_dev}:{metadata.st_ino}"


def _directory_identity(path: Path) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise GuardError(f"cannot open directory {path}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        published = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or _identity(published) != _identity(metadata)
            or str(path.resolve(strict=True)) != str(path)
        ):
            raise GuardError(f"directory identity changed: {path}")
        return _identity(metadata)
    finally:
        os.close(descriptor)


def _open_state_directory(
    marker: Path,
    lease: Path,
    expected_identity: str,
) -> int:
    if (
        not marker.is_absolute()
        or not lease.is_absolute()
        or marker.parent != lease.parent
        or marker.name in {"", ".", ".."}
        or lease.name in {"", ".", ".."}
        or marker.name == lease.name
        or str(Path(os.path.normpath(str(marker)))) != str(marker)
        or str(Path(os.path.normpath(str(lease)))) != str(lease)
        or re.fullmatch(r"[0-9]+:[0-9]+", expected_identity) is None
    ):
        raise GuardError("release state paths or directory identity are invalid")
    parent = marker.parent
    try:
        descriptor = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise GuardError(f"cannot open release state directory: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        published = parent.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or mode & 0o022
            or _identity(metadata) != expected_identity
            or _identity(published) != expected_identity
            or str(parent.resolve(strict=True)) != str(parent)
        ):
            raise GuardError(
                "release state directory identity, ownership, or mode changed"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_root_json(
    directory_fd: int,
    name: str,
    *,
    missing_ok: bool = False,
):
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise GuardError(f"state file is missing: {name}") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > MAX_STATE_BYTES
        ):
            raise GuardError(f"unsafe ownership, mode, type, or size: {name}")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
            payload = json.load(stream)
        published = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(published) != _identity(metadata):
            raise GuardError(f"state file changed while reading: {name}")
        return payload, _identity(metadata)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GuardError(f"cannot read state file {name}: {error}") from error
    finally:
        os.close(descriptor)


def _validate_common(payload, *, keys: set[str], target: Path, target_identity: str, service: str):
    if not isinstance(payload, dict) or set(payload) != keys:
        raise GuardError("release state schema fields are invalid")
    for key, value in payload.items():
        if key == "installer_pid":
            if not isinstance(value, int) or isinstance(value, bool) or value <= 1:
                raise GuardError("installer pid is invalid")
        elif not isinstance(value, str):
            raise GuardError(f"release state field {key} must be a string")
    if payload["schema"] not in {
        "hermes.release-candidate.v1",
        "hermes.release-start-lease.v1",
    }:
        raise GuardError("release state schema is unsupported")
    if payload["phase"] != "candidate":
        raise GuardError("release state phase is invalid")
    if re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", service) is None:
        raise GuardError("service name is invalid")
    if re.fullmatch(r"[0-9a-f]{32}", payload["txid"]) is None:
        raise GuardError("release transaction id is invalid")
    if re.fullmatch(r"[0-9]+:[0-9]+", payload["target_identity"]) is None:
        raise GuardError("target identity is invalid")
    if re.fullmatch(r"[0-9]+:[0-9]+", payload["runtime_identity"]) is None:
        raise GuardError("runtime identity is invalid")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", payload["version"]) is None:
        raise GuardError("release version is invalid")
    if re.fullmatch(r"[0-9a-f]{40}", payload["commit"]) is None:
        raise GuardError("release commit is invalid")
    if keys == LEASE_KEYS:
        if re.fullmatch(r"[0-9]+", payload["installer_starttime"]) is None:
            raise GuardError("installer process start time is invalid")
        if (
            re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                r"[0-9a-f]{4}-[0-9a-f]{12}",
                payload["boot_id"],
            )
            is None
        ):
            raise GuardError("lease boot id is invalid")
    if (
        payload["target_root"] != str(target)
        or payload["target_identity"] != target_identity
        or payload["service"] != service
        or _directory_identity(target) != target_identity
    ):
        raise GuardError("target root or service identity changed")
    runtime_home = Path(payload["runtime_home"])
    if not runtime_home.is_absolute() or str(runtime_home) != payload["runtime_home"]:
        raise GuardError("runtime home path is invalid")
    if _directory_identity(runtime_home) != payload["runtime_identity"]:
        raise GuardError("runtime home identity changed")
    return payload


def _marker(
    directory_fd: int,
    name: str,
    target: Path,
    target_identity: str,
    service: str,
    *,
    missing_ok: bool = False,
):
    result = _read_root_json(directory_fd, name, missing_ok=missing_ok)
    if result is None:
        return None
    payload, identity = result
    payload = _validate_common(
        payload,
        keys=MARKER_KEYS,
        target=target,
        target_identity=target_identity,
        service=service,
    )
    if payload["schema"] != "hermes.release-candidate.v1":
        raise GuardError("candidate marker schema is invalid")
    return payload, identity


def _process_starttime(pid: int) -> str:
    value = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    closing = value.rfind(")")
    if closing < 0:
        raise GuardError("installer process stat is malformed")
    fields_after_command = value[closing + 2 :].split()
    if len(fields_after_command) <= 19:
        raise GuardError("installer process stat is incomplete")
    if fields_after_command[0] in {"Z", "X", "x"}:
        raise GuardError("installer process is no longer runnable")
    return fields_after_command[19]


def _boot_id() -> str:
    value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    if (
        re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}",
            value,
        )
        is None
    ):
        raise GuardError("kernel boot id is invalid")
    return value


def _write_root_json(directory_fd: int, name: str, payload: dict) -> None:
    temporary = f".{name}.new-{uuid.uuid4().hex}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise


def _remove_matching_lease(
    directory_fd: int,
    name: str,
    marker_payload,
    target: Path,
    target_identity: str,
    service: str,
    expected_release: dict,
) -> None:
    result = _read_root_json(directory_fd, name, missing_ok=True)
    if result is None:
        os.fsync(directory_fd)
        return
    lease, lease_identity = result
    lease = _validate_common(
        lease,
        keys=LEASE_KEYS,
        target=target,
        target_identity=target_identity,
        service=service,
    )
    if lease["schema"] != "hermes.release-start-lease.v1":
        raise GuardError("start lease schema is invalid")
    if marker_payload is not None:
        for key in MARKER_KEYS - {"schema"}:
            if lease[key] != marker_payload[key]:
                raise GuardError(f"start lease does not match marker field {key}")
    for key, value in expected_release.items():
        if lease[key] != value:
            raise GuardError(f"start lease does not match release field {key}")
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if _identity(current) != lease_identity:
        raise GuardError("start lease changed before removal")
    os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _authorization_payload(
    marker_path: Path,
    lease_path: Path,
    target: Path,
    target_identity: str,
    service: str,
    state_directory_identity: str,
):
    directory_fd = _open_state_directory(
        marker_path,
        lease_path,
        state_directory_identity,
    )
    try:
        marker_result = _marker(
            directory_fd,
            marker_path.name,
            target,
            target_identity,
            service,
            missing_ok=True,
        )
        if marker_result is None:
            return None
        marker, _ = marker_result
        lease, _ = _read_root_json(directory_fd, lease_path.name)
        lease = _validate_common(
            lease,
            keys=LEASE_KEYS,
            target=target,
            target_identity=target_identity,
            service=service,
        )
        if lease["schema"] != "hermes.release-start-lease.v1":
            raise GuardError("start lease schema is invalid")
        for key in MARKER_KEYS - {"schema"}:
            if lease[key] != marker[key]:
                raise GuardError(f"start lease does not match marker field {key}")
        if lease["boot_id"] != _boot_id():
            raise GuardError("start lease belongs to another boot")
        if lease["installer_starttime"] != _process_starttime(
            lease["installer_pid"]
        ):
            raise GuardError("start lease installer process is no longer alive")
        return marker, lease
    finally:
        os.close(directory_fd)


def check(
    marker_path: Path,
    lease_path: Path,
    target: Path,
    target_identity: str,
    service: str,
    state_directory_identity: str,
) -> None:
    _authorization_payload(
        marker_path,
        lease_path,
        target,
        target_identity,
        service,
        state_directory_identity,
    )


def _marker_belongs_to_release(
    marker_path: Path,
    lease_path: Path,
    target: Path,
    target_identity: str,
    service: str,
    state_directory_identity: str,
    expected_release: dict,
) -> bool:
    directory_fd = _open_state_directory(
        marker_path,
        lease_path,
        state_directory_identity,
    )
    try:
        marker_result = _marker(
            directory_fd,
            marker_path.name,
            target,
            target_identity,
            service,
            missing_ok=True,
        )
        if marker_result is None:
            return False
        marker, _ = marker_result
        return all(marker[key] == value for key, value in expected_release.items())
    finally:
        os.close(directory_fd)


def _validate_systemctl(path: Path) -> str:
    if (
        not path.is_absolute()
        or path.name != "systemctl"
        or str(Path(os.path.normpath(str(path)))) != str(path)
        or str(path.resolve(strict=True)) != str(path)
    ):
        raise GuardError("systemctl path is invalid")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        published = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or (stat.S_IMODE(metadata.st_mode) & 0o111) == 0
            or _identity(published) != _identity(metadata)
        ):
            raise GuardError("systemctl executable is unsafe")
    finally:
        os.close(descriptor)
    return str(path)


def _stop_service(systemctl: str, service: str) -> None:
    result = subprocess.run(
        [systemctl, "stop", service],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.strip()[-500:]
        raise GuardError(f"could not stop uncommitted service: {detail}")
    for _ in range(100):
        active = subprocess.run(
            [systemctl, "is-active", "--quiet", service],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if active.returncode != 0:
            return
        time.sleep(0.1)
    raise GuardError("uncommitted service remained active after stop")


def watch(
    marker_path: Path,
    lease_path: Path,
    target: Path,
    target_identity: str,
    service: str,
    state_directory_identity: str,
    runtime_home: str,
    txid: str,
    version: str,
    commit: str,
    installer_pid: int,
    systemctl_path: str,
) -> None:
    expected_release = {
        "runtime_home": runtime_home,
        "txid": txid,
        "version": version,
        "commit": commit,
    }
    systemctl = _validate_systemctl(Path(systemctl_path))
    expected_installer_starttime = None
    while True:
        try:
            authorization = _authorization_payload(
                marker_path,
                lease_path,
                target,
                target_identity,
                service,
                state_directory_identity,
            )
        except (GuardError, FileNotFoundError, OSError, ValueError):
            try:
                belongs = _marker_belongs_to_release(
                    marker_path,
                    lease_path,
                    target,
                    target_identity,
                    service,
                    state_directory_identity,
                    expected_release,
                )
            except (GuardError, FileNotFoundError, OSError, ValueError):
                # An unreadable/corrupt state directory cannot prove commit or
                # supersession. Stop the candidate rather than fail open.
                belongs = True
            if belongs:
                _stop_service(systemctl, service)
            return
        if authorization is None:
            if expected_installer_starttime is None:
                return
            try:
                current_starttime = _process_starttime(installer_pid)
            except (GuardError, FileNotFoundError, OSError):
                return
            if current_starttime != expected_installer_starttime:
                return
            # Marker absence is committed, but the installer must first
            # daemon-reload a drop-in without BindsTo before this watchdog may
            # exit. Staying alive here keeps the service attached during that
            # final fail-safe transition.
            time.sleep(0.1)
            continue
        marker, lease = authorization
        if not all(marker[key] == value for key, value in expected_release.items()):
            return
        if lease["installer_pid"] != installer_pid:
            _stop_service(systemctl, service)
            return
        expected_installer_starttime = lease["installer_starttime"]
        time.sleep(0.1)


def write_lease(
    marker_path: Path,
    lease_path: Path,
    target: Path,
    target_identity: str,
    service: str,
    runtime_home: str,
    txid: str,
    version: str,
    commit: str,
    installer_pid: int,
    state_directory_identity: str,
) -> None:
    directory_fd = _open_state_directory(
        marker_path,
        lease_path,
        state_directory_identity,
    )
    try:
        marker, _ = _marker(
            directory_fd,
            marker_path.name,
            target,
            target_identity,
            service,
        )
        expected = {
            "runtime_home": runtime_home,
            "txid": txid,
            "version": version,
            "commit": commit,
        }
        for key, value in expected.items():
            if marker[key] != value:
                raise GuardError(f"candidate marker does not match {key}")
        lease = dict(marker)
        lease.update(
            schema="hermes.release-start-lease.v1",
            installer_pid=installer_pid,
            installer_starttime=_process_starttime(installer_pid),
            boot_id=_boot_id(),
        )
        _write_root_json(directory_fd, lease_path.name, lease)
    finally:
        os.close(directory_fd)


def remove_lease(
    marker_path: Path,
    lease_path: Path,
    target: Path,
    target_identity: str,
    service: str,
    runtime_home: str,
    txid: str,
    version: str,
    commit: str,
    state_directory_identity: str,
) -> None:
    directory_fd = _open_state_directory(
        marker_path,
        lease_path,
        state_directory_identity,
    )
    try:
        marker_result = _marker(
            directory_fd,
            marker_path.name,
            target,
            target_identity,
            service,
            missing_ok=True,
        )
        expected = {
            "runtime_home": runtime_home,
            "txid": txid,
            "version": version,
            "commit": commit,
        }
        marker = None
        if marker_result is not None:
            marker, _ = marker_result
            for key, value in expected.items():
                if marker[key] != value:
                    raise GuardError(f"candidate marker does not match {key}")
        _remove_matching_lease(
            directory_fd,
            lease_path.name,
            marker,
            target,
            target_identity,
            service,
            expected,
        )
    finally:
        os.close(directory_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("check", "watch", "write-lease", "remove-lease"),
    )
    parser.add_argument("marker")
    parser.add_argument("lease")
    parser.add_argument("target_root")
    parser.add_argument("target_identity")
    parser.add_argument("service")
    parser.add_argument("state_directory_identity")
    parser.add_argument("extra", nargs="*")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    marker = Path(arguments.marker)
    lease = Path(arguments.lease)
    target = Path(arguments.target_root)
    try:
        if arguments.action == "check":
            if arguments.extra:
                raise GuardError("check received unexpected arguments")
            check(
                marker,
                lease,
                target,
                arguments.target_identity,
                arguments.service,
                arguments.state_directory_identity,
            )
        elif arguments.action == "watch":
            if len(arguments.extra) != 6:
                raise GuardError(
                    "watch requires release identity, installer pid, and systemctl"
                )
            runtime_home, txid, version, commit, pid, systemctl = arguments.extra
            watch(
                marker,
                lease,
                target,
                arguments.target_identity,
                arguments.service,
                arguments.state_directory_identity,
                runtime_home,
                txid,
                version,
                commit,
                int(pid),
                systemctl,
            )
        elif arguments.action == "write-lease":
            if len(arguments.extra) != 5:
                raise GuardError("write-lease requires release identity and installer pid")
            runtime_home, txid, version, commit, pid = arguments.extra
            write_lease(
                marker,
                lease,
                target,
                arguments.target_identity,
                arguments.service,
                runtime_home,
                txid,
                version,
                commit,
                int(pid),
                arguments.state_directory_identity,
            )
        else:
            if len(arguments.extra) != 4:
                raise GuardError("remove-lease requires the release identity")
            remove_lease(
                marker,
                lease,
                target,
                arguments.target_identity,
                arguments.service,
                *arguments.extra,
                arguments.state_directory_identity,
            )
    except (GuardError, FileNotFoundError, OSError, ValueError) as error:
        print(f"candidate-start-guard: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
