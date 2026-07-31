"""Durable, allowlisted installation orchestration for managed Hermes nodes.

The main server owns the operation log. DBB3 and WSL expose the same narrow
receiver protocol and execute only structured skill, MCP, or project installs.
No endpoint accepts a command line, environment value, or credential payload.
"""

from __future__ import annotations

from contextlib import closing, contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import random
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from hermes_constants import get_hermes_home
from hermes_runtime.config import read_raw_config_strict
from hermes_services.resource_catalog import (
    ResourceRecord,
    resolve_resource_collisions,
    resource_identity,
)


KINDS = frozenset({"skill", "mcp", "project"})
NODES = frozenset({"server", "dbb3", "wsl"})
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "rolled_back"})
ACTIVE_STATES = frozenset({"pending", "accepted", "running", "retry"})
MAX_RESPONSE_BYTES = 256 * 1024
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_COMMAND_TIMEOUT_SECONDS = 15 * 60
DEFAULT_LEASE_SECONDS = 30.0
REMOTE_POLL_INTERVAL_SECONDS = 1.0
MAX_INSTALL_ATTEMPTS = 8
MAX_ERROR_LENGTH = 2048
PROBE_KIND = "probe"
PROBE_IDENTIFIER = "managed-installation-route-probe"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+#=-]{0,511}$")
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SOURCE_REF_RE = re.compile(r"^(?:HEAD|[A-Za-z0-9][A-Za-z0-9._/-]{0,255})$")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMMUTABLE_VERSION_RE = re.compile(
    r"^v?\d+(?:\.\d+){0,3}(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?$"
)
_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|token|secret|password)\s*[=:]\s*[^\s,;]+"
)
_THREADS_LOCK = threading.Lock()
_RECEIVER_THREADS: dict[str, threading.Thread] = {}
_ACCOUNT_RUNTIME_LOCK_TIMEOUT_SECONDS = 30.0
_ACCOUNT_RUNTIME_LOCK_INITIAL_DELAY_SECONDS = 0.01
_ACCOUNT_RUNTIME_LOCK_MAX_DELAY_SECONDS = 0.25
_ACCOUNT_RUNTIME_LOCK_BACKOFF_MULTIPLIER = 2.0
_ACCOUNT_RUNTIME_LOCK_JITTER_FRACTION = 0.2
_ACCOUNT_RUNTIME_METADATA = ".managed-runtime-overlay.json"
_BUILTIN_MANAGED_SOURCE_HOSTS = frozenset({"github.com"})
MANAGED_SOURCE_POLICY_VERSION = "managed-source-v2"
MANAGED_INSTALLATIONS_SCHEMA_VERSION = 2
_MANAGED_GIT_OPTIONS = (
    "-c", "http.followRedirects=false",
    "-c", "http.sslVerify=true",
    "-c", "credential.helper=",
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "protocol.file.allow=never",
    "-c", "protocol.ext.allow=never",
    "-c", "protocol.git.allow=never",
    "-c", "protocol.ssh.allow=never",
    "-c", "protocol.http.allow=never",
    "-c", "protocol.https.allow=always",
)


class _ExecutionFence:
    """An OS-backed target lock held for the full side-effect window."""

    def __init__(self, file_handle: Any) -> None:
        self.file_handle = file_handle
        self.released = False

    @property
    def fd(self) -> int:
        return self.file_handle.fileno()

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            if os.name == "nt":
                import msvcrt

                self.file_handle.seek(0)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            self.file_handle.close()


def _try_execution_fence(path: Path, operation_id: str, node_id: str) -> _ExecutionFence | None:
    lock_root = path.parent / ".managed-installation-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(f"{operation_id}:{node_id}".encode("utf-8")).hexdigest()
    lock_path = lock_root / f"{digest}.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        if os.name == "nt":
            import msvcrt

            if lock_path.stat().st_size == 0:
                handle.write(b"0")
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        handle.close()
        return None
    return _ExecutionFence(handle)


def _release_execution_fence(claimed: dict[str, Any]) -> None:
    fence = claimed.pop("_execution_fence", None)
    if isinstance(fence, _ExecutionFence):
        fence.release()


def managed_installations_db_path() -> Path:
    return Path(get_hermes_home()) / "managed-installations.db"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current_version > MANAGED_INSTALLATIONS_SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            "managed-installations.db was created by a newer Hermes version "
            f"(schema {current_version} > {MANAGED_INSTALLATIONS_SCHEMA_VERSION})"
        )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS managed_installations (
          id TEXT PRIMARY KEY,
          request_id TEXT NOT NULL UNIQUE,
          kind TEXT NOT NULL,
          identifier TEXT NOT NULL,
          profile TEXT NOT NULL,
          scope TEXT NOT NULL,
          locality TEXT NOT NULL,
          project_name TEXT NOT NULL DEFAULT '',
          canonical_source TEXT NOT NULL DEFAULT '',
          source_ref TEXT NOT NULL DEFAULT '',
          resolved_commit TEXT NOT NULL DEFAULT '',
          resolved_tree TEXT NOT NULL DEFAULT '',
          policy_version TEXT NOT NULL DEFAULT '',
          action TEXT NOT NULL DEFAULT 'install',
          rollback_of TEXT NOT NULL DEFAULT '',
          rollback_receipts_json TEXT NOT NULL DEFAULT '{}',
          state TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS managed_installation_targets (
          operation_id TEXT NOT NULL,
          node_id TEXT NOT NULL,
          state TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          failure_count INTEGER NOT NULL DEFAULT 0,
          next_attempt_at REAL NOT NULL DEFAULT 0,
          lease_token TEXT NOT NULL DEFAULT '',
          lease_until REAL NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL,
          error TEXT NOT NULL DEFAULT '',
          detail_json TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY (operation_id, node_id),
          FOREIGN KEY (operation_id) REFERENCES managed_installations(id)
            ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS managed_installation_targets_ready
          ON managed_installation_targets(state, next_attempt_at, lease_until);
        CREATE TABLE IF NOT EXISTS managed_resource_catalog (
          resource_id TEXT PRIMARY KEY,
          operation_id TEXT NOT NULL UNIQUE,
          owner_id TEXT NOT NULL DEFAULT 'server-admin',
          account_generation TEXT NOT NULL DEFAULT '',
          record_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS managed_resource_events (
          cursor INTEGER PRIMARY KEY AUTOINCREMENT,
          resource_id TEXT NOT NULL,
          operation_id TEXT NOT NULL,
          owner_id TEXT NOT NULL DEFAULT 'server-admin',
          account_generation TEXT NOT NULL DEFAULT '',
          event_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS managed_resource_events_operation
          ON managed_resource_events(operation_id, cursor);
        CREATE TABLE IF NOT EXISTS managed_owner_tombstones (
          owner_id TEXT NOT NULL,
          account_generation TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending',
          updated_at TEXT NOT NULL,
          error TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (owner_id, account_generation)
        );
        CREATE TABLE IF NOT EXISTS managed_owner_deletion_targets (
          owner_id TEXT NOT NULL,
          account_generation TEXT NOT NULL,
          node_id TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt_at REAL NOT NULL DEFAULT 0,
          lease_token TEXT NOT NULL DEFAULT '',
          lease_until REAL NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL,
          error TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (owner_id, account_generation, node_id),
          FOREIGN KEY (owner_id, account_generation)
            REFERENCES managed_owner_tombstones(owner_id, account_generation)
            ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS managed_owner_deletions_ready
          ON managed_owner_deletion_targets(state, next_attempt_at, lease_until);
        """
    )
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(managed_installation_targets)").fetchall()
    }
    if "failure_count" not in columns:
        conn.execute(
            "ALTER TABLE managed_installation_targets "
            "ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0"
        )
    operation_columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(managed_installations)").fetchall()
    }
    if "owner_id" not in operation_columns:
        conn.execute(
            "ALTER TABLE managed_installations "
            "ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'server-admin'"
        )
    if "account_generation" not in operation_columns:
        conn.execute(
            "ALTER TABLE managed_installations "
            "ADD COLUMN account_generation TEXT NOT NULL DEFAULT ''"
        )
    source_columns = {
        "canonical_source": "TEXT NOT NULL DEFAULT ''",
        "source_ref": "TEXT NOT NULL DEFAULT ''",
        "resolved_commit": "TEXT NOT NULL DEFAULT ''",
        "resolved_tree": "TEXT NOT NULL DEFAULT ''",
        "policy_version": "TEXT NOT NULL DEFAULT ''",
        "action": "TEXT NOT NULL DEFAULT 'install'",
        "rollback_of": "TEXT NOT NULL DEFAULT ''",
        "rollback_receipts_json": "TEXT NOT NULL DEFAULT '{}'",
    }
    for column, declaration in source_columns.items():
        if column not in operation_columns:
            conn.execute(
                f"ALTER TABLE managed_installations ADD COLUMN {column} {declaration}"
            )
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS managed_installation_source_lock_immutable
        BEFORE UPDATE OF canonical_source,source_ref,resolved_commit,resolved_tree,policy_version
        ON managed_installations
        WHEN OLD.canonical_source != NEW.canonical_source
          OR OLD.source_ref != NEW.source_ref
          OR OLD.resolved_commit != NEW.resolved_commit
          OR OLD.resolved_tree != NEW.resolved_tree
          OR OLD.policy_version != NEW.policy_version
        BEGIN
          SELECT RAISE(ABORT, 'managed installation source lock is immutable');
        END;
        """
    )
    for table in ("managed_resource_catalog", "managed_resource_events"):
        resource_columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if "owner_id" not in resource_columns:
            conn.execute(
                f"ALTER TABLE {table} "
                "ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'server-admin'"
            )
        if "account_generation" not in resource_columns:
            conn.execute(
                f"ALTER TABLE {table} "
                "ADD COLUMN account_generation TEXT NOT NULL DEFAULT ''"
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS managed_resource_catalog_owner "
        "ON managed_resource_catalog(owner_id, account_generation, updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS managed_resource_events_owner "
        "ON managed_resource_events(owner_id, account_generation, cursor)"
    )
    conn.execute(f"PRAGMA user_version={MANAGED_INSTALLATIONS_SCHEMA_VERSION}")
    return conn


def connect_managed_installations_database(path: Path) -> sqlite3.Connection:
    """Open and migrate the database for deployment health tooling."""

    return _connect(path)


def _normalize_owner_boundary(
    owner_id: str | None,
    account_generation: str | None,
) -> tuple[str, str]:
    owner = str(owner_id or "server-admin").strip() or "server-admin"
    generation = str(account_generation or "").strip()
    if owner != "server-admin" and not generation:
        raise ValueError("account_generation is required for account-scoped installations")
    if len(owner) > 256 or len(generation) > 256 or "\x00" in owner + generation:
        raise ValueError("invalid installation owner boundary")
    return owner, generation


def _owner_boundary_digest(owner_id: str, account_generation: str) -> str:
    return hashlib.sha256(
        f"{owner_id}\0{account_generation}".encode("utf-8")
    ).hexdigest()


def _account_profile_name(owner_id: str, account_generation: str, profile: str) -> str:
    boundary = _owner_boundary_digest(owner_id, account_generation)[:20]
    profile_digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:12]
    return f"acct-{boundary}-{profile_digest}"


def _account_resource_profile_name(owner_id: str, account_generation: str) -> str:
    """Return the profile that owns resources shared by all account roles."""

    boundary = _owner_boundary_digest(owner_id, account_generation)[:20]
    return f"acct-{boundary}-resources"


def _ensure_boundary_marker(
    marker: Path,
    expected: dict[str, Any],
    *,
    label: str,
) -> None:
    if marker.is_symlink():
        raise RuntimeError(f"{label} boundary marker is unsafe")
    encoded = (
        json.dumps(expected, ensure_ascii=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    created = False
    try:
        descriptor = os.open(marker, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        pass
    except BaseException:
        if created:
            marker.unlink(missing_ok=True)
        raise
    try:
        actual = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} boundary marker is invalid") from exc
    if actual != expected:
        raise RuntimeError(f"{label} boundary collision")


def _account_profile_home(
    owner_id: str,
    account_generation: str,
    profile: str,
    *,
    create: bool,
) -> Path:
    from hermes_cli.profiles import get_profile_dir

    candidate = get_profile_dir(
        _account_profile_name(owner_id, account_generation, profile)
    )
    profiles_root = candidate.parent.resolve()
    if candidate.is_symlink():
        raise RuntimeError("managed account profile path is unsafe")
    profile_home = candidate.resolve()
    profile_home.relative_to(profiles_root)
    if not create:
        return profile_home
    profile_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = profile_home / ".managed-owner-boundary.json"
    expected = {
        "version": 1,
        "boundary": _owner_boundary_digest(owner_id, account_generation),
        "profile": profile,
    }
    _ensure_boundary_marker(marker, expected, label="managed account profile")
    for directory in ("skills", "mcp-installs", "logs", "home"):
        (profile_home / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
    (profile_home / ".no-bundled-skills").touch(exist_ok=True)
    return profile_home


def _account_resource_home(
    owner_id: str,
    account_generation: str,
    *,
    create: bool,
) -> Path:
    """Return the generation-scoped Skill/MCP store used by every role profile."""

    from hermes_cli.profiles import get_profile_dir

    candidate = get_profile_dir(
        _account_resource_profile_name(owner_id, account_generation)
    )
    profiles_root = candidate.parent.resolve()
    if candidate.is_symlink():
        raise RuntimeError("managed account resource path is unsafe")
    resource_home = candidate.resolve()
    resource_home.relative_to(profiles_root)
    if not create:
        return resource_home
    resource_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = resource_home / ".managed-owner-boundary.json"
    expected = {
        "version": 1,
        "boundary": _owner_boundary_digest(owner_id, account_generation),
        "profile": "resources",
    }
    _ensure_boundary_marker(marker, expected, label="managed account resource")
    for directory in ("skills", "mcp-installs", "logs", "home"):
        (resource_home / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
    (resource_home / ".no-bundled-skills").touch(exist_ok=True)
    return resource_home


@contextmanager
def _account_runtime_lock(
    owner_id: str,
    account_generation: str,
    profile: str,
    *,
    timeout: float = _ACCOUNT_RUNTIME_LOCK_TIMEOUT_SECONDS,
):
    """Serialize all resource, runtime, and deletion work for one generation."""

    from hermes_cli.profiles import get_profile_dir

    profiles_root = get_profile_dir("managed-placeholder").parent.resolve()
    anchor = profiles_root / ".managed-account-runtime"
    deadline = time.monotonic() + max(0.0, float(timeout))
    boundary = _owner_boundary_digest(owner_id, account_generation)
    retry_delay = _ACCOUNT_RUNTIME_LOCK_INITIAL_DELAY_SECONDS
    while True:
        # The resource store is shared by every logical role profile. A single
        # boundary lock prevents one role from materializing a partially
        # installed Skill/MCP while another role is updating that store.
        fence = _try_execution_fence(anchor, f"runtime:{boundary}", "account")
        if fence is not None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("managed account runtime is busy")
        jitter = random.uniform(
            1.0 - _ACCOUNT_RUNTIME_LOCK_JITTER_FRACTION,
            1.0 + _ACCOUNT_RUNTIME_LOCK_JITTER_FRACTION,
        )
        time.sleep(min(remaining, retry_delay * jitter))
        retry_delay = min(
            _ACCOUNT_RUNTIME_LOCK_MAX_DELAY_SECONDS,
            retry_delay * _ACCOUNT_RUNTIME_LOCK_BACKOFF_MULTIPLIER,
        )
    try:
        yield
    finally:
        fence.release()


def _read_yaml_mapping(path: Path, *, label: str) -> dict[str, Any]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} path is unsafe")
    try:
        value = read_raw_config_strict(config_path=path)
    except Exception as exc:
        raise RuntimeError(f"{label} cannot be read") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must contain a mapping")
    return dict(value)


def _atomic_runtime_write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Install a complete runtime file without exposing a partial document."""

    if path.is_symlink():
        raise RuntimeError(f"managed runtime target is unsafe: {path.name}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory = -1
        if directory >= 0:
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _runtime_overlay_metadata(profile_home: Path) -> dict[str, Any]:
    path = profile_home / _ACCOUNT_RUNTIME_METADATA
    if not path.exists():
        return {"version": 1, "account_mcp_servers": []}
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("managed runtime metadata is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("managed runtime metadata is invalid") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise RuntimeError("managed runtime metadata is invalid")
    names = value.get("account_mcp_servers") or []
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        raise RuntimeError("managed runtime metadata is invalid")
    return {"version": 1, "account_mcp_servers": sorted(set(names))}


def _write_runtime_overlay_metadata(
    profile_home: Path,
    metadata: dict[str, Any],
) -> None:
    encoded = (
        json.dumps(metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    _atomic_runtime_write(profile_home / _ACCOUNT_RUNTIME_METADATA, encoded)


def _record_account_mcp_server(profile_home: Path, server_name: str) -> None:
    metadata = _runtime_overlay_metadata(profile_home)
    names = set(metadata["account_mcp_servers"])
    names.add(str(server_name).strip())
    metadata["account_mcp_servers"] = sorted(name for name in names if name)
    _write_runtime_overlay_metadata(profile_home, metadata)


def _absolute_external_skill_dirs(
    base_home: Path,
    raw: Any,
    *,
    resource_home: Path | None = None,
) -> list[str]:
    values = [raw] if isinstance(raw, str) else raw
    if not isinstance(values, list):
        values = []
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        expanded = Path(os.path.expanduser(os.path.expandvars(text)))
        resolved = expanded.resolve() if expanded.is_absolute() else (base_home / expanded).resolve()
        rendered = str(resolved)
        if rendered not in result:
            result.append(rendered)
    base_skills = base_home / "skills"
    if base_skills.is_dir():
        rendered = str(base_skills.resolve())
        if rendered not in result:
            result.append(rendered)
    if resource_home is not None:
        account_skills = resource_home / "skills"
        if account_skills.is_dir():
            rendered = str(account_skills.resolve())
            if rendered not in result:
                result.append(rendered)
    return result


def _materialize_account_runtime_locked(
    owner_id: str,
    account_generation: str,
    profile: str,
    *,
    profile_home: Path,
    base_home: Path,
    resource_home: Path | None = None,
) -> Path:
    """Build an account-isolated runtime that inherits only base capabilities."""

    import yaml

    account_home = Path(resource_home or profile_home).resolve()
    if profile_home == base_home or account_home == base_home:
        raise RuntimeError("managed account runtime must not alias its base profile")
    base_config = _read_yaml_mapping(base_home / "config.yaml", label="base profile config")
    account_config = _read_yaml_mapping(
        account_home / "config.yaml", label="managed account config"
    )
    metadata = _runtime_overlay_metadata(account_home)
    account_names = set(metadata["account_mcp_servers"])
    account_servers = account_config.get("mcp_servers")
    account_servers = account_servers if isinstance(account_servers, dict) else {}

    merged = deepcopy(base_config)
    base_servers = merged.get("mcp_servers")
    merged_servers = deepcopy(base_servers) if isinstance(base_servers, dict) else {}
    for name in sorted(account_names):
        entry = account_servers.get(name)
        if isinstance(entry, dict):
            merged_servers[name] = deepcopy(entry)
    if merged_servers:
        merged["mcp_servers"] = merged_servers
    else:
        merged.pop("mcp_servers", None)

    raw_skills = merged.get("skills")
    skills_config = deepcopy(raw_skills) if isinstance(raw_skills, dict) else {}
    skills_config["external_dirs"] = _absolute_external_skill_dirs(
        base_home,
        skills_config.get("external_dirs"),
        resource_home=account_home,
    )
    merged["skills"] = skills_config

    encoded_config = yaml.safe_dump(
        merged,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).encode("utf-8")
    _atomic_runtime_write(profile_home / "config.yaml", encoded_config)

    base_env = base_home / ".env"
    env_content = base_env.read_bytes() if base_env.is_file() and not base_env.is_symlink() else b""
    _atomic_runtime_write(profile_home / ".env", env_content)
    base_soul = base_home / "SOUL.md"
    if base_soul.is_file() and not base_soul.is_symlink():
        _atomic_runtime_write(profile_home / "SOUL.md", base_soul.read_bytes())
    if profile_home == account_home:
        _write_runtime_overlay_metadata(account_home, metadata)
    return profile_home


def managed_account_runtime_home(
    owner_id: str,
    account_generation: str,
    profile: str,
    *,
    db_path: Path | None = None,
    base_profile_home: Path | None = None,
) -> Path:
    """Return the ready-to-run account overlay for a hosted conversation."""

    owner, generation = _normalize_owner_boundary(owner_id, account_generation)
    if owner == "server-admin":
        raise ValueError("an account owner_id is required")
    path = db_path or managed_installations_db_path()
    with closing(_connect(path)) as conn:
        deleted = conn.execute(
            "SELECT 1 FROM managed_owner_tombstones "
            "WHERE owner_id=? AND account_generation=?",
            (owner, generation),
        ).fetchone()
    if deleted is not None:
        raise PermissionError("account generation is deleted")

    resource_home = _account_resource_home(owner, generation, create=True)
    profile_home = _account_profile_home(owner, generation, profile, create=True)
    base_home = Path(base_profile_home or _profile_home(profile)).resolve()
    with _account_runtime_lock(owner, generation, profile):
        # Recheck after acquiring the filesystem lock so deletion cannot win
        # between the database fence and runtime materialization.
        with closing(_connect(path)) as conn:
            deleted = conn.execute(
                "SELECT 1 FROM managed_owner_tombstones "
                "WHERE owner_id=? AND account_generation=?",
                (owner, generation),
            ).fetchone()
        if deleted is not None:
            raise PermissionError("account generation is deleted")
        return _materialize_account_runtime_locked(
            owner,
            generation,
            profile,
            profile_home=profile_home,
            base_home=base_home,
            resource_home=resource_home,
        )


def managed_account_runtime_profile(
    owner_id: str,
    account_generation: str,
    profile: str,
    *,
    db_path: Path | None = None,
    base_profile_home: Path | None = None,
) -> str:
    """Materialize an account runtime and return its internal profile name."""

    owner, generation = _normalize_owner_boundary(owner_id, account_generation)
    normalized_profile = str(profile or "default").strip() or "default"
    runtime_home = managed_account_runtime_home(
        owner,
        generation,
        normalized_profile,
        db_path=db_path,
        base_profile_home=base_profile_home,
    )
    expected = _account_profile_name(owner, generation, normalized_profile)
    if runtime_home.name != expected:
        raise RuntimeError("managed account runtime resolved outside its profile")
    return expected


def _account_project_root(
    base_root: Path,
    owner_id: str,
    account_generation: str,
    *,
    create: bool,
) -> Path:
    boundary = _owner_boundary_digest(owner_id, account_generation)
    resolved_base = base_root.resolve()
    accounts_root = resolved_base / ".managed-accounts"
    candidate = accounts_root / boundary[:24]
    if accounts_root.is_symlink() or candidate.is_symlink():
        raise RuntimeError("managed project boundary path is unsafe")
    root = candidate.resolve()
    root.relative_to(resolved_base)
    marker = root / ".managed-owner-boundary.json"
    if not create:
        return root
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    expected = {"version": 1, "boundary": boundary}
    _ensure_boundary_marker(marker, expected, label="managed project")
    return root


def _normalize_kind(value: str) -> str:
    kind = str(value or "").strip().lower()
    if kind not in KINDS:
        raise ValueError("kind must be skill, mcp, or project")
    return kind


def _managed_source_hosts() -> frozenset[str]:
    """Return the immutable source-host trust root.

    Runtime host extensions cannot be made safe for git clone without pinning
    the validated peer address while preserving TLS hostname verification.
    Keep the release path on audited built-in hosts only.
    """

    return _BUILTIN_MANAGED_SOURCE_HOSTS


def _validate_managed_https_source(identifier: str) -> str:
    try:
        parsed = urlsplit(identifier)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("managed source URL is invalid") from exc
    host = (parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
        or "%" in parsed.netloc
        or "\\" in parsed.netloc
    ):
        raise ValueError(
            "managed source must be credential-free HTTPS on port 443 without query or fragment"
        )
    try:
        host.encode("ascii")
        ipaddress.ip_address(host)
    except UnicodeEncodeError as exc:
        raise ValueError("managed source hostname must be ASCII") from exc
    except ValueError:
        pass
    else:
        raise ValueError("managed source IP literals are not allowed")
    if host not in _managed_source_hosts():
        raise ValueError("managed source host is not approved")
    return identifier


def _normalize_identifier(kind: str, value: str) -> str:
    identifier = str(value or "").strip()
    if not identifier or len(identifier) > 1024 or "\x00" in identifier:
        raise ValueError("identifier is required")
    if kind == "project":
        if "://" not in identifier:
            if not re.fullmatch(r"github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?", identifier):
                raise ValueError("project source must be an HTTPS git repository")
            identifier = f"https://{identifier.rstrip('/')}"
        try:
            return _validate_managed_https_source(identifier)
        except ValueError as exc:
            raise ValueError(f"project source must be an approved HTTPS git repository: {exc}") from exc
    if kind == "mcp" and "://" in identifier:
        raise ValueError("MCP identifier must be a catalog name")
    if identifier.startswith("https://"):
        try:
            return _validate_managed_https_source(identifier)
        except ValueError as exc:
            if "credential-free" in str(exc):
                raise ValueError(
                    "HTTPS identifiers must not contain credentials, query, or fragment"
                ) from exc
            raise
    if not _IDENTIFIER_RE.fullmatch(identifier):
        raise ValueError("identifier contains unsupported characters")
    return identifier


def resolve_installation_targets(
    kind: str,
    *,
    scope: str = "auto",
    locality: str = "portable",
    targets: Iterable[str] | None = None,
) -> list[str]:
    """Resolve the product policy into an explicit, stable node list."""

    normalized_kind = _normalize_kind(kind)
    explicit = [str(item).strip().lower() for item in (targets or []) if str(item).strip()]
    if explicit:
        if len(set(explicit)) != len(explicit) or any(item not in NODES for item in explicit):
            raise ValueError("targets must contain unique server, dbb3, or wsl values")
        return [node for node in ("server", "dbb3", "wsl") if node in explicit]

    normalized_scope = str(scope or "auto").strip().lower()
    if normalized_scope in {"fleet", "all"}:
        return ["server", "dbb3", "wsl"]
    if normalized_scope == "server":
        return ["server"]
    if normalized_scope in {"workers", "worker"}:
        return ["dbb3", "wsl"]
    if normalized_scope != "auto":
        raise ValueError("scope must be auto, fleet, server, or workers")

    if normalized_kind == "skill":
        return ["server", "dbb3", "wsl"]
    if normalized_kind == "project":
        return ["dbb3", "wsl"]

    normalized_locality = str(locality or "portable").strip().lower()
    if normalized_locality in {"portable", "network", "ios-relay", "auto"}:
        return ["server", "dbb3", "wsl"]
    if normalized_locality == "server":
        return ["server"]
    if normalized_locality in {"worker", "workers"}:
        return ["dbb3", "wsl"]
    if normalized_locality == "node":
        raise ValueError("node-local MCP installation requires explicit targets")
    raise ValueError("unsupported MCP locality")


def require_managed_installation_topology(
    targets: Iterable[str],
    *,
    config_path: Path | None = None,
) -> None:
    """Fail before persistence when the main-server fleet is not deployable."""

    from hermes_cli.managed_nodes import load_managed_nodes_config

    resolved_targets = [str(item).strip().lower() for item in targets]
    configs = load_managed_nodes_config(config_path)
    if not configs:
        raise RuntimeError("managed-nodes configuration is required for fleet installations")
    for node_id in resolved_targets:
        if node_id != "server":
            _installation_route(node_id, config_path)


def _normalize_source_ref(value: str) -> str:
    source_ref = str(value or "HEAD").strip() or "HEAD"
    if (
        not _SOURCE_REF_RE.fullmatch(source_ref)
        or ".." in source_ref
        or "//" in source_ref
        or "@{" in source_ref
        or source_ref.endswith(("/", ".", ".lock"))
    ):
        raise ValueError("project source_ref is invalid")
    return source_ref


def _managed_source_curl_resolve(identifier: str) -> tuple[str, ...]:
    """Resolve once, reject non-public peers, and pin Git's actual connection."""

    canonical = _normalize_project_origin(identifier)
    parsed = urlsplit(canonical)
    host = str(parsed.hostname or "").lower()
    try:
        answers = socket.getaddrinfo(
            host,
            443,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise RuntimeError("managed project source DNS resolution failed") from exc
    addresses: list[str] = []
    for answer in answers:
        raw = str(answer[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise RuntimeError("managed project source DNS returned an invalid address") from exc
        if not address.is_global:
            raise RuntimeError("managed project source DNS returned a non-public address")
        normalized = address.compressed
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise RuntimeError("managed project source DNS returned no usable addresses")
    return tuple(
        f"+{host}:443:{'[' + address + ']' if ':' in address else address}"
        for address in addresses
    )


def _validate_project_source_lock(
    identifier: str,
    source_ref: str,
    source_lock: dict[str, Any],
) -> dict[str, str]:
    canonical = _normalize_project_origin(identifier)
    locked = {
        "canonical_source": str(source_lock.get("canonical_source") or "").strip(),
        "source_ref": _normalize_source_ref(
            str(source_lock.get("source_ref") or source_ref)
        ),
        "resolved_commit": str(source_lock.get("resolved_commit") or "").strip().lower(),
        "resolved_tree": str(source_lock.get("resolved_tree") or "").strip().lower(),
        "policy_version": str(source_lock.get("policy_version") or "").strip(),
    }
    if locked["canonical_source"] != canonical:
        raise ValueError("managed project source lock canonical URL does not match")
    if locked["source_ref"] != source_ref:
        raise ValueError("managed project source lock ref does not match")
    if not _GIT_COMMIT_RE.fullmatch(locked["resolved_commit"]):
        raise ValueError("managed project source lock commit is invalid")
    if not _GIT_COMMIT_RE.fullmatch(locked["resolved_tree"]):
        raise ValueError("managed project source lock tree is invalid")
    if locked["policy_version"] != MANAGED_SOURCE_POLICY_VERSION:
        raise ValueError("managed project source lock policy is unsupported")
    return locked


def _resolve_managed_project_source(
    identifier: str,
    source_ref: str,
    *,
    runner: Any = None,
) -> dict[str, str]:
    """Resolve a mutable ref to immutable commit/tree objects before persistence."""

    canonical = _normalize_project_origin(identifier)
    normalized_ref = _normalize_source_ref(source_ref)
    pins = _managed_source_curl_resolve(canonical)
    execute = runner or _run_command
    with tempfile.TemporaryDirectory(prefix="hermes-managed-source-") as temporary:
        repository = Path(temporary) / "source.git"
        execute(
            _managed_git_command("init", "--bare", str(repository)),
            timeout=30,
        )
        execute(
            _managed_git_command(
                "-C", str(repository),
                "fetch", "--no-tags", "--depth=1", "--filter=blob:none",
                "--", canonical, normalized_ref,
                curl_resolve=pins,
            ),
            timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )
        commit = _command_output(execute(
            _managed_git_command(
                "-C", str(repository), "rev-parse", "--verify", "FETCH_HEAD^{commit}"
            ),
            timeout=30,
        )).lower()
        tree = _command_output(execute(
            _managed_git_command(
                "-C", str(repository), "rev-parse", "--verify", "FETCH_HEAD^{tree}"
            ),
            timeout=30,
        )).lower()
    return _validate_project_source_lock(
        canonical,
        normalized_ref,
        {
            "canonical_source": canonical,
            "source_ref": normalized_ref,
            "resolved_commit": commit,
            "resolved_tree": tree,
            "policy_version": MANAGED_SOURCE_POLICY_VERSION,
        },
    )


def create_managed_installation(
    *,
    kind: str,
    identifier: str,
    profile: str = "default",
    request_id: str = "",
    scope: str = "auto",
    locality: str = "portable",
    targets: Iterable[str] | None = None,
    project_name: str = "",
    source_ref: str = "",
    source_lock: dict[str, Any] | None = None,
    db_path: Path | None = None,
    require_topology: bool = False,
    config_path: Path | None = None,
    owner_id: str | None = None,
    account_generation: str | None = None,
    _action: str = "install",
    _rollback_of: str = "",
    _rollback_receipts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized_action = str(_action or "install").strip().lower()
    if normalized_action not in {"install", "rollback"}:
        raise ValueError("managed installation action is invalid")
    normalized_rollback_of = str(_rollback_of or "").strip()
    if normalized_action == "rollback" and not normalized_rollback_of:
        raise ValueError("rollback operation requires its installation id")
    if normalized_action == "install" and normalized_rollback_of:
        raise ValueError("install operation cannot reference a rollback source")
    rollback_receipts = dict(_rollback_receipts or {})
    if any(node not in NODES or not isinstance(value, dict) for node, value in rollback_receipts.items()):
        raise ValueError("rollback receipts are invalid")
    encoded_rollback_receipts = json.dumps(
        rollback_receipts,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    normalized_kind = _normalize_kind(kind)
    normalized_identifier = _normalize_identifier(normalized_kind, identifier)
    normalized_profile = str(profile or "default").strip() or "default"
    if len(normalized_profile) > 80 or not re.fullmatch(r"[A-Za-z0-9._-]+", normalized_profile):
        raise ValueError("profile contains unsupported characters")
    normalized_project_name = str(project_name or "").strip()
    if normalized_project_name and not _PROJECT_NAME_RE.fullmatch(normalized_project_name):
        raise ValueError("project_name contains unsupported characters")
    normalized_source_ref = (
        _normalize_source_ref(source_ref) if normalized_kind == "project" else ""
    )
    if normalized_kind != "project" and (source_ref or source_lock):
        raise ValueError("source_ref and source_lock are only valid for projects")
    provided_source_lock = (
        _validate_project_source_lock(
            normalized_identifier,
            normalized_source_ref,
            source_lock,
        )
        if source_lock is not None
        else None
    )
    normalized_owner, normalized_generation = _normalize_owner_boundary(
        owner_id, account_generation
    )
    normalized_request_id = str(request_id or "").strip() or f"install-{uuid4()}"
    if len(normalized_request_id) > 160 or "\x00" in normalized_request_id:
        raise ValueError("request_id is invalid")
    stored_request_id = normalized_request_id
    if normalized_owner != "server-admin":
        boundary = hashlib.sha256(
            f"{normalized_owner}\0{normalized_generation}".encode("utf-8")
        ).hexdigest()
        stored_request_id = f"acct:{boundary}:{normalized_request_id}"
    resolved_targets = resolve_installation_targets(
        normalized_kind,
        scope=scope,
        locality=locality,
        targets=targets,
    )
    if require_topology:
        require_managed_installation_topology(resolved_targets, config_path=config_path)
    path = db_path or managed_installations_db_path()
    now = _utc_now()
    operation_id = f"mi-{uuid4()}"
    expected_request = (
        normalized_kind,
        normalized_identifier,
        normalized_profile,
        str(scope or "auto").strip().lower(),
        str(locality or "portable").strip().lower(),
        normalized_project_name,
        normalized_source_ref,
        normalized_action,
        normalized_rollback_of,
        encoded_rollback_receipts,
        resolved_targets,
    )

    # Return an already accepted idempotency key before resolving a ref again.
    # A branch may have moved since the first request; the original source lock
    # remains authoritative for every replay of that request id.
    with closing(_connect(path)) as conn:
        existing = conn.execute(
            "SELECT * FROM managed_installations WHERE request_id = ? "
            "AND owner_id = ? AND account_generation = ?",
            (stored_request_id, normalized_owner, normalized_generation),
        ).fetchone()
        if existing is not None:
            existing_targets = [
                str(row["node_id"])
                for row in conn.execute(
                    "SELECT node_id FROM managed_installation_targets "
                    "WHERE operation_id = ? ORDER BY CASE node_id "
                    "WHEN 'server' THEN 0 WHEN 'dbb3' THEN 1 ELSE 2 END",
                    (existing["id"],),
                ).fetchall()
            ]
            actual_request = (
                str(existing["kind"]),
                str(existing["identifier"]),
                str(existing["profile"]),
                str(existing["scope"]),
                str(existing["locality"]),
                str(existing["project_name"]),
                str(existing["source_ref"]),
                str(existing["action"]),
                str(existing["rollback_of"]),
                str(existing["rollback_receipts_json"]),
                existing_targets,
            )
            existing_lock = {
                key: str(existing[key])
                for key in (
                    "canonical_source", "source_ref", "resolved_commit",
                    "resolved_tree", "policy_version",
                )
            }
            if actual_request != expected_request or (
                provided_source_lock is not None
                and existing_lock != provided_source_lock
            ):
                raise ValueError("request_id is already bound to a different installation")
            return _operation_payload(conn, existing)

    if normalized_kind == "project":
        resolved_source = provided_source_lock or (
            _resolve_managed_project_source(
                normalized_identifier,
                normalized_source_ref,
            )
            if require_topology
            else {
                "canonical_source": _normalize_project_origin(normalized_identifier),
                "source_ref": normalized_source_ref,
                "resolved_commit": "",
                "resolved_tree": "",
                "policy_version": MANAGED_SOURCE_POLICY_VERSION,
            }
        )
    else:
        resolved_source = {
            "canonical_source": "",
            "source_ref": "",
            "resolved_commit": "",
            "resolved_tree": "",
            "policy_version": MANAGED_SOURCE_POLICY_VERSION,
        }
    with closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if normalized_owner != "server-admin":
            tombstone = conn.execute(
                "SELECT state FROM managed_owner_tombstones "
                "WHERE owner_id=? AND account_generation=?",
                (normalized_owner, normalized_generation),
            ).fetchone()
            if tombstone is not None:
                conn.execute("ROLLBACK")
                raise ValueError("account generation is deleted")
        existing = conn.execute(
            "SELECT * FROM managed_installations WHERE request_id = ? "
            "AND owner_id = ? AND account_generation = ?",
            (stored_request_id, normalized_owner, normalized_generation),
        ).fetchone()
        if existing:
            existing_targets = [
                str(row["node_id"])
                for row in conn.execute(
                    "SELECT node_id FROM managed_installation_targets "
                    "WHERE operation_id = ? ORDER BY CASE node_id "
                    "WHEN 'server' THEN 0 WHEN 'dbb3' THEN 1 ELSE 2 END",
                    (existing["id"],),
                ).fetchall()
            ]
            actual = (
                str(existing["kind"]),
                str(existing["identifier"]),
                str(existing["profile"]),
                str(existing["scope"]),
                str(existing["locality"]),
                str(existing["project_name"]),
                str(existing["source_ref"]),
                str(existing["action"]),
                str(existing["rollback_of"]),
                str(existing["rollback_receipts_json"]),
                existing_targets,
            )
            if actual != expected_request:
                conn.execute("ROLLBACK")
                raise ValueError("request_id is already bound to a different installation")
            conn.execute("COMMIT")
            return get_managed_installation(str(existing["id"]), db_path=path)
        conn.execute(
            """
            INSERT INTO managed_installations(
              id, request_id, kind, identifier, profile, scope, locality,
              project_name, canonical_source, source_ref, resolved_commit,
              resolved_tree, policy_version, action, rollback_of,
              rollback_receipts_json, state, created_at, updated_at, owner_id,
              account_generation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?)
            """,
            (
                operation_id,
                stored_request_id,
                normalized_kind,
                normalized_identifier,
                normalized_profile,
                str(scope or "auto").strip().lower(),
                str(locality or "portable").strip().lower(),
                normalized_project_name,
                resolved_source["canonical_source"],
                resolved_source["source_ref"],
                resolved_source["resolved_commit"],
                resolved_source["resolved_tree"],
                resolved_source["policy_version"],
                normalized_action,
                normalized_rollback_of,
                encoded_rollback_receipts,
                now,
                now,
                normalized_owner,
                normalized_generation,
            ),
        )
        conn.executemany(
            """
            INSERT INTO managed_installation_targets(
              operation_id, node_id, state, updated_at
            ) VALUES (?, ?, 'pending', ?)
            """,
            [(operation_id, node_id, now) for node_id in resolved_targets],
        )
        conn.execute("COMMIT")
    return get_managed_installation(operation_id, db_path=path)


def _operation_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    targets = conn.execute(
        """
        SELECT node_id, state, attempts, failure_count, updated_at, error, detail_json
        FROM managed_installation_targets
        WHERE operation_id = ? ORDER BY CASE node_id
          WHEN 'server' THEN 0 WHEN 'dbb3' THEN 1 ELSE 2 END
        """,
        (row["id"],),
    ).fetchall()
    aggregate_state = _aggregate_installation_state(row, targets)
    return {
        "id": row["id"],
        "request_id": (
            str(row["request_id"]).split(":", 2)[-1]
            if str(row["request_id"]).startswith("acct:")
            else row["request_id"]
        ),
        "kind": row["kind"],
        "identifier": row["identifier"],
        "profile": row["profile"],
        "scope": row["scope"],
        "locality": row["locality"],
        "project_name": row["project_name"],
        "action": row["action"],
        "rollback_of": row["rollback_of"],
        "source_lock": {
            "canonical_source": row["canonical_source"],
            "source_ref": row["source_ref"],
            "resolved_commit": row["resolved_commit"],
            "resolved_tree": row["resolved_tree"],
            "policy_version": row["policy_version"],
        },
        "state": row["state"],
        "aggregate_state": aggregate_state,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "error": row["error"],
        "owner_id": row["owner_id"],
        "account_generation": row["account_generation"],
        "targets": [
            {
                "node_id": target["node_id"],
                "state": target["state"],
                "attempts": int(target["attempts"]),
                "failure_count": int(target["failure_count"]),
                "updated_at": target["updated_at"],
                "error": target["error"],
                "detail": _safe_json_object(target["detail_json"]),
            }
            for target in targets
        ],
    }


def _validated_target_receipt(
    operation: sqlite3.Row | dict[str, Any],
    detail: dict[str, Any],
) -> tuple[tuple[str, str], str]:
    proof, reason = _validated_managed_resource_proof(detail)
    if reason:
        return proof, reason
    try:
        receipt_schema = int(detail.get("receipt_schema") or 0)
    except (TypeError, ValueError):
        receipt_schema = 0
    expected_commit = str(operation["resolved_commit"] or "").lower()
    if (
        str(operation["policy_version"] or "") == MANAGED_SOURCE_POLICY_VERSION
        and receipt_schema != 1
    ):
        return ("", ""), "managed installation is missing a versioned node receipt"
    if receipt_schema == 1:
        artifact_hash = str(detail.get("artifact_hash") or "").lower()
        if not _SHA256_RE.fullmatch(artifact_hash):
            return ("", ""), "node receipt artifact hash is invalid"
        if artifact_hash != proof[1]:
            return ("", ""), "node receipt artifact hash does not match local proof"
        if not isinstance(detail.get("tools"), list):
            return ("", ""), "node receipt tool inventory is invalid"
        health = detail.get("health")
        if not isinstance(health, dict) or health.get("status") != "healthy":
            return ("", ""), "node receipt health verification did not pass"
        if str(detail.get("policy_version") or "") != str(
            operation["policy_version"] or ""
        ):
            return ("", ""), "node receipt policy version does not match"
    if expected_commit:
        expected_tree = str(operation["resolved_tree"] or "").lower()
        if proof[0] != expected_commit:
            return ("", ""), "node receipt commit does not match the source lock"
        if str(detail.get("tree_sha") or "").lower() != expected_tree:
            return ("", ""), "node receipt tree does not match the source lock"
        if str(detail.get("canonical_source") or "") != str(
            operation["canonical_source"] or ""
        ):
            return ("", ""), "node receipt canonical source does not match"
    return proof, ""


def _aggregate_installation_state(
    operation: sqlite3.Row | dict[str, Any],
    targets: Iterable[sqlite3.Row | dict[str, Any]],
) -> str:
    if str(operation["state"] or "") == "rolled_back":
        return "rolled_back"
    rows = list(targets)
    if not rows:
        return "pending"
    completed = [target for target in rows if str(target["state"]) == "completed"]
    if completed and len(completed) != len(rows):
        return "partial"
    if len(completed) == len(rows):
        for target in completed:
            detail = _safe_json_object(str(target["detail_json"] or "{}"))
            if _validated_target_receipt(operation, detail)[1]:
                return "failed"
        return "verified"
    if all(str(target["state"]) in TERMINAL_STATES for target in rows):
        return "failed"
    return "pending"


def get_managed_installation(
    operation_id: str,
    *,
    db_path: Path | None = None,
    owner_id: str | None = None,
    account_generation: str | None = None,
) -> dict[str, Any]:
    with closing(_connect(db_path or managed_installations_db_path())) as conn:
        sql = "SELECT * FROM managed_installations WHERE id = ?"
        params: tuple[Any, ...] = (str(operation_id or "").strip(),)
        if owner_id is not None:
            owner = _normalize_owner_boundary(owner_id, account_generation)
            sql += " AND owner_id=? AND account_generation=?"
            params += owner
        row = conn.execute(sql, params).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return _operation_payload(conn, row)


def rollback_managed_installation(
    operation_id: str,
    *,
    request_id: str = "",
    db_path: Path | None = None,
    config_path: Path | None = None,
    owner_id: str | None = None,
    account_generation: str | None = None,
) -> dict[str, Any]:
    """Create one durable, per-node uninstall operation from verified receipts."""

    path = db_path or managed_installations_db_path()
    with closing(_connect(path)) as conn:
        sql = "SELECT * FROM managed_installations WHERE id=? AND action='install'"
        params: tuple[Any, ...] = (str(operation_id or "").strip(),)
        if owner_id is not None:
            owner = _normalize_owner_boundary(owner_id, account_generation)
            sql += " AND owner_id=? AND account_generation=?"
            params += owner
        original = conn.execute(sql, params).fetchone()
        if original is None:
            raise KeyError(operation_id)
        target_rows = conn.execute(
            "SELECT node_id,state,detail_json FROM managed_installation_targets "
            "WHERE operation_id=? ORDER BY CASE node_id "
            "WHEN 'server' THEN 0 WHEN 'dbb3' THEN 1 ELSE 2 END",
            (original["id"],),
        ).fetchall()
        if _aggregate_installation_state(original, target_rows) != "verified":
            raise ValueError("only a verified installation can be rolled back")
        existing = conn.execute(
            "SELECT * FROM managed_installations WHERE rollback_of=? "
            "AND owner_id=? AND account_generation=? ORDER BY created_at DESC LIMIT 1",
            (original["id"], original["owner_id"], original["account_generation"]),
        ).fetchone()
        if existing is not None and str(existing["state"]) != "failed":
            return _operation_payload(conn, existing)
        targets = [str(row["node_id"]) for row in target_rows]
        receipts = {
            str(row["node_id"]): _safe_json_object(str(row["detail_json"] or "{}"))
            for row in target_rows
        }
        original_values = dict(original)

    source_lock = None
    if str(original_values["kind"]) == "project":
        source_lock = {
            key: str(original_values[key] or "")
            for key in (
                "canonical_source", "source_ref", "resolved_commit",
                "resolved_tree", "policy_version",
            )
        }
    return create_managed_installation(
        kind=str(original_values["kind"]),
        identifier=str(original_values["identifier"]),
        profile=str(original_values["profile"]),
        request_id=str(request_id or f"rollback-{operation_id}-{uuid4().hex[:12]}"),
        scope=str(original_values["scope"]),
        locality=str(original_values["locality"]),
        targets=targets,
        project_name=str(original_values["project_name"]),
        source_ref=str(original_values["source_ref"]),
        source_lock=source_lock,
        db_path=path,
        require_topology=True,
        config_path=config_path,
        owner_id=str(original_values["owner_id"]),
        account_generation=str(original_values["account_generation"]),
        _action="rollback",
        _rollback_of=str(original_values["id"]),
        _rollback_receipts=receipts,
    )


def list_managed_installations(
    *,
    kind: str = "",
    profile: str = "",
    limit: int = 50,
    db_path: Path | None = None,
    owner_id: str | None = None,
    account_generation: str | None = None,
) -> dict[str, Any]:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind and normalized_kind not in KINDS:
        raise ValueError("kind must be skill, mcp, project, or empty")
    normalized_profile = str(profile or "").strip()
    sql = "SELECT * FROM managed_installations"
    params: list[Any] = []
    clauses: list[str] = []
    if normalized_kind:
        clauses.append("kind = ?")
        params.append(normalized_kind)
    if normalized_profile:
        clauses.append("profile = ?")
        params.append(normalized_profile)
    if owner_id is not None:
        owner = _normalize_owner_boundary(owner_id, account_generation)
        clauses.extend(("owner_id = ?", "account_generation = ?"))
        params.extend(owner)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(int(limit), 200)))
    with closing(_connect(db_path or managed_installations_db_path())) as conn:
        rows = conn.execute(sql, params).fetchall()
        return {"operations": [_operation_payload(conn, row) for row in rows]}


def _safe_json_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
        return decoded if isinstance(decoded, dict) else {}
    except json.JSONDecodeError:
        return {}


def _redact_error(value: Any) -> str:
    text = str(value or "").replace("\x00", " ")
    return _SECRET_RE.sub(
        lambda match: re.split(r"[=:]", match.group(0), maxsplit=1)[0] + "=<redacted>",
        text,
    )[:MAX_ERROR_LENGTH]


def _recompute_operation(conn: sqlite3.Connection, operation_id: str) -> None:
    operation = conn.execute(
        "SELECT action FROM managed_installations WHERE id=?",
        (operation_id,),
    ).fetchone()
    action = str(operation["action"] or "install") if operation else "install"
    rows = conn.execute(
        "SELECT state, error FROM managed_installation_targets WHERE operation_id = ?",
        (operation_id,),
    ).fetchall()
    states = [str(row["state"]) for row in rows]
    if states and all(state == "completed" for state in states):
        state, error = ("rolled_back" if action == "rollback" else "completed"), ""
    elif states and all(state in TERMINAL_STATES for state in states):
        state = "failed"
        error = next((str(row["error"]) for row in rows if row["error"]), "installation failed")
    elif any(state == "running" for state in states):
        state, error = "running", ""
    elif any(state in {"accepted", "retry"} for state in states):
        state, error = "dispatching", ""
    else:
        state, error = "accepted", ""
    conn.execute(
        "UPDATE managed_installations SET state = ?, error = ?, updated_at = ? WHERE id = ?",
        (state, error, _utc_now(), operation_id),
    )
    if state in TERMINAL_STATES:
        _publish_resource_catalog(conn, operation_id)


def _publish_resource_catalog(conn: sqlite3.Connection, operation_id: str) -> None:
    operation = conn.execute(
        "SELECT * FROM managed_installations WHERE id=?",
        (operation_id,),
    ).fetchone()
    if operation is None:
        return
    if str(operation["action"] or "install") == "rollback":
        _publish_rollback_catalog(conn, operation)
        return
    target_rows = conn.execute(
        "SELECT node_id,state,detail_json FROM managed_installation_targets "
        "WHERE operation_id=? ORDER BY CASE node_id "
        "WHEN 'server' THEN 0 WHEN 'dbb3' THEN 1 ELSE 2 END",
        (operation_id,),
    ).fetchall()
    target_nodes = tuple(str(row["node_id"]) for row in target_rows)
    loaded_nodes = tuple(
        str(row["node_id"])
        for row in target_rows
        if str(row["state"]) == "completed"
    )
    node_details = {
        str(row["node_id"]): _safe_json_object(str(row["detail_json"] or "{}"))
        for row in target_rows
        if str(row["state"]) == "completed"
    }
    proof_results = {
        node_id: _validated_target_receipt(operation, detail)
        for node_id, detail in node_details.items()
    }
    proofs = {node_id: result[0] for node_id, result in proof_results.items()}
    invalid_proof_nodes = {
        node_id: reason
        for node_id, (_proof, reason) in proof_results.items()
        if reason and reason != "missing"
    }
    missing_proof_nodes = sorted(
        node_id
        for node_id, (proof, reason) in proof_results.items()
        if reason == "missing" or (not reason and not any(proof))
    )
    distinct_proofs = {proof for proof in proofs.values() if any(proof)}
    proof_mismatch = len(distinct_proofs) > 1 or bool(
        distinct_proofs and missing_proof_nodes
    )
    proof_verified = (
        bool(proofs)
        and not missing_proof_nodes
        and not invalid_proof_nodes
        and not proof_mismatch
    )
    verified_proof = next(iter(distinct_proofs), ("", "")) if proof_verified else ("", "")
    detail = next(iter(node_details.values()), {})
    kind = str(operation["kind"])
    identifier = str(operation["identifier"])
    project_name = str(operation["project_name"] or "")
    source_type = "git" if identifier.startswith("https://") else "managed"
    name = project_name or identifier.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    source_ref = str(operation["source_ref"] or detail.get("source_ref") or "")
    resolved, content_hash = verified_proof
    state = str(operation["state"])
    aggregate_state = _aggregate_installation_state(operation, target_rows)
    owner_id = str(operation["owner_id"] or "server-admin")
    account_generation = str(operation["account_generation"] or "")
    base_resource_id = resource_identity(
        kind=kind,
        name=name,
        source_type=source_type,
        source_uri=str(operation["canonical_source"] or identifier),
        source_ref=source_ref,
    )
    scoped_resource_id = (
        base_resource_id
        if owner_id == "server-admin"
        else "resource_" + hashlib.sha256(
            f"{owner_id}\0{account_generation}\0{base_resource_id}".encode("utf-8")
        ).hexdigest()
    )
    conflicts: list[dict[str, Any]] = []
    if state == "completed" and missing_proof_nodes:
        conflicts.append({
            "code": "resource_proof_missing",
            "nodes": missing_proof_nodes,
            "reason": "completed nodes must report an immutable version/commit or content hash",
        })
    if state == "completed" and invalid_proof_nodes:
        conflicts.append({
            "code": "resource_proof_invalid",
            "nodes": sorted(invalid_proof_nodes),
            "reasons": dict(sorted(invalid_proof_nodes.items())),
            "reason": "proof must be derived locally and use an immutable commit, version, or SHA-256 digest",
        })
    if state == "completed" and proof_mismatch:
        conflicts.append({
            "code": "resource_proof_mismatch",
            "nodes": sorted(proofs),
            "proofs": {
                node_id: {"resolved": proof[0], "content_hash": proof[1]}
                for node_id, proof in sorted(proofs.items())
            },
            "reason": "all completed nodes must resolve the same immutable resource",
        })
    if aggregate_state in {"partial", "rolled_back"} or state != "completed":
        health, trust_state, enabled = "failed", "blocked", False
    elif invalid_proof_nodes or proof_mismatch:
        health, trust_state, enabled = "failed", "blocked", False
    elif not proof_verified:
        health, trust_state, enabled = "degraded", "pending", False
    else:
        health, trust_state, enabled = "healthy", "approved", True
    record = ResourceRecord(
        resource_id=scoped_resource_id,
        kind=kind,
        name=name,
        source_type=source_type,
        source_uri=str(operation["canonical_source"] or identifier),
        source_ref=source_ref,
        resolved_commit_or_version=resolved,
        content_hash=content_hash,
        scope="project" if kind == "project" else "account",
        target_nodes=target_nodes,
        loaded_nodes=loaded_nodes,
        aggregate_state=aggregate_state,
        node_receipts=node_details,
        policy_version=str(operation["policy_version"] or ""),
        tree_sha=str(operation["resolved_tree"] or ""),
        tools=tuple(sorted({
            str(tool)
            for receipt in node_details.values()
            for tool in (receipt.get("tools") or [])
            if isinstance(tool, str) and tool
        })),
        permissions=tuple(sorted({
            str(permission)
            for receipt in node_details.values()
            for permission in (receipt.get("permissions") or [])
            if isinstance(permission, str) and permission
        })),
        last_verified_at=(str(operation["updated_at"]) if aggregate_state == "verified" else ""),
        rollback_available=aggregate_state == "verified",
        enabled=enabled,
        trust_state=trust_state,
        health=health,
        conflicts=tuple(conflicts),
        installed_at=str(operation["created_at"]),
        updated_at=str(operation["updated_at"]),
        operation_id=operation_id,
        install_operation_id=operation_id,
    )
    payload = record.public_dict()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    existing = conn.execute(
        "SELECT record_json FROM managed_resource_catalog WHERE resource_id=?",
        (record.resource_id,),
    ).fetchone()
    if existing is not None and hmac.compare_digest(str(existing["record_json"]), encoded):
        return
    now = _utc_now()
    conn.execute(
        "DELETE FROM managed_resource_catalog WHERE operation_id=? AND resource_id<>?",
        (operation_id, record.resource_id),
    )
    conn.execute(
        "INSERT INTO managed_resource_catalog(resource_id,operation_id,owner_id,account_generation,record_json,updated_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(resource_id) DO UPDATE SET "
        "operation_id=excluded.operation_id,owner_id=excluded.owner_id,"
        "account_generation=excluded.account_generation,record_json=excluded.record_json,"
        "updated_at=excluded.updated_at",
        (record.resource_id, operation_id, owner_id, account_generation, encoded, now),
    )
    conn.execute(
        "INSERT INTO managed_resource_events(resource_id,operation_id,owner_id,account_generation,event_json,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (record.resource_id, operation_id, owner_id, account_generation, encoded, now),
    )


def _publish_rollback_catalog(
    conn: sqlite3.Connection,
    operation: sqlite3.Row,
) -> None:
    rollback_of = str(operation["rollback_of"] or "")
    original = conn.execute(
        "SELECT record_json FROM managed_resource_catalog WHERE operation_id=?",
        (rollback_of,),
    ).fetchone()
    if original is None:
        original = next((
            row
            for row in conn.execute(
                "SELECT record_json FROM managed_resource_catalog "
                "WHERE owner_id=? AND account_generation=?",
                (operation["owner_id"], operation["account_generation"]),
            ).fetchall()
            if str(
                _safe_json_object(str(row["record_json"] or "{}")).get(
                    "install_operation_id"
                ) or ""
            ) == rollback_of
        ), None)
    if original is None:
        return
    payload = _safe_json_object(str(original["record_json"] or "{}"))
    target_rows = conn.execute(
        "SELECT node_id,state,detail_json FROM managed_installation_targets "
        "WHERE operation_id=? ORDER BY CASE node_id "
        "WHEN 'server' THEN 0 WHEN 'dbb3' THEN 1 ELSE 2 END",
        (operation["id"],),
    ).fetchall()
    completed_nodes = {
        str(row["node_id"])
        for row in target_rows
        if str(row["state"]) == "completed"
    }
    all_completed = bool(target_rows) and len(completed_nodes) == len(target_rows)
    if all_completed:
        aggregate_state = "rolled_back"
        loaded_nodes: list[str] = []
        enabled = False
        health = "rolled_back"
        rollback_available = False
    elif completed_nodes:
        aggregate_state = "partial"
        loaded_nodes = [
            str(node)
            for node in (payload.get("loaded_nodes") or [])
            if str(node) not in completed_nodes
        ]
        enabled = False
        health = "failed"
        rollback_available = False
    else:
        aggregate_state = str(payload.get("aggregate_state") or "verified")
        loaded_nodes = [str(node) for node in (payload.get("loaded_nodes") or [])]
        enabled = bool(payload.get("enabled"))
        health = str(payload.get("health") or "healthy")
        rollback_available = True
        conflicts = list(payload.get("conflicts") or [])
        conflicts.append({
            "code": "resource_rollback_failed",
            "operation_id": str(operation["id"]),
        })
        payload["conflicts"] = conflicts
    payload.update({
        "aggregate_state": aggregate_state,
        "loaded_nodes": loaded_nodes,
        "node_receipts": {
            str(row["node_id"]): _safe_json_object(str(row["detail_json"] or "{}"))
            for row in target_rows
        },
        "enabled": enabled,
        "health": health,
        "trust_state": "blocked" if completed_nodes else payload.get("trust_state", "approved"),
        "last_verified_at": "" if completed_nodes else payload.get("last_verified_at", ""),
        "rollback_available": rollback_available,
        "updated_at": str(operation["updated_at"]),
        "operation_id": str(operation["id"]),
    })
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    now = _utc_now()
    conn.execute(
        "UPDATE managed_resource_catalog SET operation_id=?,record_json=?,updated_at=? "
        "WHERE resource_id=?",
        (operation["id"], encoded, now, payload.get("resource_id")),
    )
    conn.execute(
        "INSERT INTO managed_resource_events(resource_id,operation_id,owner_id,account_generation,event_json,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (
            payload.get("resource_id"),
            operation["id"],
            operation["owner_id"],
            operation["account_generation"],
            encoded,
            now,
        ),
    )


def _validated_managed_resource_proof(
    detail: dict[str, Any],
) -> tuple[tuple[str, str], str]:
    if not detail:
        return ("", ""), "missing"
    try:
        proof_schema = int(detail.get("proof_schema") or 0)
    except (TypeError, ValueError):
        proof_schema = 0
    if detail.get("proof_source") != "local_filesystem" or proof_schema != 1:
        return ("", ""), "proof source is not a verified local installation"

    commit = str(
        detail.get("resolved_commit") or detail.get("commit") or detail.get("head") or ""
    ).strip().lower()
    version = str(
        detail.get("resolved_version") or detail.get("version") or ""
    ).strip()
    content_hash = str(
        detail.get("sha256") or detail.get("content_hash") or ""
    ).strip().lower()
    if commit and not _GIT_COMMIT_RE.fullmatch(commit):
        return ("", ""), "commit is not a full immutable object id"
    if version and not _IMMUTABLE_VERSION_RE.fullmatch(version):
        return ("", ""), "version is not an immutable release identifier"
    if content_hash and not _SHA256_RE.fullmatch(content_hash):
        return ("", ""), "content hash is not a SHA-256 digest"
    resolved = commit or version
    if not resolved and not content_hash:
        return ("", ""), "missing"
    return (resolved, content_hash), ""


def _managed_resource_proof(detail: dict[str, Any]) -> tuple[str, str]:
    return _validated_managed_resource_proof(detail)[0]


def _hash_resource_tree(root: Path) -> str:
    root = root.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("installed resource directory is unsafe")
    digest = hashlib.sha256()
    entries = list(root.rglob("*"))
    if any(item.is_symlink() for item in entries):
        raise RuntimeError("installed resource contains a symbolic link")
    files = sorted(
        (item for item in entries if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    )
    if not files:
        raise RuntimeError("installed resource directory is empty")
    for item in files:
        relative = item.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(item.stat().st_size.to_bytes(8, "big"))
        with item.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _profile_home(profile: str) -> Path:
    from hermes_cli.profiles import resolve_profile_env

    return Path(resolve_profile_env(profile)).resolve()


def _operation_profile_home(
    operation: dict[str, Any], profile: str, *, resolve_profile: bool
) -> Path:
    override = str(operation.get("_profile_home") or "").strip()
    if override:
        return Path(override).resolve()
    owner_id = str(operation.get("owner_id") or "server-admin")
    account_generation = str(operation.get("account_generation") or "")
    if owner_id != "server-admin":
        owner_id, account_generation = _normalize_owner_boundary(
            owner_id, account_generation
        )
        return _account_resource_home(
            owner_id,
            account_generation,
            create=True,
        )
    if resolve_profile:
        return _profile_home(profile)
    return Path(get_hermes_home()).resolve()


def _operation_profile_name(operation: dict[str, Any], profile: str) -> str:
    owner_id = str(operation.get("owner_id") or "server-admin")
    account_generation = str(operation.get("account_generation") or "")
    if owner_id == "server-admin" or operation.get("_profile_home"):
        return profile
    owner_id, account_generation = _normalize_owner_boundary(
        owner_id, account_generation
    )
    return _account_resource_profile_name(owner_id, account_generation)


def _load_skill_lock(profile_home: Path) -> dict[str, dict[str, Any]]:
    path = profile_home / "skills" / ".hub" / "lock.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    installed = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(installed, dict):
        return {}
    return {
        str(name): dict(value)
        for name, value in installed.items()
        if isinstance(value, dict)
    }


def _installed_skill_proof(
    profile_home: Path,
    identifier: str,
    before: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    after = _load_skill_lock(profile_home)
    changed = [name for name, value in after.items() if before.get(name) != value]
    normalized_tail = identifier.rstrip("/").rsplit("/", 1)[-1].lower()
    exact = [
        name
        for name, value in after.items()
        if str(value.get("identifier") or "").strip() == identifier
        or name.lower() == normalized_tail
    ]
    candidates = changed if len(changed) == 1 else [name for name in changed if name in exact]
    if len(candidates) != 1:
        candidates = exact
    if len(candidates) != 1:
        raise RuntimeError("installed skill could not be identified from the profile lock")
    entry = after[candidates[0]]
    install_path = Path(str(entry.get("install_path") or ""))
    if install_path.is_absolute() or ".." in install_path.parts:
        raise RuntimeError("installed skill lock path is unsafe")
    skills_root = (profile_home / "skills").resolve(strict=True)
    destination = (skills_root / install_path).resolve(strict=True)
    destination.relative_to(skills_root)
    if not (destination / "SKILL.md").is_file():
        raise RuntimeError("installed skill is missing SKILL.md")
    content_hash = _hash_resource_tree(destination)
    return {
        "proof_schema": 1,
        "proof_source": "local_filesystem",
        "content_hash": content_hash,
        "path": str(destination),
        "installed_name": candidates[0],
    }


def _installed_mcp_proof(
    profile_home: Path,
    identifier: str,
    *,
    probe_health: bool = False,
) -> dict[str, Any]:
    from hermes_cli.mcp_catalog import _build_server_config, get_entry

    entry = get_entry(identifier)
    if entry is None:
        raise RuntimeError("installed MCP is not present in the managed catalog")
    config_path = profile_home / "config.yaml"
    config = _read_yaml_mapping(
        config_path,
        label="installed MCP configuration",
    )
    servers = config.get("mcp_servers") if isinstance(config, dict) else None
    actual = servers.get(entry.name) if isinstance(servers, dict) else None
    if not isinstance(actual, dict):
        raise RuntimeError("installed MCP is missing from the profile configuration")
    install_dir = profile_home / "mcp-installs" / entry.name
    expected = _build_server_config(entry, install_dir if entry.install else None)
    for key in ("command", "args", "url", "auth", "env"):
        if key in expected and actual.get(key) != expected[key]:
            raise RuntimeError(f"installed MCP configuration does not match manifest field {key}")
    manifest = entry.manifest_path.resolve(strict=True)
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    proof: dict[str, Any] = {
        "proof_schema": 1,
        "proof_source": "local_filesystem",
        "content_hash": manifest_hash,
        "manifest_path": str(manifest),
        "installed_name": entry.name,
        "health_checks": [{"name": "manifest_configuration", "ok": True}],
        "permissions": sorted({
            permission
            for permission, required in (
                ("network", bool(actual.get("url"))),
                ("process", bool(actual.get("command"))),
                ("credentials", bool(actual.get("auth") or actual.get("env"))),
            )
            if required
        }),
    }
    configured_tools = actual.get("tools") if isinstance(actual, dict) else None
    included_tools = (
        configured_tools.get("include")
        if isinstance(configured_tools, dict)
        else None
    )
    if isinstance(included_tools, list) and all(
        isinstance(tool, str) for tool in included_tools
    ):
        proof["tools"] = list(included_tools)
        proof["tools_complete"] = True
    else:
        proof["tools"] = []
        proof["tools_complete"] = False
    if probe_health:
        from hermes_cli.mcp_config import _probe_single_server

        capability_counts: dict[str, Any] = {}
        discovered = _probe_single_server(
            entry.name,
            actual,
            details=capability_counts,
        )
        proof["tools"] = sorted({str(tool[0]) for tool in discovered})
        proof["tools_complete"] = True
        proof["health_checks"].append({
            "name": "mcp_initialize_and_tools_list",
            "ok": True,
            "tool_count": len(proof["tools"]),
            "capabilities": capability_counts,
        })
    if entry.install is not None:
        git_dir = install_dir / ".git"
        if not git_dir.is_dir() or git_dir.is_symlink():
            raise RuntimeError("installed MCP git worktree is missing")
        completed = _run_command(
            _managed_git_command(
                "-C", str(install_dir), "rev-parse", "--verify", "HEAD",
            ),
            timeout=30,
        )
        head = str(completed.stdout or "").strip().lower()
        expected_ref = str(entry.install.ref or "").strip().lower()
        if completed.returncode or not _GIT_COMMIT_RE.fullmatch(head):
            raise RuntimeError("installed MCP has no immutable git HEAD")
        if _GIT_COMMIT_RE.fullmatch(expected_ref) and head != expected_ref:
            raise RuntimeError("installed MCP HEAD does not match its pinned manifest")
        proof["resolved_commit"] = head
    elif entry.transport.version:
        proof["resolved_version"] = str(entry.transport.version).strip()
    return proof


def list_managed_resources(
    *,
    since_cursor: int = 0,
    limit: int = 500,
    db_path: Path | None = None,
    owner_id: str | None = None,
    account_generation: str | None = None,
) -> dict[str, Any]:
    if int(since_cursor) < 0:
        raise ValueError("since_cursor must be non-negative")
    bounded_limit = min(2000, max(1, int(limit)))
    owner_filter = None
    if owner_id is not None:
        owner_filter = _normalize_owner_boundary(owner_id, account_generation)
    with closing(_connect(db_path or managed_installations_db_path())) as conn:
        where = ""
        params: tuple[Any, ...] = ()
        if owner_filter is not None:
            where = " WHERE owner_id=? AND account_generation=?"
            params = owner_filter
        rows = conn.execute(
            "SELECT record_json FROM managed_resource_catalog" + where + " ORDER BY updated_at DESC",
            params,
        ).fetchall()
        records = [
            ResourceRecord(**_resource_record_kwargs(_safe_json_object(row["record_json"])))
            for row in rows
        ]
        winners, diagnostics = resolve_resource_collisions(records)
        event_where = "WHERE cursor>?"
        event_params: tuple[Any, ...] = (int(since_cursor),)
        if owner_filter is not None:
            event_where += " AND owner_id=? AND account_generation=?"
            event_params += owner_filter
        event_rows = conn.execute(
            "SELECT cursor,event_json,created_at FROM managed_resource_events "
            + event_where + " ORDER BY cursor ASC LIMIT ?",
            event_params + (bounded_limit,),
        ).fetchall()
        latest_sql = "SELECT COALESCE(MAX(cursor),0) FROM managed_resource_events"
        latest = int(conn.execute(latest_sql + where, params).fetchone()[0])
    events = [
        {
            "cursor": int(row["cursor"]),
            "resource": _safe_json_object(row["event_json"]),
            "created_at": str(row["created_at"]),
        }
        for row in event_rows
    ]
    next_cursor = int(events[-1]["cursor"]) if events else latest
    reset_cursor = int(since_cursor) > latest
    return {
        "account_generation": owner_filter[1] if owner_filter is not None else "",
        "resources": [record.public_dict() for record in winners],
        "diagnostics": diagnostics,
        "events": events,
        "cursor": next_cursor,
        "reset_cursor": reset_cursor,
        "reset_reason": "future_cursor" if reset_cursor else "",
        "has_more": next_cursor < latest,
    }


def delete_owner_managed_resources(
    owner_id: str,
    *,
    account_generation: str,
    include_known_generations: bool = False,
    db_path: Path | None = None,
    _node_id: str = "server",
    _project_root: Path | None = None,
) -> dict[str, int]:
    owner = str(owner_id or "").strip()
    if not owner or owner == "server-admin":
        raise ValueError("an account owner_id is required")
    path = db_path or managed_installations_db_path()
    with closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        owner, generation = _normalize_owner_boundary(owner, account_generation)
        if include_known_generations:
            generation_clause = ""
            params: tuple[Any, ...] = (owner,)
        else:
            generation_clause = " AND account_generation=?"
            params = (owner, generation)
        counts = {
            "resources": int(conn.execute(
                "SELECT COUNT(*) FROM managed_resource_catalog WHERE owner_id=?" + generation_clause,
                params,
            ).fetchone()[0]),
            "events": int(conn.execute(
                "SELECT COUNT(*) FROM managed_resource_events WHERE owner_id=?" + generation_clause,
                params,
            ).fetchone()[0]),
            "operations": int(conn.execute(
                "SELECT COUNT(*) FROM managed_installations WHERE owner_id=?" + generation_clause,
                params,
            ).fetchone()[0]),
        }
        generations = {generation}
        if include_known_generations:
            generations.update(
                str(row[0])
                for row in conn.execute(
                    "SELECT account_generation FROM managed_installations WHERE owner_id=? "
                    "UNION SELECT account_generation FROM managed_resource_catalog WHERE owner_id=? "
                    "UNION SELECT account_generation FROM managed_resource_events WHERE owner_id=? "
                    "UNION SELECT account_generation FROM managed_owner_tombstones WHERE owner_id=?",
                    (owner, owner, owner, owner),
                ).fetchall()
                if str(row[0] or "")
            )
        targets_by_generation: dict[str, set[str]] = {
            generation_value: {_node_id} for generation_value in sorted(generations)
        }
        for row in conn.execute(
            "SELECT DISTINCT i.account_generation,t.node_id "
            "FROM managed_installations i JOIN managed_installation_targets t "
            "ON t.operation_id=i.id WHERE i.owner_id=?" + generation_clause,
            params,
        ).fetchall():
            generation_value = str(row["account_generation"] or "")
            if generation_value:
                targets_by_generation.setdefault(generation_value, {_node_id}).add(
                    str(row["node_id"])
                )
        now = _utc_now()
        for generation_value, target_nodes in targets_by_generation.items():
            conn.execute(
                "INSERT INTO managed_owner_tombstones(owner_id,account_generation,state,updated_at,error) "
                "VALUES(?,?,'pending',?,'') ON CONFLICT(owner_id,account_generation) "
                "DO UPDATE SET state='pending',updated_at=excluded.updated_at,error=''",
                (owner, generation_value, now),
            )
            for node_id in sorted(target_nodes):
                conn.execute(
                    "INSERT INTO managed_owner_deletion_targets("
                    "owner_id,account_generation,node_id,state,updated_at) "
                    "VALUES(?,?,?,'pending',?) ON CONFLICT(owner_id,account_generation,node_id) "
                    "DO NOTHING",
                    (owner, generation_value, node_id, now),
                )
            remaining = int(conn.execute(
                "SELECT COUNT(*) FROM managed_owner_deletion_targets "
                "WHERE owner_id=? AND account_generation=? AND state<>'completed'",
                (owner, generation_value),
            ).fetchone()[0])
            if remaining == 0:
                conn.execute(
                    "UPDATE managed_owner_tombstones SET state='complete' "
                    "WHERE owner_id=? AND account_generation=?",
                    (owner, generation_value),
                )
        conn.execute(
            "UPDATE managed_installation_targets SET state='cancelled',lease_token='',"
            "lease_until=0,updated_at=? WHERE operation_id IN ("
            "SELECT id FROM managed_installations WHERE owner_id=?" + generation_clause + ")",
            (now,) + params,
        )
        conn.execute(
            "DELETE FROM managed_resource_events WHERE owner_id=?" + generation_clause,
            params,
        )
        conn.execute(
            "DELETE FROM managed_resource_catalog WHERE owner_id=?" + generation_clause,
            params,
        )
        conn.execute(
            "DELETE FROM managed_installations WHERE owner_id=?" + generation_clause,
            params,
        )
        conn.execute("COMMIT")
    for generation_value in sorted(generations):
        _dispatch_owner_deletion_once(
            path,
            only_node=_node_id,
            project_root=_project_root,
        )
    return counts


def _owner_deletion_state(path: Path, owner_id: str, account_generation: str) -> str:
    with closing(_connect(path)) as conn:
        row = conn.execute(
            "SELECT state FROM managed_owner_tombstones "
            "WHERE owner_id=? AND account_generation=?",
            (owner_id, account_generation),
        ).fetchone()
    return str(row["state"]) if row is not None else "complete"


def _cleanup_owner_files(
    owner_id: str,
    account_generation: str,
    *,
    project_root: Path | None,
) -> None:
    boundary = _owner_boundary_digest(owner_id, account_generation)
    from hermes_cli.profiles import get_profile_dir

    profiles_root = get_profile_dir("managed-placeholder").parent.resolve()
    profile_prefix = f"acct-{boundary[:20]}-"
    if profiles_root.is_dir():
        for profile_home in profiles_root.glob(f"{profile_prefix}*"):
            if profile_home.is_symlink() or not profile_home.is_dir():
                raise RuntimeError("managed account profile cleanup path is unsafe")
            marker = profile_home / ".managed-owner-boundary.json"
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("managed account profile cleanup marker is invalid") from exc
            if payload.get("boundary") != boundary:
                raise RuntimeError("managed account profile cleanup boundary mismatch")
            profile = str(payload.get("profile") or "").strip()
            if not profile:
                raise RuntimeError("managed account profile cleanup marker is invalid")
            with _account_runtime_lock(owner_id, account_generation, profile):
                # Recheck under the same lock used by installs/runtime refresh.
                if profile_home.is_symlink() or not profile_home.is_dir():
                    raise RuntimeError("managed account profile cleanup path is unsafe")
                current = json.loads(marker.read_text(encoding="utf-8"))
                if current.get("boundary") != boundary or current.get("profile") != profile:
                    raise RuntimeError("managed account profile cleanup boundary mismatch")
                shutil.rmtree(profile_home)

    base_projects = Path(
        project_root or (Path(get_hermes_home()) / "managed-projects")
    ).resolve()
    account_projects = _account_project_root(
        base_projects,
        owner_id,
        account_generation,
        create=False,
    )
    if account_projects.exists() or account_projects.is_symlink():
        if account_projects.is_symlink() or not account_projects.is_dir():
            raise RuntimeError("managed account project cleanup path is unsafe")
        marker = account_projects / ".managed-owner-boundary.json"
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("managed account project cleanup marker is invalid") from exc
        if payload != {"version": 1, "boundary": boundary}:
            raise RuntimeError("managed account project cleanup boundary mismatch")
        shutil.rmtree(account_projects)


def _dispatch_owner_deletion_once(
    path: Path,
    *,
    config_path: Path | None = None,
    only_node: str | None = None,
    project_root: Path | None = None,
) -> bool:
    now = time.time()
    token = uuid4().hex
    with closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        sql = (
            "SELECT owner_id,account_generation,node_id FROM managed_owner_deletion_targets "
            "WHERE state IN ('pending','retry','running') AND next_attempt_at<=? "
            "AND (lease_token='' OR lease_until<=?)"
        )
        params: list[Any] = [now, now]
        if only_node is not None:
            sql += " AND node_id=?"
            params.append(only_node)
        sql += " ORDER BY updated_at ASC LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return False
        owner_id = str(row["owner_id"])
        generation = str(row["account_generation"])
        node_id = str(row["node_id"])
        fence = None
        if only_node is not None or node_id == "server":
            fence = _try_execution_fence(
                path, f"account:{owner_id}:{generation}", node_id
            )
            if fence is None:
                conn.execute(
                    "UPDATE managed_owner_deletion_targets SET state='retry',"
                    "next_attempt_at=?,updated_at=? WHERE owner_id=? AND account_generation=? "
                    "AND node_id=?",
                    (now + 0.25, _utc_now(), owner_id, generation, node_id),
                )
                conn.execute("COMMIT")
                return True
        conn.execute(
            "UPDATE managed_owner_deletion_targets SET state='running',attempts=attempts+1,"
            "lease_token=?,lease_until=?,updated_at=? WHERE owner_id=? AND account_generation=? "
            "AND node_id=?",
            (token, now + DEFAULT_LEASE_SECONDS, _utc_now(), owner_id, generation, node_id),
        )
        conn.execute("COMMIT")
    state = "completed"
    error = ""
    try:
        if node_id == "server" or only_node is not None:
            _cleanup_owner_files(
                owner_id,
                generation,
                project_root=project_root,
            )
        else:
            route = _installation_route(node_id, config_path)
            request = Request(
                route["url"],
                data=json.dumps({
                    "action": "delete_owner",
                    "node_id": node_id,
                    "owner_id": owner_id,
                    "account_generation": generation,
                }, separators=(",", ":")).encode("utf-8"),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-DBB3-Token": route["token"],
                },
                method="POST",
            )
            response = _read_json_response(request, route["timeout"])
            if str(response.get("state") or "") != "complete":
                raise RuntimeError("remote managed resource cleanup is pending")
    except Exception as exc:
        state = "retry"
        error = _redact_error(exc)
    finally:
        if fence is not None:
            fence.release()
    with closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            "UPDATE managed_owner_deletion_targets SET state=?,next_attempt_at=?,"
            "lease_token='',lease_until=0,updated_at=?,error=? "
            "WHERE owner_id=? AND account_generation=? AND node_id=? AND lease_token=?",
            (
                state,
                0 if state == "completed" else time.time() + 1,
                _utc_now(),
                error,
                owner_id,
                generation,
                node_id,
                token,
            ),
        ).rowcount
        if updated:
            remaining = int(conn.execute(
                "SELECT COUNT(*) FROM managed_owner_deletion_targets "
                "WHERE owner_id=? AND account_generation=? AND state<>'completed'",
                (owner_id, generation),
            ).fetchone()[0])
            conn.execute(
                "UPDATE managed_owner_tombstones SET state=?,updated_at=?,error=? "
                "WHERE owner_id=? AND account_generation=?",
                (
                    "complete" if remaining == 0 else "pending",
                    _utc_now(),
                    "" if remaining == 0 else error,
                    owner_id,
                    generation,
                ),
            )
        conn.execute("COMMIT")
    return True


def _resource_record_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["target_nodes"] = tuple(payload.get("target_nodes") or ())
    normalized["loaded_nodes"] = tuple(payload.get("loaded_nodes") or ())
    normalized["conflicts"] = tuple(payload.get("conflicts") or ())
    return normalized


def _claim_target(
    path: Path,
    *,
    now: float,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> dict[str, Any] | None:
    token = uuid4().hex
    with closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT t.operation_id, t.node_id, i.owner_id, i.account_generation
            FROM managed_installation_targets t
            JOIN managed_installations i ON i.id=t.operation_id
            WHERE t.state IN ('pending', 'accepted', 'running', 'retry')
              AND t.next_attempt_at <= ?
              AND (t.lease_until <= ? OR t.lease_token = '')
              AND NOT EXISTS (
                SELECT 1 FROM managed_owner_tombstones d
                WHERE d.owner_id=i.owner_id
                  AND d.account_generation=i.account_generation
              )
            ORDER BY t.updated_at ASC LIMIT 1
            """,
            (now, now),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        owner_id = str(row["owner_id"] or "server-admin")
        generation = str(row["account_generation"] or "")
        fence_key = (
            f"account:{owner_id}:{generation}"
            if owner_id != "server-admin"
            else str(row["operation_id"])
        )
        fence = _try_execution_fence(path, fence_key, str(row["node_id"]))
        if fence is None:
            conn.execute("COMMIT")
            return None
        try:
            conn.execute(
                """
                UPDATE managed_installation_targets
                SET lease_token = ?, lease_until = ?, attempts = attempts + 1,
                    updated_at = ?
                WHERE operation_id = ? AND node_id = ?
                """,
                (token, now + lease_seconds, _utc_now(), row["operation_id"], row["node_id"]),
            )
            operation = conn.execute(
                "SELECT * FROM managed_installations WHERE id = ?",
                (row["operation_id"],),
            ).fetchone()
            target = conn.execute(
                "SELECT * FROM managed_installation_targets "
                "WHERE operation_id = ? AND node_id = ?",
                (row["operation_id"], row["node_id"]),
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            fence.release()
            raise
        result = dict(operation)
        result["target_state"] = target["state"]
        result["node_id"] = target["node_id"]
        result["attempts"] = int(target["attempts"])
        result["failure_count"] = int(target["failure_count"])
        result["lease_token"] = token
        result["lease_until"] = float(target["lease_until"])
        result["_execution_fence"] = fence
        return result


def _renew_target_lease(
    path: Path,
    claimed: dict[str, Any],
    lease_seconds: float,
) -> float | None:
    with closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        renewed_at = time.time()
        lease_deadline = renewed_at + lease_seconds
        updated = conn.execute(
            """
            UPDATE managed_installation_targets SET lease_until = ?, updated_at = ?
            WHERE operation_id = ? AND node_id = ? AND lease_token = ?
              AND state NOT IN ('completed', 'failed', 'cancelled')
              AND lease_until > ?
            """,
            (
                lease_deadline,
                _utc_now(),
                claimed["id"],
                claimed["node_id"],
                claimed["lease_token"],
                renewed_at,
            ),
        ).rowcount
        conn.execute("COMMIT")
        return lease_deadline if updated else None


class _LeaseHeartbeat:
    """Keep a claim exclusive while an allowlisted install or HTTP call is in flight."""

    def __init__(
        self,
        path: Path,
        claimed: dict[str, Any],
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self.path = path
        self.claimed = claimed
        self.lease_seconds = max(1.0, lease_seconds)
        self._stop = threading.Event()
        self.lost = threading.Event()
        self._state_lock = threading.Lock()
        self._lease_deadline = float(claimed.get("lease_until") or time.time())
        self.last_successful_renewal = time.time()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"managed-install-lease-{str(claimed['id'])[-8:]}",
        )

    def __enter__(self) -> "_LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=min(2.0, self.lease_seconds))
        self._mark_lost_if_expired()

    def _mark_lost_if_expired(self) -> bool:
        with self._state_lock:
            if time.time() >= self._lease_deadline:
                self.lost.set()
        return self.lost.is_set()

    def ensure_owned(self) -> None:
        if self._mark_lost_if_expired():
            raise RuntimeError("managed installation lease was lost")

    def _run(self) -> None:
        interval = max(0.25, self.lease_seconds / 3)
        while True:
            if self._mark_lost_if_expired():
                return
            with self._state_lock:
                remaining = max(0.0, self._lease_deadline - time.time())
            if self._stop.wait(min(interval, remaining)):
                return
            if self._mark_lost_if_expired():
                return
            try:
                lease_deadline = _renew_target_lease(
                    self.path,
                    self.claimed,
                    self.lease_seconds,
                )
                if lease_deadline is None:
                    self.lost.set()
                    return
                with self._state_lock:
                    self.last_successful_renewal = lease_deadline - self.lease_seconds
                    self._lease_deadline = lease_deadline
            except sqlite3.Error:
                # Retrying is safe only while the last confirmed lease is still live.
                if self._mark_lost_if_expired():
                    return
                continue


def _finish_target(
    path: Path,
    claimed: dict[str, Any],
    *,
    state: str,
    error: str = "",
    detail: dict[str, Any] | None = None,
    retry_after: float = 0,
    failure_count: int | None = None,
) -> bool:
    persisted_detail = dict(detail or {})
    if (
        state == "completed"
        and persisted_detail.get("proof_source") == "local_filesystem"
        and not persisted_detail.get("receipt_schema")
    ):
        # Compatibility for an in-flight v1 receiver during a rolling deploy.
        # New receivers already return the receipt; the controller adds it only
        # when the old response contains a locally-derived immutable proof.
        persisted_detail.setdefault("installed", True)
        persisted_detail = _finalize_installation_detail(claimed, persisted_detail)
    now = time.time()
    with closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """
            UPDATE managed_installation_targets
            SET state = ?, error = ?, detail_json = ?, next_attempt_at = ?,
                failure_count = COALESCE(?, failure_count),
                lease_token = '', lease_until = 0, updated_at = ?
            WHERE operation_id = ? AND node_id = ? AND lease_token = ?
              AND state NOT IN ('completed', 'failed', 'cancelled')
              AND lease_until > ?
            """,
            (
                state,
                _redact_error(error),
                json.dumps(persisted_detail, ensure_ascii=True, separators=(",", ":")),
                now + max(0, retry_after),
                failure_count,
                _utc_now(),
                claimed["id"],
                claimed["node_id"],
                claimed["lease_token"],
                now,
            ),
        ).rowcount
        if updated:
            _recompute_operation(conn, claimed["id"])
        conn.execute("COMMIT")
        return bool(updated)


def dispatch_managed_installations_once(
    *,
    db_path: Path | None = None,
    config_path: Path | None = None,
    executor: Any = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> bool:
    path = db_path or managed_installations_db_path()
    if _dispatch_owner_deletion_once(path, config_path=config_path):
        return True
    claimed = _claim_target(path, now=time.time(), lease_seconds=lease_seconds)
    if claimed is None:
        return False
    try:
        with _LeaseHeartbeat(path, claimed, lease_seconds=lease_seconds) as heartbeat:
            if claimed["node_id"] == "server":
                heartbeat.ensure_owned()
                if str(claimed.get("action") or "install") == "rollback":
                    detail = _execute_allowlisted_rollback(
                        claimed,
                        executor=executor,
                        ownership_guard=heartbeat.ensure_owned,
                    )
                else:
                    detail = _execute_allowlisted_installation(
                        claimed,
                        executor=executor,
                        ownership_guard=heartbeat.ensure_owned,
                    )
                heartbeat.ensure_owned()
                if not heartbeat.lost.is_set():
                    _finish_target(
                        path,
                        claimed,
                        state="completed",
                        detail=detail,
                        failure_count=0,
                    )
            else:
                heartbeat.ensure_owned()
                remote = _dispatch_or_poll_remote(claimed, config_path=config_path)
                heartbeat.ensure_owned()
                if heartbeat.lost.is_set():
                    return True
                remote_state = str(remote.get("state") or "running").lower()
                if remote_state == "completed":
                    completed_detail = remote.get("detail")
                    if not isinstance(completed_detail, dict):
                        completed_detail = remote
                    _finish_target(
                        path,
                        claimed,
                        state="completed",
                        detail=completed_detail,
                        failure_count=0,
                    )
                elif remote_state in {"failed", "cancelled"}:
                    _finish_target(
                        path,
                        claimed,
                        state="failed",
                        error=str(remote.get("error") or "remote installation failed"),
                        detail=remote,
                    )
                else:
                    _finish_target(
                        path,
                        claimed,
                        state="running",
                        detail=remote,
                        retry_after=REMOTE_POLL_INTERVAL_SECONDS,
                        failure_count=0,
                    )
    except Exception as exc:
        failures = int(claimed.get("failure_count") or 0) + 1
        terminal = failures >= MAX_INSTALL_ATTEMPTS
        _finish_target(
            path,
            claimed,
            state="failed" if terminal else "retry",
            error=exc,
            retry_after=0 if terminal else min(60, 2 ** min(failures, 5)),
            failure_count=failures,
        )
    finally:
        _release_execution_fence(claimed)
    return True


def _dispatch_or_poll_remote(
    claimed: dict[str, Any],
    *,
    config_path: Path | None,
) -> dict[str, Any]:
    route = _installation_route(str(claimed["node_id"]), config_path)
    operation_id = quote(str(claimed["id"]), safe="")
    if str(claimed.get("target_state") or "") in {"pending", "accepted", "retry"}:
        body_payload = {
            "id": claimed["id"],
            "request_id": claimed["request_id"],
            "node_id": claimed["node_id"],
            "kind": claimed["kind"],
            "identifier": claimed["identifier"],
            "profile": claimed["profile"],
            "project_name": claimed["project_name"],
            "action": str(claimed.get("action") or "install"),
            "rollback_of": str(claimed.get("rollback_of") or ""),
            "owner_id": claimed["owner_id"],
            "account_generation": claimed["account_generation"],
        }
        if body_payload["action"] == "rollback":
            receipts = _safe_json_object(
                str(claimed.get("rollback_receipts_json") or "{}")
            )
            body_payload["rollback_receipt"] = receipts.get(
                str(claimed["node_id"]),
                {},
            )
        if str(claimed.get("kind") or "") == "project":
            body_payload["source_lock"] = {
                "canonical_source": claimed.get("canonical_source", ""),
                "source_ref": claimed.get("source_ref", ""),
                "resolved_commit": claimed.get("resolved_commit", ""),
                "resolved_tree": claimed.get("resolved_tree", ""),
                "policy_version": claimed.get("policy_version", ""),
            }
        body = json.dumps(body_payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            route["url"],
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-DBB3-Token": route["token"],
            },
            method="POST",
        )
    else:
        request = Request(
            f"{route['url'].rstrip('/')}/{operation_id}",
            headers={"Accept": "application/json", "X-DBB3-Token": route["token"]},
            method="GET",
        )
    return _read_json_response(request, route["timeout"])


def _installation_route(node_id: str, config_path: Path | None) -> dict[str, Any]:
    from hermes_cli.managed_nodes import load_managed_nodes_config, read_private_token

    for config in load_managed_nodes_config(config_path):
        url = str((config.get("installation_urls") or {}).get(node_id) or "").strip()
        if not url:
            continue
        token_path = str(config.get("installation_token_file") or "").strip()
        if not token_path:
            raise RuntimeError(
                f"managed installation route for {node_id} has no dedicated credential"
            )
        if (
            Path(token_path).expanduser().resolve(strict=False)
            == Path(str(config.get("token_file") or "")).expanduser().resolve(strict=False)
        ):
            raise RuntimeError(
                f"managed installation route for {node_id} reuses the status credential"
            )
        token = read_private_token(token_path, label="managed installation credential")
        return {"url": url, "token": token, "timeout": config["timeout_seconds"]}
    raise RuntimeError(f"managed installation route for {node_id} is not configured")


def _read_json_response(request: Request, timeout: float) -> dict[str, Any]:
    with urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("managed installation response exceeded the size limit")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("managed installation response must be an object")
    return payload


def run_managed_installation_dispatcher(
    stop_event: threading.Event,
    *,
    db_path: Path | None = None,
    config_path: Path | None = None,
    interval: float = 1.0,
) -> None:
    while not stop_event.is_set():
        worked = dispatch_managed_installations_once(db_path=db_path, config_path=config_path)
        stop_event.wait(0 if worked else max(0.1, interval))


def load_managed_installation_receiver_config(
    path: Path | None = None,
) -> dict[str, Any] | None:
    from hermes_cli.managed_nodes import managed_nodes_config_path

    config_path = path or managed_nodes_config_path()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid managed-nodes configuration: {exc}") from exc
    raw = payload.get("installation_receiver") if isinstance(payload, dict) else None
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("installation_receiver must be an object")
    node_id = str(raw.get("node_id") or "").strip().lower()
    if node_id not in {"dbb3", "wsl"}:
        raise ValueError("installation_receiver node_id must be dbb3 or wsl")
    token_file = str(raw.get("token_file") or "").strip()
    if not token_file:
        raise ValueError("installation_receiver requires token_file")
    state_file = str(raw.get("state_file") or "managed-installations.db").strip()
    project_root = str(raw.get("project_root") or "managed-projects").strip()
    release_evidence_file = str(raw.get("release_evidence_file") or "").strip()
    state_path = Path(state_file)
    root_path = Path(project_root)
    if not state_path.is_absolute():
        state_path = config_path.parent / state_path
    if not root_path.is_absolute():
        root_path = config_path.parent / root_path
    evidence_path: Path | None = None
    if release_evidence_file:
        evidence_path = Path(release_evidence_file)
        if not evidence_path.is_absolute():
            evidence_path = config_path.parent / evidence_path
    return {
        "node_id": node_id,
        "token_file": token_file,
        "state_file": state_path.resolve(),
        "project_root": root_path.resolve(),
        "release_evidence_file": (
            evidence_path.resolve(strict=False) if evidence_path is not None else None
        ),
    }


def accept_managed_installation(
    payload: dict[str, Any],
    presented_token: str,
    config_path: Path | None = None,
) -> dict[str, Any]:
    config = load_managed_installation_receiver_config(config_path)
    if config is None:
        raise RuntimeError("managed installation receiver is not configured")
    _authenticate_receiver(config, presented_token)
    node_id = str(payload.get("node_id") or "").strip().lower()
    if node_id != config["node_id"]:
        raise ValueError("installation target does not match this node")
    if str(payload.get("action") or "install").strip().lower() == "delete_owner":
        owner_id, generation = _normalize_owner_boundary(
            str(payload.get("owner_id") or ""),
            str(payload.get("account_generation") or ""),
        )
        if owner_id == "server-admin":
            raise ValueError("an account owner_id is required")
        delete_owner_managed_resources(
            owner_id,
            account_generation=generation,
            db_path=Path(config["state_file"]),
            _node_id=node_id,
            _project_root=Path(config["project_root"]),
        )
        state = _owner_deletion_state(
            Path(config["state_file"]), owner_id, generation
        )
        return {
            "accepted": True,
            "owner_id": owner_id,
            "account_generation": generation,
            "node_id": node_id,
            "state": state,
        }
    action = str(payload.get("action") or "install").strip().lower()
    if action not in {"install", "rollback"}:
        raise ValueError("managed installation action is invalid")
    is_probe = payload.get("probe") is True
    if is_probe and (
        str(payload.get("kind") or "").strip().lower() != PROBE_KIND
        or str(payload.get("identifier") or "").strip() != PROBE_IDENTIFIER
    ):
        raise ValueError("managed installation probe contract is invalid")
    operation = create_managed_installation(
        kind="skill" if is_probe else str(payload.get("kind") or ""),
        identifier=(
            "builtin/managed-installation-route-probe"
            if is_probe
            else str(payload.get("identifier") or "")
        ),
        profile=str(payload.get("profile") or "default"),
        request_id=str(payload.get("id") or payload.get("request_id") or ""),
        scope="auto",
        targets=[node_id],
        project_name=str(payload.get("project_name") or ""),
        source_ref=(
            str((payload.get("source_lock") or {}).get("source_ref") or "")
            if isinstance(payload.get("source_lock"), dict)
            else ""
        ),
        source_lock=(
            dict(payload["source_lock"])
            if not is_probe
            and str(payload.get("kind") or "").strip().lower() == "project"
            and isinstance(payload.get("source_lock"), dict)
            else None
        ),
        db_path=config["state_file"],
        owner_id=str(payload.get("owner_id") or "server-admin"),
        account_generation=str(payload.get("account_generation") or ""),
        _action=action,
        _rollback_of=str(payload.get("rollback_of") or ""),
        _rollback_receipts=(
            {node_id: dict(payload["rollback_receipt"])}
            if action == "rollback" and isinstance(payload.get("rollback_receipt"), dict)
            else None
        ),
    )
    if is_probe:
        claimed, _wait_seconds = _claim_received_target(
            Path(config["state_file"]),
            operation["id"],
            node_id,
        )
        if claimed is not None:
            try:
                _finish_target(
                    Path(config["state_file"]),
                    claimed,
                    state="completed",
                    detail={"probe": True, "persisted": True, "node_id": node_id},
                    failure_count=0,
                )
            finally:
                _release_execution_fence(claimed)
    else:
        _start_receiver_thread(operation["id"], config)
    current = get_managed_installation(operation["id"], db_path=config["state_file"])
    target = current["targets"][0]
    return {
        "accepted": True,
        "id": str(payload.get("id") or operation["id"]),
        "state": target["state"],
        "node_id": node_id,
    }


def get_received_managed_installation(
    operation_id: str,
    presented_token: str,
    config_path: Path | None = None,
) -> dict[str, Any]:
    config = load_managed_installation_receiver_config(config_path)
    if config is None:
        raise RuntimeError("managed installation receiver is not configured")
    _authenticate_receiver(config, presented_token)
    # The receiver uses the main operation id as its idempotency key.
    with closing(_connect(config["state_file"])) as conn:
        row = conn.execute(
            "SELECT id FROM managed_installations WHERE request_id = ? OR id = ? "
            "OR (request_id LIKE 'acct:%' AND "
            "substr(request_id,length(request_id)-length(?) + 1)=?)",
            (operation_id, operation_id, operation_id, operation_id),
        ).fetchone()
    if row is None:
        raise KeyError(operation_id)
    current = get_managed_installation(str(row["id"]), db_path=config["state_file"])
    target = current["targets"][0]
    return {
        "id": operation_id,
        "node_id": target["node_id"],
        "state": target["state"],
        "error": target["error"],
        "detail": target["detail"],
        "updated_at": target["updated_at"],
    }


def resume_received_managed_installations(config_path: Path | None = None) -> int:
    config = load_managed_installation_receiver_config(config_path)
    if config is None:
        return 0
    with closing(_connect(config["state_file"])) as conn:
        conn.execute("BEGIN IMMEDIATE")
        ids = [
            str(row["id"])
            for row in conn.execute(
                """
                SELECT DISTINCT i.id FROM managed_installations i
                JOIN managed_installation_targets t ON t.operation_id = i.id
                WHERE t.state IN ('pending', 'accepted', 'running', 'retry')
                """
            ).fetchall()
        ]
        conn.execute("COMMIT")
    for operation_id in ids:
        _start_receiver_thread(operation_id, config)
    return len(ids)


def _authenticate_receiver(config: dict[str, Any], presented_token: str) -> None:
    from hermes_cli.managed_nodes import read_private_token

    expected = read_private_token(
        config["token_file"], label="managed installation receiver credential"
    )
    supplied = str(presented_token or "").strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise PermissionError("invalid managed installation credential")


def _start_receiver_thread(operation_id: str, config: dict[str, Any]) -> None:
    key = f"{config['state_file']}:{operation_id}"
    with _THREADS_LOCK:
        existing = _RECEIVER_THREADS.get(key)
        if existing and existing.is_alive():
            return
        thread = threading.Thread(
            target=_run_received_installation,
            args=(operation_id, config, key),
            daemon=True,
            name=f"managed-install-{operation_id[-8:]}",
        )
        _RECEIVER_THREADS[key] = thread
        thread.start()


def _run_received_installation(operation_id: str, config: dict[str, Any], key: str) -> None:
    try:
        path = Path(config["state_file"])
        while True:
            claimed, wait_seconds = _claim_received_target(
                path,
                operation_id,
                str(config["node_id"]),
            )
            if claimed is None:
                if wait_seconds <= 0:
                    return
                time.sleep(min(wait_seconds, DEFAULT_LEASE_SECONDS))
                continue
            try:
                with _LeaseHeartbeat(path, claimed) as heartbeat:
                    heartbeat.ensure_owned()
                    if str(claimed.get("action") or "install") == "rollback":
                        detail = _execute_allowlisted_rollback(
                            claimed,
                            project_root=config["project_root"],
                            ownership_guard=heartbeat.ensure_owned,
                        )
                    else:
                        detail = _execute_allowlisted_installation(
                            claimed,
                            project_root=config["project_root"],
                            ownership_guard=heartbeat.ensure_owned,
                        )
                    heartbeat.ensure_owned()
                    if heartbeat.lost.is_set():
                        return
                    _finish_target(
                        path,
                        claimed,
                        state="completed",
                        detail=detail,
                        failure_count=0,
                    )
                return
            except Exception as exc:
                failures = int(claimed.get("failure_count") or 0) + 1
                terminal = failures >= MAX_INSTALL_ATTEMPTS
                updated = _finish_target(
                    path,
                    claimed,
                    state="failed" if terminal else "retry",
                    error=exc,
                    retry_after=0 if terminal else min(60, 2 ** min(failures, 5)),
                    failure_count=failures,
                )
                if terminal or not updated:
                    return
            finally:
                _release_execution_fence(claimed)
    finally:
        with _THREADS_LOCK:
            _RECEIVER_THREADS.pop(key, None)


def _claim_received_target(
    path: Path,
    operation_id: str,
    node_id: str,
    *,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> tuple[dict[str, Any] | None, float]:
    """Claim one receiver operation, returning how long an active peer owns it."""

    now = time.time()
    token = uuid4().hex
    with closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        operation = conn.execute(
            "SELECT * FROM managed_installations WHERE id = ?",
            (operation_id,),
        ).fetchone()
        target = conn.execute(
            "SELECT * FROM managed_installation_targets "
            "WHERE operation_id = ? AND node_id = ?",
            (operation_id, node_id),
        ).fetchone()
        if operation is None or target is None or str(target["state"]) in TERMINAL_STATES:
            conn.execute("COMMIT")
            return None, 0
        ready_at = max(float(target["next_attempt_at"]), float(target["lease_until"]))
        if (
            float(target["next_attempt_at"]) > now
            or (str(target["lease_token"]) and float(target["lease_until"]) > now)
        ):
            conn.execute("COMMIT")
            return None, max(0.1, ready_at - now)
        updated = conn.execute(
            """
            UPDATE managed_installation_targets
            SET state = 'running', lease_token = ?, lease_until = ?,
                attempts = attempts + 1, updated_at = ?
            WHERE operation_id = ? AND node_id = ?
              AND state IN ('pending', 'accepted', 'running', 'retry')
              AND next_attempt_at <= ?
              AND (lease_token = '' OR lease_until <= ?)
            """,
            (
                token,
                now + lease_seconds,
                _utc_now(),
                operation_id,
                node_id,
                now,
                now,
            ),
        ).rowcount
        if not updated:
            conn.execute("COMMIT")
            return None, 0.1
        owner_id = str(operation["owner_id"] or "server-admin")
        generation = str(operation["account_generation"] or "")
        fence_key = (
            f"account:{owner_id}:{generation}"
            if owner_id != "server-admin"
            else operation_id
        )
        fence = _try_execution_fence(path, fence_key, node_id)
        if fence is None:
            conn.execute("ROLLBACK")
            return None, 0.1
        try:
            _recompute_operation(conn, operation_id)
            target = conn.execute(
                "SELECT * FROM managed_installation_targets "
                "WHERE operation_id = ? AND node_id = ?",
                (operation_id, node_id),
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            fence.release()
            raise
    claimed = dict(operation)
    claimed.update({
        "node_id": node_id,
        "target_state": str(target["state"]),
        "attempts": int(target["attempts"]),
        "failure_count": int(target["failure_count"]),
        "lease_token": token,
        "lease_until": float(target["lease_until"]),
        "_execution_fence": fence,
    })
    return claimed, 0


def _finalize_installation_detail(
    operation: dict[str, Any],
    detail: dict[str, Any],
) -> dict[str, Any]:
    """Attach the locally-derived, versioned node receipt to target detail."""

    result = dict(detail)
    proof, proof_error = _validated_managed_resource_proof(result)
    tools = sorted({
        str(tool).strip()
        for tool in (result.get("tools") or [])
        if isinstance(tool, str) and str(tool).strip()
    })
    permissions = sorted({
        str(permission).strip()
        for permission in (result.get("permissions") or [])
        if isinstance(permission, str) and str(permission).strip()
    })
    checks = list(result.pop("health_checks", []) or [])
    checks.insert(0, {
        "name": "immutable_local_proof",
        "ok": not proof_error,
        "detail": proof_error,
    })
    artifact_hash = proof[1] or (
        hashlib.sha256(f"immutable-proof\0{proof[0]}".encode("utf-8")).hexdigest()
        if proof[0]
        else ""
    )
    if artifact_hash and not result.get("content_hash") and not result.get("sha256"):
        result["content_hash"] = artifact_hash
    result.update({
        "receipt_schema": 1,
        "node_id": str(operation.get("node_id") or "server"),
        "policy_version": str(
            operation.get("policy_version") or MANAGED_SOURCE_POLICY_VERSION
        ),
        "canonical_source": str(operation.get("canonical_source") or ""),
        "source_ref": str(operation.get("source_ref") or ""),
        "artifact_hash": artifact_hash,
        "installed_version": str(result.get("resolved_version") or ""),
        "immutable_revision": proof[0],
        "tools": tools,
        "permissions": permissions,
        "tools_complete": bool(result.get("tools_complete", True)),
        "health": {
            "status": "healthy" if not proof_error and bool(result.get("installed")) else "failed",
            "checks": checks,
        },
        "verified_at": _utc_now(),
    })
    return result


def _rollback_receipt_for_node(operation: dict[str, Any]) -> dict[str, Any]:
    receipts = _safe_json_object(str(operation.get("rollback_receipts_json") or "{}"))
    receipt = receipts.get(str(operation.get("node_id") or ""))
    if not isinstance(receipt, dict) or int(receipt.get("receipt_schema") or 0) != 1:
        raise RuntimeError("rollback requires the verified receipt for this node")
    receipt_node = str(receipt.get("node_id") or operation.get("node_id") or "")
    if receipt_node != str(operation.get("node_id") or ""):
        raise RuntimeError("rollback receipt belongs to another node")
    return receipt


def _execute_allowlisted_rollback(
    operation: dict[str, Any],
    *,
    executor: Any = None,
    project_root: Path | None = None,
    ownership_guard: Any = None,
) -> dict[str, Any]:
    kind = _normalize_kind(str(operation["kind"]))
    identifier = _normalize_identifier(kind, str(operation["identifier"]))
    profile = str(operation.get("profile") or "default")
    execution_profile = _operation_profile_name(operation, profile)
    owner_id = str(operation.get("owner_id") or "server-admin")
    account_generation = str(operation.get("account_generation") or "")
    guard = ownership_guard or (lambda: None)
    if executor is None:
        fence = operation.get("_execution_fence")
        runner = lambda command, *, timeout: _run_command_fenced(
            command,
            timeout=timeout,
            ownership_guard=guard,
            fence=fence if isinstance(fence, _ExecutionFence) else None,
        )
    else:
        runner = executor
    receipt = _rollback_receipt_for_node(operation)
    installed_name = str(receipt.get("installed_name") or "").strip()

    if kind in {"skill", "mcp"}:
        if not installed_name:
            raise RuntimeError("rollback receipt is missing the installed resource name")
        command = [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "-p",
            execution_profile,
            "skills" if kind == "skill" else "mcp",
            "uninstall" if kind == "skill" else "remove",
            installed_name,
        ]
        if kind == "skill":
            command.append("--yes")
        lock = (
            _account_runtime_lock(owner_id, account_generation, profile)
            if owner_id != "server-admin" and executor is None
            else nullcontext()
        )
        with lock:
            guard()
            result = runner(command, timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS)
            guard()
        return {
            "rollback_receipt_schema": 1,
            "removed": True,
            "kind": kind,
            "installed_name": installed_name,
            "previous_artifact_hash": str(receipt.get("artifact_hash") or ""),
            "node_id": str(operation.get("node_id") or "server"),
            "health": {"status": "healthy", "checks": [{"name": "uninstall_exit", "ok": True}]},
            "summary": _command_summary(result),
            "verified_at": _utc_now(),
        }

    root = Path(project_root or (Path(get_hermes_home()) / "managed-projects")).resolve()
    if owner_id != "server-admin":
        owner_id, account_generation = _normalize_owner_boundary(
            owner_id, account_generation
        )
        root = _account_project_root(root, owner_id, account_generation, create=False)
    name = str(operation.get("project_name") or "").strip() or _project_name(identifier)
    destination = (root / name).resolve()
    destination.relative_to(root)
    if not destination.exists():
        raise RuntimeError("managed project rollback target is missing")
    guard()
    _validate_managed_project(
        destination,
        str(operation.get("canonical_source") or identifier),
        runner=runner,
        require_marker=True,
        source_lock=operation,
    )
    guard()
    if destination.is_symlink() or not destination.is_dir():
        raise RuntimeError("managed project rollback path is unsafe")
    shutil.rmtree(destination)
    guard()
    return {
        "rollback_receipt_schema": 1,
        "removed": True,
        "kind": kind,
        "previous_artifact_hash": str(receipt.get("artifact_hash") or ""),
        "node_id": str(operation.get("node_id") or "server"),
        "health": {"status": "healthy", "checks": [{"name": "managed_tree_removed", "ok": True}]},
        "verified_at": _utc_now(),
    }


def _execute_allowlisted_installation(
    operation: dict[str, Any],
    *,
    executor: Any = None,
    project_root: Path | None = None,
    ownership_guard: Any = None,
) -> dict[str, Any]:
    kind = _normalize_kind(str(operation["kind"]))
    identifier = _normalize_identifier(kind, str(operation["identifier"]))
    profile = str(operation.get("profile") or "default")
    execution_profile = _operation_profile_name(operation, profile)
    owner_id = str(operation.get("owner_id") or "server-admin")
    account_generation = str(operation.get("account_generation") or "")
    guard = ownership_guard or (lambda: None)
    if executor is None:
        fence = operation.get("_execution_fence")
        runner = lambda command, *, timeout: _run_command_fenced(
            command,
            timeout=timeout,
            ownership_guard=guard,
            fence=fence if isinstance(fence, _ExecutionFence) else None,
        )
    else:
        runner = executor
    if kind == "skill":
        profile_home = _operation_profile_home(
            operation, profile, resolve_profile=executor is None
        )
        account_scope = owner_id != "server-admin" and executor is None
        if account_scope:
            owner_id, account_generation = _normalize_owner_boundary(
                owner_id, account_generation
            )
        lock = (
            _account_runtime_lock(owner_id, account_generation, profile)
            if account_scope
            else nullcontext()
        )
        with lock:
            if account_scope:
                _materialize_account_runtime_locked(
                    owner_id,
                    account_generation,
                    profile,
                    profile_home=profile_home,
                    base_home=_profile_home(profile),
                    resource_home=profile_home,
                )
            before = _load_skill_lock(profile_home)
            guard()
            command = [sys.executable, "-m", "hermes_cli.main", "-p", execution_profile, "skills", "install", identifier, "--yes"]
            result = runner(command, timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS)
            guard()
            detail = {"installed": True, "kind": kind, "summary": _command_summary(result)}
            try:
                detail.update(_installed_skill_proof(profile_home, identifier, before))
            except RuntimeError:
                if executor is None:
                    raise
            return _finalize_installation_detail(operation, detail)
    if kind == "mcp":
        profile_home = _operation_profile_home(
            operation, profile, resolve_profile=executor is None
        )
        account_scope = owner_id != "server-admin" and executor is None
        if account_scope:
            owner_id, account_generation = _normalize_owner_boundary(
                owner_id, account_generation
            )
        lock = (
            _account_runtime_lock(owner_id, account_generation, profile)
            if account_scope
            else nullcontext()
        )
        with lock:
            if account_scope:
                _materialize_account_runtime_locked(
                    owner_id,
                    account_generation,
                    profile,
                    profile_home=profile_home,
                    base_home=_profile_home(profile),
                    resource_home=profile_home,
                )
            guard()
            command = [sys.executable, "-m", "hermes_cli.main", "-p", execution_profile, "mcp", "install", identifier]
            result = runner(command, timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS)
            guard()
            detail = {"installed": True, "kind": kind, "summary": _command_summary(result)}
            try:
                proof = _installed_mcp_proof(
                    profile_home,
                    identifier,
                    probe_health=executor is None,
                )
                detail.update(proof)
                if account_scope:
                    _record_account_mcp_server(
                        profile_home, str(proof.get("installed_name") or "")
                    )
            except RuntimeError:
                if executor is None:
                    raise
            return _finalize_installation_detail(operation, detail)

    guard()
    root = Path(project_root or (Path(get_hermes_home()) / "managed-projects")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if owner_id != "server-admin":
        owner_id, account_generation = _normalize_owner_boundary(
            owner_id, account_generation
        )
        root = _account_project_root(
            root,
            owner_id,
            account_generation,
            create=True,
        )
    name = str(operation.get("project_name") or "").strip() or _project_name(identifier)
    destination = (root / name).resolve()
    destination.relative_to(root)
    locked_commit = str(operation.get("resolved_commit") or "").strip().lower()
    locked_tree = str(operation.get("resolved_tree") or "").strip().lower()
    canonical_source = str(
        operation.get("canonical_source") or _normalize_project_origin(identifier)
    )
    if destination.exists():
        guard()
        head = _validate_managed_project(
            destination,
            canonical_source,
            runner=runner,
            require_marker=True,
            source_lock=operation if locked_commit else None,
        )
        artifact_hash = hashlib.sha256(
            f"git-tree\0{locked_tree or head}".encode("ascii")
        ).hexdigest()
        guard()
        return _finalize_installation_detail(operation, {
            "installed": True,
            "kind": kind,
            "path": str(destination),
            "existing": True,
            "head": head,
            "resolved_commit": head,
            "tree_sha": locked_tree,
            "content_hash": artifact_hash,
            "permissions": ["managed-project-filesystem"],
            "proof_schema": 1,
            "proof_source": "local_filesystem",
        })
    staging_id = hashlib.sha256(
        f"{operation.get('id') or identifier}:{name}".encode("utf-8")
    ).hexdigest()[:16]
    staging = (root / f".{name}.managed-install-{staging_id}").resolve()
    staging.relative_to(root)
    if staging.exists() or staging.is_symlink():
        if staging.is_symlink() or not staging.is_dir():
            raise RuntimeError("managed project staging path is unsafe")
        shutil.rmtree(staging)
    guard()
    if locked_commit:
        pins = tuple(operation.get("_source_pins") or ())
        if not pins:
            pins = _managed_source_curl_resolve(canonical_source)
        runner(
            _managed_git_command("init", str(staging)),
            timeout=30,
        )
        runner(
            _managed_git_command(
                "-C", str(staging), "remote", "add", "origin", canonical_source
            ),
            timeout=30,
        )
        result = runner(
            _managed_git_command(
                "-C", str(staging),
                "fetch", "--no-tags", "--depth=1", "--filter=blob:none",
                "origin", locked_commit,
                curl_resolve=pins,
            ),
            timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )
        runner(
            _managed_git_command(
                "-C", str(staging), "checkout", "--detach", locked_commit
            ),
            timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )
    else:
        result = runner(
            _managed_git_command(
                "clone",
                "--filter=blob:none",
                "--",
                identifier,
                str(staging),
            ),
            timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )
    head = _validate_managed_project(
        staging,
        canonical_source,
        runner=runner,
        require_marker=False,
    )
    if locked_commit and head != locked_commit:
        raise RuntimeError("project checkout does not match the immutable source lock")
    if locked_commit:
        tree = _command_output(runner(
            _managed_git_command(
                "-C", str(staging), "rev-parse", "--verify", "HEAD^{tree}"
            ),
            timeout=30,
        )).lower()
        branch = _command_output(runner(
            _managed_git_command(
                "-C", str(staging), "rev-parse", "--abbrev-ref", "HEAD"
            ),
            timeout=30,
        ))
        if tree != locked_tree:
            raise RuntimeError("project tree does not match the immutable source lock")
        if branch != "HEAD":
            raise RuntimeError("managed project checkout is not detached")
    else:
        tree = ""
    artifact_hash = hashlib.sha256(
        f"git-tree\0{tree or head}".encode("ascii")
    ).hexdigest()
    marker = staging / ".git" / "hermes-managed-install.json"
    marker_temporary = marker.with_name(f"{marker.name}.new")
    marker_temporary.write_text(
        json.dumps(
            {
                "version": 2 if locked_commit else 1,
                "origin": canonical_source,
                "source_ref": str(operation.get("source_ref") or ""),
                "head": head,
                "tree": tree,
                "artifact_hash": artifact_hash,
                "policy_version": str(
                    operation.get("policy_version") or MANAGED_SOURCE_POLICY_VERSION
                ),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ) + "\n",
        encoding="utf-8",
    )
    os.replace(marker_temporary, marker)
    guard()
    os.replace(staging, destination)
    return _finalize_installation_detail(operation, {
        "installed": True,
        "kind": kind,
        "path": str(destination),
        "existing": False,
        "head": head,
        "resolved_commit": head,
        "tree_sha": tree,
        "content_hash": artifact_hash,
        "permissions": ["managed-project-filesystem"],
        "proof_schema": 1,
        "proof_source": "local_filesystem",
        "summary": _command_summary(result),
    })


def _command_output(result: Any) -> str:
    if isinstance(result, subprocess.CompletedProcess):
        return str(result.stdout or "").strip()
    return str(result or "").strip()


def _validate_managed_project(
    destination: Path,
    identifier: str,
    *,
    runner: Any,
    require_marker: bool,
    source_lock: sqlite3.Row | dict[str, Any] | None = None,
) -> str:
    git_directory = destination / ".git"
    if not destination.is_dir() or not git_directory.is_dir() or git_directory.is_symlink():
        raise RuntimeError("project destination is not a complete git worktree")
    # `git remote get-url` applies url.<base>.insteadOf rules from the
    # repository's local configuration.  A modified worktree could therefore
    # store an attacker origin while making the validation command print the
    # approved GitHub URL.  Read the raw local value, with includes disabled,
    # and reject ambiguous multi-value origins instead.
    origin_result = runner(
        _managed_git_command(
            "-C", str(destination),
            "config", "--local", "--no-includes", "--get-all",
            "remote.origin.url",
        ),
        timeout=30,
    )
    expected_origin = _normalize_project_origin(identifier)
    raw_origins = [
        value.strip()
        for value in _command_output(origin_result).splitlines()
        if value.strip()
    ]
    try:
        actual_origin = (
            _normalize_project_origin(raw_origins[0])
            if len(raw_origins) == 1
            else None
        )
    except ValueError:
        actual_origin = None
    if (
        actual_origin is None
        or actual_origin != expected_origin
    ):
        raise RuntimeError("project destination origin does not match requested repository")
    head_result = runner(
        _managed_git_command("-C", str(destination), "rev-parse", "--verify", "HEAD"),
        timeout=30,
    )
    head = _command_output(head_result).lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", head):
        raise RuntimeError("project destination has no valid checked-out HEAD")
    locked_tree = ""
    if source_lock is not None:
        locked_commit = str(source_lock["resolved_commit"] or "").lower()
        locked_tree = str(source_lock["resolved_tree"] or "").lower()
        if head != locked_commit:
            raise RuntimeError("project destination commit does not match source lock")
        tree = _command_output(runner(
            _managed_git_command(
                "-C", str(destination), "rev-parse", "--verify", "HEAD^{tree}"
            ),
            timeout=30,
        )).lower()
        branch = _command_output(runner(
            _managed_git_command(
                "-C", str(destination), "rev-parse", "--abbrev-ref", "HEAD"
            ),
            timeout=30,
        ))
        if tree != locked_tree:
            raise RuntimeError("project destination tree does not match source lock")
        if branch != "HEAD":
            raise RuntimeError("managed project destination is not detached")
    if not any(item.name != ".git" for item in destination.iterdir()):
        raise RuntimeError("project destination worktree is empty")
    marker = git_directory / "hermes-managed-install.json"
    if require_marker:
        try:
            marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("project destination has no valid completion marker") from exc
        expected_version = 2 if source_lock is not None else 1
        if (
            not isinstance(marker_payload, dict)
            or marker_payload.get("version") != expected_version
            or marker_payload.get("origin") != expected_origin
            or str(marker_payload.get("head") or "").lower() != head
            or (
                source_lock is not None
                and (
                    str(marker_payload.get("tree") or "").lower() != locked_tree
                    or str(marker_payload.get("source_ref") or "")
                    != str(source_lock["source_ref"] or "")
                    or str(marker_payload.get("policy_version") or "")
                    != str(source_lock["policy_version"] or "")
                    or str(marker_payload.get("artifact_hash") or "").lower()
                    != hashlib.sha256(
                        f"git-tree\0{locked_tree}".encode("ascii")
                    ).hexdigest()
                )
            )
        ):
            raise RuntimeError("project destination completion marker does not match")
    return head


def _normalize_project_origin(identifier: str) -> str:
    """Return a credential-free canonical HTTPS repository origin."""

    value = _normalize_identifier("project", identifier)
    parsed = urlsplit(value)
    host = (parsed.hostname or "").rstrip(".").lower()
    port = parsed.port
    if port not in (None, 443):
        host = f"{host}:{port}"
    path = re.sub(r"/+", "/", unquote(parsed.path)).rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    if not path or path == "/" or any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("project source path is invalid")
    return urlunsplit(("https", host, path, "", ""))


def _project_name(identifier: str) -> str:
    path = urlsplit(identifier if "://" in identifier else f"https://{identifier}").path
    candidate = Path(path.rstrip("/")).name.removesuffix(".git") or "project"
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate).strip("-.")[:80]
    if not candidate:
        candidate = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:12]
    return candidate


def _managed_git_command(
    *arguments: str,
    curl_resolve: Iterable[str] = (),
) -> list[str]:
    """Build a git invocation which accepts only the managed HTTPS transport."""

    pin_options: list[str] = []
    for raw in curl_resolve:
        pin = str(raw or "").strip()
        if not pin.startswith("+github.com:443:") or any(
            character in pin for character in ("\n", "\r", "\x00")
        ):
            raise ValueError("managed Git connection pin is invalid")
        pin_options.extend(("-c", f"http.curloptResolve={pin}"))
    return ["git", *_MANAGED_GIT_OPTIONS, *pin_options, *arguments]


def _run_command(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_hardened_command_environment(),
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or f"command exited {completed.returncode}")
    return completed


def _hardened_command_environment() -> dict[str, str]:
    """Return the minimal trusted environment for managed child processes."""

    # Git accepts configuration, repository locations, and transport overrides
    # through environment variables.  Do not let a receiver inherit any of
    # those controls from its service manager or caller.
    environment = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https",
    })
    return environment


def _run_command_fenced(
    command: list[str],
    *,
    timeout: int,
    ownership_guard: Any,
    fence: _ExecutionFence | None,
) -> subprocess.CompletedProcess[str]:
    """Run a command in a cancellable process group while holding its OS fence."""

    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file, \
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": stdout_file,
            "stderr": stderr_file,
            "text": True,
            "env": _hardened_command_environment(),
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
            if fence is not None:
                kwargs["pass_fds"] = (fence.fd,)
        process = subprocess.Popen(command, **kwargs)
        try:
            while process.poll() is None:
                ownership_guard()
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(0.1)
            ownership_guard()
        except BaseException:
            _terminate_process_group(process)
            raise
        stdout_file.seek(0)
        stderr_file.seek(0)
        completed = subprocess.CompletedProcess(
            command,
            int(process.returncode or 0),
            stdout_file.read(),
            stderr_file.read(),
        )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr or completed.stdout or f"command exited {completed.returncode}"
        )
    return completed


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            taskkill = shutil.which("taskkill")
            if not taskkill:
                raise OSError("taskkill is unavailable")
            subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            kill_process_group = getattr(os, "killpg", None)
            if not callable(kill_process_group):
                raise OSError("process-group termination is unavailable")
            kill_process_group(process.pid, signal.SIGTERM)
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "nt":
                process.kill()
            else:
                kill_process_group = getattr(os, "killpg", None)
                if not callable(kill_process_group):
                    raise OSError("process-group termination is unavailable")
                kill_process_group(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _command_summary(result: Any) -> str:
    if isinstance(result, subprocess.CompletedProcess):
        value = result.stdout or result.stderr or "completed"
    else:
        value = str(result or "completed")
    return _redact_error(value)[-1024:]
