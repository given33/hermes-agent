# Package layering and migration contract

**Status:** active migration with executable boundaries and downward-only
architecture ratchets. Measurements below were generated on 2026-07-27 by
`tests/architecture/archlint.py` after the Claude audit remediation pass.

This document supersedes the audit-era snapshot. The source of truth is the
AST baseline under `tests/architecture/baselines/`; when prose and a baseline
disagree, the baseline wins.

## Target layers

Higher layers may call lower layers. Lower layers must not import entry points
or UI adapters.

```text
L6  hermes_cli, tui_gateway              entry points and protocol adapters
L5  gateway, plugins                     long-running orchestration and edges
L4  tools                                agent capabilities
L3  agent                                model loop and conversation behavior
L2  hermes_services                      framework-neutral application policy
L1  hermes_runtime                       config/process/security primitives
L0  leaf modules                         constants, time, utilities
```

The target is a narrow core, not one physical process. FastAPI, aiohttp and
TUI JSON-RPC serve different protocols and may remain separate adapters. They
must share application decisions and security policy instead of reimplementing
them.

## Boundaries implemented in this remediation

### Configuration authority

`hermes_runtime.config` owns raw reads, strict reads, environment expansion,
last-known-good behavior and atomic locked mutation. Production callers use
`read_raw_config`, `read_raw_config_strict`, `load_config` or `mutate_config`.

The audit found 23 direct `config.yaml` reads across 15 modules. The current
AST measurement contains one read in `hermes_runtime/managed_scope.py`, which
is the authority's own managed-overlay loader. Plugin manifest YAML is parsed
with `utils.fast_safe_load` and is no longer mistaken for user configuration.

### Application services

`hermes_services` is a framework-neutral layer containing:

- bearer/auth error contracts and constant-time authorization decisions;
- response and service-failure contracts;
- shared CORS, request-size and security-header policy;
- Chronos fire authentication;
- JSON-RPC method registration and error mapping.
- one `HermesApplicationKernel` composition root that owns the HTTP boundary,
  live-session registry and JSON-RPC registry for each adapter process.

`HttpBoundaryPolicy` is adapted by the FastAPI dashboard and aiohttp API
server. `JsonRpcMethodRegistry` and `LiveSessionRegistry` provide the matching
application/session boundary for the TUI transport. Protocol serialization and
route registration remain in the adapter that owns them; physically merging
three incompatible wire protocols is not an application-layer requirement.

### Runtime foundations

Config, managed scope, redaction, process compatibility, timeout, redirect
security, console output, runtime cwd and session context live in
`hermes_runtime`. Historical `hermes_cli.*` paths are compatibility exports;
new lower-layer imports must use `hermes_runtime` or `hermes_services`.

### Public cross-layer contracts

Provider auth failures are represented by `hermes_services.auth.AuthError`.
External consumers use public credential accessors such as
`read_codex_tokens` instead of lock-aware private CLI helpers. New cross-package
private imports are rejected by the architecture ratchet.

## Current measured dependency graph

The graph is improved but is not yet a DAG. Exact cross-package statement
counts are stored in `dependency_direction.json`. The largest remaining
forbidden/upward edges are:

| edge | statements |
|---|---:|
| `hermes_cli -> hermes_runtime` | 325 |
| `hermes_cli -> agent` | 200 |
| `hermes_cli -> tools` | 169 |
| `plugins -> gateway` | 131 |
| `plugins -> hermes_cli` | 119 |
| `gateway -> hermes_cli` | 92 |
| `tui_gateway -> hermes_cli` | 87 |
| `agent -> hermes_cli` | 32 |

Deferred cross-package imports, usually evidence of a cycle, are:

| package | deferred imports |
|---|---:|
| `hermes_cli` | 685 |
| `plugins` | 391 |
| `tools` | 303 |
| `gateway` | 295 |
| `tui_gateway` | 256 |
| `agent` | 206 |
| `hermes_runtime` | 0 |
| `hermes_services` | 0 |

The heaviest function-body importers are `tui_gateway/server.py` (281),
`gateway/run.py` (272), `hermes_cli/main.py` (268) and
`hermes_cli/web_server.py` (248). These are migration targets, not acceptable
templates for new code.

Cross-package private-symbol imports are tracked by
`private_symbol_imports.json`; the baseline can only shrink.

The legacy `cli.CLI_CONFIG` snapshot is now private to `cli.py`. Callback,
mixin, gateway and tool code must use an injected instance config or
`hermes_runtime.config`; external imports of the snapshot are prohibited.

## Migration sequence

Future work must move one complete behavior at a time and keep wire contracts
stable:

1. Extract provider credentials and provider selection from `hermes_cli.auth`
   into a service with injected storage and refresh clients.
2. Define one session/application kernel used by CLI, gateway, TUI, cron and
   hosted collaboration; adapters own only protocol metadata.
3. Move platform registration and message envelopes behind public gateway
   contracts so plugins stop importing gateway internals.
4. Split `gateway/run.py`, `tui_gateway/server.py`, `hermes_cli/main.py` and
   `hermes_cli/web_server.py` by lifecycle, command and route ownership.
5. Remove compatibility imports only after every caller and deployment has
   migrated.

This sequence is intentionally incremental. A mass import rewrite without an
application contract would only relocate the cycles.

## Enforcement

Run:

```powershell
.\.venv-test\Scripts\python.exe -m pytest tests\architecture -q
```

The suite rejects:

- a new direct `config.yaml` read outside the authority;
- a new cross-package edge or a heavier existing edge;
- an increase in deferred imports;
- a new cross-package private-symbol import;
- foundation packages importing higher layers;
- security controls that are silently unwired;
- loss of config write locking or ignore-user-config convergence.

Baselines may be regenerated only after a measured improvement:

```powershell
.\.venv-test\Scripts\python.exe tests\architecture\archlint.py --write-baselines
```

Never expand a baseline to make a feature change pass.
