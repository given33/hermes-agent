# Third-Party Notices

## Pi

- Project: `earendil-works/pi`
- Source: https://github.com/earendil-works/pi
- Reviewed revision: `a597371bda2af70372d1323d550483b5f4a0ae36`
- License: MIT, reproduced in `licenses/pi-MIT.txt`

Hermes uses protocol, queue, tool-ordering, resource-provenance, session-entry,
and behavioral-evaluation design patterns derived from this revision. The Pi
runtime, experimental server, TUI, provider credentials, package installer,
and Node runtime are not bundled with Hermes Agent or Hermes iOS.

Source-to-target adaptation map:

| Pi source | Hermes Agent target |
|---|---|
| `packages/agent/src/types.ts`, `packages/agent/src/agent-loop.ts` | `hermes_services/hosted_event_protocol.py` |
| `packages/agent/src/agent.ts`, `packages/agent/src/agent-loop.ts` | hosted intervention logic in `plugins/collaboration/dashboard/plugin_api.py` |
| `packages/agent/src/types.ts`, `packages/agent/test/agent-loop.test.ts` | `hermes_services/tool_contract.py`, `agent/tool_dispatch_helpers.py` |
| `packages/coding-agent/src/core/tools/output-accumulator.ts` | `hermes_services/tool_output_artifacts.py`, `tools/tool_result_storage.py` |
| `packages/coding-agent/src/core/source-info.ts`, `diagnostics.ts` | `hermes_services/resource_catalog.py`, `hermes_cli/managed_installations.py` |
| `packages/agent/src/harness/session`, `packages/storage/sqlite-node/src` | `hermes_services/session_entries.py` |
| `packages/tui/src/autocomplete.ts`, `packages/coding-agent/src/core/slash-commands.ts` | `hermes_cli/mobile_console.py` |
| `packages/evals/src/pi-harness.ts` | `hermes_services/behavior_eval.py` |
| `packages/coding-agent/src/core/extensions/types.ts`, `runner.ts` | `hermes_services/internal_hooks.py` |

These files are Python adaptations integrated with Hermes account generations,
durable hosted execution, authenticated HTTP/SSE, encryption, and fleet
resource policy. They are not verbatim TypeScript copies.
