# Codex Trajectory Adoption

Hermes adopts the read-only event-ledger ideas from
[icesixgod/codex-trajectory](https://github.com/icesixgod/codex-trajectory): a
versioned trajectory schema, bounded records, turn and step projections,
tool/subagent timing, failure counts, and safe summary details.

This implementation is intentionally an adapter over Hermes Hosted Event
Protocol. It does not read arbitrary Codex files, accept filesystem paths from
the client, or install the upstream plugin as a runtime dependency. Hosted
events are already scoped by conversation, turn, account generation, and
cursor. The projection therefore preserves those boundaries and exposes a
read-only endpoint at:

```text
GET /api/plugins/collaboration/single/conversations/{conversation_id}/hosted-turns/{turn_id}/trajectory
```

`detailLevel=summary` is the default. Both levels omit prompts, system
instructions, encrypted reasoning, raw tool arguments, and raw tool output;
`full` only adds bounded summaries and runtime metadata. Records are capped at
1,000, duplicate event ids are ignored, and secret-shaped strings are redacted
before the response is returned.

The iOS client consumes the same canonical events through a pure reducer and
shows the recent bounded timeline in the Hosted status bar. This keeps the
viewer useful during a live turn without introducing a second event source.

The upstream repository is MIT licensed. Hermes did not copy its source
files; this module is an independent implementation of the public data-model
and privacy boundary. The upstream project remains the reference for the
optional Codex-local MCP viewer and its `schemaVersion: 1` trajectory shape.
