# W3 hermes_cli cluster — remediation progress

## 1. tests/hermes_cli/test_terminal_breadcrumbs.py — DONE (14/14 pass)
- Root cause: test helpers _fake_tty/_fake_no_tty used monkeypatch.setattr(tb.os, "ttyname", ...)
  which fails on Windows (os.ttyname doesn't exist). Product code already guards with try/except.
- Fix: raising=False injection so Windows exercises the same tty-probe branch. No coverage loss.
- Files: tests/hermes_cli/test_terminal_breadcrumbs.py only.

## 2. tests/hermes_cli/test_managed_uv.py — DONE (29 passed / 13 skipped, 0 fail)
Product bug fixed (whole class):
- hermes_cli/managed_uv.py had TWO unguarded cosmetic `uv --version` probes
  (_ensure_uv_path post-install ~L220; update_managed_uv post-self-update ~L351).
  A binary that passes is_file()+X_OK but fails to exec (truncated download;
  WinError 216 on non-PE file on Windows) crashed ensure_uv()/update_managed_uv()
  after success instead of degrading. Both now route through the existing guarded
  _uv_version_string() helper (same pattern module already used at L1031).
Test fixes (Windows portability, no coverage loss):
- uv vs uv.exe naming drift in TestResolveUv::test_existing_executable,
  TestEnsureUv::test_installs_if_missing, ...reports_runtime_repair_to_observer,
  TestUpdateManagedUv::test_fresh_stamp.../test_stale_stamp...
- TestInstallUvInternals::test_posix_sets_uv_unmanaged_install was invoking REAL
  PowerShell network installer on Windows (only _install_uv_posix patched).
  Now skipif(win32) + win32 twin test_windows_sets_uv_install_dir.
- TestDefaultLiveVenv._checkout created bin/python unconditionally; now mirrors
  real venv layout (Scripts/python.exe on Windows) — managed-vs-dotvenv
  precedence is now genuinely verified on Windows too.
- Both files byte-identical to _ref-hermes-agent-remote before edits (no drift).

## Remaining files (pending)
3. test_goal_gates.py (3)
4. test_kanban_boards.py (3)
5. test_gemini_free_tier_setup_block.py (1)
6. test_web_server_oauth_write.py (1)
7. dashboard_auth/test_owner_mobile_auth.py (1)
8. test_update_autostash.py (1)
9. test_claw.py (1)
10. test_mcp_catalog.py (1)
