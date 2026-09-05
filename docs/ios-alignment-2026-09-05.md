# Official and iOS Alignment, 2026-09-05

## Baseline

Official revision: `79445a496c86a19332ad786494b8384d2167e2d0`.
Product revision at inventory: `705cfa6947`.
There are 5,237 upstream-only commits, 3,602 changed upstream files and 338
overlapping changed paths. Static inventory finds 357 official HTTP/RPC
declarations and 337 matching product declarations. The 20 missing declarations
are not the complete functional backlog: dynamic/plugin surfaces and behavior
require separate checks. No capability is accepted from declarations alone.

## Tracking

Run `scripts/feature_alignment_inventory.py --official-ref upstream/main
--ios-root ../hermes-ios --output ../findings/HERMES-API-ALIGNMENT-2026-09-05.json`
to produce the JSON inventory and its Markdown companion. The existing daily
upstream workflow captures these files, the upstream risk report, merge
conflicts and the tested iOS SHA even when merge/reporting fails.

Before backend promotion, the workflow now requires the existing backend
preflight plus the checked-out iOS main snapshot's tests and typecheck.
The backend preflight uses the canonical isolated per-file test runner.
This is not yet a live backend/iOS contract or device acceptance gate.

`scripts/upstream_change_report.py` counts all upstream commits before
truncating the detail list. Deferred iOS work remains incomplete in reports.
The four report/inventory behavior tests passed through `scripts/run_tests.sh`.
Ruff passed for the changed Python tools and tests.

## Open Work

- Resolve the upstream backlog in dependency-ordered batches, preserving
  official implementations and recording necessary compatibility adaptations.
- Cover local models (16 routes), official SkillHub catalog, TTS leases,
  gateway capabilities and diagnostics upload on backend and iOS.
- Reconcile the existing gate's ban on official review features with the
  current full-feature requirement; prior intentional removals are not waivers.
- Finish transaction installer verification and align hub, DBB3, WSL and HK
  deployed revisions. DBB3 was unreachable during the baseline check.
- No Mac/iPhone device entry is available. macOS CI compilation is available,
  but signed installation, physical-device interaction and Instruments
  latency/frame/memory/CPU measurements remain unverified.
