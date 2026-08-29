"""Safe ``tar.gz`` primitives shared by the profile and kanban transfer paths.

Both ``hermes profile export|import`` and ``hermes kanban export|import``
ship a directory to another machine and unpack whatever comes back. The
unpack side is the dangerous half: a hand-crafted archive can carry
``../`` members, absolute paths, symlinks, or device nodes, any of which
turn an import into an arbitrary-write primitive. These helpers are the
one place that logic lives so a second transfer surface can't ship a
second, subtly weaker extractor.

The writer is deliberately not :func:`shutil.make_archive`: that emits
PAX (Python's tarfile default since 3.8), whose fractional-mtime records
macOS Archive Utility rejects — double-clicking an exported profile threw
"Error 94 - Bad message." GNU format keeps long paths working (longlink
extensions) and stays integer-mtime, so Finder, bsdtar, and gnutar all
extract it.
"""

from __future__ import annotations

import os
import shutil
import tarfile
from pathlib import Path, PurePosixPath, PureWindowsPath


# Shared import limits for profile and Kanban transfer archives. Keeping the
# policy at the common extraction boundary prevents a newly-added importer
# from accidentally reintroducing an unbounded decompression path.
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 20_000
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024


def normalize_archive_parts(member_name: str) -> list[str]:
    """Return safe path parts for an archive member, or raise.

    Rejects absolute paths (POSIX and Windows, including drive letters),
    empty names, and any ``..`` component. Backslashes are folded to
    ``/`` first so a Windows-authored archive can't smuggle a separator
    past the POSIX parse.
    """
    normalized_name = member_name.replace("\\", "/")
    posix_path = PurePosixPath(normalized_name)
    windows_path = PureWindowsPath(member_name)

    if (
        not normalized_name
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
    ):
        raise ValueError(f"Unsafe archive member path: {member_name}")

    parts = [part for part in posix_path.parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Unsafe archive member path: {member_name}")
    return parts


def make_targz(base: str, root_dir: str, base_dir: str) -> str:
    """Create ``<base>.tar.gz`` of ``root_dir/base_dir`` in GNU tar format."""
    archive_path = f"{base}.tar.gz"
    with tarfile.open(archive_path, "w:gz", format=tarfile.GNU_FORMAT) as tf:
        tf.add(str(Path(root_dir) / base_dir), arcname=base_dir)
    return archive_path


def _validated_archive_members(
    archive: Path,
    *,
    max_archive_bytes: int,
    max_members: int,
    max_member_bytes: int,
    max_expanded_bytes: int,
) -> tuple[tarfile.TarFile, list[tuple[tarfile.TarInfo, list[str]]]]:
    """Open *archive* and validate all members before destination writes."""
    archive = Path(archive)
    try:
        if archive.stat().st_size > max_archive_bytes:
            raise ValueError(f"Archive exceeds {max_archive_bytes // (1024 * 1024)} MiB")
    except OSError as exc:
        raise ValueError(f"Cannot inspect archive: {archive}") from exc

    try:
        tf = tarfile.open(archive, "r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise ValueError(f"Cannot open archive: {archive}") from exc

    validated: list[tuple[tarfile.TarInfo, list[str]]] = []
    seen: set[tuple[str, ...]] = set()
    file_paths: set[tuple[str, ...]] = set()
    expanded = 0
    try:
        # Iterate incrementally so the member-count limit also bounds the
        # in-memory metadata list.  No destination directory exists yet.
        for member in tf:
            if len(validated) >= max_members:
                raise ValueError(f"Archive contains more than {max_members} entries")
            parts = normalize_archive_parts(member.name)
            key = tuple(parts)
            if key in seen:
                raise ValueError(f"Duplicate archive member path: {member.name}")
            # A directory entry is commonly followed by its children in tar
            # order, so only a previously validated *file* may conflict with
            # a later path component.
            if any(key[:index] in file_paths for index in range(1, len(key))):
                raise ValueError(f"Archive member conflicts with a file parent: {member.name}")
            seen.add(key)
            if not member.isdir() and not member.isfile():
                raise ValueError(f"Unsupported archive member type: {member.name}")
            size = int(member.size)
            if size < 0 or size > max_member_bytes:
                raise ValueError(
                    f"Archive member exceeds {max_member_bytes // (1024 * 1024)} MiB: {member.name}"
                )
            expanded += size
            if expanded > max_expanded_bytes:
                raise ValueError(
                    f"Archive expands beyond {max_expanded_bytes // (1024 * 1024)} MiB"
                )
            validated.append((member, parts))
            if member.isfile():
                file_paths.add(key)
    except BaseException:
        tf.close()
        raise
    return tf, validated


def safe_extract_targz(
    archive: Path,
    destination: Path,
    *,
    max_archive_bytes: int | None = None,
    max_members: int | None = None,
    max_member_bytes: int | None = None,
    max_expanded_bytes: int | None = None,
) -> None:
    """Extract a tar.gz safely, with bounded expansion and no links."""
    limits = {
        "max_archive_bytes": MAX_ARCHIVE_BYTES if max_archive_bytes is None else max_archive_bytes,
        "max_members": MAX_ARCHIVE_MEMBERS if max_members is None else max_members,
        "max_member_bytes": MAX_MEMBER_BYTES if max_member_bytes is None else max_member_bytes,
        "max_expanded_bytes": MAX_EXPANDED_BYTES if max_expanded_bytes is None else max_expanded_bytes,
    }
    _extract_targz_validated(archive, destination, **limits)


def _extract_targz_validated(
    archive: Path,
    destination: Path,
    *,
    max_archive_bytes: int,
    max_members: int,
    max_member_bytes: int,
    max_expanded_bytes: int,
) -> None:
    tf, validated = _validated_archive_members(
        archive,
        max_archive_bytes=max_archive_bytes,
        max_members=max_members,
        max_member_bytes=max_member_bytes,
        max_expanded_bytes=max_expanded_bytes,
    )
    destination = Path(destination)
    try:
        destination.mkdir(parents=True, exist_ok=True)
        base = destination.resolve()
        for member, parts in validated:
            target = destination.joinpath(*parts)
            if target.is_symlink():
                raise ValueError(f"Archive destination contains a link: {member.name}")
            parent = target.parent.resolve(strict=False)
            if parent != base and base not in parent.parents:
                raise ValueError(f"Archive destination escaped: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(member)
            if extracted is None:
                raise ValueError(f"Cannot read archive member: {member.name}")
            import tempfile
            fd, partial_name = tempfile.mkstemp(
                dir=str(target.parent), prefix=f".{target.name}.", suffix=".partial"
            )
            partial = Path(partial_name)
            try:
                written = 0
                with extracted, os.fdopen(fd, "wb") as dst:
                    fd = -1
                    while True:
                        chunk = extracted.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_member_bytes:
                            raise ValueError("Archive member expanded beyond limit")
                        dst.write(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
                if written != int(member.size):
                    raise ValueError(f"Archive member size mismatch: {member.name}")
                os.replace(partial, target)
            finally:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                try:
                    partial.unlink()
                except FileNotFoundError:
                    pass
            try:
                os.chmod(target, member.mode & 0o777)
            except OSError:
                pass
    finally:
        tf.close()


def archive_root_dirs(
    archive: Path,
    *,
    max_archive_bytes: int | None = None,
    max_members: int | None = None,
    max_member_bytes: int | None = None,
    max_expanded_bytes: int | None = None,
) -> set[str]:
    """Return the archive's top-level directory names.

    Transfer archives carry exactly one root directory, which names the
    thing being imported. Inspecting the archive before extraction lets
    the caller resolve the target name (and refuse a malformed archive)
    without first mutating a live tree.
    """
    limits = {
        "max_archive_bytes": MAX_ARCHIVE_BYTES if max_archive_bytes is None else max_archive_bytes,
        "max_members": MAX_ARCHIVE_MEMBERS if max_members is None else max_members,
        "max_member_bytes": MAX_MEMBER_BYTES if max_member_bytes is None else max_member_bytes,
        "max_expanded_bytes": MAX_EXPANDED_BYTES if max_expanded_bytes is None else max_expanded_bytes,
    }
    tf, validated = _validated_archive_members(archive, **limits)
    try:
        return {parts[0] for member, parts in validated if len(parts) > 1 or member.isdir()}
    finally:
        tf.close()


def copy_regular_files(src: Path, dst: Path) -> int:
    """Copy the regular files under ``src`` into ``dst``, skipping symlinks.

    Used on the *export* side so a symlink planted in an attachments or
    logs tree can't pull an arbitrary file into the archive. Returns the
    number of files copied; a missing ``src`` copies nothing.
    """
    if not src.is_dir():
        return 0
    copied = 0
    for entry in sorted(src.rglob("*")):
        if entry.is_symlink() or not entry.is_file():
            continue
        target = dst / entry.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(entry, target)
        copied += 1
    return copied
