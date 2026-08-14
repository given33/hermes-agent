# Hermes Validation Runbook

This directory contains the local validation boundary used for the visual MCP,
REA adapter, irreversible-effect sandbox, deployment controller, and soak
runner.  It is deliberately not a production credential or deployment target.

## MCP endpoints

The active Hermes config registers two supervised stdio executables:

```text
C:\Users\given\hermes-audit\hermes-agent\.venv\Scripts\python.exe -m hermes_cli.visual_evidence_mcp --root C:\Users\given\hermes-audit\validation-targets\visual
C:\Users\given\hermes-audit\hermes-agent\.venv\Scripts\python.exe -m hermes_cli.rea_mcp_server --root C:\Users\given\hermes-audit\validation-targets\rea
```

Run `hermes mcp test hermes-visual-evidence` and
`hermes mcp test hermes-rea-readonly` to perform MCP initialize and
`tools/list`.  The visual provider is deterministic and read-only; OCR reports
`unavailable` when its optional dependency is absent.  The REA adapter lists,
scans, and inspects artifacts but never executes or extracts them.

## Irreversible boundary

`hermes_cli.validation_side_effect` only writes an append-only local ledger.
An operation must match the exact `operation_id` and `target` allowlist entry.
Irreversible entries require an explicit, unexpired, single-use approval with a
matching approval subject and named approver.  Idempotency keys return the
original receipt and cannot create a second record.  No network, payment,
email, remote deletion, or production deployment target is present.

Inspect the policy with:

```text
C:\Users\given\hermes-audit\hermes-agent\.venv\Scripts\python.exe -m hermes_cli.validation_side_effect --root C:\Users\given\hermes-audit\validation-targets\side-effects
```

The local execution drill must remain explicit and can be run only against the
validation root with a one-shot approval supplied by the operator.  It is not a
substitute for a production approval service.

## Deployment control path

`ValidationDeploymentController` stages immutable release data, verifies a
content digest, performs a health-gated blue/green pointer switch, marks drain
with a deadline, and restores the previous release only when the exact local
rollback owner is supplied.  It does not start services or use SSH, Docker, or
cloud credentials.  Production remains blocked until a real authorized target,
least-privilege identity, health endpoint, change window, and rollback owner
are supplied by the deployment operator.

The repository's existing production entry point is
`deploy/public/deploy-collaboration-backend.sh`. It requires a full release
commit, uses batch-mode SSH, supports a pinned known-hosts file, stages the
release remotely, and has rollback handling. Its default remote is
`admin@10.66.0.1`, but this validation run did not invoke it because the
required SSH identity/authorization was not available. The production inputs
are recorded in `validation/production-readiness.yaml`; a host name alone is
not authorization.

## Soak and stop policy

Run a bounded smoke soak first:

```text
C:\Users\given\hermes-audit\hermes-agent\.venv\Scripts\python.exe -m hermes_cli.validation_soak --visual-root C:\Users\given\hermes-audit\validation-targets\visual --rea-root C:\Users\given\hermes-audit\validation-targets\rea --duration-seconds 30 --interval-seconds 1
```

For a two-hour local soak, use `--duration-hours 2`.  The runner holds actual
stdio MCP sessions open, checks identity/generation, calls both providers, and
closes sessions on normal completion, operator interrupt, or any stop
condition.  It stops after three consecutive errors by default.  A production
soak is not authorized by this local runbook; its rollback owner is a required
deployment input, not an assumed identity.
