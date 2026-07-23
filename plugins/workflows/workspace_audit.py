"""Bounded, secret-aware snapshots and diffs for Workflow workspaces."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any


MAX_AUDIT_FILES = 80
MAX_PATCH_BYTES = 1_048_576
MAX_TEXT_FILE_BYTES = 1_048_576
MAX_BASELINE_TEXT_BYTES = 8 * 1_048_576

_EXCLUDED_DIRS = {
    ".aws",
    ".azure",
    ".git",
    ".gnupg",
    ".ssh",
    ".venv",
    "node_modules",
}
_SENSITIVE_NAMES = {
    ".env",
    "auth.json",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
    "secrets",
    "service-account.json",
}
_SENSITIVE_DIRS = {"credentials", "secrets"}
_SENSITIVE_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|passwd|private[_-]?key|secret|token)\b\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)(\b(?:authorization\s*:\s*)?bearer\s+)[A-Za-z0-9._~+/=-]+")
_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})\b"
)
_CREDENTIAL_URI_RE = re.compile(
    r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s/@]+(@)"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)


class WorkspaceUnavailable(RuntimeError):
    """The workspace cannot be inspected without inventing audit data."""


def _sensitive_path(relative_path: str) -> bool:
    if any(ord(character) < 32 for character in relative_path):
        return True
    path = PurePosixPath(relative_path)
    lowered = [part.casefold() for part in path.parts]
    if any(part in _EXCLUDED_DIRS | _SENSITIVE_DIRS for part in lowered[:-1]):
        return True
    name = lowered[-1] if lowered else ""
    return (
        name in _SENSITIVE_NAMES | _EXCLUDED_DIRS
        or name.startswith(".env.")
        or name.startswith("service-account-") and name.endswith(".json")
        or PurePosixPath(name).suffix in _SENSITIVE_SUFFIXES
    )


def redact_secrets(value: str) -> str:
    """Redact common credentials from every diff line, including context."""

    redacted = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", value)
    redacted = _SECRET_ASSIGNMENT_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _BEARER_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _AWS_ACCESS_KEY_RE.sub("[REDACTED AWS ACCESS KEY]", redacted)
    redacted = _KNOWN_TOKEN_RE.sub("[REDACTED TOKEN]", redacted)
    return _CREDENTIAL_URI_RE.sub(r"\1[REDACTED]\2", redacted)


def capture_snapshot(
    root: str | Path, *, tracked_paths: set[str] | None = None
) -> dict[str, Any]:
    """Capture a stable set of at most 80 non-sensitive regular files."""

    requested = Path(root).expanduser()
    if not requested.is_absolute():
        raise WorkspaceUnavailable("workspace path is not absolute")
    try:
        resolved = requested.resolve(strict=True)
    except FileNotFoundError as exc:
        raise WorkspaceUnavailable("workspace directory is unavailable") from exc
    if not resolved.is_dir():
        raise WorkspaceUnavailable("workspace path is not a directory")

    tracked = sorted(set(tracked_paths or ()))
    if len(tracked) > MAX_AUDIT_FILES:
        raise WorkspaceUnavailable("workspace baseline exceeds the audit file limit")
    selected: dict[str, Path] = {}
    for relative in tracked:
        pure = PurePosixPath(relative)
        if (
            not relative
            or pure.is_absolute()
            or ".." in pure.parts
            or _sensitive_path(relative)
        ):
            raise WorkspaceUnavailable("workspace baseline contains an ineligible path")
        path = resolved.joinpath(*pure.parts)
        if path.is_symlink() or not path.is_file():
            continue
        selected[relative] = path

    additions: list[tuple[str, Path]] = []
    addition_capacity = MAX_AUDIT_FILES - len(tracked)

    def walk_error(error: OSError) -> None:
        raise WorkspaceUnavailable(f"workspace traversal failed: {error}") from error

    limit_reached = False
    for current, directories, filenames in os.walk(
        resolved, followlinks=False, onerror=walk_error
    ):
        directories[:] = sorted(
            directory
            for directory in directories
            if directory.casefold() not in _EXCLUDED_DIRS | _SENSITIVE_DIRS
            and not (Path(current) / directory).is_symlink()
        )
        for filename in sorted(filenames):
            path = Path(current) / filename
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(resolved).as_posix()
            if relative in tracked or _sensitive_path(relative):
                continue
            if len(additions) >= addition_capacity:
                limit_reached = True
                break
            additions.append((relative, path))
            if len(additions) == addition_capacity:
                # Continue until one more eligible path proves truncation.
                continue
        if limit_reached:
            break

    selected.update(additions)
    files: dict[str, dict[str, Any]] = {}
    captured_bytes = 0
    content_truncated = False
    for relative, path in sorted(selected.items()):
        try:
            size = path.stat().st_size
            if size > MAX_TEXT_FILE_BYTES:
                files[relative] = {
                    "sha256": None,
                    "content": None,
                    "binary": True,
                    "omitted": True,
                    "byte_count": size,
                }
                content_truncated = True
                continue
            data = path.read_bytes()
        except (FileNotFoundError, OSError) as exc:
            raise WorkspaceUnavailable(
                f"workspace changed while capturing {relative}"
            ) from exc
        include_text = captured_bytes + len(data) <= MAX_BASELINE_TEXT_BYTES
        captured_bytes += len(data)
        try:
            content = data.decode("utf-8")
            binary = "\x00" in content
        except UnicodeDecodeError:
            content, binary = None, True
        if binary or not include_text:
            content = None
        if not include_text:
            content_truncated = True
        files[relative] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "content": content,
            "binary": binary,
            "omitted": not include_text,
            "byte_count": len(data),
        }
    return {
        "files": files,
        "file_count": len(files),
        "eligible_file_count": len(selected) + (1 if limit_reached else 0),
        "captured_bytes": captured_bytes,
        "truncated": limit_reached or content_truncated,
    }


def _text_patch(path: str, change_type: str, before: dict[str, Any] | None,
                after: dict[str, Any] | None) -> str:
    old_content = None if before is None else before.get("content")
    new_content = None if after is None else after.get("content")
    if (before and before.get("omitted")) or (after and after.get("omitted")):
        return f"Files a/{path} and b/{path} differ; content omitted by audit limits\n"
    if (before and before.get("binary")) or (after and after.get("binary")):
        return f"Binary files a/{path} and b/{path} differ\n"
    old_lines = [] if old_content is None else str(old_content).splitlines(keepends=True)
    new_lines = [] if new_content is None else str(new_content).splitlines(keepends=True)
    from_file = "/dev/null" if change_type == "added" else f"a/{path}"
    to_file = "/dev/null" if change_type == "deleted" else f"b/{path}"
    return "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=from_file,
            tofile=to_file,
            lineterm="\n",
        )
    )


def _bounded_patch(patch: str, remaining: int) -> tuple[str, bool]:
    encoded = patch.encode("utf-8")
    if len(encoded) <= remaining:
        return patch, False
    marker = b"\n... workflow workspace diff truncated ...\n"
    available = max(0, remaining - len(marker))
    prefix = encoded[:available].decode("utf-8", errors="ignore")
    bounded = (prefix.encode("utf-8") + marker)[:remaining]
    return bounded.decode("utf-8", errors="ignore"), True


def diff_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Return bounded unified patches and an explicit truncation summary."""

    before_files = before.get("files") if isinstance(before.get("files"), dict) else {}
    after_files = after.get("files") if isinstance(after.get("files"), dict) else {}
    changed_paths = [
        path
        for path in sorted(set(before_files) | set(after_files))
        if before_files.get(path, {}).get("sha256") != after_files.get(path, {}).get("sha256")
    ]
    file_limit_truncated = len(changed_paths) > MAX_AUDIT_FILES
    changed_paths = changed_paths[:MAX_AUDIT_FILES]
    files: list[dict[str, Any]] = []
    total_bytes = 0
    patch_truncated = False
    for path in changed_paths:
        old = before_files.get(path)
        new = after_files.get(path)
        change_type = "added" if old is None else "deleted" if new is None else "modified"
        patch = redact_secrets(_text_patch(path, change_type, old, new))
        remaining = MAX_PATCH_BYTES - total_bytes
        if remaining <= 0:
            patch_truncated = True
            break
        patch, truncated = _bounded_patch(patch, remaining)
        encoded_size = len(patch.encode("utf-8"))
        total_bytes += encoded_size
        files.append({"path": path, "change_type": change_type, "patch": patch})
        if truncated:
            patch_truncated = True
            break

    truncated = bool(
        before.get("truncated")
        or after.get("truncated")
        or file_limit_truncated
        or patch_truncated
    )
    if not files and not changed_paths:
        summary = "No workspace changes detected."
    else:
        summary = f"Captured {len(files)} changed file(s) in {total_bytes} patch byte(s)."
    if truncated:
        summary += " Audit limits truncated the captured workspace diff."
    return {"files": files, "summary": summary, "truncated": truncated}
