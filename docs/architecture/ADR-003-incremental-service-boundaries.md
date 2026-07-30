# ADR-003: Incremental Service Boundaries

## Status

Accepted.

## Context

Hermes has three HTTP-facing adapters (FastAPI, aiohttp, and JSON-RPC) and a
native iOS client. A one-shot rewrite would combine release-critical fixes with
large ownership changes and remove the ability to compare old behavior or roll
back one domain at a time.

## Decision

`hermes_services` is the framework-neutral application and contract layer.
FastAPI, aiohttp, and JSON-RPC remain protocol adapters and may not own a second
copy of authentication, request policy, or domain state transitions.

The application composition root declares five bounded contexts: account,
hosted task, resource catalog, notification, and intelligence. Each context has
an explicit domain owner, port name, adapter list, migration flag, and status.
Resource catalog is the first canonical context; the others remain behind
compatibility adapters until their contract tests pass.

HTTP migration uses `HERMES_HTTP_CONTRACT_MODE` with `legacy`, `dual`, and
`canonical` values. Production defaults to `dual`. In dual mode both policies
must return the same decision; a difference fails closed. Rolling back changes
only the flag and does not require a database downgrade.

The iOS chat surface keeps four dependency layers: transport, repository,
state-machine/domain logic, and presentation. Presentation may compose lower
layers; lower layers may not import React Native presentation modules. Facades
remain composition roots and do not regain endpoint or persistence bodies.

The versioned SwiftUI route schema is the source of truth for generated
TypeScript, Swift, and Python contracts. Generated outputs are checked in CI.

## Migration Procedure

1. Add a domain service and port behind the existing adapter.
2. Run legacy and canonical contracts in dual mode and compare results.
3. Enforce dependency direction, API contract, and module performance tests.
4. Switch one context to canonical and retain its compatibility adapter.
5. Exercise rollback to legacy before starting the next context.

## Consequences

Migration is intentionally incremental. Compatibility code is temporary but
remains testable, release rollback stays operational, and module-size ratchets
prevent the old dependency mesh from returning.
