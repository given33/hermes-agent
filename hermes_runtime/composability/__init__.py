"""Small runtime primitives for safe dynamic Hermes composition.

The package deliberately stays independent from the agent loop and dashboard
plugin.  It provides the invariants those layers can adopt incrementally:
effect ownership, provider identity, turn-plan validation, and typed
supervisor/runtime metadata.
"""

from .effects import EffectScope, EffectScopeClosedError, EffectScopeError
from .providers import (
    DependencyGraph,
    DependencyTransition,
    DependencySpec,
    PolicyInterceptor,
    ProviderBinding,
    ProviderCatalog,
    ProviderRecord,
    ProviderStatus,
)
from .turn_plan import TurnPlan, TurnPlanError, TurnPlanNode
from .resources import (
    create_temporary_workspace,
    register_scope_resources,
    track_hosted_role_claim,
    track_lease,
    track_resource,
    track_stream,
    track_temporary_workspace,
)
from .provider_update import ProviderUpdateResult, ProviderUpdateTransaction
from .prompt_metrics import PromptMetrics
from .golden_metrics import (
    GoldenPathMetrics,
    GoldenPathQualityGateError,
    GoldenPathThresholds,
)
from hermes_runtime.prompt_runtime import (
    PromptAssembly,
    PromptAssemblyContext,
    PromptDraft,
    PromptFragment,
    PromptMiddleware,
    PromptRegistrationError,
    PromptRuntime,
    PromptRuntimeError,
    PromptTemplateError,
    default_prompt_runtime,
)
from .hosted_plan import build_hosted_turn_plan, hosted_turn_plan_snapshot, next_ready_plan_nodes
from .update_strategy import (
    TransactionalUpdate,
    UpdateClassification,
    UpdateMode,
    UpdateResult,
    classify_update,
)
from .side_effects import (
    AllowlistViolation,
    ApprovalRecord,
    ApprovalRequired,
    SideEffectClass,
    SideEffectError,
    SideEffectReceipt,
    SideEffectRule,
    SideEffectSandbox,
    make_validation_sandbox,
)
from .validation_deployment import (
    DeploymentControlError,
    DeploymentReceipt,
    ValidationDeploymentController,
)
from .long_task import (
    BoundedEventBuffer,
    LongTaskBudget,
    LongTaskController,
    recover_after_process_exit,
)
from .lifecycle import (
    LIFECYCLE_STATES,
    LIFECYCLE_TRANSITIONS,
    assert_lifecycle_transition,
    lifecycle_transition_allowed,
    normalize_lifecycle_state,
)

__all__ = [
    "DependencySpec",
    "DependencyGraph",
    "DependencyTransition",
    "EffectScope",
    "EffectScopeClosedError",
    "EffectScopeError",
    "ProviderBinding",
    "ProviderCatalog",
    "ProviderRecord",
    "ProviderStatus",
    "PolicyInterceptor",
    "TurnPlan",
    "TurnPlanError",
    "TurnPlanNode",
    "create_temporary_workspace",
    "register_scope_resources",
    "track_hosted_role_claim",
    "track_lease",
    "track_resource",
    "track_stream",
    "track_temporary_workspace",
    "ProviderUpdateResult",
    "ProviderUpdateTransaction",
    "PromptMetrics",
    "GoldenPathMetrics",
    "GoldenPathQualityGateError",
    "GoldenPathThresholds",
    "PromptAssembly",
    "PromptAssemblyContext",
    "PromptDraft",
    "PromptFragment",
    "PromptMiddleware",
    "PromptRegistrationError",
    "PromptRuntime",
    "PromptRuntimeError",
    "PromptTemplateError",
    "default_prompt_runtime",
    "build_hosted_turn_plan",
    "hosted_turn_plan_snapshot",
    "next_ready_plan_nodes",
    "TransactionalUpdate",
    "UpdateClassification",
    "UpdateMode",
    "UpdateResult",
    "classify_update",
    "AllowlistViolation",
    "ApprovalRecord",
    "ApprovalRequired",
    "SideEffectClass",
    "SideEffectError",
    "SideEffectReceipt",
    "SideEffectRule",
    "SideEffectSandbox",
    "make_validation_sandbox",
    "DeploymentControlError",
    "DeploymentReceipt",
    "ValidationDeploymentController",
    "LongTaskBudget",
    "LongTaskController",
    "BoundedEventBuffer",
    "recover_after_process_exit",
    "LIFECYCLE_STATES",
    "LIFECYCLE_TRANSITIONS",
    "assert_lifecycle_transition",
    "lifecycle_transition_allowed",
    "normalize_lifecycle_state",
]
