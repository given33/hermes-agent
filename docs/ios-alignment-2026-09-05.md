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

## Official Skill Catalog Batch

Ported `GET /api/skills/hub/official` and `OptionalSkillSource.list_local` from
the official baseline. The sole structural adaptation is importing the source
class from this checkout's existing monolithic `tools.skills_hub` module and
using the existing router exception style. No second scanner or installer is
introduced. The response includes identifier, category, metadata and the
requested profile's installed flag. Six mounted-router tests pass, including
real temporary skill files, profile provenance and scan failure reporting.
The iOS facade, metadata loader and native catalog now use this endpoint;
preview, scan and install reuse existing actions. Deployment and live-device
acceptance remain pending.

## Terminal Deadline Regression

The Windows regression run reproduced lost partial output: a printing command
followed by sleep returned only the timeout notice when cleanup exceeded the
outer deadline. The backstop now reuses the per-execution capture, including
bounded capture and spill metadata. Output recovery itself has a 200 ms bound
so a stalled capture lock cannot defeat the command deadline.

The canonical runner passed 32 tests with 3 platform skips on Windows and all
35 tests on the isolated Linux source checkout. Tests exercise real subprocess
output with forced outer expiry in both capture modes, interruption propagation,
and a deliberately stalled capture lock. Ruff passed in the Linux environment.
Logs: `windows-terminal-backstop-final-20260905.log` and
`linux-terminal-backstop-20260905.log` in the audit findings directory.
This fixes output loss, not the separate Windows process-cleanup latency or
physical iPhone performance acceptance. Production deployment remains pending.
