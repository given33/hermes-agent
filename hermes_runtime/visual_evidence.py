"""Provider-neutral visual evidence seam for an MCP/sidecar adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .evidence import EvidenceArtifact, validate_evidence_refs


VISUAL_KINDS = frozenset({"ocr", "regions", "pixel_diff", "grounding"})


@dataclass(frozen=True)
class VisualEvidenceRequest:
    kind: str
    artifact_ref: str
    options: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.kind not in VISUAL_KINDS:
            raise ValueError(f"unsupported visual evidence kind: {self.kind}")
        if not self.artifact_ref:
            raise ValueError("artifact_ref is required")


class VisualEvidenceProvider(Protocol):
    def analyze(self, request: VisualEvidenceRequest) -> Mapping[str, Any]: ...


def invoke_visual_provider(provider: VisualEvidenceProvider, request: VisualEvidenceRequest) -> EvidenceArtifact:
    """Convert a sidecar response to a provenance-carrying artifact.

    The provider owns OCR/vision implementation. Hermes only validates the
    boundary and records a digest, so an external MCP service cannot inject
    arbitrary fields into the event protocol.
    """

    response = provider.analyze(request)
    if not isinstance(response, Mapping):
        raise ValueError("visual provider response must be an object")
    refs = validate_evidence_refs(response.get("evidence_refs"))
    return EvidenceArtifact.from_payload(
        request.kind,
        request.artifact_ref,
        response.get("result"),
        refs=(request.artifact_ref, *refs),
        metadata={
            "provider_id": str(response.get("provider_id") or "")[:256],
            "provider_generation": int(response.get("provider_generation") or 0),
            "model": str(response.get("model") or "")[:256],
        },
        sensitive=bool(response.get("sensitive", False)),
    )
