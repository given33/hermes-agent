"""Structured prompt/runtime quality metrics for hosted supervision."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PromptMetrics:
    schema_valid: int = 0
    schema_total: int = 0
    false_pass: int = 0
    false_reject: int = 0
    strict_reject: int = 0
    verdict_total: int = 0
    rework_total: int = 0
    rework_accepted: int = 0
    prompt_cache_hits: int = 0
    prompt_cache_total: int = 0
    token_cost: int = 0
    artifact_checks: int = 0
    artifact_acceptance: int = 0
    _observations: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def observe(
        self,
        *,
        schema_valid: bool,
        verdict: str,
        false_pass: bool = False,
        false_reject: bool = False,
        strict_reject: bool = False,
        rework_requested: bool = False,
        rework_accepted: bool = False,
        prompt_cache_hit: bool | None = None,
        token_cost: int = 0,
        artifact_checked: bool = False,
        artifact_accepted: bool = False,
        context: str = "",
    ) -> None:
        self.schema_total += 1
        self.verdict_total += 1
        if schema_valid:
            self.schema_valid += 1
        if false_pass:
            self.false_pass += 1
        if false_reject:
            self.false_reject += 1
        if strict_reject:
            self.strict_reject += 1
        if rework_requested:
            self.rework_total += 1
        if rework_accepted:
            self.rework_accepted += 1
        if prompt_cache_hit is not None:
            self.prompt_cache_total += 1
            if prompt_cache_hit:
                self.prompt_cache_hits += 1
        self.token_cost += max(0, int(token_cost))
        if artifact_checked:
            self.artifact_checks += 1
            if artifact_accepted:
                self.artifact_acceptance += 1
        self._observations.append(
            {
                "context": str(context or "")[:128],
                "schema_valid": bool(schema_valid),
                "verdict": str(verdict or "unknown"),
                "false_pass": bool(false_pass),
                "false_reject": bool(false_reject),
                "strict_reject": bool(strict_reject),
            }
        )
        del self._observations[:-256]

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_valid_rate": self.schema_valid / self.schema_total if self.schema_total else 0.0,
            "false_pass": self.false_pass,
            "false_pass_rate": self.false_pass / self.verdict_total if self.verdict_total else 0.0,
            "false_reject": self.false_reject,
            "false_reject_rate": self.false_reject / self.verdict_total if self.verdict_total else 0.0,
            "strict_reject": self.strict_reject,
            "rework_precision": self.rework_accepted / self.rework_total if self.rework_total else 0.0,
            "prompt_cache_hit_rate": self.prompt_cache_hits / self.prompt_cache_total if self.prompt_cache_total else 0.0,
            "token_cost": self.token_cost,
            "artifact_acceptance_rate": self.artifact_acceptance / self.artifact_checks if self.artifact_checks else 0.0,
            "schema_valid": self.schema_valid,
            "schema_total": self.schema_total,
            "rework_total": self.rework_total,
            "artifact_checks": self.artifact_checks,
            "quality_guardrails": {
                "false_pass_rate_max": 0.0,
                "false_pass_safe": self.false_pass == 0,
                "multi_objective": True,
                "token_cost_observed": self.token_cost,
                "artifact_acceptance_is_not_sufficient": True,
            },
        }
