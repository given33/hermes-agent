# Golden Path Convergence

This document freezes new runtime feature work until one user-visible workflow
is proven end to end. The existing native Agent Loop remains the default
execution path. The golden path is an opt-in validation/runtime layer and is
not imported by native mode.

## Accepted workflow

```text
user task
  -> resolve model:golden once
  -> retain provider_id@generation for the whole turn
  -> local.read_task
  -> mcp.visual.inspect_image
  -> atomic JSON artifact with SHA-256 evidence
  -> strict supervisor verdict
  -> canonical result
  -> hosted events for Desktop/iOS reducers
```

The path admits exactly two tools, one local and one read-only MCP-shaped tool,
one JSON artifact type, one strict verdict contract, and one transient retry
path. A provider is never looked up again by interface name during a turn.

Run the deterministic acceptance probe from the repository root:

```text
C:\Users\given\hermes-audit\hermes-agent\.venv\Scripts\python.exe -m hermes_cli.golden_path_probe --visual-root C:\Users\given\hermes-audit\validation-targets\visual --artifact-root C:\Users\given\hermes-audit\validation-targets\golden-artifacts --output C:\Users\given\hermes-audit\validation-targets\golden-result.json
```

This probe uses the actual read-only visual implementation and the same
artifact, event, and supervisor contracts. It uses a deterministic validation
provider, so it is evidence for runtime correctness rather than evidence that
an external model or production service is authorized.

## Core/runtime boundary

| Area | Default | Rule |
|---|---|---|
| Native Agent Loop | on | No golden-path import or behavior change |
| Prompt/runtime layer | off | Enabled only by explicit feature flag |
| Provider generation | pinned | Bind once before the turn and fail closed on staleness |
| EffectScope | boundary resources only | No rollback claim for irreversible external effects |
| Pure functions | native code | Do not wrap them as Provider/Effect/Artifact |
| Artifact | JSON only | Atomic write, digest, no absolute host path in canonical result |
| Client state | event reducer | Consume lifecycle metadata; do not infer stage strings |

## Quality gate

`GoldenPathMetrics` reports ordinary task success, tool success, schema-valid
rate, false PASS, false reject, rework precision, token cost, prompt cache hit,
artifact acceptance, provider drain completion, stale-event rejection,
process-kill recovery, and replay consistency together. A cost or acceptance
improvement cannot make a hard safety failure pass. The default hard limits are
zero false PASS and complete rejection/consistency for every injected fault
case that has observations.

The acceptance suite must include:

- valid completion with both admitted tools and a digest-bound artifact;
- a transient tool failure followed by one retry using the same provider generation;
- a permanent failure that emits `turn.failed` and releases provider inflight;
- duplicate event delivery and stale generation rejection;
- process-kill checkpoint recovery;
- random event ordering and final reducer convergence;
- drain completion and explicit deadline behavior.

## Freeze and deletion/defer rule

No new Harness-style UI, provider, prompt, or plugin abstraction is admitted
until it has a named production consumer, a decision it changes, or a recovery
or useful audit value. Candidate modules with test-only or export-only evidence
are deferred rather than deleted in this dirty migration worktree; deleting
them now would hide compatibility risk. The consumer audit is recorded in
`GOLDEN-PATH-CONSUMER-AUDIT-2026-08-15.md`.

## Staging boundary

The local visual MCP, local read-only REA adapter, irreversible side-effect
sandbox, blue/green deployment controller, and bounded soak runner remain
local validation only. Production or multi-hour staging execution requires a
real target, credential, allowlist, approval owner, monitoring destination,
stop condition, and rollback owner. A host name or configured command alone is
not authorization.
