# Transaction Installer Verification, 2026-09-05

## Implementation

The pending installer work is retained and verified as one transaction change:
root-controlled candidate markers and process leases, isolated profile I/O,
descriptor-relative runtime-home guards, durable virtualenv swap recovery,
writer quiescence and profile migration, startup watchdog binding, and
authenticated post-start checks before the candidate becomes authoritative.
After candidate writes begin, failure preserves the candidate and keeps the
service stopped pending a marker-aware retry.

This verification found `/run` at 950 MB used of 952 MB. Interrupted installer
snapshots occupied that tmpfs; extraction failed with ENOSPC despite 948 GB free
on the root filesystem. New source snapshots use validated root-owned
`/var/tmp` and retain existing cleanup and ownership checks. SIGKILL can still
leave disk snapshots; old directories were not indiscriminately removed.

The harness now finds `.venv` as well as `venv`, reports credential-stage
stderr, and consumes stdin only for mocked SSH tar uploads. The old unconditional
`cat` blocked manually invoked tests. The root ownership fixture now requests
a real non-root service UID/GID, rather than root ownership forbidden by the
production helper.

## Evidence

The canonical runner executed isolated Linux copies of the working sources,
using a local test virtualenv and temporary Hermes homes. Production services
were not deployed or restarted by these tests.

- Six deployment test files: 147 passed, 0 failed, 26 skipped (349.5 seconds).
- Root runtime-home guards: 31 passed, 1 skipped; the skipped case requires a
  non-root caller.
- Root profile I/O after fixture correction: 27 passed, 1 skipped; the skipped
  permission-denial case passed in the non-root run.
- Final deployment asset and full transaction harness rerun: 68 passed,
  0 failed/skipped (316.5 seconds), including hard-kill and phase failures.
- Ruff passed for all three helpers and five added guard/recovery test files.
- Windows-to-WSL launcher timeouts and a root Git ownership mismatch were
  recorded as failed environment attempts, not counted as passing results.

Full raw logs are under the workspace `findings` directory. The four-machine
release is still pending: HK integration coverage and DBB3 reachability need
resolution, and unified runtime SHA evidence has not been established.
