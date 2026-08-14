"""Small adapters for registering common runtime resources in an EffectScope.

The adapters deliberately accept acquisition-specific callbacks.  They do not
pretend that a remote HTTP emission or an uploaded artifact can be rolled back;
only the local ownership boundary is registered with the scope.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable, Iterable

from .effects import EffectHandle, EffectScope


def track_resource(
    scope: EffectScope,
    disposer: Callable[[], Any],
    *,
    description: str,
    durability: str = "in_memory",
    external_boundary: str = "internal",
    idempotency_key: str = "",
) -> EffectHandle:
    """Register a resource after its acquisition has completed."""

    return scope.add(
        disposer,
        description=description,
        durability=durability,
        external_boundary=external_boundary,
        idempotency_key=idempotency_key,
    )


def track_hosted_role_claim(
    scope: EffectScope,
    release: Callable[[], Any],
    *,
    role_stage: str,
    owner: str,
) -> EffectHandle:
    return track_resource(
        scope,
        release,
        description=f"hosted-role-claim:{role_stage}",
        durability="durable_claim",
        external_boundary="account_state",
        idempotency_key=f"hosted-role:{role_stage}:{owner}",
    )


def track_lease(
    scope: EffectScope,
    release: Callable[[], Any],
    *,
    lease_id: str,
    boundary: str = "account_state",
) -> EffectHandle:
    return track_resource(
        scope,
        release,
        description=f"lease:{lease_id}",
        durability="durable_claim",
        external_boundary=boundary,
        idempotency_key=f"lease:{lease_id}",
    )


def track_stream(
    scope: EffectScope,
    close: Callable[[], Any],
    *,
    stream_id: str,
    external: bool = True,
) -> EffectHandle:
    return track_resource(
        scope,
        close,
        description=f"stream:{stream_id}",
        durability="in_memory",
        external_boundary="network_stream" if external else "internal",
        idempotency_key=f"stream:{stream_id}",
    )


def track_temporary_workspace(
    scope: EffectScope,
    path: str | Path,
    *,
    workspace_id: str,
    keep_on_failure: bool = False,
) -> EffectHandle:
    """Track a private scratch directory without deleting caller-owned paths."""

    workspace = Path(path).resolve()
    if Path(path).is_symlink() or not workspace.exists() or not workspace.is_dir():
        raise ValueError("temporary workspace must be an existing directory")
    marker = workspace / ".hermes-temporary-workspace"
    if not marker.exists():
        raise ValueError("workspace is missing the Hermes temporary marker")

    def dispose() -> None:
        if keep_on_failure:
            return
        shutil.rmtree(workspace, ignore_errors=False)

    return track_resource(
        scope,
        dispose,
        description=f"temporary-workspace:{workspace_id}",
        durability="filesystem",
        external_boundary="local_filesystem",
        idempotency_key=f"workspace:{workspace_id}",
    )


def create_temporary_workspace(
    scope: EffectScope,
    *,
    workspace_id: str,
    parent: str | Path | None = None,
) -> Path:
    """Create and register a marked scratch directory owned by ``scope``."""

    normalized_id = str(workspace_id or "").strip()
    if (
        not normalized_id
        or len(normalized_id) > 128
        or normalized_id in {".", ".."}
        or Path(normalized_id).name != normalized_id
    ):
        raise ValueError("workspace_id must be a bounded path-free identifier")
    root = str(Path(parent).resolve()) if parent is not None else None
    path = Path(tempfile.mkdtemp(prefix=f"hermes-{normalized_id}-", dir=root))
    (path / ".hermes-temporary-workspace").write_text(
        f"workspace_id={normalized_id}\n", encoding="utf-8"
    )
    track_temporary_workspace(scope, path, workspace_id=normalized_id)
    return path


def register_scope_resources(
    scope: EffectScope,
    resources: Iterable[tuple[str, Callable[[], Any]]],
) -> tuple[EffectHandle, ...]:
    """Register a small batch while retaining one stable owner scope."""

    return tuple(
        track_resource(scope, disposer, description=description)
        for description, disposer in resources
    )
