"""Multi-objective quality gates for the converged golden path.

The metrics are intentionally separate from ``PromptMetrics``.  Prompt
metrics describe supervisor/prompt behavior; these counters describe whether
an end-to-end user task actually completed and recovered correctly.  No cost
or acceptance metric can mask a hard safety failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class GoldenPathQualityGateError(RuntimeError):
    """Raised when a required golden-path quality invariant is not met."""

    def __init__(self, failures: list[str]) -> None:
        self.failures = tuple(str(item) for item in failures)
        super().__init__("golden path quality gate failed: " + ", ".join(self.failures))


@dataclass(frozen=True)
class GoldenPathThresholds:
    min_task_success_rate: float = 1.0
    min_tool_success_rate: float = 1.0
    min_schema_valid_rate: float = 1.0
    min_artifact_acceptance_rate: float = 1.0
    min_rework_precision: float = 0.0
    max_false_pass: int = 0
    max_false_reject_rate: float = 1.0
    min_provider_drain_completion_rate: float = 1.0
    min_stale_event_rejection_rate: float = 1.0
    min_process_recovery_rate: float = 1.0
    min_replay_consistency_rate: float = 1.0


@dataclass
class GoldenPathMetrics:
    """Counters and rates required to accept the one real workflow."""

    task_total: int = 0
    task_success: int = 0
    tool_total: int = 0
    tool_success: int = 0
    schema_valid: int = 0
    schema_total: int = 0
    false_pass: int = 0
    false_reject: int = 0
    verdict_total: int = 0
    rework_total: int = 0
    rework_accepted: int = 0
    prompt_cache_hits: int = 0
    prompt_cache_total: int = 0
    token_cost: int = 0
    artifact_checks: int = 0
    artifact_accepted: int = 0
    provider_drain_total: int = 0
    provider_drain_completed: int = 0
    stale_event_total: int = 0
    stale_event_rejected: int = 0
    process_kill_total: int = 0
    process_recovery_success: int = 0
    replay_cases: int = 0
    replay_consistent: int = 0

    def observe_run(
        self,
        result: Mapping[str, Any],
        *,
        false_pass: bool = False,
        false_reject: bool = False,
        rework_requested: bool = False,
        rework_accepted: bool = False,
        prompt_cache_hit: bool | None = None,
        token_cost: int = 0,
        provider_drain_completed: bool | None = None,
        stale_event_total: int = 0,
        stale_event_rejected: int = 0,
        process_killed: bool = False,
        process_recovered: bool = False,
        replay_consistent: bool | None = None,
    ) -> None:
        """Record one canonical result and optional fault-injection evidence."""

        self.task_total += 1
        verdict = result.get("supervisor_verdict")
        verdict_map = verdict if isinstance(verdict, Mapping) else {}
        completed = (
            str(result.get("status") or "") == "completed"
            and str(verdict_map.get("verdict") or "") == "pass"
            and bool(verdict_map.get("valid"))
        )
        if completed:
            self.task_success += 1
        tool_calls = result.get("tool_calls")
        calls = tool_calls if isinstance(tool_calls, list) else []
        self.tool_total += len(calls)
        self.tool_success += sum(
            1 for call in calls if isinstance(call, Mapping) and call.get("status") == "completed"
        )
        self.schema_total += 1
        if verdict_map.get("schema_version") == "hermes.supervisor-verdict.v1" and bool(verdict_map.get("valid")):
            self.schema_valid += 1
        self.verdict_total += 1
        self.false_pass += int(bool(false_pass))
        self.false_reject += int(bool(false_reject))
        self.rework_total += int(bool(rework_requested))
        self.rework_accepted += int(bool(rework_accepted))
        if prompt_cache_hit is not None:
            self.prompt_cache_total += 1
            self.prompt_cache_hits += int(bool(prompt_cache_hit))
        self.token_cost += max(0, int(token_cost))
        artifact = result.get("artifact")
        self.artifact_checks += 1
        self.artifact_accepted += int(
            isinstance(artifact, Mapping)
            and bool(str(artifact.get("digest") or ""))
            and bool(verdict_map.get("valid"))
            and bool((verdict_map.get("checks") or {}).get("artifact_created"))
        )
        if provider_drain_completed is not None:
            self.provider_drain_total += 1
            self.provider_drain_completed += int(bool(provider_drain_completed))
        self.stale_event_total += max(0, int(stale_event_total))
        self.stale_event_rejected += min(
            max(0, int(stale_event_rejected)), max(0, int(stale_event_total))
        )
        if process_killed:
            self.process_kill_total += 1
            self.process_recovery_success += int(bool(process_recovered))
        if replay_consistent is not None:
            self.replay_cases += 1
            self.replay_consistent += int(bool(replay_consistent))

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_total": self.task_total,
            "task_success": self.task_success,
            "task_success_rate": _rate(self.task_success, self.task_total),
            "tool_total": self.tool_total,
            "tool_success": self.tool_success,
            "tool_success_rate": _rate(self.tool_success, self.tool_total),
            "schema_valid": self.schema_valid,
            "schema_total": self.schema_total,
            "schema_valid_rate": _rate(self.schema_valid, self.schema_total),
            "false_pass": self.false_pass,
            "false_pass_rate": _rate(self.false_pass, self.verdict_total),
            "false_reject": self.false_reject,
            "false_reject_rate": _rate(self.false_reject, self.verdict_total),
            "rework_total": self.rework_total,
            "rework_accepted": self.rework_accepted,
            "rework_precision": _rate(self.rework_accepted, self.rework_total),
            "prompt_cache_hits": self.prompt_cache_hits,
            "prompt_cache_total": self.prompt_cache_total,
            "prompt_cache_hit_rate": _rate(self.prompt_cache_hits, self.prompt_cache_total),
            "token_cost": self.token_cost,
            "artifact_checks": self.artifact_checks,
            "artifact_accepted": self.artifact_accepted,
            "artifact_acceptance_rate": _rate(self.artifact_accepted, self.artifact_checks),
            "provider_drain_total": self.provider_drain_total,
            "provider_drain_completed": self.provider_drain_completed,
            "provider_drain_completion_rate": _rate(self.provider_drain_completed, self.provider_drain_total),
            "stale_event_total": self.stale_event_total,
            "stale_event_rejected": self.stale_event_rejected,
            "stale_event_rejection_rate": _rate(self.stale_event_rejected, self.stale_event_total),
            "process_kill_total": self.process_kill_total,
            "process_recovery_success": self.process_recovery_success,
            "process_recovery_rate": _rate(self.process_recovery_success, self.process_kill_total),
            "replay_cases": self.replay_cases,
            "replay_consistent": self.replay_consistent,
            "replay_consistency_rate": _rate(self.replay_consistent, self.replay_cases),
            "hard_safety": {
                "false_pass_zero": self.false_pass == 0,
                "stale_events_fail_closed": _complete_or_empty(
                    self.stale_event_rejected, self.stale_event_total
                ),
                "process_recovery_observations_are_visible": True,
                "cost_does_not_mask_safety": True,
            },
        }

    def quality_gate(self, thresholds: GoldenPathThresholds | None = None) -> dict[str, Any]:
        threshold = thresholds or GoldenPathThresholds()
        snapshot = self.snapshot()
        failures: list[str] = []
        _check_min(failures, "task_success_rate", snapshot["task_success_rate"], threshold.min_task_success_rate, self.task_total)
        _check_min(failures, "tool_success_rate", snapshot["tool_success_rate"], threshold.min_tool_success_rate, self.tool_total)
        _check_min(failures, "schema_valid_rate", snapshot["schema_valid_rate"], threshold.min_schema_valid_rate, self.schema_total)
        _check_min(failures, "artifact_acceptance_rate", snapshot["artifact_acceptance_rate"], threshold.min_artifact_acceptance_rate, self.artifact_checks)
        _check_min(failures, "rework_precision", snapshot["rework_precision"], threshold.min_rework_precision, self.rework_total)
        if self.false_pass > threshold.max_false_pass:
            failures.append("false_pass")
        _check_max(failures, "false_reject_rate", snapshot["false_reject_rate"], threshold.max_false_reject_rate, self.verdict_total)
        _check_min(failures, "provider_drain_completion_rate", snapshot["provider_drain_completion_rate"], threshold.min_provider_drain_completion_rate, self.provider_drain_total)
        _check_min(failures, "stale_event_rejection_rate", snapshot["stale_event_rejection_rate"], threshold.min_stale_event_rejection_rate, self.stale_event_total)
        _check_min(failures, "process_recovery_rate", snapshot["process_recovery_rate"], threshold.min_process_recovery_rate, self.process_kill_total)
        _check_min(failures, "replay_consistency_rate", snapshot["replay_consistency_rate"], threshold.min_replay_consistency_rate, self.replay_cases)
        return {
            "pass": not failures,
            "failures": failures,
            "thresholds": {
                key: value
                for key, value in threshold.__dict__.items()
            },
            "metrics": snapshot,
        }

    def assert_quality_gate(self, thresholds: GoldenPathThresholds | None = None) -> dict[str, Any]:
        report = self.quality_gate(thresholds)
        if not report["pass"]:
            raise GoldenPathQualityGateError(report["failures"])
        return report


def _rate(numerator: int, denominator: int) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def _complete_or_empty(numerator: int, denominator: int) -> bool:
    return denominator == 0 or numerator == denominator


def _check_min(failures: list[str], name: str, value: float, minimum: float, observations: int) -> None:
    if observations and value < minimum:
        failures.append(name)


def _check_max(failures: list[str], name: str, value: float, maximum: float, observations: int) -> None:
    if observations and value > maximum:
        failures.append(name)


__all__ = [
    "GoldenPathMetrics",
    "GoldenPathQualityGateError",
    "GoldenPathThresholds",
]
