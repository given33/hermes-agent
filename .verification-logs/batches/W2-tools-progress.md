# W2 tools batch progress

Started: 2026-08-24T16:25:39.327Z

## Files

## 1. tests/tools/test_file_tools_cwd_resolution.py — DONE (11 passed)

- Product fix `tools/file_tools.py` (whole Windows bug class: absolute anchors re-anchored onto process cwd in container mode):
  - `_WINDOWS_ABSOLUTE_PREFIX` regex (drive-letter/UNC) — NOT ntpath.isabs, which false-positives rooted POSIX paths on py<=3.12.
  - `_normalize_without_host_deref` now flavour-preserving (PureWindowsPath for drive/UNC input; PurePosixPath otherwise); never touches FS.
  - `_resolve_base_dir(container_paths=True)` returns windows-abs anchors verbatim instead of `posixpath.join(os.getcwd(), anchor)` garbage.
  - `_resolve_path_for_task` container branch treats windows-abs input as sandbox-local absolute (no host deref, no re-anchor).
  - `_path_resolution_warning`: replaced `!r` repr-quoting with explicit quotes — repr doubled every backslash in the user-facing message on Windows.
- Test fix: `_make_host_dir_link` helper uses NTFS junction (`_winapi.CreateJunction`) on win32 — symlink_to needs SeCreateSymbolicLinkPrivilege; junction is unprivileged and followed by all host-deref paths, preserving the premise.
- Sibling safety: tests/tools/test_file_tools_tilde_profile.py + tests/hermes_cli/test_kanban_worker_terminal_cwd.py → 5 passed.
## 2. tests/tools/test_execution_flag_detection.py — DONE (81 passed, 4 skipped)

## 3. tests/tools/test_voice_mode.py — DONE (59 passed, 5 skipped)

- No product changes needed. 5 skips are native POSIX (AF_UNIX) + macOS-only markers already correctly applied. Class structure covers WSL, SSH, Docker, PulseAudio fallback, silence detection, max-recording cap, playback interrupt, and Whisper hallucination filtering.

## 4. tests/tools/test_modal_sandbox_fixes.py — DONE (30 passed)

- Product fix `tools/terminal_tool.py`: in `_get_env_config`, the Docker mount-cwd branch was running user-supplied TERMINAL_CWD through `os.path.abspath`. On a Windows host that re-anchors POSIX host prefixes (`/Users/...`, `/home/...`) to `C:\\...` and mangles the value (the docker mount layer then sees a Windows host path that was never intended). Fix: if the source string starts with any `_HOST_CWD_PREFIXES` member, pass it through verbatim; only Windows drive prefixes get `ntpath.normpath` (the only thing `os.path.abspath` was actually helping with). Whole bug class: any user-declared remote-host path survived host re-anchoring on win32.

## 5. tests/tools/test_browser_content_none_guard.py — DONE (6 passed)

- Test fix: the source-line verification helper opened `tools/browser_tool.py` with bare `open()`, which uses the host default codec — GBK (cp936) on this Windows box — and the file has a Windows-1252 byte (0x94) that crashes a strict decode. Now opens with `encoding="utf-8"` + `errors="replace"`. Source-guard assertions still pass: both call sites (`_extract_relevant_content` line 3195 and `browser_vision` line 4807) are already correctly guarded with `(response.choices[0].message.content or "")`.


- A concurrent worker had blanket-gated both real-binary tests with @pytest.mark.linux_only (over-broad: loses verified-working Windows rg coverage). Replaced with precise gating:
  - rg "--" ownership rows: cross-platform as-is (passing); sort row split into test_sort_end_of_options_owns_flag_looking_operand with platform-aware diagnostics (POSIX rc==2 exact; win32 native sort.exe: rc!=0 + stderr diagnostic, empty stdout).
  - payload-execution test: POSIX path byte-identical to original; win32 runs the 2 rg rows via NTFS-native .cmd payload twin (probe-verified CreateProcess executes it; marker written); sort/ag/man rows skip on win32 with reasons (native sort.exe has no GNU --compress-program; script PTY wrapper absent).
- subprocess captures now use encoding="utf-8" (+errors="replace" in payload test) — GBK locale decode crash class.
- NOTE for orchestrator: another agent edited this file mid-session (added linux_only markers); my edits supersede them deliberately.
