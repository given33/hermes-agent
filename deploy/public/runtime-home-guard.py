#!/usr/bin/env python3
"""Harden the managed Hermes runtime-home directory boundary.

The collaboration installer runs as root while the Hermes gateway runs as an
unprivileged service account.  This helper owns the *directory topology* at
that privilege boundary; it deliberately does not read or write files below
the runtime-home leaf.

All path traversal is descriptor-relative and rejects symlinks.  The seal
journal is root-owned, durable, and idempotent so a killed installer can tell
whether the profile leaf is intentionally inaccessible to the service user.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import uuid
from typing import Any

try:  # POSIX-only modules; keep --help/import deterministic on Windows.
    import grp
    import pwd
except ImportError:  # pragma: no cover - exercised by Windows collection
    grp = None  # type: ignore[assignment]
    pwd = None  # type: ignore[assignment]


SCHEMA = "hermes.runtime-home-seal.v1"
MIGRATION_SCHEMA = "hermes.dispatcher-migration.v1"
MAX_JOURNAL_BYTES = 16 * 1024
IDENTITY_RE = re.compile(r"^[0-9]+:[0-9]+$")
TXID_RE = re.compile(r"^[0-9a-f]{32}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
JOURNAL_KEYS = {
    "schema",
    "phase",
    "leaf",
    "leaf_identity",
    "leaf_uid",
    "leaf_gid",
    "live_mode",
    "sealed_mode",
    "parent_identity",
    "journal_parent_identity",
}
JOURNAL_PHASES = {"planned", "sealed", "unsealing", "removing"}
MIGRATION_KEYS = {
    "schema",
    "phase",
    "txid",
    "source",
    "source_identity",
    "destination",
    "destination_identity",
    "version",
    "commit",
}
MIGRATION_FIELDS = {
    "phase",
    "txid",
    "source",
    "destination",
    "source_identity",
    "destination_identity",
}

O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
DIRECTORY_FLAGS = os.O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY
# O_NONBLOCK prevents a malicious FIFO/device below a service-owned profile
# from making the root helper hang before it can reject the non-regular inode.
READ_FLAGS = os.O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK


class GuardError(RuntimeError):
    """The requested runtime-home transition was unsafe or inconsistent."""


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys instead of silently accepting last-wins."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GuardError("journal JSON contains a duplicate field")
        result[key] = value
    return result


def _require_supported_root() -> None:
    if os.name != "posix" or not O_NOFOLLOW or not O_DIRECTORY:
        raise GuardError("runtime-home guard requires POSIX openat/O_NOFOLLOW support")
    if os.geteuid() != 0:
        raise GuardError("runtime-home guard must run as root")


def _absolute_path(raw: str, label: str, *, allow_root: bool = False) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\n" in raw:
        raise GuardError(f"{label} path is invalid")
    if not raw.startswith("/") or raw.startswith("//"):
        raise GuardError(f"{label} path must be an absolute POSIX path")
    if os.path.normpath(raw) != raw:
        raise GuardError(f"{label} path must be lexically normalized")
    if raw == "/" and not allow_root:
        raise GuardError(f"{label} path must not be the filesystem root")
    components = [] if raw == "/" else raw.split("/")[1:]
    if any(component in {"", ".", ".."} for component in components):
        raise GuardError(f"{label} path contains an unsafe component")
    return raw


def _identity(metadata: os.stat_result) -> str:
    return f"{metadata.st_dev}:{metadata.st_ino}"


def _parse_identity(raw: str, label: str) -> str:
    if IDENTITY_RE.fullmatch(raw or "") is None:
        raise GuardError(f"{label} identity is invalid")
    device, inode = raw.split(":")
    if (len(device) > 1 and device.startswith("0")) or (
        len(inode) > 1 and inode.startswith("0")
    ):
        raise GuardError(f"{label} identity is not canonical")
    return raw


def _parse_id(raw: str, label: str) -> int:
    try:
        value = int(raw, 10)
    except (TypeError, ValueError):
        raise GuardError(f"{label} must be a decimal integer") from None
    # POSIX uid_t/gid_t are at most 32 bits on supported deployment hosts.
    # Reject oversized decimal strings before they reach pwd/grp or fchown,
    # where platform-specific OverflowError messages would otherwise escape
    # the fail-closed CLI boundary.
    # 0xffffffff is the chown(2) sentinel meaning "unchanged", so it is not
    # a usable uid/gid even on hosts with unsigned 32-bit id_t values.
    if value < 0 or value >= 0xFFFFFFFF or str(value) != raw:
        raise GuardError(f"{label} must be a canonical non-negative integer")
    return value


def _fsync(descriptor: int, label: str) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise GuardError(f"could not fsync {label}: {error}") from error


def _reject_acl(descriptor: int, label: str) -> None:
    """Reject access/default ACLs that make mode-bit validation incomplete."""

    try:
        names = os.listxattr(descriptor)
    except (AttributeError, NotImplementedError):
        return
    except OSError as error:
        unsupported = {
            getattr(os, "ENOTSUP", 95),
            getattr(os, "EOPNOTSUPP", getattr(os, "ENOTSUP", 95)),
        }
        if error.errno in unsupported:
            return
        raise GuardError(f"could not inspect {label} ACLs: {error}") from error
    if "system.posix_acl_access" in names or "system.posix_acl_default" in names:
        raise GuardError(f"{label} must not carry a POSIX ACL")


def _published_metadata(parent_fd: int, name: str, label: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise GuardError(f"could not stat {label}: {error}") from error


def _verify_published(
    parent_fd: int,
    name: str,
    metadata: os.stat_result,
    label: str,
) -> None:
    published = _published_metadata(parent_fd, name, label)
    if (
        _identity(published) != _identity(metadata)
        or not stat.S_ISDIR(published.st_mode)
    ):
        raise GuardError(f"{label} changed while it was open")


def _validate_root_directory(
    metadata: os.stat_result,
    label: str,
    *,
    allow_sticky_writable: bool,
) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0:
        raise GuardError(f"{label} must be a root-owned directory")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o022 and not (allow_sticky_writable and mode & stat.S_ISVTX):
        raise GuardError(f"{label} is writable without sticky-bit protection")


def _open_root() -> int:
    descriptor = os.open("/", DIRECTORY_FLAGS)
    try:
        metadata = os.fstat(descriptor)
        _reject_acl(descriptor, "filesystem root")
        _validate_root_directory(
            metadata,
            "filesystem root",
            allow_sticky_writable=False,
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory_chain(
    path: str,
    label: str,
    *,
    final_must_not_be_writable: bool = False,
) -> int:
    """Open an absolute directory one component at a time without symlinks."""

    normalized = _absolute_path(path, label, allow_root=True)
    descriptor = _open_root()
    if normalized == "/":
        return descriptor
    try:
        for index, component in enumerate(normalized.split("/")[1:]):
            parent_fd = descriptor
            try:
                next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=parent_fd)
            except OSError as error:
                raise GuardError(
                    f"could not open {label} component {component!r}: {error}"
                ) from error
            try:
                metadata = os.fstat(next_fd)
                # Mode bits are not a complete access-control boundary when
                # an ancestor carries a POSIX access/default ACL.  Reject it
                # while the descriptor is still anchored so a service user
                # cannot race a later mkdirat/renameat through an ACL grant.
                _reject_acl(next_fd, f"{label} component {component!r}")
                is_final = index == len(normalized.split("/")[1:]) - 1
                _validate_root_directory(
                    metadata,
                    label,
                    allow_sticky_writable=not (is_final and final_must_not_be_writable),
                )
                published = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if _identity(published) != _identity(metadata):
                    raise GuardError(f"{label} changed during path traversal")
            except BaseException:
                os.close(next_fd)
                raise
            os.close(parent_fd)
            descriptor = next_fd
        return descriptor
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _open_parent(path: str, label: str) -> tuple[int, str]:
    normalized = _absolute_path(path, label)
    parent = os.path.dirname(normalized)
    name = os.path.basename(normalized)
    return _open_directory_chain(parent, f"{label} parent"), name


def _open_directory_entry(
    parent_fd: int,
    name: str,
    label: str,
) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise GuardError(f"could not open {label}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise GuardError(f"{label} is not a directory")
        _verify_published(parent_fd, name, metadata, label)
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _mkdir_entry(
    parent_fd: int,
    name: str,
    label: str,
    mode: int,
) -> bool:
    try:
        os.mkdir(name, mode=mode, dir_fd=parent_fd)
    except FileExistsError:
        return False
    except OSError as error:
        raise GuardError(f"could not create {label}: {error}") from error
    _fsync(parent_fd, f"{label} parent")
    return True


def _normalize_created_directory(
    descriptor: int,
    parent_fd: int,
    name: str,
    label: str,
    *,
    gid: int,
    mode: int,
) -> os.stat_result:
    try:
        os.fchown(descriptor, 0, gid)
        os.fchmod(descriptor, mode)
    except OSError as error:
        raise GuardError(f"could not normalize {label}: {error}") from error
    _reject_acl(descriptor, label)
    _fsync(descriptor, label)
    metadata = os.fstat(descriptor)
    _verify_published(parent_fd, name, metadata, label)
    if (
        metadata.st_uid != 0
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise GuardError(f"{label} ownership or mode normalization failed")
    return metadata


def _validate_service_identity(uid: int, gid: int) -> None:
    if pwd is None or grp is None:
        raise GuardError("service identity lookup requires POSIX pwd/grp support")
    try:
        account = pwd.getpwuid(uid)
    except (KeyError, OverflowError, ValueError):
        raise GuardError(f"service uid does not exist: {uid}") from None
    try:
        grp.getgrgid(gid)
    except (KeyError, OverflowError, ValueError):
        raise GuardError(f"service gid does not exist: {gid}") from None
    # ``systemd`` may intentionally run a UID with an explicit ``Group=``
    # that is not the account's supplementary/primary group.  Membership is
    # therefore not an invariant of the directory contract; existence and a
    # non-root service UID are the only checks made here.
    if uid == 0:
        raise GuardError("service uid must not be root")
    if gid == 0:
        raise GuardError("service gid must not be root")


def ensure_managed(
    state_root_raw: str,
    profiles_parent_raw: str,
    leaf_raw: str,
    uid_raw: str,
    gid_raw: str,
) -> dict[str, Any]:
    """Create or validate the managed root/parent/dispatcher leaf topology."""

    _require_supported_root()
    state_root = _absolute_path(state_root_raw, "state root")
    profiles_parent = _absolute_path(profiles_parent_raw, "profiles parent")
    leaf = _absolute_path(leaf_raw, "runtime-home leaf")
    uid = _parse_id(uid_raw, "service uid")
    gid = _parse_id(gid_raw, "service gid")
    _validate_service_identity(uid, gid)
    if os.path.dirname(profiles_parent) != state_root:
        raise GuardError("profiles parent must be an immediate child of state root")
    if os.path.dirname(leaf) != profiles_parent:
        raise GuardError("runtime-home leaf must be an immediate child of profiles parent")

    state_parent_fd, state_name = _open_parent(state_root, "state root")
    state_fd = parent_fd = leaf_fd = None
    try:
        state_created = _mkdir_entry(
            state_parent_fd,
            state_name,
            "state root",
            0o700,
        )
        state_fd, state_metadata = _open_directory_entry(
            state_parent_fd,
            state_name,
            "state root",
        )
        if state_created:
            state_metadata = _normalize_created_directory(
                state_fd,
                state_parent_fd,
                state_name,
                "state root",
                gid=gid,
                mode=0o1770,
            )
        else:
            _reject_acl(state_fd, "state root")
            if (
                state_metadata.st_uid != 0
                or state_metadata.st_gid != gid
                or stat.S_IMODE(state_metadata.st_mode) != 0o1770
            ):
                raise GuardError("existing state root must be root:service-group mode 01770")

        parent_name = os.path.basename(profiles_parent)
        parent_created = _mkdir_entry(
            state_fd,
            parent_name,
            "profiles parent",
            0o700,
        )
        parent_fd, parent_metadata = _open_directory_entry(
            state_fd,
            parent_name,
            "profiles parent",
        )
        if parent_created:
            parent_metadata = _normalize_created_directory(
                parent_fd,
                state_fd,
                parent_name,
                "profiles parent",
                gid=gid,
                mode=0o1770,
            )
        else:
            _reject_acl(parent_fd, "profiles parent")
            if (
                parent_metadata.st_uid != 0
                or parent_metadata.st_gid != gid
                or stat.S_IMODE(parent_metadata.st_mode) != 0o1770
            ):
                raise GuardError(
                    "existing profiles parent must be root:service-group mode 01770"
                )

        leaf_name = os.path.basename(leaf)
        leaf_created = _mkdir_entry(
            parent_fd,
            leaf_name,
            "runtime-home leaf",
            0o700,
        )
        leaf_fd, leaf_metadata = _open_directory_entry(
            parent_fd,
            leaf_name,
            "runtime-home leaf",
        )
        if leaf_created:
            leaf_metadata = _normalize_created_directory(
                leaf_fd,
                parent_fd,
                leaf_name,
                "runtime-home leaf",
                gid=gid,
                mode=0o770,
            )
        else:
            _reject_acl(leaf_fd, "runtime-home leaf")
            if (
                leaf_metadata.st_dev != parent_metadata.st_dev
                or leaf_metadata.st_uid not in {0, uid}
                or leaf_metadata.st_gid != gid
                or stat.S_IMODE(leaf_metadata.st_mode) != 0o770
            ):
                raise GuardError(
                    "existing runtime-home leaf must be root/service-owned mode 0770"
                )
        if leaf_metadata.st_dev != parent_metadata.st_dev:
            raise GuardError("runtime-home leaf must remain on its parent filesystem")

        _verify_published(state_parent_fd, state_name, state_metadata, "state root")
        _verify_published(state_fd, parent_name, parent_metadata, "profiles parent")
        _verify_published(parent_fd, leaf_name, leaf_metadata, "runtime-home leaf")
        _fsync(state_parent_fd, "state-root parent")
        _fsync(state_fd, "state root")
        _fsync(parent_fd, "profiles parent")
        return {
            "state_root_identity": _identity(state_metadata),
            "profiles_parent_identity": _identity(parent_metadata),
            "leaf_identity": _identity(leaf_metadata),
            "service_uid": uid,
            "service_gid": gid,
            "leaf_mode": "0770",
        }
    finally:
        for descriptor in (leaf_fd, parent_fd, state_fd, state_parent_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def ensure_leaf(
    parent_raw: str,
    leaf_raw: str,
    uid_raw: str,
    gid_raw: str,
    expected_parent_identity_raw: str,
) -> dict[str, Any]:
    """Create/adopt one custom managed leaf below an already-trusted parent."""

    _require_supported_root()
    parent = _absolute_path(parent_raw, "runtime-home parent")
    leaf = _absolute_path(leaf_raw, "runtime-home leaf")
    uid = _parse_id(uid_raw, "service uid")
    gid = _parse_id(gid_raw, "service gid")
    expected_parent_identity = _parse_identity(
        expected_parent_identity_raw,
        "runtime-home parent",
    )
    _validate_service_identity(uid, gid)
    if os.path.dirname(leaf) != parent:
        raise GuardError("runtime-home leaf must be an immediate child of its parent")

    parent_fd = _open_directory_chain(parent, "runtime-home parent")
    leaf_fd = None
    try:
        parent_metadata = os.fstat(parent_fd)
        if _identity(parent_metadata) != expected_parent_identity:
            raise GuardError("runtime-home parent identity changed")
        _reject_acl(parent_fd, "runtime-home parent")
        _validate_runtime_parent(parent_metadata, gid)

        leaf_name = os.path.basename(leaf)
        created = _mkdir_entry(
            parent_fd,
            leaf_name,
            "runtime-home leaf",
            0o700,
        )
        leaf_fd, leaf_metadata = _open_directory_entry(
            parent_fd,
            leaf_name,
            "runtime-home leaf",
        )
        _reject_acl(leaf_fd, "runtime-home leaf")
        if not created and leaf_metadata.st_uid not in {0, uid}:
            raise GuardError(
                "existing runtime-home leaf must be root- or service-owned"
            )
        leaf_metadata = _normalize_created_directory(
            leaf_fd,
            parent_fd,
            leaf_name,
            "runtime-home leaf",
            gid=gid,
            mode=0o770,
        )
        if leaf_metadata.st_dev != parent_metadata.st_dev:
            raise GuardError("runtime-home leaf must remain on its parent filesystem")
        _verify_published(parent_fd, leaf_name, leaf_metadata, "runtime-home leaf")
        _fsync(parent_fd, "runtime-home parent")
        return {
            "profiles_parent_identity": expected_parent_identity,
            "leaf_identity": _identity(leaf_metadata),
            "service_uid": uid,
            "service_gid": gid,
            "leaf_mode": "0770",
        }
    finally:
        for descriptor in (leaf_fd, parent_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _validate_direct_child(parent: str, child: str, label: str) -> str:
    """Validate that *child* is a direct, normalized child of *parent*."""

    parent = _absolute_path(parent, f"{label} parent")
    child = _absolute_path(child, label)
    if os.path.dirname(child) != parent:
        raise GuardError(f"{label} must be an immediate child of its parent")
    return child


def adopt_staging(
    parent_raw: str,
    staging_raw: str,
    uid_raw: str,
    gid_raw: str,
    expected_parent_identity_raw: str,
    expected_staging_identity_raw: str,
) -> dict[str, Any]:
    """Adopt a service-built staging directory as a root-owned leaf candidate.

    The staging directory is intentionally required to be service-owned before
    this operation.  Root changes only the already-open directory descriptor's
    ownership/mode and verifies that the published name still points at the
    same inode afterwards.
    """

    _require_supported_root()
    parent = _absolute_path(parent_raw, "staging parent")
    staging = _validate_direct_child(parent, staging_raw, "staging directory")
    uid = _parse_id(uid_raw, "service uid")
    gid = _parse_id(gid_raw, "service gid")
    expected_parent_identity = _parse_identity(
        expected_parent_identity_raw,
        "staging parent",
    )
    expected_staging_identity = _parse_identity(
        expected_staging_identity_raw,
        "staging directory",
    )
    _validate_service_identity(uid, gid)

    parent_fd = staging_fd = None
    try:
        parent_fd = _open_directory_chain(parent, "staging parent")
        parent_metadata = os.fstat(parent_fd)
        if _identity(parent_metadata) != expected_parent_identity:
            raise GuardError("staging parent identity changed")
        _reject_acl(parent_fd, "staging parent")
        _validate_runtime_parent(parent_metadata, gid)
        staging_name = os.path.basename(staging)
        staging_fd, staging_metadata = _open_directory_entry(
            parent_fd,
            staging_name,
            "staging directory",
        )
        _reject_acl(staging_fd, "staging directory")
        if (
            _identity(staging_metadata) != expected_staging_identity
            or staging_metadata.st_dev != parent_metadata.st_dev
            or staging_metadata.st_uid != uid
            or staging_metadata.st_gid != gid
            or stat.S_IMODE(staging_metadata.st_mode) != 0o770
        ):
            raise GuardError(
                "staging directory must be the expected service-owned mode-0770 inode on the parent filesystem"
            )
        _verify_published(parent_fd, staging_name, staging_metadata, "staging directory")
        os.fchown(staging_fd, 0, gid)
        os.fchmod(staging_fd, 0o770)
        _fsync(staging_fd, "adopted staging directory")
        adopted = os.fstat(staging_fd)
        if (
            _identity(adopted) != expected_staging_identity
            or adopted.st_uid != 0
            or adopted.st_gid != gid
            or stat.S_IMODE(adopted.st_mode) != 0o770
        ):
            raise GuardError("staging directory changed during adoption")
        _verify_published(parent_fd, staging_name, adopted, "staging directory")
        _fsync(parent_fd, "staging parent")
        return {
            "leaf_identity": expected_staging_identity,
            "leaf_mode": "0770",
        }
    finally:
        for descriptor in (staging_fd, parent_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def replace_empty_leaf(
    parent_raw: str,
    leaf_raw: str,
    expected_leaf_identity_raw: str,
    staging_raw: str,
    expected_staging_identity_raw: str,
) -> dict[str, Any]:
    """Replace an empty leaf with an adopted staging directory.

    The leaf is sealed through its open descriptor before the emptiness check,
    and the parent is temporarily made root-only while the two directory
    entries are switched.  This closes the otherwise unavoidable gap between
    ``rmdir`` and ``renameat`` in a sticky, service-writable profiles parent:
    a service process cannot recreate ``leaf`` in that interval.  If anything
    fails after the old leaf is removed, the parent is deliberately left
    root-only so the incomplete transition fails closed and can be recovered
    by a later invocation.
    """

    _require_supported_root()
    parent = _absolute_path(parent_raw, "replacement parent")
    leaf = _validate_direct_child(parent, leaf_raw, "runtime-home leaf")
    staging = _validate_direct_child(parent, staging_raw, "staging directory")
    if leaf == staging:
        raise GuardError("runtime-home leaf and staging directory must differ")
    expected_leaf_identity = _parse_identity(
        expected_leaf_identity_raw,
        "runtime-home leaf",
    )
    expected_staging_identity = _parse_identity(
        expected_staging_identity_raw,
        "staging directory",
    )
    if expected_leaf_identity == expected_staging_identity:
        raise GuardError("runtime-home leaf and staging directory identities must differ")

    parent_fd = leaf_fd = staging_fd = None
    parent_locked = False
    leaf_removed = False
    original_parent_mode = None
    original_leaf_mode = None
    leaf_mode_changed = False
    try:
        parent_fd = _open_directory_chain(parent, "replacement parent")
        parent_metadata = os.fstat(parent_fd)
        _reject_acl(parent_fd, "replacement parent")
        # The service group is learned from the staging inode below.  Do not
        # pass a synthetic gid here: a perfectly valid sticky parent may use
        # any non-root service group, and comparing it to ``0`` would reject
        # the normal deployment topology.  We still reject non-root or
        # group/world-writable non-sticky parents before opening either leaf.
        _validate_root_directory(
            parent_metadata,
            "replacement parent",
            allow_sticky_writable=True,
        )
        if stat.S_IMODE(parent_metadata.st_mode) & 0o022 and stat.S_IMODE(
            parent_metadata.st_mode
        ) != 0o1770:
            raise GuardError(
                "replacement parent must be non-writable or sticky mode 01770"
            )
        original_parent_mode = stat.S_IMODE(parent_metadata.st_mode)
        leaf_name = os.path.basename(leaf)
        staging_name = os.path.basename(staging)
        leaf_fd, leaf_metadata = _open_directory_entry(
            parent_fd,
            leaf_name,
            "runtime-home leaf",
        )
        staging_fd, staging_metadata = _open_directory_entry(
            parent_fd,
            staging_name,
            "staging directory",
        )
        _reject_acl(leaf_fd, "runtime-home leaf")
        _reject_acl(staging_fd, "staging directory")
        if (
            _identity(leaf_metadata) != expected_leaf_identity
            or leaf_metadata.st_dev != parent_metadata.st_dev
            or leaf_metadata.st_uid != 0
            or stat.S_IMODE(leaf_metadata.st_mode) not in {0o700, 0o770}
        ):
            raise GuardError("runtime-home leaf identity, owner, or mode changed")
        if (
            _identity(staging_metadata) != expected_staging_identity
            or staging_metadata.st_dev != parent_metadata.st_dev
            or staging_metadata.st_uid != 0
            or staging_metadata.st_gid == 0
            or stat.S_IMODE(staging_metadata.st_mode) != 0o770
            or staging_metadata.st_gid != leaf_metadata.st_gid
        ):
            raise GuardError(
                "staging directory identity, owner, group, mode, or filesystem changed"
            )
        if (
            stat.S_IMODE(parent_metadata.st_mode) == 0o1770
            and parent_metadata.st_gid != staging_metadata.st_gid
        ):
            raise GuardError(
                "sticky replacement parent must use the staging service group"
            )
        _verify_published(parent_fd, leaf_name, leaf_metadata, "runtime-home leaf")
        _verify_published(parent_fd, staging_name, staging_metadata, "staging directory")

        # Prevent a service process from creating/replacing either direct
        # entry while the old leaf is removed and the staging name is moved.
        os.fchmod(parent_fd, 0o700)
        _fsync(parent_fd, "locked replacement parent")
        parent_locked = True
        locked = os.fstat(parent_fd)
        if _identity(locked) != _identity(parent_metadata):
            raise GuardError("replacement parent changed while locking")
        # Revalidate both names after locking the parent; root-level races are
        # detected before any destructive operation.
        leaf_now = os.stat(leaf_name, dir_fd=parent_fd, follow_symlinks=False)
        staging_now = os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _identity(leaf_now) != expected_leaf_identity
            or _identity(staging_now) != expected_staging_identity
            or not stat.S_ISDIR(leaf_now.st_mode)
            or not stat.S_ISDIR(staging_now.st_mode)
            or leaf_now.st_dev != locked.st_dev
            or staging_now.st_dev != locked.st_dev
            or leaf_now.st_uid != 0
            or staging_now.st_uid != 0
            or staging_now.st_gid == 0
            or stat.S_IMODE(staging_now.st_mode) != 0o770
        ):
            raise GuardError("replacement entries changed before switch")
        # A legacy dispatcher leaf may still be mode 0770.  Once the parent is
        # locked, close that second write path before checking emptiness.  The
        # mode is restored only if the switch does not proceed, and only after
        # the original inode has been revalidated.
        original_leaf_mode = stat.S_IMODE(leaf_now.st_mode)
        os.fchmod(leaf_fd, 0o700)
        leaf_mode_changed = True
        _fsync(leaf_fd, "sealed replacement leaf")
        sealed_leaf = os.fstat(leaf_fd)
        published_sealed = os.stat(
            leaf_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            _identity(sealed_leaf) != expected_leaf_identity
            or _identity(published_sealed) != expected_leaf_identity
            or not stat.S_ISDIR(sealed_leaf.st_mode)
            or sealed_leaf.st_uid != 0
            or stat.S_IMODE(sealed_leaf.st_mode) != 0o700
        ):
            raise GuardError("runtime-home leaf changed while sealing for replacement")
        if os.listdir(leaf_fd):
            raise GuardError("runtime-home leaf changed before switch")

        os.rmdir(leaf_name, dir_fd=parent_fd)
        leaf_removed = True
        _fsync(parent_fd, "replacement parent after leaf removal")
        staging_after = os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _identity(staging_after) != expected_staging_identity
            or not stat.S_ISDIR(staging_after.st_mode)
            or staging_after.st_dev != locked.st_dev
            or staging_after.st_uid != 0
            or staging_after.st_gid == 0
            or stat.S_IMODE(staging_after.st_mode) != 0o770
        ):
            raise GuardError("staging directory changed after leaf removal")
        os.rename(
            staging_name,
            leaf_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        _fsync(parent_fd, "replacement parent after staging rename")
        resulting = os.stat(leaf_name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _identity(resulting) != expected_staging_identity
            or not stat.S_ISDIR(resulting.st_mode)
            or resulting.st_uid != 0
            or stat.S_IMODE(resulting.st_mode) != 0o770
        ):
            raise GuardError("replacement leaf identity changed after rename")
        os.fchmod(parent_fd, original_parent_mode)
        _fsync(parent_fd, "replacement parent restored")
        parent_locked = False
        return {
            "leaf_identity": expected_staging_identity,
            "leaf_mode": "0770",
        }
    finally:
        # Keep the parent locked after a post-rmdir failure.  Restoring its
        # writable mode would reopen a service race while the leaf is absent.
        if parent_locked and not leaf_removed and original_parent_mode is not None:
            try:
                if leaf_mode_changed and leaf_fd is not None:
                    current_leaf = os.fstat(leaf_fd)
                    if (
                        _identity(current_leaf) == expected_leaf_identity
                        and stat.S_ISDIR(current_leaf.st_mode)
                    ):
                        os.fchmod(leaf_fd, original_leaf_mode)
                        _fsync(leaf_fd, "replacement leaf mode rollback")
                os.fchmod(parent_fd, original_parent_mode)
                _fsync(parent_fd, "replacement parent rollback")
            except OSError:
                pass
        for descriptor in (staging_fd, leaf_fd, parent_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _relative_path(raw: str) -> tuple[str, ...]:
    if (
        not isinstance(raw, str)
        or not raw
        or raw.startswith("/")
        or "\x00" in raw
        or "\n" in raw
        or os.path.normpath(raw) != raw
    ):
        raise GuardError("normalized file path must be a normalized relative path")
    parts = tuple(raw.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise GuardError("normalized file path contains an unsafe component")
    return parts


def _open_relative_directory(
    root_fd: int,
    components: tuple[str, ...],
    root_device: int,
) -> int:
    descriptor = os.dup(root_fd)
    try:
        for component in components:
            parent_fd = descriptor
            try:
                next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=parent_fd)
            except OSError as error:
                raise GuardError(
                    f"could not open normalized file parent component: {error}"
                ) from error
            try:
                metadata = os.fstat(next_fd)
                published = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_dev != root_device
                    or _identity(published) != _identity(metadata)
                ):
                    raise GuardError(
                        "normalized file parent left the runtime-home filesystem"
                    )
            except BaseException:
                os.close(next_fd)
                raise
            os.close(parent_fd)
            descriptor = next_fd
        return descriptor
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def normalize_files(
    leaf_raw: str,
    expected_identity_raw: str,
    uid_raw: str,
    gid_raw: str,
    relative_paths: list[str],
) -> dict[str, Any]:
    """Normalize existing SQLite files without creating or following paths."""

    _require_supported_root()
    leaf = _absolute_path(leaf_raw, "runtime-home leaf")
    expected_identity = _parse_identity(expected_identity_raw, "runtime-home leaf")
    uid = _parse_id(uid_raw, "service uid")
    gid = _parse_id(gid_raw, "service gid")
    _validate_service_identity(uid, gid)
    if not relative_paths:
        raise GuardError("normalize-files requires at least one relative path")

    requested: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for raw in relative_paths:
        base = _relative_path(raw)
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = (*base[:-1], f"{base[-1]}{suffix}")
            if candidate not in seen:
                seen.add(candidate)
                requested.append(candidate)

    parent_fd = leaf_fd = None
    normalized_count = 0
    try:
        parent_fd, leaf_fd, leaf_name, _, leaf_metadata = _open_leaf(
            leaf,
            expected_identity,
        )
        if stat.S_IMODE(leaf_metadata.st_mode) != 0o700:
            raise GuardError("runtime-home leaf must be sealed before file normalization")
        root_device = leaf_metadata.st_dev
        for components in requested:
            file_parent_fd = _open_relative_directory(
                leaf_fd,
                components[:-1],
                root_device,
            )
            try:
                try:
                    descriptor = os.open(
                        components[-1],
                        READ_FLAGS,
                        dir_fd=file_parent_fd,
                    )
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise GuardError(f"could not open normalized file: {error}") from error
                try:
                    metadata = os.fstat(descriptor)
                    published = os.stat(
                        components[-1],
                        dir_fd=file_parent_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_nlink != 1
                        or metadata.st_dev != root_device
                        or _identity(published) != _identity(metadata)
                    ):
                        raise GuardError(
                            "normalized target must be a single-link regular file inside runtime home"
                        )
                    _reject_acl(descriptor, "normalized runtime file")
                    os.fchown(descriptor, uid, gid)
                    os.fchmod(descriptor, 0o600)
                    _fsync(descriptor, "normalized runtime file")
                    _fsync(file_parent_fd, "normalized runtime file parent")
                    metadata = os.fstat(descriptor)
                    published = os.stat(
                        components[-1],
                        dir_fd=file_parent_fd,
                        follow_symlinks=False,
                    )
                    if (
                        metadata.st_uid != uid
                        or metadata.st_gid != gid
                        or stat.S_IMODE(metadata.st_mode) != 0o600
                        or metadata.st_nlink != 1
                        or _identity(published) != _identity(metadata)
                    ):
                        raise GuardError("normalized runtime file metadata changed")
                    normalized_count += 1
                finally:
                    os.close(descriptor)
            finally:
                os.close(file_parent_fd)
        _verify_leaf_mode(
            parent_fd,
            leaf_fd,
            leaf_name,
            expected_identity,
            leaf_metadata.st_gid,
            0o700,
        )
        return {
            "leaf_identity": expected_identity,
            "normalized_count": normalized_count,
        }
    finally:
        for descriptor in (leaf_fd, parent_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _validate_runtime_parent(metadata: os.stat_result, leaf_gid: int) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0:
        raise GuardError("runtime-home parent must be root-owned")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o022:
        if mode != 0o1770 or metadata.st_gid != leaf_gid:
            raise GuardError(
                "writable runtime-home parent must be root:leaf-group mode 01770"
            )


def _open_leaf(
    leaf: str,
    expected_identity: str,
) -> tuple[int, int, str, os.stat_result, os.stat_result]:
    parent_fd, leaf_name = _open_parent(leaf, "runtime-home leaf")
    try:
        leaf_fd, leaf_metadata = _open_directory_entry(
            parent_fd,
            leaf_name,
            "runtime-home leaf",
        )
    except BaseException:
        os.close(parent_fd)
        raise
    try:
        parent_metadata = os.fstat(parent_fd)
        _validate_runtime_parent(parent_metadata, leaf_metadata.st_gid)
        _reject_acl(parent_fd, "runtime-home parent")
        _reject_acl(leaf_fd, "runtime-home leaf")
        if _identity(leaf_metadata) != expected_identity:
            raise GuardError("runtime-home leaf identity changed")
        if leaf_metadata.st_uid != 0:
            raise GuardError("runtime-home leaf must remain root-owned")
        if leaf_metadata.st_dev != parent_metadata.st_dev:
            raise GuardError("runtime-home leaf must remain on its parent filesystem")
        _verify_published(parent_fd, leaf_name, leaf_metadata, "runtime-home leaf")
        return parent_fd, leaf_fd, leaf_name, parent_metadata, leaf_metadata
    except BaseException:
        os.close(leaf_fd)
        os.close(parent_fd)
        raise


def _journal_paths(leaf_raw: str, journal_raw: str) -> tuple[str, str]:
    leaf = _absolute_path(leaf_raw, "runtime-home leaf")
    journal = _absolute_path(journal_raw, "seal journal")
    try:
        if os.path.commonpath((leaf, journal)) == leaf:
            raise GuardError("seal journal must live outside the runtime-home leaf")
    except ValueError:
        raise GuardError("runtime-home leaf and journal paths are incompatible") from None
    return leaf, journal


def _open_journal_parent(
    journal: str,
    expected_identity: str,
) -> tuple[int, str, os.stat_result]:
    parent = os.path.dirname(journal)
    name = os.path.basename(journal)
    parent_fd = _open_directory_chain(
        parent,
        "seal journal parent",
        final_must_not_be_writable=True,
    )
    try:
        metadata = os.fstat(parent_fd)
        _reject_acl(parent_fd, "seal journal parent")
        if _identity(metadata) != expected_identity:
            raise GuardError("seal journal parent identity changed")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise GuardError("seal journal parent must not be group/world-writable")
        return parent_fd, name, metadata
    except BaseException:
        os.close(parent_fd)
        raise


def _read_journal(
    parent_fd: int,
    name: str,
    *,
    missing_ok: bool = False,
) -> tuple[dict[str, Any], str] | None:
    try:
        descriptor = os.open(name, READ_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise GuardError("seal journal is missing") from None
    except OSError as error:
        raise GuardError(f"could not open seal journal: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_JOURNAL_BYTES
        ):
            raise GuardError("seal journal ownership, mode, type, link count, or size is unsafe")
        with os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as stream:
            payload = json.load(stream, object_pairs_hook=_strict_object_pairs)
        published = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(published) != _identity(metadata):
            raise GuardError("seal journal changed while it was read")
        if not isinstance(payload, dict) or set(payload) != JOURNAL_KEYS:
            raise GuardError("seal journal schema fields are invalid")
        if payload.get("schema") != SCHEMA or payload.get("phase") not in JOURNAL_PHASES:
            raise GuardError("seal journal schema or phase is invalid")
        return payload, _identity(metadata)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise GuardError(f"seal journal is not valid JSON: {error}") from error
    finally:
        os.close(descriptor)


def _read_migration_journal(
    parent_fd: int,
    name: str,
    *,
    missing_ok: bool = False,
) -> tuple[dict[str, Any], str] | None:
    try:
        descriptor = os.open(name, READ_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise GuardError("dispatcher migration journal is missing") from None
    except OSError as error:
        raise GuardError(f"could not open dispatcher migration journal: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_JOURNAL_BYTES
        ):
            raise GuardError(
                "dispatcher migration journal ownership, mode, type, link count, or size is unsafe"
            )
        with os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as stream:
            payload = json.load(stream, object_pairs_hook=_strict_object_pairs)
        published = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(published) != _identity(metadata):
            raise GuardError("dispatcher migration journal changed while it was read")
        _validate_migration_payload(payload)
        if payload["phase"] == "copied":
            # Once the copy is published the adopted staging directory has
            # deliberately replaced the recorded destination inode, so only
            # the source can still be required to match its journal entry.
            _validate_migration_source(payload)
        else:
            _validate_migration_directories(payload)
        return payload, _identity(metadata)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise GuardError(
            f"dispatcher migration journal is not valid JSON: {error}"
        ) from error
    finally:
        os.close(descriptor)


def _open_directory_chain_unprivileged(path: str, label: str) -> int:
    """Open a possibly service-owned directory without following any link."""

    normalized = _absolute_path(path, label)
    descriptor = _open_root()
    try:
        for component in normalized.split("/")[1:]:
            parent_fd = descriptor
            try:
                next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=parent_fd)
            except OSError as error:
                raise GuardError(f"could not open {label}: {error}") from error
            try:
                metadata = os.fstat(next_fd)
                published = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or _identity(published) != _identity(metadata)
                ):
                    raise GuardError(f"{label} changed during path traversal")
            except BaseException:
                os.close(next_fd)
                raise
            os.close(parent_fd)
            descriptor = next_fd
        return descriptor
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _directory_identity_unprivileged(path: str, label: str) -> str:
    descriptor = _open_directory_chain_unprivileged(path, label)
    try:
        return _identity(os.fstat(descriptor))
    finally:
        os.close(descriptor)


def _validate_migration_payload(payload: Any) -> None:
    if not isinstance(payload, dict) or set(payload) != MIGRATION_KEYS:
        raise GuardError("dispatcher migration journal schema fields are invalid")
    if payload.get("schema") != MIGRATION_SCHEMA:
        raise GuardError("dispatcher migration journal schema is invalid")
    if payload.get("phase") not in {"prepared", "copied"}:
        raise GuardError("dispatcher migration journal phase is invalid")
    for key in MIGRATION_KEYS - {"schema"}:
        if not isinstance(payload.get(key), str):
            raise GuardError(f"dispatcher migration journal field {key} must be a string")
    if TXID_RE.fullmatch(payload["txid"]) is None:
        raise GuardError("dispatcher migration transaction id is invalid")
    if VERSION_RE.fullmatch(payload["version"]) is None:
        raise GuardError("dispatcher migration version is invalid")
    if COMMIT_RE.fullmatch(payload["commit"]) is None:
        raise GuardError("dispatcher migration commit is invalid")
    _parse_identity(payload["source_identity"], "migration source")
    _parse_identity(payload["destination_identity"], "migration destination")
    source = _absolute_path(payload["source"], "migration source")
    destination = _absolute_path(payload["destination"], "migration destination")
    common = os.path.commonpath((source, destination))
    if common in {source, destination}:
        raise GuardError("migration source and destination must not overlap")


def _validate_migration_source(payload: dict[str, Any]) -> None:
    source_identity = _directory_identity_unprivileged(
        payload["source"],
        "migration source",
    )
    if source_identity != payload["source_identity"]:
        raise GuardError("dispatcher migration source identity changed")


def _validate_migration_directories(payload: dict[str, Any]) -> None:
    _validate_migration_source(payload)
    destination_identity = _directory_identity_unprivileged(
        payload["destination"],
        "migration destination",
    )
    if destination_identity != payload["destination_identity"]:
        raise GuardError("dispatcher migration destination identity changed")


def _write_migration_payload(
    parent_fd: int,
    name: str,
    payload: dict[str, Any],
    *,
    expected_existing_identity: str | None,
) -> str:
    _validate_migration_payload(payload)
    _validate_migration_directories(payload)
    temporary = f".{name}.new-{uuid.uuid4().hex}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_CLOEXEC | O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="", closefd=False) as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            _fsync(stream.fileno(), "dispatcher migration journal temporary")
    except BaseException:
        os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    os.close(descriptor)
    try:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if expected_existing_identity is None:
            if current is not None:
                raise GuardError("dispatcher migration journal appeared concurrently")
        elif current is None or _identity(current) != expected_existing_identity:
            raise GuardError("dispatcher migration journal changed before update")
        os.replace(
            temporary,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        _fsync(parent_fd, "dispatcher migration journal parent")
        written = _read_migration_journal(parent_fd, name)
        if written is None:  # defensive: missing_ok is deliberately false
            raise GuardError("dispatcher migration journal disappeared after publication")
        written_payload, identity = written
        if written_payload != payload:
            raise GuardError("dispatcher migration journal changed after publication")
        return identity
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except OSError:
            pass
        raise


def _migration_parent(
    journal_raw: str,
    parent_identity_raw: str,
) -> tuple[str, str, int, str]:
    _require_supported_root()
    journal = _absolute_path(journal_raw, "dispatcher migration journal")
    parent_identity = _parse_identity(
        parent_identity_raw,
        "dispatcher migration journal parent",
    )
    parent_fd, name, _ = _open_journal_parent(journal, parent_identity)
    return journal, parent_identity, parent_fd, name


def migration_journal_inspect(journal_raw: str, parent_identity_raw: str) -> str:
    _, _, parent_fd, name = _migration_parent(journal_raw, parent_identity_raw)
    try:
        result = _read_migration_journal(parent_fd, name, missing_ok=True)
        return "absent" if result is None else result[0]["phase"]
    finally:
        os.close(parent_fd)


def migration_journal_field(
    journal_raw: str,
    parent_identity_raw: str,
    field: str,
) -> str:
    if field not in MIGRATION_FIELDS:
        raise GuardError("dispatcher migration journal field is not allowed")
    _, _, parent_fd, name = _migration_parent(journal_raw, parent_identity_raw)
    try:
        result = _read_migration_journal(parent_fd, name)
        if result is None:  # defensive: missing_ok is deliberately false
            raise GuardError("dispatcher migration journal is missing")
        return result[0][field]
    finally:
        os.close(parent_fd)


def migration_journal_write(
    journal_raw: str,
    parent_identity_raw: str,
    txid: str,
    source_raw: str,
    source_identity: str,
    destination_raw: str,
    destination_identity: str,
    version: str,
    commit: str,
) -> dict[str, Any]:
    _, _, parent_fd, name = _migration_parent(journal_raw, parent_identity_raw)
    try:
        payload = {
            "schema": MIGRATION_SCHEMA,
            "phase": "prepared",
            "txid": txid,
            "source": _absolute_path(source_raw, "migration source"),
            "source_identity": _parse_identity(source_identity, "migration source"),
            "destination": _absolute_path(destination_raw, "migration destination"),
            "destination_identity": _parse_identity(
                destination_identity,
                "migration destination",
            ),
            "version": version,
            "commit": commit,
        }
        existing = _read_migration_journal(parent_fd, name, missing_ok=True)
        if existing is not None:
            current, identity = existing
            comparable = dict(current)
            comparable["phase"] = "prepared"
            if comparable != payload:
                raise GuardError("dispatcher migration journal belongs to another transaction")
            return {"journal_identity": identity, "phase": current["phase"]}
        identity = _write_migration_payload(
            parent_fd,
            name,
            payload,
            expected_existing_identity=None,
        )
        return {"journal_identity": identity, "phase": "prepared"}
    finally:
        os.close(parent_fd)


def migration_journal_advance(
    journal_raw: str,
    parent_identity_raw: str,
    txid: str,
) -> dict[str, Any]:
    if TXID_RE.fullmatch(txid or "") is None:
        raise GuardError("dispatcher migration transaction id is invalid")
    _, _, parent_fd, name = _migration_parent(journal_raw, parent_identity_raw)
    try:
        result = _read_migration_journal(parent_fd, name)
        if result is None:  # defensive: missing_ok is deliberately false
            raise GuardError("dispatcher migration journal is missing")
        payload, identity = result
        if payload["txid"] != txid:
            raise GuardError("dispatcher migration journal belongs to another transaction")
        if payload["phase"] == "prepared":
            payload = dict(payload)
            payload["phase"] = "copied"
            identity = _write_migration_payload(
                parent_fd,
                name,
                payload,
                expected_existing_identity=identity,
            )
        return {"journal_identity": identity, "phase": "copied"}
    finally:
        os.close(parent_fd)


def migration_journal_remove(
    journal_raw: str,
    parent_identity_raw: str,
    txid: str,
) -> dict[str, Any]:
    if TXID_RE.fullmatch(txid or "") is None:
        raise GuardError("dispatcher migration transaction id is invalid")
    _, _, parent_fd, name = _migration_parent(journal_raw, parent_identity_raw)
    try:
        # Removal is the cleanup point of an idempotent transaction.  A
        # retry after a process died immediately after unlink/fsync should
        # report the already-achieved absent state rather than fail because
        # the journal is no longer present.
        result = _read_migration_journal(parent_fd, name, missing_ok=True)
        if result is None:
            return {"phase": "absent"}
        payload, identity = result
        if payload["txid"] != txid:
            raise GuardError("dispatcher migration journal belongs to another transaction")
        _unlink_exact_journal(parent_fd, name, identity)
        return {"phase": "absent"}
    finally:
        os.close(parent_fd)


def _write_journal(
    parent_fd: int,
    name: str,
    payload: dict[str, Any],
    *,
    expected_existing_identity: str | None,
) -> str:
    temporary = f".{name}.new-{uuid.uuid4().hex}"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_CLOEXEC | O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise GuardError(f"could not create seal journal temporary: {error}") from error
    try:
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="", closefd=False) as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            _fsync(stream.fileno(), "seal journal temporary")
    except BaseException:
        os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    os.close(descriptor)
    try:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if expected_existing_identity is None:
            if current is not None:
                raise GuardError("seal journal appeared concurrently")
        elif current is None or _identity(current) != expected_existing_identity:
            raise GuardError("seal journal changed before update")
        os.replace(
            temporary,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        _fsync(parent_fd, "seal journal parent")
        written = _read_journal(parent_fd, name)
        if written is None:  # defensive: missing_ok is deliberately false
            raise GuardError("seal journal disappeared after publication")
        written_payload, identity = written
        if written_payload != payload:
            raise GuardError("seal journal content changed after publication")
        return identity
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except OSError:
            pass
        raise


def _validate_journal(
    payload: dict[str, Any],
    *,
    leaf: str,
    leaf_identity: str,
    parent_identity: str | None,
    journal_parent_identity: str,
) -> None:
    leaf_uid = payload.get("leaf_uid")
    leaf_gid = payload.get("leaf_gid")
    if (
        not isinstance(leaf_uid, int)
        or isinstance(leaf_uid, bool)
        or leaf_uid != 0
        or not isinstance(leaf_gid, int)
        or isinstance(leaf_gid, bool)
        or not 0 < leaf_gid < 0xFFFFFFFF
    ):
        raise GuardError("seal journal leaf identity fields are invalid")
    if (
        payload["leaf"] != leaf
        or payload["leaf_identity"] != leaf_identity
        or payload["leaf_uid"] != leaf_uid
        or payload["leaf_gid"] != leaf_gid
        or payload["live_mode"] != "0770"
        or payload["sealed_mode"] != "0700"
        or payload["journal_parent_identity"] != journal_parent_identity
    ):
        raise GuardError("seal journal does not match the requested runtime-home leaf")
    if parent_identity is not None and payload["parent_identity"] != parent_identity:
        raise GuardError("runtime-home parent identity changed since seal publication")


def _journal_payload(
    *,
    phase: str,
    leaf: str,
    leaf_metadata: os.stat_result,
    parent_metadata: os.stat_result,
    journal_parent_metadata: os.stat_result,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "phase": phase,
        "leaf": leaf,
        "leaf_identity": _identity(leaf_metadata),
        "leaf_uid": 0,
        "leaf_gid": leaf_metadata.st_gid,
        "live_mode": "0770",
        "sealed_mode": "0700",
        "parent_identity": _identity(parent_metadata),
        "journal_parent_identity": _identity(journal_parent_metadata),
    }


def _verify_leaf_mode(
    parent_fd: int,
    leaf_fd: int,
    leaf_name: str,
    expected_identity: str,
    gid: int,
    mode: int,
) -> os.stat_result:
    metadata = os.fstat(leaf_fd)
    _verify_published(parent_fd, leaf_name, metadata, "runtime-home leaf")
    if (
        _identity(metadata) != expected_identity
        or metadata.st_uid != 0
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise GuardError("runtime-home leaf ownership, mode, or identity changed")
    return metadata


def _transition_arguments(
    leaf_raw: str,
    expected_identity_raw: str,
    journal_raw: str,
    journal_parent_identity_raw: str,
) -> tuple[str, str, str, str]:
    _require_supported_root()
    leaf, journal = _journal_paths(leaf_raw, journal_raw)
    expected_identity = _parse_identity(expected_identity_raw, "runtime-home leaf")
    journal_parent_identity = _parse_identity(
        journal_parent_identity_raw,
        "seal journal parent",
    )
    return leaf, expected_identity, journal, journal_parent_identity


def seal(
    leaf_raw: str,
    expected_identity_raw: str,
    journal_raw: str,
    journal_parent_identity_raw: str,
) -> dict[str, Any]:
    leaf, expected_identity, journal, journal_parent_identity = _transition_arguments(
        leaf_raw,
        expected_identity_raw,
        journal_raw,
        journal_parent_identity_raw,
    )
    journal_fd, journal_name, journal_parent_metadata = _open_journal_parent(
        journal,
        journal_parent_identity,
    )
    parent_fd = leaf_fd = None
    try:
        parent_fd, leaf_fd, leaf_name, parent_metadata, leaf_metadata = _open_leaf(
            leaf,
            expected_identity,
        )
        mode = stat.S_IMODE(leaf_metadata.st_mode)
        existing = _read_journal(journal_fd, journal_name, missing_ok=True)
        if existing is None:
            if mode != 0o770:
                raise GuardError("un-journaled runtime-home leaf must be live mode 0770")
            payload = _journal_payload(
                phase="planned",
                leaf=leaf,
                leaf_metadata=leaf_metadata,
                parent_metadata=parent_metadata,
                journal_parent_metadata=journal_parent_metadata,
            )
            journal_identity = _write_journal(
                journal_fd,
                journal_name,
                payload,
                expected_existing_identity=None,
            )
        else:
            payload, journal_identity = existing
            _validate_journal(
                payload,
                leaf=leaf,
                leaf_identity=expected_identity,
                parent_identity=_identity(parent_metadata),
                journal_parent_identity=journal_parent_identity,
            )
            if payload["phase"] not in {"planned", "sealed"}:
                raise GuardError("seal journal is in a non-sealable phase")
            if payload["phase"] == "sealed" and mode != 0o700:
                raise GuardError("sealed journal does not match runtime-home leaf mode")
            if mode not in {0o700, 0o770}:
                raise GuardError("runtime-home leaf has an unsupported seal mode")

        if mode == 0o770:
            os.fchmod(leaf_fd, 0o700)
            _fsync(leaf_fd, "sealed runtime-home leaf")
        _verify_leaf_mode(
            parent_fd,
            leaf_fd,
            leaf_name,
            expected_identity,
            payload["leaf_gid"],
            0o700,
        )
        if payload["phase"] != "sealed":
            payload = dict(payload)
            payload["phase"] = "sealed"
            journal_identity = _write_journal(
                journal_fd,
                journal_name,
                payload,
                expected_existing_identity=journal_identity,
            )
        return {
            "leaf_identity": expected_identity,
            "leaf_mode": "0700",
            "journal_identity": journal_identity,
            "phase": "sealed",
        }
    finally:
        for descriptor in (leaf_fd, parent_fd, journal_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _unlink_exact_journal(parent_fd: int, name: str, expected_identity: str) -> None:
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if _identity(current) != expected_identity:
        raise GuardError("seal journal changed before removal")
    os.unlink(name, dir_fd=parent_fd)
    _fsync(parent_fd, "seal journal parent")


def unseal(
    leaf_raw: str,
    expected_identity_raw: str,
    journal_raw: str,
    journal_parent_identity_raw: str,
) -> dict[str, Any]:
    leaf, expected_identity, journal, journal_parent_identity = _transition_arguments(
        leaf_raw,
        expected_identity_raw,
        journal_raw,
        journal_parent_identity_raw,
    )
    journal_fd, journal_name, _ = _open_journal_parent(
        journal,
        journal_parent_identity,
    )
    parent_fd = leaf_fd = None
    try:
        existing = _read_journal(journal_fd, journal_name)
        if existing is None:  # defensive: missing_ok is deliberately false
            raise GuardError("seal journal is missing")
        payload, journal_identity = existing
        parent_fd, leaf_fd, leaf_name, parent_metadata, leaf_metadata = _open_leaf(
            leaf,
            expected_identity,
        )
        _validate_journal(
            payload,
            leaf=leaf,
            leaf_identity=expected_identity,
            parent_identity=_identity(parent_metadata),
            journal_parent_identity=journal_parent_identity,
        )
        mode = stat.S_IMODE(leaf_metadata.st_mode)
        phase = payload["phase"]
        if phase == "planned" and mode == 0o770:
            # The process died after publishing intent but before sealing.
            _unlink_exact_journal(journal_fd, journal_name, journal_identity)
            return {
                "leaf_identity": expected_identity,
                "leaf_mode": "0770",
                "journal_removed": True,
            }
        if phase not in {"planned", "sealed", "unsealing"}:
            raise GuardError("seal journal is in a non-unsealable phase")
        if mode not in {0o700, 0o770}:
            raise GuardError("runtime-home leaf has an unsupported unseal mode")
        if phase in {"planned", "sealed"}:
            if phase == "sealed" and mode != 0o700:
                raise GuardError("sealed journal does not match runtime-home leaf mode")
            payload = dict(payload)
            payload["phase"] = "unsealing"
            journal_identity = _write_journal(
                journal_fd,
                journal_name,
                payload,
                expected_existing_identity=journal_identity,
            )
        if mode == 0o700:
            os.fchmod(leaf_fd, 0o770)
            _fsync(leaf_fd, "unsealed runtime-home leaf")
        _verify_leaf_mode(
            parent_fd,
            leaf_fd,
            leaf_name,
            expected_identity,
            payload["leaf_gid"],
            0o770,
        )
        _unlink_exact_journal(journal_fd, journal_name, journal_identity)
        return {
            "leaf_identity": expected_identity,
            "leaf_mode": "0770",
            "journal_removed": True,
        }
    finally:
        for descriptor in (leaf_fd, parent_fd, journal_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _leaf_entry_absent(leaf: str) -> bool:
    parent_fd, name = _open_parent(leaf, "runtime-home leaf")
    try:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    finally:
        os.close(parent_fd)


def remove_empty(
    leaf_raw: str,
    expected_identity_raw: str,
    journal_raw: str,
    journal_parent_identity_raw: str,
) -> dict[str, Any]:
    leaf, expected_identity, journal, journal_parent_identity = _transition_arguments(
        leaf_raw,
        expected_identity_raw,
        journal_raw,
        journal_parent_identity_raw,
    )
    journal_fd, journal_name, _ = _open_journal_parent(
        journal,
        journal_parent_identity,
    )
    parent_fd = leaf_fd = None
    try:
        existing = _read_journal(journal_fd, journal_name)
        if existing is None:  # defensive: missing_ok is deliberately false
            raise GuardError("seal journal is missing")
        payload, journal_identity = existing
        _validate_journal(
            payload,
            leaf=leaf,
            leaf_identity=expected_identity,
            parent_identity=None,
            journal_parent_identity=journal_parent_identity,
        )
        try:
            parent_fd, leaf_fd, leaf_name, parent_metadata, leaf_metadata = _open_leaf(
                leaf,
                expected_identity,
            )
        except GuardError as error:
            if payload["phase"] == "removing" and _leaf_entry_absent(leaf):
                _unlink_exact_journal(journal_fd, journal_name, journal_identity)
                return {
                    "leaf_identity": expected_identity,
                    "removed": True,
                    "journal_removed": True,
                }
            raise error
        _validate_journal(
            payload,
            leaf=leaf,
            leaf_identity=expected_identity,
            parent_identity=_identity(parent_metadata),
            journal_parent_identity=journal_parent_identity,
        )
        if payload["phase"] not in {"sealed", "removing"}:
            raise GuardError("runtime-home leaf must be sealed before removal")
        _verify_leaf_mode(
            parent_fd,
            leaf_fd,
            leaf_name,
            expected_identity,
            payload["leaf_gid"],
            0o700,
        )
        if os.listdir(leaf_fd):
            raise GuardError("sealed runtime-home leaf is not empty")
        if payload["phase"] != "removing":
            payload = dict(payload)
            payload["phase"] = "removing"
            journal_identity = _write_journal(
                journal_fd,
                journal_name,
                payload,
                expected_existing_identity=journal_identity,
            )
        _verify_leaf_mode(
            parent_fd,
            leaf_fd,
            leaf_name,
            expected_identity,
            payload["leaf_gid"],
            0o700,
        )
        if os.listdir(leaf_fd):
            raise GuardError("sealed runtime-home leaf changed before removal")
        os.rmdir(leaf_name, dir_fd=parent_fd)
        _fsync(parent_fd, "runtime-home parent")
        os.close(leaf_fd)
        leaf_fd = None
        _unlink_exact_journal(journal_fd, journal_name, journal_identity)
        return {
            "leaf_identity": expected_identity,
            "removed": True,
            "journal_removed": True,
        }
    finally:
        for descriptor in (leaf_fd, parent_fd, journal_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    ensure = subparsers.add_parser("ensure-managed")
    ensure.add_argument("state_root")
    ensure.add_argument("profiles_parent")
    ensure.add_argument("leaf")
    ensure.add_argument("uid")
    ensure.add_argument("gid")

    ensure_custom = subparsers.add_parser("ensure-leaf")
    ensure_custom.add_argument("parent")
    ensure_custom.add_argument("leaf")
    ensure_custom.add_argument("uid")
    ensure_custom.add_argument("gid")
    ensure_custom.add_argument("expected_parent_devino")

    adopt = subparsers.add_parser("adopt-staging")
    adopt.add_argument("parent")
    adopt.add_argument("staging")
    adopt.add_argument("uid")
    adopt.add_argument("gid")
    adopt.add_argument("expected_parent_devino")
    adopt.add_argument("expected_staging_devino")

    replace = subparsers.add_parser("replace-empty-leaf")
    replace.add_argument("parent")
    replace.add_argument("leaf")
    replace.add_argument("expected_leaf_devino")
    replace.add_argument("staging")
    replace.add_argument("expected_staging_devino")

    normalize = subparsers.add_parser("normalize-files")
    normalize.add_argument("leaf")
    normalize.add_argument("expected_devino")
    normalize.add_argument("uid")
    normalize.add_argument("gid")
    normalize.add_argument("relative", nargs="+")

    for action in ("seal", "unseal", "remove-empty"):
        transition = subparsers.add_parser(action)
        transition.add_argument("leaf")
        transition.add_argument("expected_devino")
        transition.add_argument("journal")
        transition.add_argument("journal_parent_devino")

    inspect = subparsers.add_parser("journal-inspect")
    inspect.add_argument("journal")
    inspect.add_argument("parent_devino")

    field = subparsers.add_parser("journal-field")
    field.add_argument("journal")
    field.add_argument("parent_devino")
    field.add_argument("field")

    write = subparsers.add_parser("journal-write")
    write.add_argument("journal")
    write.add_argument("parent_devino")
    write.add_argument("txid")
    write.add_argument("source")
    write.add_argument("source_devino")
    write.add_argument("destination")
    write.add_argument("destination_devino")
    write.add_argument("version")
    write.add_argument("commit")

    for action in ("journal-advance", "journal-remove"):
        migration = subparsers.add_parser(action)
        migration.add_argument("journal")
        migration.add_argument("parent_devino")
        migration.add_argument("txid")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.action == "ensure-managed":
            result = ensure_managed(
                arguments.state_root,
                arguments.profiles_parent,
                arguments.leaf,
                arguments.uid,
                arguments.gid,
            )
        elif arguments.action == "ensure-leaf":
            result = ensure_leaf(
                arguments.parent,
                arguments.leaf,
                arguments.uid,
                arguments.gid,
                arguments.expected_parent_devino,
            )
        elif arguments.action == "adopt-staging":
            result = adopt_staging(
                arguments.parent,
                arguments.staging,
                arguments.uid,
                arguments.gid,
                arguments.expected_parent_devino,
                arguments.expected_staging_devino,
            )
        elif arguments.action == "replace-empty-leaf":
            result = replace_empty_leaf(
                arguments.parent,
                arguments.leaf,
                arguments.expected_leaf_devino,
                arguments.staging,
                arguments.expected_staging_devino,
            )
        elif arguments.action == "normalize-files":
            result = normalize_files(
                arguments.leaf,
                arguments.expected_devino,
                arguments.uid,
                arguments.gid,
                arguments.relative,
            )
        elif arguments.action == "journal-inspect":
            result = migration_journal_inspect(
                arguments.journal,
                arguments.parent_devino,
            )
        elif arguments.action == "journal-field":
            result = migration_journal_field(
                arguments.journal,
                arguments.parent_devino,
                arguments.field,
            )
        elif arguments.action == "journal-write":
            result = migration_journal_write(
                arguments.journal,
                arguments.parent_devino,
                arguments.txid,
                arguments.source,
                arguments.source_devino,
                arguments.destination,
                arguments.destination_devino,
                arguments.version,
                arguments.commit,
            )
        elif arguments.action == "journal-advance":
            result = migration_journal_advance(
                arguments.journal,
                arguments.parent_devino,
                arguments.txid,
            )
        elif arguments.action == "journal-remove":
            result = migration_journal_remove(
                arguments.journal,
                arguments.parent_devino,
                arguments.txid,
            )
        else:
            function = {
                "seal": seal,
                "unseal": unseal,
                "remove-empty": remove_empty,
            }[arguments.action]
            result = function(
                arguments.leaf,
                arguments.expected_devino,
                arguments.journal,
                arguments.journal_parent_devino,
            )
    except (GuardError, OSError, ValueError) as error:
        print(f"runtime-home-guard: {error}", file=sys.stderr)
        return 1
    if arguments.action in {
        "ensure-managed",
        "ensure-leaf",
        "adopt-staging",
        "replace-empty-leaf",
    }:
        # Deliberately machine-minimal: the installer captures this exact
        # identity and passes it back to subsequent topology transitions.
        # Do not leak paths.
        print(result["leaf_identity"])
    elif arguments.action in {"journal-inspect", "journal-field"}:
        print(result)
    else:
        # Transition results contain identities/state only, never a path.
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
