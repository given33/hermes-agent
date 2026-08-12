"""Best-effort pre-execution backup for high-risk (destructive) commands.

The approval model for agent commands is "full access by default, but back
up before the risky ones": when a command matches a destructive pattern
(recursive deletes, truncation, shell overwrite redirects, git hard
resets / cleans / checkout discards, forced moves), the local session
snapshots what it can BEFORE the command runs, so the user can always
recover without an approval round-trip.

What gets snapshotted:

- If the working directory is inside a git work tree, ``git stash create``
  captures the full dirty tracked working tree as a commit object without
  touching it (fast, complete, no file copies). Untracked files are not
  included by ``stash create``; untracked targets named explicitly in the
  command are copied individually.
- Explicit target paths parsed from the command (existing files/dirs only)
  are copied into the backup directory, covering non-git directories and
  targets outside the repository.

Everything is best-effort: failures are logged and never block execution.
Disable with ``HERMES_RISK_BACKUP=0``; override the backup root with
``HERMES_RISK_BACKUP_DIR``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from utils import env_var_enabled
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# Destructive patterns worth a snapshot. Narrower than the approval guard's
# full DANGEROUS_PATTERNS on purpose: only data-destructive mutations, not
# permission changes or remote fetch-style commands.
_DESTRUCTIVE_PATTERNS = (
    # Unix recursive/force deletes.
    (re.compile(r"\brm\s+(?:-[^\s]*[rf][^\s]*\s+|--recursive\s+|--force\s+)"), "unix delete"),
    # GNU rm with flags after operands (rm build/ -rf).
    (re.compile(r"\brm\s+(?!--(?:\s|$))(?:(?!\s--(?:\s|$))[^\n\"';|&])*\s(?:-[a-z]*r[a-z]*\b|--recursive\b)"), "unix delete (flags after operands)"),
    # Windows destructive built-ins via cmd/powershell.
    (re.compile(r"\b(?:cmd(?:\.exe)?\s+/(?:c|k)\s+.*\b(?:del|erase|rd|rmdir)\b)", re.IGNORECASE), "windows delete"),
    (re.compile(r"\b(?:powershell|pwsh)(?:\.exe)?\b(?:\s+-\S+)*\s+(?:-(?:command|c)\s+)?[\"']?(?:remove-item|rmdir|erase|del|rd|ri|rm)\b", re.IGNORECASE), "powershell delete"),
    # Truncation and overwrite redirects.
    (re.compile(r"\btruncate\s+-s\b"), "truncate"),
    (re.compile(r"(?:^|[;&|])\s*[^>&\n]*\s>(?!=)\s*\S"), "shell overwrite redirect"),
    # git working-tree discards.
    (re.compile(r"\bgit\s+reset\s+(?:-[^\s]*\s+)*--hard\b"), "git reset --hard"),
    (re.compile(r"\bgit\s+clean\s+(?:-[^\s]*[df][^\s]*(?:\s+|$)|--force\b)"), "git clean"),
    (re.compile(r"\bgit\s+checkout\s+(?:-[^\s]*\s+)*--\b"), "git checkout discard"),
    # Forced moves/copies overwrite destinations.
    (re.compile(r"\b(?:mv|cp)\s+(-[^\s]*f[^\s]*\s+|-f\s+|--force\s+)"), "forced move/copy"),
    # dd writing to a path (not a raw device — those are hardline-blocked).
    (re.compile(r"\bdd\s+.*\bof=([^\s;|&]+)"), "dd output file"),
)

# System prefixes we never copy into backups: backing up /etc or /usr is
# both useless and unsafe to attempt from an agent session.
_SKIP_PATH_PREFIXES = (
    "/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin",
    "/boot", "/var", "/lib", "/lib64", "/run", "/opt",
)

# Budgets: keep the backup cheap enough to run on every risky command
# without adding meaningful latency to the agent loop.
_MAX_TARGETS = 64
_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 8


def _enabled() -> bool:
    # Explicitly enabled by default unless HERMES_RISK_BACKUP=0/off/no/false.
    raw = os.environ.get("HERMES_RISK_BACKUP", "")
    if raw.strip().lower() in {"0", "false", "no", "off", "disable", "disabled"}:
        return False
    return True


def _backup_root() -> Optional[Path]:
    override = os.environ.get("HERMES_RISK_BACKUP_DIR", "").strip()
    root = Path(override) if override else get_hermes_home() / "backups"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("risk backup: cannot create %s", root)
        return None
    return root


def _is_destructive(command: str) -> bool:
    lowered = command.lower()
    for pattern, _label in _DESTRUCTIVE_PATTERNS:
        if pattern.search(lowered):
            return True
    return False


def _parse_targets(command: str) -> list[str]:
    """Extract plausible file/dir operands of destructive verbs.

    Conservative by design: bare path tokens following a destructive verb,
    shell `>` redirect destinations, and `dd of=` outputs. Tokens that are
    flags, options, or command separators are dropped; only paths that
    actually exist are returned by the caller.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    destructive_verbs = {
        "rm", "del", "erase", "rd", "rmdir", "remove-item", "ri",
        "truncate", "mv", "cp",
    }
    separators = {";", "|", "&", "&&", "||", "(", ")", "`", "$(", "${"}
    targets: list[str] = []
    collecting = False
    for token in tokens:
        lower = token.lower()
        if lower in separators:
            collecting = False
            continue
        if lower in destructive_verbs:
            collecting = True
            continue
        if not collecting:
            # Shell overwrite redirect: the next token is the destination.
            if token == ">":
                collecting = True
            continue
        if token == ">":
            # Chained redirect (e.g. `cmd > a > b`): keep collecting.
            continue
        if lower.startswith("-") or lower.startswith("--"):
            continue
        # dd of=/path and similar option-attached outputs.
        match = re.match(r"^of=(.+)$", token)
        if match:
            targets.append(match.group(1))
            continue
        if "=" in token and not token.startswith(("/", "./", "../", "~")):
            continue
        targets.append(token)
    return targets


def _is_safe_to_copy(path: Path) -> bool:
    try:
        resolved = str(path.resolve())
    except OSError:
        return False
    if resolved in {"/", os.sep}:
        return False
    if any(resolved == prefix or resolved.startswith(prefix + os.sep)
           for prefix in _SKIP_PATH_PREFIXES):
        return False
    return True


def _git_snapshot(cwd: Path) -> tuple[Optional[str], Optional[str]]:
    """Return (commit_sha, repo_root) for the dirty tracked working tree."""
    try:
        root_raw = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if root_raw.returncode != 0:
            return None, None
        root = root_raw.stdout.strip()
        if not root:
            return None, None
        stash = subprocess.run(
            ["git", "-C", root, "stash", "create", "hermes-risk-backup"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        sha = stash.stdout.strip()
        if stash.returncode != 0 or not sha:
            return None, root
        return sha, root
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None


def _copy_targets(
    targets: list[str],
    backup_dir: Path,
    *,
    cwd: Path,
) -> tuple[list[str], int]:
    copied: list[str] = []
    skipped = 0
    total_bytes = 0
    seen: set[str] = set()
    for raw in targets:
        if len(copied) >= _MAX_TARGETS or total_bytes >= _MAX_TOTAL_BYTES:
            break
        expanded = os.path.expanduser(raw)
        path = Path(expanded)
        if not path.is_absolute():
            path = cwd / path
        try:
            resolved = path.resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if not path.exists() or not _is_safe_to_copy(path):
            continue
        try:
            size = shutil.disk_usage(str(path)).total if path.is_dir() else path.stat().st_size
        except OSError:
            continue
        # Directories: use a cheap size probe for very large trees.
        if path.is_dir():
            try:
                probe = subprocess.run(
                    ["du", "-sk", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                size = int(probe.stdout.split()[0]) * 1024 if probe.returncode == 0 else 0
            except (OSError, ValueError, subprocess.SubprocessError, IndexError):
                size = 0
        if size and total_bytes + size > _MAX_TOTAL_BYTES:
            skipped += 1
            continue
        rel = key.lstrip(os.sep).replace(os.sep, "_")
        dest = backup_dir / f"files/{rel}"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                shutil.copytree(path, dest, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(path, dest, follow_symlinks=True)
        except (OSError, shutil.Error):
            skipped += 1
            continue
        copied.append(key)
        total_bytes += size
    return copied, skipped


def backup_risky_command(command: str, cwd: Optional[str]) -> Optional[dict[str, Any]]:
    """Snapshot destructive command targets. Returns a manifest or None.

    Never raises: every failure path is logged and returns None so the
    command execution path is unaffected.
    """
    if not _enabled() or not command:
        return None
    if not _is_destructive(command):
        return None
    try:
        cwd_path = Path(cwd) if cwd else Path.cwd()
        cwd_path = cwd_path.resolve()
    except OSError:
        cwd_path = Path.cwd()
    root = _backup_root()
    if root is None:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(
        f"{stamp}:{command[:200]}:{cwd_path}".encode("utf-8")
    ).hexdigest()[:8]
    backup_dir = root / f"risk-{stamp}-{digest}"
    try:
        backup_dir.mkdir(parents=True, exist_ok=False)
    except OSError:
        logger.warning("risk backup: cannot create %s", backup_dir)
        return None
    git_sha, git_root = _git_snapshot(cwd_path)
    targets = _parse_targets(command)
    copied: list[str] = []
    skipped = 0
    if targets:
        try:
            copied, skipped = _copy_targets(
                targets, backup_dir, cwd=cwd_path
            )
        except Exception:
            logger.exception("risk backup: file copy failed")
    manifest = {
        "created_at": stamp,
        "command": command[:500],
        "cwd": str(cwd_path),
        "git_root": git_root,
        "git_snapshot_sha": git_sha,
        "copied": copied,
        "skipped": skipped,
        "recover": (
            f"git -C {git_root} stash apply {git_sha}" if git_sha
            else f"restore files from {backup_dir / 'files'}"
        ),
    }
    try:
        (backup_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("risk backup: cannot write manifest for %s", backup_dir)
    if git_sha or copied:
        logger.warning(
            "risk backup before destructive command: %s (git=%s, files=%d) → %s",
            command[:120], git_sha or "-", len(copied), backup_dir,
        )
    return {
        "backup_dir": str(backup_dir),
        "git_snapshot_sha": git_sha,
        "git_root": git_root,
        "copied_count": len(copied),
        "skipped": skipped,
        "recover": manifest["recover"],
    }
