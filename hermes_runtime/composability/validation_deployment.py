"""Local-only deployment control path for validation and rollback drills.

The controller models the production invariants without touching a production
host: immutable staged release directories, digest verification, health-gated
blue/green pointer switching, drain deadlines, and an explicitly named
rollback owner.  It never starts a process or connects to a remote host.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import time
from threading import RLock
from typing import Any, Callable, Mapping
import uuid


class DeploymentControlError(RuntimeError):
    """A local deployment control invariant was violated."""


@dataclass(frozen=True)
class DeploymentReceipt:
    version: str
    previous_version: str | None
    committed: bool
    rolled_back: bool
    phase: str
    release_digest: str
    reason: str = ""


_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ValidationDeploymentController:
    """Filesystem-backed blue/green controller with no remote side effects."""

    def __init__(
        self,
        root: str | Path,
        *,
        rollback_owner: str,
        drain_deadline_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not str(rollback_owner or "").strip():
            raise ValueError("rollback_owner is required")
        if drain_deadline_seconds <= 0:
            raise ValueError("drain_deadline_seconds must be positive")
        self.root = Path(root).expanduser().resolve()
        self.releases = self.root / "releases"
        self.state_path = self.root / "deployment-state.json"
        self.audit_path = self.root / "deployment-events.jsonl"
        self.releases.mkdir(parents=True, exist_ok=True)
        self.rollback_owner = str(rollback_owner).strip()
        self.drain_deadline_seconds = float(drain_deadline_seconds)
        self._clock = clock
        self._lock = RLock()

    def _version_path(self, version: str) -> Path:
        version = str(version or "").strip()
        if not _VERSION_RE.fullmatch(version):
            raise DeploymentControlError("release version contains unsafe characters")
        return self.releases / version

    @staticmethod
    def _digest_files(root: Path) -> str:
        hasher = hashlib.sha256()
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
            if relative == "release.json":
                continue
            hasher.update(relative.encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(path.read_bytes())
        return hasher.hexdigest()

    def stage_release(self, version: str, files: Mapping[str, bytes | str]) -> str:
        """Stage immutable validation data and return its content digest."""

        with self._lock:
            destination = self._version_path(version)
            if destination.exists():
                raise DeploymentControlError(f"release already staged: {version}")
            destination.mkdir(parents=True)
            try:
                for name, content in files.items():
                    relative = Path(str(name))
                    if relative.is_absolute() or ".." in relative.parts:
                        raise DeploymentControlError("release file escapes staging root")
                    target = (destination / relative).resolve()
                    target.relative_to(destination.resolve())
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content.encode("utf-8") if isinstance(content, str) else bytes(content))
                digest = self._digest_files(destination)
                (destination / "release.json").write_text(
                    json.dumps(
                        {"schema_version": "1.0", "version": version, "release_digest": digest},
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                self._audit("stage", version=version, release_digest=digest)
                return digest
            except Exception:
                # The target is a newly-created private staging directory.  Do
                # not leave a partial release that could be selected later.
                import shutil

                shutil.rmtree(destination, ignore_errors=True)
                raise

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema_version": "1.0", "current_version": None, "previous_version": None}
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DeploymentControlError("deployment state is not valid JSON") from exc
        if not isinstance(value, dict):
            raise DeploymentControlError("deployment state must be an object")
        return value

    def _write_state(self, state: dict[str, Any]) -> None:
        temporary = self.state_path.with_name(f".{self.state_path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(state, sort_keys=True, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    def _audit(self, event: str, **fields: Any) -> None:
        record = {"schema_version": "1.0", "event": event, "at": self._clock(), **fields}
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def _verify_release(self, version: str) -> tuple[Path, str]:
        path = self._version_path(version)
        manifest = path / "release.json"
        if not path.is_dir() or not manifest.is_file():
            raise DeploymentControlError(f"release is not staged: {version}")
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DeploymentControlError("release manifest is invalid") from exc
        digest = self._digest_files(path)
        if data.get("version") != version or data.get("release_digest") != digest:
            raise DeploymentControlError("release digest or version verification failed")
        return path, digest

    def deploy(self, version: str, *, health_check: Callable[[Path], bool]) -> DeploymentReceipt:
        """Health-gate and atomically switch the local active release pointer."""

        with self._lock:
            state = self._read_state()
            previous = state.get("current_version")
            try:
                self._audit("preflight", version=version, previous_version=previous)
                path, digest = self._verify_release(version)
                self._audit("isolated_load", version=version, release_digest=digest)
                if not bool(health_check(path)):
                    raise DeploymentControlError("candidate health check failed")
                self._audit("health_pass", version=version, release_digest=digest)
                state.update(
                    {
                        "schema_version": "1.0",
                        "current_version": version,
                        "previous_version": previous,
                        "updated_at": self._clock(),
                        "release_digest": digest,
                        "lifecycle": "active",
                        "drain_deadline": None,
                    }
                )
                self._write_state(state)
                self._audit("traffic_shift_commit", version=version, previous_version=previous)
                return DeploymentReceipt(version, previous, True, False, "commit", digest)
            except Exception as exc:
                self._audit("deployment_rejected", version=version, reason=str(exc)[:512])
                return DeploymentReceipt(version, previous, False, False, "rollback_ready", "", str(exc))

    def begin_drain(self) -> dict[str, Any]:
        """Mark the active release draining with a bounded deadline."""

        with self._lock:
            state = self._read_state()
            if not state.get("current_version"):
                raise DeploymentControlError("no active release to drain")
            deadline = self._clock() + self.drain_deadline_seconds
            state.update({"lifecycle": "draining", "drain_deadline": deadline})
            self._write_state(state)
            self._audit("drain_started", version=state["current_version"], deadline=deadline)
            return state

    def rollback(self, *, owner: str) -> DeploymentReceipt:
        """Restore the exact previous local pointer after owner verification."""

        with self._lock:
            if str(owner or "").strip() != self.rollback_owner:
                raise DeploymentControlError("rollback owner is not authorized")
            state = self._read_state()
            previous = state.get("previous_version")
            current = state.get("current_version")
            if not previous:
                raise DeploymentControlError("no previous release is available")
            _path, digest = self._verify_release(previous)
            state.update(
                {
                    "current_version": previous,
                    "previous_version": current,
                    "release_digest": digest,
                    "lifecycle": "active",
                    "drain_deadline": None,
                    "rolled_back_at": self._clock(),
                }
            )
            self._write_state(state)
            self._audit("rollback_commit", from_version=current, to_version=previous, owner=owner)
            return DeploymentReceipt(previous, current, True, True, "rollback", digest)

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._read_state()
            state["rollback_owner"] = self.rollback_owner
            state["drain_deadline_seconds"] = self.drain_deadline_seconds
            return state


__all__ = ["DeploymentControlError", "DeploymentReceipt", "ValidationDeploymentController"]
