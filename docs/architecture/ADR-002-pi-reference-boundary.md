# ADR-002: Pi Reference Boundary

## Status

Accepted.

## Decision

The main Hermes server remains the durable authority for mobile accounts,
conversations, hosted-task state, append-only events, files, notifications, and
resource provenance. DBB3 and WSL are execution nodes: they may plan or execute
work, but they do not become authoritative stores for the iOS product.

`earendil-works/pi` is an MIT-licensed reference implementation only. Hermes may
adapt narrowly selected protocol and runtime ideas with attribution, tests, and
product-specific security boundaries. Pi is not a runtime dependency and its
Web, TUI, and desktop surfaces are outside this integration.

## Consequences

- iOS reconnects to the main server and resumes from server-owned cursors.
- DBB3/WSL loss pauses assigned execution without losing accepted work.
- Resource installation and tool artifacts remain account/generation scoped.
- Upstream reference changes are reviewed; they are never merged automatically
  into the product runtime without Hermes tests and attribution review.
