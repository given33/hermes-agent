# Hermes Prompt Runtime

This document records the Hermes adaptation of the prompt-runtime ideas studied
from DeepSeek Harness commit `47f943859bef60e4160492346772ded9b24f765a`.

## Design Boundary

Hermes keeps the existing prompt-cache contract: the stable system prefix and
the visible tool set are fixed for a session. A plugin must not mutate either
one in the middle of a conversation. Per-request or per-turn information still
belongs in the existing ephemeral/context paths.

The new runtime is deliberately a narrow composition layer for model-facing
capabilities that cross a plugin, session, network, or lifecycle boundary. A
normal pure helper should remain a normal pure helper.

The assembled model contract is treated as four related parts:

1. Stable instructions: identity, persona, operating posture, and capability
   guidance.
2. Runtime context: a bounded snapshot for the assembled request. It is kept
   separate from stable instructions and has its own fingerprint.
3. Tool schemas: the exact schemas passed to the model. They remain owned by
   the normal Hermes tool registry and executor; the Prompt Runtime validates
   and fingerprints the snapshot so presentation and dispatch cannot silently
   drift.
4. Middleware: trusted, deterministic transforms applied in explicit order.

`PromptAssembly.as_metadata()` exposes only non-sensitive provenance: schema
version, mode, fingerprints, registry revision, and tool count. It does not
include prompt text, credentials, filesystem paths, or provider secrets.

## Scopes And Variables

Fragments, variables, and middleware can be registered in `global`, an agent
scope, or an override scope. Resolution is deterministic:

```text
global -> agent -> override
```

An item with the same name in a nearer scope shadows the wider item. Different
names are retained and sorted by `(order, name)`.

Prompt variables use strict `{{name}}` syntax. Unknown names, missing values,
malformed braces, invalid variable names, duplicate registrations, and invalid
tool schemas raise a `PromptRuntimeError` before a model request is assembled.
This is intentional: dropping a malformed capability instruction would make
the model-facing contract differ from the plugin's declared contract.

Use a capability-owned fragment when the instruction is stable and explains
how a visible capability should be used. Put changing values in
`runtime_context`, a variable provider, or Hermes' existing ephemeral prompt
mechanism. Never put secrets or raw user content in a global stable fragment.

## Plugin API

Trusted plugins can use the host-owned registration surface:

```python
ctx.register_prompt_fragment(
    name="my_plugin:workflow_guidance",
    section="capability_guidance",
    text="Use the workflow tool only after checking its current state.",
    capability="my_workflow_tool",
)
ctx.register_prompt_variable(
    "workspace_kind",
    lambda assembly_context: "repository",
    scope="my-agent",
)
ctx.register_prompt_middleware(
    "my_plugin:normalizer",
    normalize_prompt_draft,
    order=100,
    scope="my-agent",
)
```

Each registration is owned by the plugin's `EffectScope`. Reload and failed
registration paths dispose the exact registration, including a replaced tool
only when the handler witness still matches. A later plugin cannot be removed
by an earlier plugin's cleanup.

Prompt middleware is trusted code. It can change tool schemas and instructions,
so it must be reviewed like a plugin executor or hook. Middleware should be
deterministic, bounded, and side-effect free. Network calls, model calls, and
external effects do not belong in prompt assembly.

## PTC Mode

Hermes already has `execute_code`, a sandboxed Python RPC tool that exposes a
small allowlisted tool surface. `AIAgent(prompt_runtime_mode="ptc")` adds
capability guidance only when `execute_code` is visible. The guidance tells the
model to use code for loops, filtering, branching, and multi-step
orchestration, while retaining direct calls for one-off actions and approval
boundaries.

This is intentionally not a second code executor and does not hide all native
tools. DeepSeek Harness code presentation can make only `run_code` visible to
the model, but Hermes has additional approval, provider, and prompt-cache
contracts that must be preserved until a separately evaluated presentation
adapter exists.

## Creation Mode

`AIAgent(prompt_runtime_mode="creation")` adds guidance only when the existing
`skill_manage` capability is visible. The model is told to inspect first, keep
changes bounded and reversible, validate the result, and avoid claiming that a
skill is active before activation is confirmed.

Creation mode therefore reuses Hermes' existing skill and plugin trust gates.
It does not introduce arbitrary in-process source loading, durable self-
modification of the core loop, or an unapproved dynamic package loader.

## Operational Rules

- Register capability guidance with the tool that owns the capability.
- Keep tool descriptions, parameter schemas, guidance, approval metadata, and
  the actual handler aligned in one registration change.
- Pin provider identity and generation in the turn/runtime layer; prompt
  assembly must not re-resolve a provider by interface name.
- Treat a prompt-runtime revision change as a new session/configuration snapshot
  or rebuild boundary, never as an invisible mid-turn mutation.
- Apply bounded sizes, redaction, retention, and access control to diagnostics.
- Measure schema-valid rate, false PASS, false reject, rework precision, prompt
  cache hit, token cost, and artifact acceptance together. No single metric is
  a sufficient optimization target.

The implementation lives in `hermes_runtime/prompt_runtime.py`; exports are
available from `hermes_runtime` and `hermes_runtime.composability`.
