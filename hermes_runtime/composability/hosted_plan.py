"""Adapter from the existing collaboration workflow to a validated TurnPlan."""

from __future__ import annotations

from typing import Any, Iterable

from .turn_plan import TurnPlan, TurnPlanNode


def build_hosted_turn_plan(
    *,
    turn_id: str,
    worker_profiles: Iterable[str],
    reviewer_profile: str,
    reporter_profile: str,
    artifact_required: bool,
    revision: int = 1,
) -> TurnPlan:
    workers_list: list[str] = []
    for profile in worker_profiles:
        normalized = str(profile).strip()
        if normalized and normalized not in workers_list:
            workers_list.append(normalized)
    workers = tuple(workers_list)
    worker_ids = tuple(f"worker:{profile}" for profile in workers)
    nodes: list[TurnPlanNode] = [
        TurnPlanNode(
            node_id="manager_planning",
            role="manager",
            output_contract="manager-plan.v1",
            acceptance_contract="valid normalized manager plan",
            conflict_keys=(f"turn:{turn_id}:plan",),
            risk_class="orchestration",
            parallel_group="planning",
            token_budget=12_000,
            time_budget_seconds=180,
        ),
        TurnPlanNode(
            node_id="dispatch",
            role="dispatcher",
            depends_on=("manager_planning",),
            input_artifact_refs=("manager-plan",),
            output_contract="worker-assignment.v1",
            acceptance_contract="every worker has one immutable assignment",
            conflict_keys=(f"turn:{turn_id}:assignment",),
            risk_class="orchestration",
            parallel_group="dispatch",
            token_budget=4_000,
            time_budget_seconds=60,
        ),
    ]
    for profile, node_id in zip(workers, worker_ids):
        nodes.append(
            TurnPlanNode(
                node_id=node_id,
                role="worker",
                depends_on=("dispatch",),
                input_artifact_refs=("manager-plan",),
                output_contract="worker-result.v1",
                acceptance_contract="worker status completed and evidence attached",
                conflict_keys=(f"turn:{turn_id}:worker:{profile}",),
                risk_class="remote_write" if artifact_required else "read_write",
                parallel_group="workers",
                token_budget=32_000,
                time_budget_seconds=1_200,
            )
        )
    nodes.extend(
        [
            TurnPlanNode(
                node_id="worker_handoff",
                role="handoff",
                depends_on=worker_ids or ("dispatch",),
                input_artifact_refs=tuple(f"worker:{profile}:result" for profile in workers),
                output_contract="worker-handoff.v1",
                acceptance_contract="all required worker evidence is present",
                conflict_keys=(f"turn:{turn_id}:handoff",),
                risk_class="read_only",
                parallel_group="handoff",
                token_budget=8_000,
                time_budget_seconds=120,
            ),
            TurnPlanNode(
                node_id="review",
                role=reviewer_profile,
                depends_on=("worker_handoff",),
                input_artifact_refs=("worker-handoff",),
                output_contract="supervisor-verdict.v1",
                acceptance_contract="strict verdict witness is valid",
                conflict_keys=(f"turn:{turn_id}:review",),
                risk_class="read_only",
                parallel_group="review",
                token_budget=16_000,
                time_budget_seconds=300,
            ),
            TurnPlanNode(
                node_id="report",
                role=reporter_profile,
                depends_on=("review",),
                input_artifact_refs=("worker-handoff", "supervisor-verdict"),
                output_contract="final-report.v1",
                acceptance_contract="report matches accepted evidence and artifact contract",
                conflict_keys=(f"turn:{turn_id}:report",),
                risk_class="external_emission" if artifact_required else "read_only",
                parallel_group="report",
                token_budget=12_000,
                time_budget_seconds=180,
            ),
        ]
    )
    return TurnPlan(plan_id=f"hosted:{turn_id}", revision=max(1, int(revision)), nodes=tuple(nodes))


def hosted_turn_plan_snapshot(
    plan: TurnPlan,
    *,
    completed: Iterable[str] = (),
    running: Iterable[str] = (),
) -> dict[str, Any]:
    ready = [
        node.node_id
        for node in next_ready_plan_nodes(
            plan,
            completed=completed,
            running=running,
        )
    ]
    return {
        **plan.public_dict(),
        "critical_path": list(plan.critical_path()),
        "initial_ready_nodes": ready,
        "parallel_ready": plan.can_run_together(
            next_ready_plan_nodes(plan, completed=completed, running=running)
        ),
    }


def next_ready_plan_nodes(
    plan: TurnPlan,
    *,
    completed: Iterable[str] = (),
    running: Iterable[str] = (),
) -> tuple[TurnPlanNode, ...]:
    """Return a conflict-safe batch for the next scheduler tick."""

    candidates = plan.ready_nodes(completed, running)
    selected: list[TurnPlanNode] = []
    for node in candidates:
        if plan.can_run_together((*selected, node)):
            selected.append(node)
    return tuple(selected)
