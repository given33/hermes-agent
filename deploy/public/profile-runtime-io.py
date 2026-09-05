#!/usr/bin/env python3
"""Unprivileged, symlink-safe profile runtime backup and restore operations.

This program is intentionally stdlib-only.  The public installer invokes it as
the Hermes service user so a compromised profile cannot turn a deployment copy,
chmod, or SQLite backup into a privileged filesystem write.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import sys
from typing import Iterator
import uuid


MAX_JSON_BYTES = 4 * 1024 * 1024
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
SQLITE_METADATA_KEYS = {
    "schema",
    "source",
    "user_version",
    "application_id",
    "integrity_check",
    "schema_sha256",
    "snapshot_sha256",
}
TREE_RECORD_KEYS = {
    "relative_path",
    "snapshot_path",
    "snapshot_sha256",
    "user_version",
    "application_id",
    "integrity_check",
    "schema_sha256",
    "mode",
}
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)
_POSIX_DIR_FD = os.name == "posix"


class RuntimeIOError(RuntimeError):
    """A profile path or snapshot failed the runtime I/O contract."""


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _normalized_path(value: str | os.PathLike[str], label: str) -> Path:
    raw = os.fspath(value)
    if (
        not raw
        or any(character in raw for character in ("\x00", "\n", "\r"))
        or not os.path.isabs(raw)
    ):
        raise RuntimeIOError(f"{label} must be an absolute path")
    if os.name == "posix" and raw.startswith("//"):
        raise RuntimeIOError(f"{label} must use one absolute-root prefix")
    if os.path.normpath(raw) != raw or os.path.abspath(raw) != raw:
        raise RuntimeIOError(f"{label} must be lexically normalized: {raw}")
    path = Path(raw)
    if path == Path(path.anchor):
        raise RuntimeIOError(f"{label} must not be a filesystem root")
    return path


def _relative_path(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise RuntimeIOError(f"{label} must be a non-empty string")
    if "\\" in value or any(ord(character) < 0x20 for character in value):
        raise RuntimeIOError(f"{label} contains an unsafe path separator")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RuntimeIOError(f"{label} is not a safe relative path: {value!r}")
    return path


def _mode(value: str, label: str = "mode") -> int:
    if not value or any(character not in "01234567" for character in value):
        raise RuntimeIOError(f"{label} must be an octal mode")
    parsed = int(value, 8)
    if parsed < 0 or parsed > 0o777:
        raise RuntimeIOError(f"{label} must be between 0000 and 0777")
    return parsed


def _is_within(path: Path, root: Path) -> bool:
    path_key = os.path.normcase(os.path.normpath(os.fspath(path)))
    root_key = os.path.normcase(os.path.normpath(os.fspath(root)))
    try:
        return os.path.commonpath((path_key, root_key)) == root_key
    except ValueError:
        return False


def _reject_tree_overlap(first: Path, second: Path, label: str) -> None:
    if _is_within(first, second) or _is_within(second, first):
        raise RuntimeIOError(f"{label} must not overlap: {first} and {second}")


def _reject_artifact_overlap(
    source_paths: set[Path], destination_paths: set[Path], label: str
) -> None:
    overlap = source_paths & destination_paths
    if overlap:
        rendered = ", ".join(str(path) for path in sorted(overlap, key=str))
        raise RuntimeIOError(f"{label} artifacts overlap: {rendered}")


def _fsync_descriptor(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError:
        if os.name == "posix":
            raise


def _chmod_descriptor(descriptor: int, path: Path, mode: int) -> None:
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, mode)
        return
    opened = os.fstat(descriptor)
    published = path.lstat()
    if stat.S_ISLNK(published.st_mode) or _identity(opened) != _identity(published):
        raise RuntimeIOError(f"file changed before chmod: {path}")
    try:
        os.chmod(path, mode, follow_symlinks=False)
    except (NotImplementedError, TypeError):
        os.chmod(path, mode)
    current = path.lstat()
    if stat.S_ISLNK(current.st_mode) or _identity(opened) != _identity(current):
        raise RuntimeIOError(f"file changed during chmod: {path}")


class _Directory:
    """An opened directory, anchored with O_NOFOLLOW on POSIX."""

    def __init__(self, path: Path, *, create: bool = False, mode: int = 0o700):
        self.path = path
        self.fd: int | None = None
        if _POSIX_DIR_FD:
            self.fd = self._open_posix(path, create=create, mode=mode)
        else:
            self._open_portable(path, create=create, mode=mode)

    @staticmethod
    def _open_posix(path: Path, *, create: bool, mode: int) -> int:
        parts = path.parts
        if not parts or parts[0] != os.path.sep:
            raise RuntimeIOError(f"directory must be absolute: {path}")
        descriptor = os.open(
            os.path.sep,
            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
        )
        try:
            for component in parts[1:]:
                if component in {"", ".", ".."}:
                    raise RuntimeIOError(f"unsafe directory component: {path}")
                try:
                    child = os.open(
                        component,
                        os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    if not create:
                        raise RuntimeIOError(f"directory is missing: {path}") from None
                    try:
                        os.mkdir(component, mode, dir_fd=descriptor)
                    except FileExistsError:
                        raise RuntimeIOError(
                            f"directory changed while being created: {path}"
                        ) from None
                    except OSError as error:
                        raise RuntimeIOError(
                            "directory is not writable by the current service user: "
                            f"{path}: {error}"
                        ) from error
                    _fsync_descriptor(descriptor)
                    child = os.open(
                        component,
                        os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                except OSError as error:
                    raise RuntimeIOError(
                        f"directory has a symlink or unsafe component: {path}: {error}"
                    ) from error
                metadata = os.fstat(child)
                published = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or _identity(metadata) != _identity(published)
                ):
                    os.close(child)
                    raise RuntimeIOError(f"directory identity changed: {path}")
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _open_portable(path: Path, *, create: bool, mode: int) -> None:
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current /= component
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                if not create:
                    raise RuntimeIOError(f"directory is missing: {path}") from None
                try:
                    current.mkdir(mode=mode)
                except OSError as error:
                    raise RuntimeIOError(
                        "directory is not writable by the current service user: "
                        f"{path}: {error}"
                    ) from error
                metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeIOError(f"directory has a symlink or unsafe component: {path}")

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> _Directory:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def stat(self, name: str) -> os.stat_result | None:
        try:
            if self.fd is not None:
                return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            return (self.path / name).lstat()
        except FileNotFoundError:
            return None

    def open(self, name: str, flags: int, mode: int = 0o600) -> int:
        flags |= _O_NOFOLLOW | _O_BINARY
        if self.fd is not None:
            return os.open(name, flags, mode, dir_fd=self.fd)
        path = self.path / name
        if path.is_symlink():
            raise RuntimeIOError(f"refusing symlink: {path}")
        return os.open(path, flags, mode)

    def mkdir(self, name: str, mode: int = 0o700) -> None:
        if self.fd is not None:
            os.mkdir(name, mode, dir_fd=self.fd)
        else:
            (self.path / name).mkdir(mode=mode)
        self.fsync()

    def unlink(self, name: str) -> None:
        if self.fd is not None:
            os.unlink(name, dir_fd=self.fd)
        else:
            (self.path / name).unlink()

    def rmdir(self, name: str) -> None:
        if self.fd is not None:
            os.rmdir(name, dir_fd=self.fd)
        else:
            (self.path / name).rmdir()
        self.fsync()

    def replace(self, source: str, destination: str) -> None:
        if self.fd is not None:
            os.replace(
                source,
                destination,
                src_dir_fd=self.fd,
                dst_dir_fd=self.fd,
            )
        else:
            os.replace(self.path / source, self.path / destination)
        self.fsync()

    def fsync(self) -> None:
        if self.fd is not None:
            _fsync_descriptor(self.fd)
            return
        try:
            descriptor = os.open(self.path, os.O_RDONLY)
        except OSError:
            return
        try:
            _fsync_descriptor(descriptor)
        finally:
            os.close(descriptor)

    def sqlite_path(self, name: str) -> str:
        if self.fd is not None and Path("/proc/self/fd").is_dir():
            return f"/proc/self/fd/{self.fd}/{name}"
        return str(self.path / name)


@contextmanager
def _regular_file(path: Path, label: str) -> Iterator[tuple[int, os.stat_result]]:
    try:
        parent_context = _Directory(path.parent)
    except RuntimeIOError as error:
        if str(error).startswith("directory is missing:"):
            raise FileNotFoundError(path) from None
        raise
    with parent_context as parent:
        published = parent.stat(path.name)
        if published is None:
            raise FileNotFoundError(path)
        if stat.S_ISLNK(published.st_mode) or not stat.S_ISREG(published.st_mode):
            raise RuntimeIOError(f"{label} must be a regular non-symlink file: {path}")
        descriptor = parent.open(path.name, os.O_RDONLY)
        try:
            metadata = os.fstat(descriptor)
            current = parent.stat(path.name)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or current is None
                or _identity(metadata) != _identity(current)
            ):
                raise RuntimeIOError(f"{label} changed while opening: {path}")
            yield descriptor, metadata
            current = parent.stat(path.name)
            if current is None or _identity(metadata) != _identity(current):
                raise RuntimeIOError(f"{label} changed while reading: {path}")
        finally:
            os.close(descriptor)


def _replaceable_destination(parent: _Directory, name: str, label: str) -> None:
    metadata = parent.stat(name)
    if metadata is None:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeIOError(f"{label} is a symlink or special file: {parent.path / name}")


def _require_absent(paths: list[Path], label: str) -> None:
    if not paths:
        return
    parent_path = paths[0].parent
    if any(path.parent != parent_path for path in paths):
        raise RuntimeIOError(f"{label} paths must share one parent directory")
    with _Directory(parent_path, create=True) as parent:
        for path in paths:
            if parent.stat(path.name) is not None:
                raise RuntimeIOError(f"{label} must be absent: {path}")


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise RuntimeIOError("short write while publishing runtime state")
        view = view[written:]


def _temporary_name(name: str) -> str:
    return f".{name}.new-{uuid.uuid4().hex}"


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    with _Directory(path.parent, create=True) as parent:
        _replaceable_destination(parent, path.name, "destination")
        temporary = _temporary_name(path.name)
        descriptor = parent.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
        )
        try:
            _chmod_descriptor(descriptor, parent.path / temporary, mode)
            _write_all(descriptor, content)
            _fsync_descriptor(descriptor)
            created = os.fstat(descriptor)
            published = parent.stat(temporary)
            if published is None or _identity(created) != _identity(published):
                raise RuntimeIOError(f"temporary file changed: {path}")
        except BaseException:
            os.close(descriptor)
            try:
                parent.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        os.close(descriptor)
        try:
            _replaceable_destination(parent, path.name, "destination")
            parent.replace(temporary, path.name)
        except BaseException:
            try:
                parent.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


def _atomic_copy(source: Path, destination: Path, mode: int) -> os.stat_result:
    if source == destination:
        raise RuntimeIOError("source and destination must be distinct")
    with _regular_file(source, "source") as (source_fd, source_metadata):
        with _Directory(destination.parent, create=True) as parent:
            _replaceable_destination(parent, destination.name, "destination")
            temporary = _temporary_name(destination.name)
            output = parent.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                mode,
            )
            try:
                _chmod_descriptor(output, parent.path / temporary, mode)
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    _write_all(output, chunk)
                _fsync_descriptor(output)
                created = os.fstat(output)
                published = parent.stat(temporary)
                if published is None or _identity(created) != _identity(published):
                    raise RuntimeIOError(f"temporary file changed: {destination}")
            except BaseException:
                os.close(output)
                try:
                    parent.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise
            os.close(output)
            try:
                _replaceable_destination(parent, destination.name, "destination")
                parent.replace(temporary, destination.name)
            except BaseException:
                try:
                    parent.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise
            return source_metadata


def _unlink_regular(path: Path, *, missing_ok: bool = True) -> bool:
    with _Directory(path.parent, create=False) as parent:
        metadata = parent.stat(path.name)
        if metadata is None:
            if missing_ok:
                return False
            raise RuntimeIOError(f"file is missing: {path}")
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeIOError(f"refusing to unlink symlink or special file: {path}")
        parent.unlink(path.name)
        parent.fsync()
        return True


def _missing_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.missing")


def _publish_missing(destination: Path) -> None:
    marker = _missing_path(destination)
    with _Directory(destination.parent, create=True) as parent:
        _replaceable_destination(parent, destination.name, "destination")
        _replaceable_destination(parent, marker.name, "missing marker")
    _atomic_write(marker, b"", 0o600)
    try:
        _unlink_regular(destination)
    except FileNotFoundError:
        pass


def _remove_missing_marker(destination: Path) -> None:
    _unlink_regular(_missing_path(destination))


def _restore_is_missing(source: Path) -> bool:
    marker = _missing_path(source)
    with _Directory(source.parent) as parent:
        source_metadata = parent.stat(source.name)
        marker_metadata = parent.stat(marker.name)
        if source_metadata is not None and marker_metadata is not None:
            raise RuntimeIOError(f"snapshot and missing marker both exist: {source}")
        if source_metadata is not None:
            if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISREG(
                source_metadata.st_mode
            ):
                raise RuntimeIOError(
                    f"snapshot is a symlink or special file: {source}"
                )
            return False
        if marker_metadata is None:
            raise RuntimeIOError(f"snapshot and missing marker are both absent: {source}")
        if stat.S_ISLNK(marker_metadata.st_mode) or not stat.S_ISREG(
            marker_metadata.st_mode
        ):
            raise RuntimeIOError(
                f"missing marker is a symlink or special file: {marker}"
            )
        return True


def _sha256_fd(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _schema_metadata(database: sqlite3.Connection) -> dict[str, object]:
    rows = database.execute(
        "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    return {
        "user_version": int(database.execute("PRAGMA user_version").fetchone()[0]),
        "application_id": int(
            database.execute("PRAGMA application_id").fetchone()[0]
        ),
        "integrity_check": str(
            database.execute("PRAGMA integrity_check").fetchone()[0]
        ),
        "schema_sha256": hashlib.sha256(
            json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _sqlite_uri(path: str, *, read_only: bool) -> str:
    suffix = "?mode=ro" if read_only else ""
    return Path(path).as_uri() + suffix


def _validate_sqlite_sidecars(
    parent: _Directory,
    name: str,
    *,
    main_exists: bool,
) -> None:
    for suffix in SIDECAR_SUFFIXES:
        sidecar_name = f"{name}{suffix}"
        metadata = parent.stat(sidecar_name)
        if metadata is None:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeIOError(
                f"SQLite sidecar is a symlink or special file: "
                f"{parent.path / sidecar_name}"
            )
        if not main_exists:
            raise RuntimeIOError(
                f"SQLite sidecar exists without its database: "
                f"{parent.path / sidecar_name}"
            )


def _remove_sqlite_sidecars(parent: _Directory, name: str, label: str) -> None:
    removed = False
    for suffix in SIDECAR_SUFFIXES:
        sidecar_name = f"{name}{suffix}"
        metadata = parent.stat(sidecar_name)
        if metadata is None:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeIOError(
                f"{label} is a symlink or special file: {parent.path / sidecar_name}"
            )
        parent.unlink(sidecar_name)
        removed = True
    if removed:
        parent.fsync()


def _cleanup_temporary_tree(root: Path, expected: tuple[int, int]) -> None:
    """Best-effort removal restricted to our still-identical temporary tree."""
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or _identity(metadata) != expected
    ):
        return
    for directory, child_directories, files in os.walk(
        root,
        topdown=False,
        followlinks=False,
    ):
        base = Path(directory)
        for name in files:
            child = base / name
            try:
                child_metadata = child.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(child_metadata.st_mode) or stat.S_ISREG(
                child_metadata.st_mode
            ):
                child.unlink()
        for name in child_directories:
            child = base / name
            try:
                child_metadata = child.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(child_metadata.st_mode):
                child.unlink()
            elif stat.S_ISDIR(child_metadata.st_mode):
                try:
                    child.rmdir()
                except OSError:
                    pass
        try:
            base.rmdir()
        except OSError:
            pass
    try:
        with _Directory(root.parent) as parent:
            parent.fsync()
    except (RuntimeIOError, OSError):
        pass


def _collect_removable_tree(root: Path) -> list[tuple[Path, os.stat_result, bool]]:
    """Validate a complete tree without following links before deleting it."""
    collected: list[tuple[Path, os.stat_result, bool]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with _Directory(directory) as opened:
            if opened.fd is not None:
                names = os.listdir(opened.fd)
            else:
                names = os.listdir(directory)
            for name in names:
                metadata = opened.stat(name)
                if metadata is None:
                    raise RuntimeIOError(
                        f"tree entry disappeared while validating: {directory / name}"
                    )
                child = directory / name
                if stat.S_ISLNK(metadata.st_mode):
                    raise RuntimeIOError(f"refusing symlink in removable tree: {child}")
                if stat.S_ISDIR(metadata.st_mode):
                    collected.append((child, metadata, True))
                    pending.append(child)
                elif stat.S_ISREG(metadata.st_mode):
                    collected.append((child, metadata, False))
                else:
                    raise RuntimeIOError(f"refusing special file in removable tree: {child}")
    return collected


def remove_tree(root_arg: str) -> None:
    root = _normalized_path(root_arg, "removable tree")
    try:
        parent_context = _Directory(root.parent)
    except RuntimeIOError as error:
        if str(error).startswith("directory is missing:"):
            return
        raise
    with parent_context as parent:
        root_metadata = parent.stat(root.name)
        if root_metadata is None:
            return
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(
            root_metadata.st_mode
        ):
            raise RuntimeIOError(f"removable tree root is not a real directory: {root}")
    entries = _collect_removable_tree(root)
    entries.sort(key=lambda item: len(item[0].parts), reverse=True)
    for path, expected, is_directory in entries:
        with _Directory(path.parent) as parent:
            current = parent.stat(path.name)
            if current is None:
                continue
            if (
                stat.S_ISLNK(current.st_mode)
                or _identity(current) != _identity(expected)
                or stat.S_ISDIR(current.st_mode) != is_directory
                or (not is_directory and not stat.S_ISREG(current.st_mode))
            ):
                raise RuntimeIOError(f"removable tree entry changed: {path}")
            if is_directory:
                parent.rmdir(path.name)
            else:
                parent.unlink(path.name)
                parent.fsync()
    with _Directory(root.parent) as parent:
        current = parent.stat(root.name)
        if current is None:
            parent.fsync()
            return
        if (
            stat.S_ISLNK(current.st_mode)
            or _identity(current) != _identity(root_metadata)
            or not stat.S_ISDIR(current.st_mode)
        ):
            raise RuntimeIOError(f"removable tree root changed: {root}")
        parent.rmdir(root.name)


def _snapshot_database(source: Path, destination: Path) -> dict[str, object]:
    try:
        source_parent_context = _Directory(source.parent)
    except RuntimeIOError as error:
        if str(error).startswith("directory is missing:"):
            raise FileNotFoundError(source) from None
        raise
    with source_parent_context as source_parent:
        source_published = source_parent.stat(source.name)
        _validate_sqlite_sidecars(
            source_parent,
            source.name,
            main_exists=source_published is not None,
        )
        if source_published is None:
            raise FileNotFoundError(source)
        if (
            stat.S_ISLNK(source_published.st_mode)
            or not stat.S_ISREG(source_published.st_mode)
        ):
            raise RuntimeIOError(f"SQLite source is not a regular file: {source}")
        source_fd = source_parent.open(source.name, os.O_RDONLY)
        try:
            source_metadata = os.fstat(source_fd)
            if _identity(source_metadata) != _identity(source_published):
                raise RuntimeIOError(f"SQLite source changed while opening: {source}")
            with _Directory(destination.parent, create=True) as destination_parent:
                _replaceable_destination(
                    destination_parent,
                    destination.name,
                    "SQLite snapshot destination",
                )
                temporary = _temporary_name(destination.name)
                descriptor = destination_parent.open(
                    temporary,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                os.close(descriptor)
                try:
                    source_path = source_parent.sqlite_path(source.name)
                    destination_path = destination_parent.sqlite_path(temporary)
                    with closing(
                        sqlite3.connect(
                            _sqlite_uri(source_path, read_only=True),
                            uri=True,
                            timeout=30,
                        )
                    ) as source_database:
                        with closing(
                            sqlite3.connect(destination_path, timeout=30)
                        ) as snapshot:
                            source_database.backup(snapshot)
                    snapshot_fd = destination_parent.open(temporary, os.O_RDWR)
                    try:
                        _chmod_descriptor(
                            snapshot_fd,
                            destination_parent.path / temporary,
                            0o600,
                        )
                        _fsync_descriptor(snapshot_fd)
                        snapshot_sha256 = _sha256_fd(snapshot_fd)
                    finally:
                        os.close(snapshot_fd)
                    with closing(
                        sqlite3.connect(
                            _sqlite_uri(destination_path, read_only=True),
                            uri=True,
                            timeout=30,
                        )
                    ) as snapshot:
                        metadata = _schema_metadata(snapshot)
                    if metadata["integrity_check"] != "ok":
                        raise RuntimeIOError(
                            f"SQLite snapshot integrity check failed: {source}"
                        )
                    current = source_parent.stat(source.name)
                    if current is None or _identity(current) != _identity(source_metadata):
                        raise RuntimeIOError(f"SQLite source changed during backup: {source}")
                    with _regular_file(
                        destination_parent.path / temporary,
                        "SQLite temporary snapshot",
                    ) as (temporary_fd, temporary_metadata):
                        if _sha256_fd(temporary_fd) != snapshot_sha256:
                            raise RuntimeIOError(
                                f"SQLite temporary snapshot changed: {destination}"
                            )
                        if not stat.S_ISREG(temporary_metadata.st_mode):
                            raise RuntimeIOError(
                                f"SQLite temporary snapshot is not regular: {destination}"
                            )
                    _remove_sqlite_sidecars(
                        destination_parent,
                        temporary,
                        "SQLite temporary sidecar",
                    )
                    _replaceable_destination(
                        destination_parent,
                        destination.name,
                        "SQLite snapshot destination",
                    )
                    destination_parent.replace(temporary, destination.name)
                except BaseException:
                    for suffix in ("", *SIDECAR_SUFFIXES):
                        try:
                            destination_parent.unlink(f"{temporary}{suffix}")
                        except FileNotFoundError:
                            pass
                    destination_parent.fsync()
                    raise
        finally:
            os.close(source_fd)
    return {
        **metadata,
        "snapshot_sha256": snapshot_sha256,
        "source_mode": stat.S_IMODE(source_metadata.st_mode),
    }


def _metadata_path(snapshot: Path) -> Path:
    return snapshot.with_name(f"{snapshot.name}.metadata.json")


def _write_json(path: Path, payload: object, mode: int = 0o600) -> None:
    content = (
        json.dumps(payload, sort_keys=True, ensure_ascii=True, indent=2) + "\n"
    ).encode("utf-8")
    _atomic_write(path, content, mode)


def _load_json(path: Path, label: str) -> object:
    with _regular_file(path, label) as (descriptor, metadata):
        if metadata.st_size <= 0 or metadata.st_size > MAX_JSON_BYTES:
            raise RuntimeIOError(f"{label} has an invalid size: {path}")
        content = bytearray()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            content.extend(chunk)
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeIOError(f"{label} is not valid UTF-8 JSON: {path}: {error}") from error


def _validate_sqlite_metadata(payload: object, label: str) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != SQLITE_METADATA_KEYS:
        raise RuntimeIOError(f"{label} has invalid fields")
    if payload.get("schema") != "hermes.sqlite-snapshot.v1":
        raise RuntimeIOError(f"{label} has an unsupported schema")
    if not isinstance(payload.get("source"), str):
        raise RuntimeIOError(f"{label} source is invalid")
    for key in ("user_version", "application_id"):
        if not isinstance(payload.get(key), int) or isinstance(payload.get(key), bool):
            raise RuntimeIOError(f"{label} {key} is invalid")
    if payload.get("integrity_check") != "ok":
        raise RuntimeIOError(f"{label} integrity check is invalid")
    for key in ("schema_sha256", "snapshot_sha256"):
        value = payload.get(key)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise RuntimeIOError(f"{label} {key} is invalid")
    return payload


def _inspect_sqlite_snapshot(
    snapshot: Path,
    expected: dict[str, object] | None = None,
) -> dict[str, object]:
    with _regular_file(snapshot, "SQLite snapshot") as (descriptor, metadata):
        snapshot_identity = _identity(metadata)
        digest = _sha256_fd(descriptor)
    with _Directory(snapshot.parent) as parent:
        sqlite_path = parent.sqlite_path(snapshot.name)
        with closing(
            sqlite3.connect(
                _sqlite_uri(sqlite_path, read_only=True),
                uri=True,
                timeout=30,
            )
        ) as database:
            actual = _schema_metadata(database)
    with _regular_file(snapshot, "SQLite snapshot") as (descriptor, metadata):
        if _identity(metadata) != snapshot_identity or _sha256_fd(descriptor) != digest:
            raise RuntimeIOError(f"SQLite snapshot changed during inspection: {snapshot}")
    if actual["integrity_check"] != "ok":
        raise RuntimeIOError(f"SQLite snapshot failed integrity check: {snapshot}")
    actual["snapshot_sha256"] = digest
    if expected is not None:
        for key in (
            "user_version",
            "application_id",
            "integrity_check",
            "schema_sha256",
            "snapshot_sha256",
        ):
            if actual[key] != expected[key]:
                raise RuntimeIOError(
                    f"SQLite snapshot does not match metadata field {key}: {snapshot}"
                )
    return actual


def backup_file(source_arg: str, destination_arg: str, mode_arg: str | None) -> None:
    source = _normalized_path(source_arg, "backup source")
    destination = _normalized_path(destination_arg, "backup destination")
    _reject_artifact_overlap(
        {source},
        {destination, _missing_path(destination)},
        "backup",
    )
    with _Directory(destination.parent, create=True) as parent:
        _replaceable_destination(parent, destination.name, "backup destination")
        _replaceable_destination(
            parent, _missing_path(destination).name, "backup missing marker"
        )
    try:
        with _regular_file(source, "backup source") as (_, metadata):
            output_mode = (
                _mode(mode_arg) if mode_arg is not None else stat.S_IMODE(metadata.st_mode)
            )
    except FileNotFoundError:
        _publish_missing(destination)
        return
    _atomic_copy(source, destination, output_mode)
    _remove_missing_marker(destination)


def snapshot_sqlite(source_arg: str, destination_arg: str) -> None:
    source = _normalized_path(source_arg, "SQLite source")
    destination = _normalized_path(destination_arg, "SQLite snapshot destination")
    source_artifacts = {
        source,
        *(source.with_name(f"{source.name}{suffix}") for suffix in SIDECAR_SUFFIXES),
    }
    destination_artifacts = {
        destination,
        _metadata_path(destination),
        _missing_path(destination),
        *(
            destination.with_name(f"{destination.name}{suffix}")
            for suffix in SIDECAR_SUFFIXES
        ),
    }
    _reject_artifact_overlap(
        source_artifacts,
        destination_artifacts,
        "SQLite snapshot",
    )
    with _Directory(destination.parent, create=True) as parent:
        for artifact in destination_artifacts:
            _replaceable_destination(
                parent, artifact.name, "SQLite snapshot destination artifact"
            )
    _require_absent(
        [
            destination.with_name(f"{destination.name}{suffix}")
            for suffix in SIDECAR_SUFFIXES
        ],
        "SQLite snapshot sidecar",
    )
    try:
        metadata = _snapshot_database(source, destination)
    except FileNotFoundError:
        _publish_missing(destination)
        try:
            _unlink_regular(_metadata_path(destination))
        except FileNotFoundError:
            pass
        return
    payload = {
        "schema": "hermes.sqlite-snapshot.v1",
        "source": str(source),
        **{key: metadata[key] for key in SQLITE_METADATA_KEYS - {"schema", "source"}},
    }
    _write_json(_metadata_path(destination), payload)
    _remove_missing_marker(destination)


def _walk_databases(root: Path) -> list[tuple[Path, PurePosixPath, int]]:
    with _Directory(root):
        pass
    databases: list[tuple[Path, PurePosixPath, int]] = []
    for directory, child_directories, files in os.walk(root, followlinks=False):
        base = Path(directory)
        safe_children: list[str] = []
        for name in child_directories:
            child = base / name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeIOError(f"profile tree contains a special directory: {child}")
            safe_children.append(name)
        child_directories[:] = safe_children
        for name in files:
            source = base / name
            if source.suffix.lower() not in SQLITE_SUFFIXES:
                continue
            metadata = source.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise RuntimeIOError(f"unsafe SQLite path in profile tree: {source}")
            relative = PurePosixPath(source.relative_to(root).as_posix())
            _relative_path(str(relative), "SQLite relative path")
            databases.append((source, relative, stat.S_IMODE(metadata.st_mode)))
    return sorted(databases, key=lambda item: str(item[1]))


def snapshot_tree(source_arg: str, destination_arg: str) -> None:
    source = _normalized_path(source_arg, "profile tree source")
    destination = _normalized_path(destination_arg, "profile tree snapshot")
    _reject_tree_overlap(source, destination, "profile tree snapshot")
    with _Directory(source):
        pass
    with _Directory(destination.parent, create=True) as parent:
        if parent.stat(destination.name) is not None:
            raise RuntimeIOError(f"tree snapshot destination already exists: {destination}")
        temporary_name = _temporary_name(destination.name)
        parent.mkdir(temporary_name, 0o700)
        temporary_metadata = parent.stat(temporary_name)
        if temporary_metadata is None:
            raise RuntimeIOError(f"temporary tree snapshot disappeared: {destination}")
        temporary_identity = _identity(temporary_metadata)
    temporary = destination.parent / temporary_name
    try:
        database_root = temporary / "databases"
        with _Directory(database_root, create=True):
            pass
        records: list[dict[str, object]] = []
        for database, relative, source_mode in _walk_databases(source):
            snapshot = database_root.joinpath(*relative.parts)
            metadata = _snapshot_database(database, snapshot)
            records.append(
                {
                    "relative_path": str(relative),
                    "snapshot_path": str(PurePosixPath("databases") / relative),
                    "snapshot_sha256": metadata["snapshot_sha256"],
                    "user_version": metadata["user_version"],
                    "application_id": metadata["application_id"],
                    "integrity_check": metadata["integrity_check"],
                    "schema_sha256": metadata["schema_sha256"],
                    "mode": source_mode,
                }
            )
        manifest = {
            "schema": "hermes.sqlite-tree-snapshot.v1",
            "root": str(source),
            "database_count": len(records),
            "databases": records,
        }
        _write_json(temporary / "manifest.json", manifest)
        with _Directory(temporary) as temporary_directory:
            temporary_directory.fsync()
        with _Directory(destination.parent) as parent:
            if parent.stat(destination.name) is not None:
                raise RuntimeIOError(
                    f"tree snapshot destination appeared during publish: {destination}"
                )
            parent.replace(temporary.name, destination.name)
    except BaseException:
        _cleanup_temporary_tree(temporary, temporary_identity)
        raise


def _preflight_replaceable(paths: list[Path], label: str) -> None:
    if not paths:
        return
    parent_path = paths[0].parent
    if any(path.parent != parent_path for path in paths):
        raise RuntimeIOError(f"{label} paths must share one parent directory")
    with _Directory(parent_path, create=True) as parent:
        for path in paths:
            _replaceable_destination(parent, path.name, label)


def _remove_regular_files(paths: list[Path], label: str) -> None:
    if not paths:
        return
    _preflight_replaceable(paths, label)
    with _Directory(paths[0].parent) as parent:
        for path in paths:
            if parent.stat(path.name) is not None:
                parent.unlink(path.name)
        parent.fsync()


def _destination_and_sidecars(destination: Path) -> list[Path]:
    return [
        destination,
        *(
            destination.with_name(f"{destination.name}{suffix}")
            for suffix in SIDECAR_SUFFIXES
        ),
    ]


def _remove_destination_and_sidecars(destination: Path) -> None:
    _remove_regular_files(
        _destination_and_sidecars(destination),
        "SQLite restore destination",
    )


def restore_file(source_arg: str, destination_arg: str, mode_arg: str) -> None:
    source = _normalized_path(source_arg, "restore source")
    destination = _normalized_path(destination_arg, "restore destination")
    _reject_artifact_overlap(
        {source, _missing_path(source)},
        {destination},
        "file restore",
    )
    output_mode = _mode(mode_arg)
    if _restore_is_missing(source):
        _remove_regular_files([destination], "file restore destination")
        _remove_missing_marker(destination)
        return
    _atomic_copy(source, destination, output_mode)
    _remove_missing_marker(destination)


def restore_sqlite(source_arg: str, destination_arg: str) -> None:
    source = _normalized_path(source_arg, "SQLite restore source")
    destination = _normalized_path(destination_arg, "SQLite restore destination")
    _reject_artifact_overlap(
        {source, _missing_path(source), _metadata_path(source)},
        set(_destination_and_sidecars(destination)),
        "SQLite restore",
    )
    if _restore_is_missing(source):
        _remove_destination_and_sidecars(destination)
        _remove_missing_marker(destination)
        return
    metadata = _validate_sqlite_metadata(
        _load_json(_metadata_path(source), "SQLite snapshot metadata"),
        "SQLite snapshot metadata",
    )
    _inspect_sqlite_snapshot(source, metadata)
    destination_paths = _destination_and_sidecars(destination)
    _preflight_replaceable(destination_paths, "SQLite restore destination")
    _remove_regular_files(destination_paths[1:], "SQLite restore sidecar")
    _atomic_copy(source, destination, 0o600)
    _inspect_sqlite_snapshot(destination, metadata)
    _remove_regular_files(destination_paths[1:], "SQLite restore sidecar")
    _remove_missing_marker(destination)


def _tree_manifest(snapshot_root: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    payload = _load_json(snapshot_root / "manifest.json", "SQLite tree manifest")
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "root",
        "database_count",
        "databases",
    }:
        raise RuntimeIOError("SQLite tree manifest has invalid fields")
    if payload.get("schema") != "hermes.sqlite-tree-snapshot.v1":
        raise RuntimeIOError("SQLite tree manifest has an unsupported schema")
    records = payload.get("databases")
    if (
        not isinstance(payload.get("root"), str)
        or not isinstance(records, list)
        or payload.get("database_count") != len(records)
    ):
        raise RuntimeIOError("SQLite tree manifest count or root is invalid")
    validated: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_record in records:
        if not isinstance(raw_record, dict) or set(raw_record) != TREE_RECORD_KEYS:
            raise RuntimeIOError("SQLite tree record has invalid fields")
        relative = _relative_path(raw_record["relative_path"], "SQLite relative path")
        snapshot = _relative_path(raw_record["snapshot_path"], "SQLite snapshot path")
        if snapshot != PurePosixPath("databases") / relative:
            raise RuntimeIOError("SQLite tree snapshot path does not match its destination")
        if str(relative) in seen:
            raise RuntimeIOError(f"duplicate SQLite tree destination: {relative}")
        seen.add(str(relative))
        if relative.suffix.lower() not in SQLITE_SUFFIXES:
            raise RuntimeIOError(f"SQLite tree record has an invalid suffix: {relative}")
        mode = raw_record.get("mode")
        if not isinstance(mode, int) or isinstance(mode, bool) or not 0 <= mode <= 0o7777:
            raise RuntimeIOError(f"SQLite tree record mode is invalid: {relative}")
        metadata = {
            "schema": "hermes.sqlite-snapshot.v1",
            "source": str(payload["root"]),
            **{
                key: raw_record[key]
                for key in (
                    "user_version",
                    "application_id",
                    "integrity_check",
                    "schema_sha256",
                    "snapshot_sha256",
                )
            },
        }
        _validate_sqlite_metadata(metadata, f"SQLite tree record {relative}")
        validated.append(dict(raw_record))
    return payload, validated


def restore_tree(snapshot_arg: str, destination_arg: str) -> None:
    snapshot_root = _normalized_path(snapshot_arg, "SQLite tree snapshot")
    destination_root = _normalized_path(destination_arg, "SQLite tree destination")
    _reject_tree_overlap(snapshot_root, destination_root, "SQLite tree restore")
    with _Directory(snapshot_root):
        pass
    _, records = _tree_manifest(snapshot_root)
    validated: list[tuple[dict[str, object], Path, Path, int]] = []
    for record in records:
        relative = _relative_path(record["relative_path"], "SQLite relative path")
        snapshot_relative = _relative_path(
            record["snapshot_path"], "SQLite snapshot path"
        )
        snapshot = snapshot_root.joinpath(*snapshot_relative.parts)
        destination = destination_root.joinpath(*relative.parts)
        expected = {
            key: record[key]
            for key in (
                "user_version",
                "application_id",
                "integrity_check",
                "schema_sha256",
                "snapshot_sha256",
            )
        }
        _inspect_sqlite_snapshot(snapshot, expected)
        restored_mode = int(record["mode"]) & 0o600
        if restored_mode & 0o400 == 0:
            raise RuntimeIOError(f"SQLite tree mode is not owner-readable: {relative}")
        validated.append((record, snapshot, destination, restored_mode))
    with _Directory(destination_root, create=True):
        pass
    expected_paths = {str(record["relative_path"]) for record in records}
    current_databases = _walk_databases(destination_root)
    for current, _, _ in current_databases:
        _preflight_replaceable(
            _destination_and_sidecars(current),
            "SQLite tree restore existing destination",
        )
    for _, _, destination, _ in validated:
        _preflight_replaceable(
            _destination_and_sidecars(destination),
            "SQLite tree restore destination",
        )
    for current, relative, _ in current_databases:
        if str(relative) not in expected_paths:
            _remove_destination_and_sidecars(current)
    for record, snapshot, destination, restored_mode in validated:
        _remove_regular_files(
            _destination_and_sidecars(destination)[1:],
            "SQLite tree restore sidecar",
        )
        _atomic_copy(snapshot, destination, restored_mode)
        expected = {
            key: record[key]
            for key in (
                "user_version",
                "application_id",
                "integrity_check",
                "schema_sha256",
                "snapshot_sha256",
            )
        }
        _inspect_sqlite_snapshot(destination, expected)
        _remove_regular_files(
            _destination_and_sidecars(destination)[1:],
            "SQLite tree restore sidecar",
        )
        # The missing marker is the transaction's absence sentinel.  Keep it
        # intact until the replacement has been copied, validated, and its
        # sidecars have been cleaned; a failed restore must remain visibly
        # missing instead of falsely advertising a successful publication.
        _remove_missing_marker(destination)


def _profile_entries(root: Path) -> tuple[list[tuple[PurePosixPath, int]], list[tuple[PurePosixPath, Path, int]], list[tuple[PurePosixPath, str]]]:
    resolved_root = root.resolve(strict=True)
    directories: list[tuple[PurePosixPath, int]] = []
    files: list[tuple[PurePosixPath, Path, int]] = []
    links: list[tuple[PurePosixPath, str]] = []
    for directory, child_directories, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        relative_base = base.relative_to(root)
        safe_children: list[str] = []
        for name in child_directories:
            child = base / name
            metadata = child.lstat()
            relative = PurePosixPath((relative_base / name).as_posix())
            if stat.S_ISLNK(metadata.st_mode):
                raw_target = os.readlink(child)
                if os.path.isabs(raw_target):
                    raise RuntimeIOError(f"profile symlink must be relative: {relative}")
                try:
                    target = child.resolve(strict=True)
                except OSError as error:
                    raise RuntimeIOError(f"profile symlink is broken: {relative}") from error
                if target != resolved_root and resolved_root not in target.parents:
                    raise RuntimeIOError(f"profile symlink escapes source root: {relative}")
                if not (target.is_file() or target.is_dir()):
                    raise RuntimeIOError(f"profile symlink target is special: {relative}")
                links.append((relative, raw_target))
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeIOError(f"profile contains a special directory: {child}")
            directories.append((relative, stat.S_IMODE(metadata.st_mode) & 0o777))
            safe_children.append(name)
        child_directories[:] = safe_children
        for name in filenames:
            child = base / name
            metadata = child.lstat()
            relative = PurePosixPath((relative_base / name).as_posix())
            if stat.S_ISLNK(metadata.st_mode):
                raw_target = os.readlink(child)
                if os.path.isabs(raw_target):
                    raise RuntimeIOError(f"profile symlink must be relative: {relative}")
                try:
                    target = child.resolve(strict=True)
                except OSError as error:
                    raise RuntimeIOError(f"profile symlink is broken: {relative}") from error
                if target != resolved_root and resolved_root not in target.parents:
                    raise RuntimeIOError(f"profile symlink escapes source root: {relative}")
                if not (target.is_file() or target.is_dir()):
                    raise RuntimeIOError(f"profile symlink target is special: {relative}")
                links.append((relative, raw_target))
            elif stat.S_ISREG(metadata.st_mode):
                files.append((relative, child, stat.S_IMODE(metadata.st_mode) & 0o777))
            else:
                raise RuntimeIOError(f"profile contains a special file: {child}")
    return directories, files, links


def _atomic_symlink(target: str, destination: Path) -> None:
    with _Directory(destination.parent, create=True) as parent:
        if parent.stat(destination.name) is not None:
            raise RuntimeIOError(f"profile copy destination already exists: {destination}")
        temporary = _temporary_name(destination.name)
        try:
            if parent.fd is not None:
                os.symlink(target, temporary, dir_fd=parent.fd)
            else:
                os.symlink(target, parent.path / temporary)
            parent.replace(temporary, destination.name)
        except BaseException:
            try:
                parent.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


def copy_profile(source_arg: str, destination_arg: str) -> None:
    source = _normalized_path(source_arg, "profile copy source")
    destination = _normalized_path(destination_arg, "profile copy destination")
    _reject_tree_overlap(source, destination, "profile copy")
    with _Directory(source):
        pass
    directories, files, links = _profile_entries(source)
    with _Directory(destination, create=True):
        pass
    if any(destination.iterdir()):
        raise RuntimeIOError(f"profile copy destination must be empty: {destination}")
    for relative, _ in sorted(directories, key=lambda item: len(item[0].parts)):
        path = destination.joinpath(*relative.parts)
        with _Directory(path, create=True):
            pass
    for relative, source_file, source_mode in files:
        _atomic_copy(source_file, destination.joinpath(*relative.parts), source_mode)
    for relative, raw_target in links:
        _atomic_symlink(raw_target, destination.joinpath(*relative.parts))
    for relative, directory_mode in sorted(
        directories,
        key=lambda item: len(item[0].parts),
        reverse=True,
    ):
        directory = destination.joinpath(*relative.parts)
        with _Directory(directory) as opened:
            if opened.fd is not None:
                _chmod_descriptor(opened.fd, directory, directory_mode)
                opened.fsync()
            else:
                os.chmod(directory, directory_mode, follow_symlinks=False)


def ensure_directory(path_arg: str, mode_arg: str) -> None:
    """Create one service-owned directory and normalize its mode.

    The installer uses this for the dispatcher control/shared directories.
    Path traversal is descriptor-relative and every component is opened with
    ``O_NOFOLLOW``; the final directory must be owned by the effective service
    UID so a later service process cannot be redirected to a root-owned or
    foreign tree.
    """

    path = _normalized_path(path_arg, "directory")
    mode = _mode(mode_arg)
    with _Directory(path, create=True, mode=mode) as directory:
        metadata = os.fstat(directory.fd) if directory.fd is not None else path.lstat()
        effective_uid = getattr(os, "geteuid", lambda: metadata.st_uid)()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != effective_uid
        ):
            raise RuntimeIOError(
                f"directory must be owned by the current service user: {path}"
            )
        if directory.fd is not None:
            _chmod_descriptor(directory.fd, path, mode)
            _fsync_descriptor(directory.fd)
        else:
            try:
                os.chmod(path, mode, follow_symlinks=False)
            except (NotImplementedError, TypeError):
                # _Directory._open_portable already rejected a final link;
                # Windows' os.chmod simply lacks the keyword.
                os.chmod(path, mode)


def ensure_owned_directory(
    path_arg: str,
    uid_arg: str,
    gid_arg: str,
    mode_arg: str,
) -> None:
    """Create/normalize one directory for a named service identity.

    This command is normally invoked by the root installer for a fixed path
    below the already-validated dispatcher profile.  It is kept separate from
    ``ensure-dir`` so an unprivileged service process can never change a
    directory's owner or group by selecting arbitrary command arguments.
    """

    path = _normalized_path(path_arg, "owned directory")
    try:
        uid = int(uid_arg, 10)
        gid = int(gid_arg, 10)
    except ValueError as error:
        raise RuntimeIOError("owned directory uid/gid must be decimal integers") from error
    if uid <= 0 or gid <= 0:
        raise RuntimeIOError("owned directory uid/gid must be non-root")
    mode = _mode(mode_arg)
    with _Directory(path, create=True, mode=mode) as directory:
        if directory.fd is None:
            raise RuntimeIOError("owned directory requires descriptor-relative POSIX I/O")
        metadata = os.fstat(directory.fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeIOError(f"owned directory is not a directory: {path}")
        os.fchown(directory.fd, uid, gid)
        os.fchmod(directory.fd, mode)
        _fsync_descriptor(directory.fd)
        current = os.fstat(directory.fd)
        if (
            current.st_uid != uid
            or current.st_gid != gid
            or stat.S_IMODE(current.st_mode) != mode
        ):
            raise RuntimeIOError(f"owned directory normalization failed: {path}")


def _copy_new(source: Path, destination: Path, mode: int) -> None:
    """Copy a regular file atomically, refusing a destination race."""

    if source == destination:
        raise RuntimeIOError("source and destination must be distinct")
    with _regular_file(source, "copy source") as (source_fd, _):
        with _Directory(destination.parent, create=True) as parent:
            if parent.stat(destination.name) is not None:
                raise RuntimeIOError(f"copy destination appeared: {destination}")
            temporary = _temporary_name(destination.name)
            output = parent.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                mode,
            )
            try:
                _chmod_descriptor(output, parent.path / temporary, mode)
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    _write_all(output, chunk)
                _fsync_descriptor(output)
                created = os.fstat(output)
                published = parent.stat(temporary)
                if published is None or _identity(created) != _identity(published):
                    raise RuntimeIOError(f"temporary copy changed: {destination}")
            except BaseException:
                os.close(output)
                try:
                    parent.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise
            os.close(output)
            # A hard-link publish is the POSIX no-replace primitive.  It fails
            # with EEXIST if another actor won the destination race and never
            # follows a destination symlink.
            try:
                if parent.fd is not None:
                    os.link(
                        temporary,
                        destination.name,
                        src_dir_fd=parent.fd,
                        dst_dir_fd=parent.fd,
                        follow_symlinks=False,
                    )
                else:
                    os.link(
                        parent.path / temporary,
                        parent.path / destination.name,
                        follow_symlinks=False,
                    )
            except FileExistsError as error:
                try:
                    parent.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise RuntimeIOError(f"copy destination appeared: {destination}") from error
            parent.unlink(temporary)
            parent.fsync()


def copy_if_absent(source_arg: str, destination_arg: str, mode_arg: str) -> None:
    """Copy a legacy credential file only when the new location is absent."""

    source = _normalized_path(source_arg, "copy source")
    destination = _normalized_path(destination_arg, "copy destination")
    _reject_artifact_overlap({source}, {destination}, "copy-if-absent")
    with _Directory(destination.parent, create=True) as parent:
        existing = parent.stat(destination.name)
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise RuntimeIOError(
                    f"copy destination is a symlink or special file: {destination}"
                )
            # A previous interrupted absence publication may have left the
            # sibling marker behind.  A regular destination is authoritative;
            # clear that stale sentinel before returning so callers never see
            # the impossible "file + .missing" state.
            _remove_missing_marker(destination)
            return
    try:
        _copy_new(source, destination, _mode(mode_arg))
    except FileNotFoundError:
        # Legacy installs may not have authenticated yet.  Absence is a
        # successful no-op; a later login writes the new isolated store.
        return
    _remove_missing_marker(destination)


def _prepare_owned_regular(path: Path) -> None:
    with _Directory(path.parent, create=True) as parent:
        metadata = parent.stat(path.name)
        if metadata is None:
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeIOError(f"SQLite runtime path is a symlink or special file: {path}")
        current_uid = getattr(os, "geteuid", lambda: metadata.st_uid)()
        if metadata.st_uid != current_uid:
            raise RuntimeIOError(
                f"SQLite runtime path is not owned by the current service user: {path}"
            )
        try:
            descriptor = parent.open(path.name, os.O_RDWR)
        except PermissionError as error:
            raise RuntimeIOError(
                f"SQLite runtime path is not writable by the current service user: {path}"
            ) from error
        try:
            opened = os.fstat(descriptor)
            current = parent.stat(path.name)
            if current is None or _identity(opened) != _identity(current):
                raise RuntimeIOError(f"SQLite runtime path changed while opening: {path}")
            _chmod_descriptor(descriptor, path, 0o600)
            _fsync_descriptor(descriptor)
        finally:
            os.close(descriptor)
        parent.fsync()


def prepare_sqlite(target_args: list[str]) -> None:
    if not target_args:
        raise RuntimeIOError("prepare-sqlite requires at least one target")
    for raw_target in target_args:
        target = _normalized_path(raw_target, "SQLite runtime target")
        _prepare_owned_regular(target)
        for suffix in SIDECAR_SUFFIXES:
            _prepare_owned_regular(target.with_name(f"{target.name}{suffix}"))


def publish_file(source_arg: str, destination_arg: str, mode_arg: str) -> None:
    source = _normalized_path(source_arg, "publish source")
    destination = _normalized_path(destination_arg, "publish destination")
    _atomic_copy(source, destination, _mode(mode_arg))
    _remove_missing_marker(destination)


def publish_stdin(destination_arg: str, mode_arg: str) -> None:
    """Atomically publish root-provided bytes while keeping path I/O unprivileged.

    The installer reads immutable code from its root-owned snapshot and pipes
    those bytes here.  This helper never opens the snapshot pathname as the
    service account, so the source can remain inaccessible while every
    destination component is still checked with ``O_NOFOLLOW``.
    """
    destination = _normalized_path(destination_arg, "publish destination")
    mode = _mode(mode_arg)
    content = sys.stdin.buffer.read(MAX_JSON_BYTES * 16)
    if len(content) >= MAX_JSON_BYTES * 16:
        raise RuntimeIOError("published input exceeds the maximum size")
    _atomic_write(destination, content, mode)
    _remove_missing_marker(destination)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup-file")
    backup.add_argument("source")
    backup.add_argument("destination")
    backup.add_argument("mode", nargs="?")

    sqlite_snapshot = subparsers.add_parser("snapshot-sqlite")
    sqlite_snapshot.add_argument("source")
    sqlite_snapshot.add_argument("destination")

    tree_snapshot = subparsers.add_parser("snapshot-tree")
    tree_snapshot.add_argument("source_root")
    tree_snapshot.add_argument("destination_root")

    file_restore = subparsers.add_parser("restore-file")
    file_restore.add_argument("source")
    file_restore.add_argument("destination")
    file_restore.add_argument("mode")

    sqlite_restore = subparsers.add_parser("restore-sqlite")
    sqlite_restore.add_argument("source")
    sqlite_restore.add_argument("destination")

    tree_restore = subparsers.add_parser("restore-tree")
    tree_restore.add_argument("snapshot_root")
    tree_restore.add_argument("destination_root")

    profile_copy = subparsers.add_parser("copy-profile")
    profile_copy.add_argument("source_root")
    profile_copy.add_argument("destination_root")

    directory = subparsers.add_parser("ensure-dir")
    directory.add_argument("path")
    directory.add_argument("mode")

    owned_directory = subparsers.add_parser("ensure-owned-dir")
    owned_directory.add_argument("path")
    owned_directory.add_argument("uid")
    owned_directory.add_argument("gid")
    owned_directory.add_argument("mode")

    copy_absent = subparsers.add_parser("copy-if-absent")
    copy_absent.add_argument("source")
    copy_absent.add_argument("destination")
    copy_absent.add_argument("mode")

    tree_remove = subparsers.add_parser("remove-tree")
    tree_remove.add_argument("root")

    sqlite_prepare = subparsers.add_parser("prepare-sqlite")
    sqlite_prepare.add_argument("targets", nargs="+")

    publish = subparsers.add_parser("publish-file")
    publish.add_argument("source")
    publish.add_argument("destination")
    publish.add_argument("mode")
    publish_stdin_parser = subparsers.add_parser("publish-stdin")
    publish_stdin_parser.add_argument("destination")
    publish_stdin_parser.add_argument("mode")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "backup-file":
            backup_file(arguments.source, arguments.destination, arguments.mode)
        elif arguments.command == "snapshot-sqlite":
            snapshot_sqlite(arguments.source, arguments.destination)
        elif arguments.command == "snapshot-tree":
            snapshot_tree(arguments.source_root, arguments.destination_root)
        elif arguments.command == "restore-file":
            restore_file(arguments.source, arguments.destination, arguments.mode)
        elif arguments.command == "restore-sqlite":
            restore_sqlite(arguments.source, arguments.destination)
        elif arguments.command == "restore-tree":
            restore_tree(arguments.snapshot_root, arguments.destination_root)
        elif arguments.command == "copy-profile":
            copy_profile(arguments.source_root, arguments.destination_root)
        elif arguments.command == "ensure-dir":
            ensure_directory(arguments.path, arguments.mode)
        elif arguments.command == "ensure-owned-dir":
            ensure_owned_directory(
                arguments.path,
                arguments.uid,
                arguments.gid,
                arguments.mode,
            )
        elif arguments.command == "copy-if-absent":
            copy_if_absent(arguments.source, arguments.destination, arguments.mode)
        elif arguments.command == "remove-tree":
            remove_tree(arguments.root)
        elif arguments.command == "prepare-sqlite":
            prepare_sqlite(arguments.targets)
        elif arguments.command == "publish-file":
            publish_file(arguments.source, arguments.destination, arguments.mode)
        elif arguments.command == "publish-stdin":
            publish_stdin(arguments.destination, arguments.mode)
        else:
            raise RuntimeIOError(f"unsupported command: {arguments.command}")
    except (RuntimeIOError, FileNotFoundError, OSError, sqlite3.Error) as error:
        print(f"profile-runtime-io: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
