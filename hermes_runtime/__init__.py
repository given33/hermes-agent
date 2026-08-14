"""Process-agnostic Hermes runtime services.

This package is the low-level authority shared by the agent, tools, gateway,
plugins, TUI, and CLI entry points.  Importing it must not start a service,
parse command-line arguments, or mutate process-wide streams.
"""

from .prompt_runtime import (
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

__all__ = [
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
    "CAPABILITY_TAGS",
    "ROLE_NAMES",
    "normalize_capability_tags",
    "normalize_role_names",
    "role_allows",
    "filter_tools_for_role",
    "CollaborationDependency",
    "DependencyGraph",
    "MailboxMessage",
    "append_mailbox",
    "read_mailbox",
    "EvidenceArtifact",
    "validate_evidence_refs",
    "TOOL_EXECUTION_SCHEMA_VERSION",
    "ToolExecutionEnvelope",
    "ToolExecutionLedger",
    "ToolPresentationMeta",
    "build_envelope",
    "replay_projection",
    "stable_digest",
    "DEFAULT_VIEWERS",
    "Viewer",
    "ViewerRegistry",
    "SessionTrace",
    "TraceEvent",
    "TRAJECTORY_SCHEMA_VERSION",
    "project_hosted_trajectory",
    "VisualEvidenceRequest",
    "invoke_visual_provider",
    "PluginCompatibility",
    "validate_plugin_compatibility",
]
from .capabilities import (
    CAPABILITY_TAGS,
    ROLE_NAMES,
    filter_tools_for_role,
    normalize_capability_tags,
    normalize_role_names,
    role_allows,
)
from .collaboration import (
    CollaborationDependency,
    DependencyGraph,
    MailboxMessage,
    append_mailbox,
    read_mailbox,
)
from .evidence import EvidenceArtifact, validate_evidence_refs
from .tool_execution import (
    SCHEMA_VERSION as TOOL_EXECUTION_SCHEMA_VERSION,
    ToolExecutionEnvelope,
    ToolExecutionLedger,
    ToolPresentationMeta,
    build_envelope,
    replay_projection,
    stable_digest,
)
from .viewer_registry import DEFAULT_VIEWERS, Viewer, ViewerRegistry
from .session_trace import SessionTrace, TraceEvent
from .trajectory import (
    SCHEMA_VERSION as TRAJECTORY_SCHEMA_VERSION,
    project_hosted_trajectory,
)
from .visual_evidence import VisualEvidenceRequest, invoke_visual_provider
from .plugin_compatibility import PluginCompatibility, validate_plugin_compatibility
