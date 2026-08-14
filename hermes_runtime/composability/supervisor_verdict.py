"""Versioned, evidence-bound supervisor verdicts.

The model-facing ``hermes.supervision.v1`` object remains a compatibility
input.  This module creates the runtime-facing record that binds the decision
to the evidence and execution revision that produced it.  A verdict without
that witness is not safe to use as a cached gate result.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping


SCHEMA_VERSION = "hermes.supervisor-verdict.v1"
VALID_VERDICTS = frozenset({"pass", "corrective_action", "unknown"})


@dataclass(frozen=True)
class SupervisorVerdict:
    schema_version: str
    verdict: str
    evidence_refs: tuple[str, ...]
    artifact_digest: str
    source_revision: str
    prompt_version: str
    evidence_digest: str
    checks: dict[str, bool]
    blockers: tuple[str, ...]
    findings: tuple[str, ...]
    required_actions: tuple[str, ...]
    model: str = "unknown"
    valid: bool = True

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict,
            "evidence_refs": list(self.evidence_refs),
            "artifact_digest": self.artifact_digest,
            "source_revision": self.source_revision,
            "prompt_version": self.prompt_version,
            "evidence_digest": self.evidence_digest,
            "checks": dict(self.checks),
            "blockers": list(self.blockers),
            "findings": list(self.findings),
            "required_actions": list(self.required_actions),
            "model": self.model,
            "valid": self.valid,
        }


def digest_json(value: Any) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _string(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text[:512] or fallback


def _refs(evidence: Mapping[str, Any], evidence_digest: str) -> tuple[str, ...]:
    raw = evidence.get("evidence_refs") or evidence.get("artifact_refs")
    if isinstance(raw, (list, tuple, set)):
        refs = tuple(
            str(item).strip()[:512]
            for item in raw
            if str(item).strip()
        )
        if refs:
            return refs[:100]
    artifacts = evidence.get("artifacts")
    if isinstance(artifacts, list):
        refs = []
        for item in artifacts[:100]:
            if isinstance(item, Mapping):
                ref = item.get("id") or item.get("artifact_id") or item.get("path")
            else:
                ref = item
            if str(ref or "").strip():
                refs.append(f"artifact:{str(ref).strip()[:480]}")
        if refs:
            return tuple(refs)
    return (f"evidence:{evidence_digest}",)


def build_supervisor_verdict(
    control: Mapping[str, Any] | None,
    *,
    evidence: Mapping[str, Any],
    evidence_digest: str = "",
    artifact_digest: str = "",
    source_revision: str = "",
    prompt_version: str = "",
    model: str = "",
) -> SupervisorVerdict:
    """Create the runtime verdict and fail closed on missing control data."""

    evidence_hash = evidence_digest or digest_json(evidence)
    checks_value = control.get("checks") if isinstance(control, Mapping) else {}
    checks = (
        {str(key): bool(value) for key, value in checks_value.items()}
        if isinstance(checks_value, Mapping)
        else {}
    )
    verdict = str(control.get("verdict") or "unknown").strip().lower() if control else "unknown"
    if verdict not in VALID_VERDICTS:
        verdict = "unknown"
    blockers = tuple(str(item)[:2000] for item in (control or {}).get("blockers", []) if str(item).strip())
    findings = tuple(str(item)[:2000] for item in (control or {}).get("findings", []) if str(item).strip())
    required_actions = tuple(str(item)[:2000] for item in (control or {}).get("required_actions", []) if str(item).strip())
    resolved_artifact_digest = _string(
        artifact_digest
        or evidence.get("artifact_digest")
        or (digest_json(evidence.get("artifacts")) if evidence.get("artifacts") else "none"),
        "none",
    )
    resolved_source_revision = _string(
        source_revision
        or evidence.get("source_revision")
        or evidence.get("git_revision")
        or evidence.get("revision"),
        "",
    )
    resolved_prompt_version = _string(
        prompt_version or evidence.get("prompt_version"),
        "",
    )
    resolved_model = _string(model or evidence.get("model"), "unknown")
    refs = _refs(evidence, evidence_hash)
    valid = bool(
        control
        and verdict in {"pass", "corrective_action"}
        and checks
        and (verdict != "pass" or all(checks.values()))
        and refs
        and resolved_artifact_digest
        and resolved_source_revision
        and resolved_prompt_version
        and not (
            verdict == "pass"
            and bool(evidence.get("artifact_acceptance_required"))
            and resolved_artifact_digest == "none"
        )
    )
    return SupervisorVerdict(
        schema_version=SCHEMA_VERSION,
        verdict=verdict if valid else "unknown",
        evidence_refs=refs,
        artifact_digest=resolved_artifact_digest,
        source_revision=resolved_source_revision,
        prompt_version=resolved_prompt_version,
        evidence_digest=evidence_hash,
        checks=checks,
        blockers=blockers,
        findings=findings,
        required_actions=required_actions,
        model=resolved_model,
        valid=valid,
    )
