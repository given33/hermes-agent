# W4 gateway cluster — remediation progress

Batch: 11 files (gateway cluster). Host: Windows. Venv: .venv\Scripts\python.exe.
Protocol: run assigned file only; diagnose; minimal fix; re-run until green.

## 1. tests/gateway/test_kanban_reconcile_orphans.py — FIXED ✅
- Was: 2 failed (`test_live_worker_pid_defers_reconcile`, `test_dead_worker_pid_orphan_requeued`) — `FileNotFoundError [WinError 2]` from `subprocess.Popen(["sleep", "30"])` / `["true"]` (POSIX-only executables).
- Diagnosis: test-side platform drift. The contract under test (recorded live PID defers reconcile / dead PID requeues) is cross-platform — `_pid_alive` already routes through `gateway.status._pid_exists` (psutil) on Windows.
- Fix (tests only): cross-platform twins — live process `[sys.executable, "-c", "import time; time.sleep(30)"]`, exited process `[sys.executable, "-c", "pass"]`. Verified empirically that psutil reports an exited child PID as gone even with the Popen object alive, so no skipif needed; assertions unchanged. No product bug.
- Re-run: **9 passed** in 4.44s.

## 2. tests/gateway/test_status.py — FIXED ✅
- Was: 2 failed.
  - `TestGetProcessStartTime::test_live_process_is_stable_int`: `Popen(["sleep", "20"])` → WinError 2. Test-side drift; `_get_process_start_time` is cross-platform (/proc → psutil quantized int). Fix: spawn `[sys.executable, "-c", "import time; time.sleep(20)"]`; assertions unchanged.
  - `TestReadProcessCmdlinePsFallback::test_ps_fallback_when_proc_unavailable`: asserted the `ps -p <pid> -o command=` fallback fires, but product deliberately gates that branch to `not _IS_WINDOWS` (Windows has no /proc and no ps — documented in status.py). Genuine POSIX-only premise → `@pytest.mark.skipif(sys.platform == "win32", ...)` + added Windows twin `test_psutil_fallback_when_proc_unavailable_windows` (`@pytest.mark.windows_only`) exercising the real psutil fallback against `os.getpid()` (no mocks). Coverage preserved on both sides; no product bug.
- Re-run: **70 passed, 1 skipped** (POSIX-only test skipped by design; win32 twin passes).

## 3. tests/gateway/test_loop_command.py — ALREADY GREEN ✅ (no change)
- Run: **10 passed** in 3.22s. The 1 reported failure did not reproduce (likely fixed by another batch's shared-fixture/product fix or run-order dependent); no edit made.

## 4. tests/gateway/test_turn_lease.py — ALREADY GREEN ✅ (no change)
- Run: **12 passed** in 3.43s. The 1 reported failure did not reproduce; no edit made.

## 5. tests/gateway/test_goal_continuation_drain.py — ALREADY GREEN ✅ (no change)
- Run: **2 passed** in 1.87s. The 1 reported failure did not reproduce; no edit made.

## 6. tests/gateway/test_notice_delivery.py — FIXED ✅ (stale test synced to upstream)
- Was: `test_deliver_platform_notice_uses_private_delivery_when_configured` — expected `metadata={'thread_id': '111.222'}` but product sends `metadata={'thread_id': '111.222', 'user_id': 'U123'}`.
- Diagnosis: NOT a product bug. `_thread_metadata_for_source` deliberately stamps `user_id` for Slack sources in BOTH this repo and `_ref-hermes-agent-remote` (R3-5 per-turn identity stamp, gateway-gateway#210 — Slack `chat.startStream` needs recipient_user_id; protects against concurrent-turn per-chat cache races). The reference repo's copy of this same test was already updated to expect `user_id` with an explanatory comment; the local test predates that change.
- Fix (tests only): assertion updated to `metadata={'thread_id': '111.222', 'user_id': 'U123'}` + comment, byte-matching upstream's test.
- Re-run: **1 passed**.

