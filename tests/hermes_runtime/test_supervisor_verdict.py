from __future__ import annotations

from hermes_runtime.composability.supervisor_verdict import (
    SCHEMA_VERSION,
    build_supervisor_verdict,
)


def _control(verdict: str = "PASS") -> dict:
    return {
        "verdict": verdict,
        "checks": {"evidence": verdict == "PASS"},
        "blockers": [] if verdict == "PASS" else ["blocked"],
        "findings": [],
        "required_actions": [] if verdict == "PASS" else ["fix"],
    }


def test_runtime_verdict_contains_evidence_witness_fields() -> None:
    verdict = build_supervisor_verdict(
        _control(),
        evidence={
            "artifact_refs": ["artifact:test-report"],
            "artifact_digest": "a" * 64,
            "source_revision": "git:abc",
            "prompt_version": "prompt:7",
            "model": "test-model",
        },
    )
    assert verdict.valid is True
    assert verdict.verdict == "pass"
    payload = verdict.public_dict()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["evidence_refs"] == ["artifact:test-report"]
    assert payload["artifact_digest"] == "a" * 64
    assert payload["source_revision"] == "git:abc"
    assert payload["prompt_version"] == "prompt:7"


def test_invalid_control_cannot_become_pass() -> None:
    verdict = build_supervisor_verdict(
        None,
        evidence={"source_revision": "git:abc"},
    )
    assert verdict.valid is False
    assert verdict.verdict == "unknown"


def test_valid_control_without_source_revision_fails_closed() -> None:
    verdict = build_supervisor_verdict(
        _control(),
        evidence={"artifact_refs": ["artifact:test"]},
        prompt_version="prompt:1",
    )
    assert verdict.valid is False
    assert verdict.verdict == "unknown"


def test_pass_with_a_failed_acceptance_check_fails_closed() -> None:
    verdict = build_supervisor_verdict(
        {
            "verdict": "pass",
            "checks": {"tests": False, "evidence": True},
        },
        evidence={
            "source_revision": "git:abc",
            "prompt_version": "prompt:1",
        },
    )
    assert verdict.valid is False
    assert verdict.verdict == "unknown"


def test_required_artifact_pass_requires_digest() -> None:
    verdict = build_supervisor_verdict(
        {"verdict": "pass", "checks": {"artifact": True}},
        evidence={
            "artifact_acceptance_required": True,
            "source_revision": "git:abc",
            "prompt_version": "prompt:1",
        },
    )
    assert verdict.valid is False
    assert verdict.verdict == "unknown"
