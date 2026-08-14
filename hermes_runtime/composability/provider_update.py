"""Transactional edge-provider replacement primitives.

The core agent loop is deliberately excluded.  Connector/plugin adapters can
use this transaction to stage a new provider, health-check it, shift traffic,
drain the old generation, and restore the previous binding on a failed commit.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable
import uuid

from .providers import ProviderCatalog, ProviderRecord


@dataclass(frozen=True)
class ProviderUpdateResult:
    provider_id: str
    previous_generation: int | None
    new_generation: int
    committed: bool
    rolled_back: bool
    phase: str
    reason: str = ""
    candidate_provider_id: str = ""


class ProviderUpdateTransaction:
    def __init__(
        self,
        catalog: ProviderCatalog,
        *,
        interface_key: str,
        provider_id: str,
        version: str,
        realm: str = "global",
        drain_deadline_seconds: float = 30.0,
    ) -> None:
        self.catalog = catalog
        self.interface_key = interface_key
        self.provider_id = provider_id
        self.version = version
        self.realm = realm
        if drain_deadline_seconds <= 0:
            raise ValueError("drain_deadline_seconds must be positive")
        self.drain_deadline_seconds = float(drain_deadline_seconds)
        self.previous: ProviderRecord | None = None
        self.candidate: ProviderRecord | None = None
        self.phase = "created"
        self.candidate_provider_id = ""

    def execute(
        self,
        *,
        load: Callable[[], Any],
        health_check: Callable[[Any], bool],
        traffic_shift: Callable[[ProviderRecord], Any] | None = None,
        drain_timeout: Callable[[ProviderRecord], bool] | None = None,
    ) -> ProviderUpdateResult:
        self.previous = self.catalog.active_for(self.provider_id)
        previous_generation = self.previous.generation if self.previous else None
        try:
            self.phase = "isolated_load"
            loaded = load()
            self.phase = "health_check"
            if not health_check(loaded):
                raise RuntimeError("candidate provider health check failed")
            if self.previous is not None and self.previous.status.value != "removed":
                # active_for() may return a previously committed candidate
                # generation, so drain the exact bound provider id.
                self.catalog.begin_drain(
                    self.previous.provider_id,
                    deadline_seconds=self.drain_deadline_seconds,
                )
            self.phase = "register"
            self.candidate_provider_id = (
                f"{self.provider_id}:candidate:{uuid.uuid4().hex[:12]}"
            )
            self.candidate = self.catalog.register(
                provider_id=self.candidate_provider_id,
                interface_key=self.interface_key,
                version=self.version,
                realm=self.realm,
                health="healthy",
                metadata={
                    "update_phase": "candidate",
                    "logical_provider_id": self.provider_id,
                },
            )
            self.phase = "traffic_shift"
            if traffic_shift is not None:
                traffic_shift(self.candidate)
            self.phase = "drain"
            if self.previous is not None:
                current_previous = self.catalog.get(self.previous.provider_id) or self.previous
                if (
                    current_previous.drain_deadline is not None
                    and time.monotonic() >= current_previous.drain_deadline
                ):
                    current_previous = self.catalog.enforce_drain_deadline(
                        current_previous.provider_id,
                    )
                    raise RuntimeError("previous provider drain deadline expired")
                drained = (
                    drain_timeout(current_previous)
                    if drain_timeout
                    else current_previous.inflight == 0
                )
                if not drained:
                    raise RuntimeError("previous provider did not drain")
                self.catalog.unload(self.previous.provider_id)
            self.phase = "commit"
            return ProviderUpdateResult(
                provider_id=self.provider_id,
                previous_generation=previous_generation,
                new_generation=self.candidate.generation,
                committed=True,
                rolled_back=False,
                phase=self.phase,
                candidate_provider_id=self.candidate.provider_id,
            )
        except Exception as exc:
            self.phase = "rollback"
            if self.candidate is not None:
                try:
                    self.catalog.unload(self.candidate.provider_id, force=True)
                except Exception:
                    pass
            if self.previous is not None:
                try:
                    current = self.catalog.get(self.previous.provider_id)
                    if current is not None and current.status.value == "draining":
                        self.catalog.resume(current.provider_id)
                        self.catalog.update_health(current.provider_id, self.previous.health)
                except Exception:
                    pass
            return ProviderUpdateResult(
                provider_id=self.provider_id,
                previous_generation=previous_generation,
                new_generation=self.candidate.generation if self.candidate else 0,
                committed=False,
                rolled_back=True,
                phase=self.phase,
                reason=str(exc),
                candidate_provider_id=self.candidate.provider_id if self.candidate else "",
            )
