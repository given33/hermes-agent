"""Boundary-checked recursive deletion for user-owned Hermes trees."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import sys
from typing import Callable, Iterable


DeleteErrorHandler = Callable[[Callable[[str], None], str, BaseException], None]


class _DeletionBoundaryError(ValueError):
    """Refuse a tree walk when its containment proof becomes invalid."""


def _is_link_like(path: Path) -> bool:
    """Return true for symlinks and Windows junctions/mount points."""

    try:
        if path.is_symlink():
            return True
        # POSIX bind mounts and WSL-mounted trees are not symlinks, but they
        # are still an external filesystem boundary.  Treat a mount point as
        # link-like so the bounded walker removes neither its contents nor a
        # directory entry that points at user data outside the approved tree.
        if os.path.ismount(os.fspath(path)):
            return True
        if sys.platform == "win32":
            # ``realpath(path) != path`` also reports a regular child below a
            # junction as link-like.  Inspect the leaf reparse attribute so
            # callers can distinguish that child from the junction component
            # itself; parent components are checked separately.
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
            reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            return bool(attributes & reparse_point)
        return False
    except OSError:
        return True


def _lexical_absolute(path: Path) -> Path:
    """Make an absolute path without resolving links or reparse points."""
    return Path(os.path.abspath(os.fspath(path)))


def _lexically_within(path: Path, root: Path) -> bool:
    """Return whether *path* is below *root* using lexical path spelling."""
    try:
        path_key = os.path.normcase(str(_lexical_absolute(path)))
        root_key = os.path.normcase(str(_lexical_absolute(root)))
        return os.path.commonpath([path_key, root_key]) == root_key
    except ValueError:
        return False


def _first_link_component(path: Path, root: Path) -> Path | None:
    """Find a link/reparse component between a target and its approved root."""
    target = _lexical_absolute(path)
    boundary = _lexical_absolute(root)
    if not _lexically_within(target, boundary):
        return None
    current = target
    while os.path.normcase(str(current)) != os.path.normcase(str(boundary)):
        if _is_link_like(current):
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _remove_unused_directory(path: Path) -> None:
    # A junction is a reparse-point directory: rmdir removes only the link,
    # never the target tree.  Try unlink first for symlink-to-file edge cases.
    try:
        path.unlink()
    except OSError as exc:
        if exc.errno not in {errno.EISDIR, errno.EPERM, errno.EACCES}:
            raise
        path.rmdir()


def _path_identity(path: Path) -> tuple[int, int, int, str]:
    """Return an identity snapshot suitable for detecting directory swaps."""

    stat_result = path.lstat()
    return (
        int(getattr(stat_result, "st_dev", 0)),
        int(getattr(stat_result, "st_ino", 0)),
        stat.S_IFMT(stat_result.st_mode),
        os.path.normcase(os.path.realpath(os.fspath(path))),
    )


def _assert_tree_path_bounded(path: Path, boundary_root: Path) -> None:
    """Reject a walk path that now crosses a link or resolved root boundary."""

    if not _lexically_within(path, boundary_root):
        raise _DeletionBoundaryError(
            f"deletion path is outside approved root: {path}"
        )
    # A link at the leaf is safe to unlink and is handled by the caller.  A
    # link in an ancestor would make child lstat/unlink follow an external
    # tree, so fail closed before touching the child.
    if _is_link_like(path):
        return
    if _first_link_component(path, boundary_root):
        raise _DeletionBoundaryError(
            f"refusing to traverse link-like path during deletion: {path}"
        )
    resolved = path.resolve(strict=False)
    resolved_root = boundary_root.resolve(strict=False)
    if not (resolved == resolved_root or resolved.is_relative_to(resolved_root)):
        raise _DeletionBoundaryError(
            f"deletion path escaped approved root during walk: {path}"
        )


def validate_deletion_root(path: Path, allowed_roots: Iterable[Path]) -> Path:
    """Resolve *path* and require it to be inside one explicit allowed root."""

    if not path.is_absolute():
        raise ValueError(f"deletion path must be absolute: {path}")
    resolved = path.resolve(strict=False)
    for root in allowed_roots:
        root_resolved = Path(root).resolve(strict=False)
        if resolved == root_resolved:
            return resolved
        try:
            resolved.relative_to(root_resolved)
            return resolved
        except ValueError:
            continue
    raise ValueError(
        f"refusing to delete outside approved roots: {resolved} "
        f"(allowed: {', '.join(str(Path(root).resolve()) for root in allowed_roots)})"
    )


def _unlink_file(path: Path) -> None:
    """Unlink *path*, clearing the Windows read-only attribute first.

    Git for Windows marks loose objects and pack files read-only
    (``st_mode`` 0o444 / ``FILE_ATTRIBUTE_READONLY``) to emulate POSIX object
    protection; on Windows that attribute blocks ``unlink`` with WinError 5
    even when the parent directory is writable, so deleting a git-cloned
    plugin tree (uninstall, install rollback) would fail. Clearing the
    attribute first is a no-op on POSIX, where a read-only file is already
    removable when its directory is writable.
    """
    try:
        path.unlink()
    except PermissionError:
        if sys.platform != "win32":
            raise
        # Best effort: only the attribute is under our control; other causes
        # (open handles, ACLs) must surface as the original error.
        os.chmod(path, stat.S_IWRITE)
        path.unlink()


def _rmtree_bounded(
    root: Path,
    onerror: DeleteErrorHandler | None,
    *,
    boundary_root: Path | None = None,
) -> None:
    def remove_path(path: Path) -> None:
        if boundary_root is not None:
            _assert_tree_path_bounded(path, boundary_root)
        if _is_link_like(path):
            _remove_unused_directory(path)
            return
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if boundary_root is not None:
            _assert_tree_path_bounded(path, boundary_root)
        if stat.S_ISDIR(mode):
            _rmtree_bounded(path, onerror, boundary_root=boundary_root)
        else:
            _unlink_file(path)

    if boundary_root is not None:
        _assert_tree_path_bounded(root, boundary_root)
    if _is_link_like(root):
        _remove_unused_directory(root)
        return

    try:
        mode = root.lstat().st_mode
    except FileNotFoundError:
        return
    if boundary_root is not None:
        _assert_tree_path_bounded(root, boundary_root)
    if not stat.S_ISDIR(mode):
        _unlink_file(root)
        return

    before_scan = _path_identity(root)
    try:
        with os.scandir(root) as entries:
            names = [entry.name for entry in entries]
        # A rename-and-replace between scandir and child traversal can turn a
        # trusted directory into a junction/symlink.  Do not hand that tree to
        # the recursive walker or to an onerror retry callback.
        if boundary_root is not None:
            _assert_tree_path_bounded(root, boundary_root)
        if _path_identity(root) != before_scan:
            raise _DeletionBoundaryError(
                f"directory changed during bounded deletion: {root}"
            )
        for name in names:
            child = root / name
            try:
                remove_path(child)
            except OSError as exc:
                if onerror is None:
                    raise
                onerror(lambda p, _child=child: remove_path(Path(p)), str(child), exc)
        if boundary_root is not None:
            _assert_tree_path_bounded(root, boundary_root)
        if _path_identity(root) != before_scan:
            raise _DeletionBoundaryError(
                f"directory changed during bounded deletion: {root}"
            )
        root.rmdir()
    except _DeletionBoundaryError:
        raise
    except OSError as exc:
        if onerror is None:
            raise
        onerror(lambda p: remove_path(Path(p)), str(root), exc)


def safe_rmtree(
    path: str | os.PathLike[str],
    allowed_roots: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
    *,
    onerror: DeleteErrorHandler | None = None,
) -> Path:
    """Delete a directory without crossing links or an approved-root boundary.

    This intentionally does not call ``shutil.rmtree`` directly: Python's
    directory walker follows NTFS junctions on some 3.11 configurations, and
    uninstall/profile deletion must never escape ``HERMES_HOME`` or the
    installation checkout because of a planted junction.
    """

    target = Path(path)
    if isinstance(allowed_roots, (str, os.PathLike)):
        roots: list[Path] = [Path(allowed_roots)]
    else:
        roots = [Path(root) for root in allowed_roots]
    if not roots:
        raise ValueError("at least one allowed root is required")
    resolved = validate_deletion_root(target, roots)
    matching_root = next(
        (
            root
            for root in roots
            if resolved == Path(root).resolve(strict=False)
            or resolved.is_relative_to(Path(root).resolve(strict=False))
        ),
        None,
    )
    if matching_root is None:
        raise ValueError(f"no approved root for deletion target: {target}")
    if _is_link_like(Path(matching_root)):
        raise ValueError(f"approved deletion root is link-like: {matching_root}")
    if not _lexically_within(target, Path(matching_root)):
        raise ValueError(
            f"deletion target is not lexically under approved root: {target}"
        )
    link_component = _first_link_component(target, Path(matching_root))
    if link_component is not None and os.path.normcase(
        str(_lexical_absolute(link_component))
    ) != os.path.normcase(str(_lexical_absolute(target))):
        raise ValueError(
            f"refusing to traverse link-like path: {link_component}"
        )
    # Preserve the original spelling so a target that is itself a symlink or
    # junction is unlinked as a link, rather than resolved and recursively
    # deleting the directory it points to.
    _rmtree_bounded(target, onerror, boundary_root=Path(matching_root))
    return resolved
