"""Bounded, redaction-first evidence artifacts for visual and tool adapters."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping


SCHEMA_VERSION = "evidence/v1"


@dataclass(frozen=True)
class EvidenceArtifact:
    kind: str
    source: str
    digest: str
    refs: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    sensitive: bool = False

    @classmethod
    def from_payload(cls, kind: str, source: str, payload: Any, *, refs: tuple[str, ...] = (), metadata: Mapping[str, Any] | None = None, sensitive: bool = False) -> "EvidenceArtifact":
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        return cls(kind=kind, source=source, digest=hashlib.sha256(encoded).hexdigest(), refs=refs, metadata=dict(metadata or {}), sensitive=sensitive)

    def as_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "kind": self.kind, "source": self.source, "digest": self.digest, "refs": list(self.refs), "metadata": dict(self.metadata), "sensitive": bool(self.sensitive)}


def validate_evidence_refs(refs: Any) -> tuple[str, ...]:
    if refs is None:
        return ()
    if not isinstance(refs, (list, tuple)):
        raise ValueError("evidence_refs must be a list")
    normalized = tuple(str(ref).strip()[:512] for ref in refs if str(ref).strip())
    if len(normalized) > 64:
        raise ValueError("evidence_refs exceeds the 64-reference limit")
    return tuple(dict.fromkeys(normalized))
