# W5 agent core + misc cluster progress

Worker: W5 (agent core + misc). Files assigned: 14.

| # | File | Failures | Status |
|---|------|----------|--------|
| 1 | tests/run_agent/test_callable_api_key.py | 4 | FIXED — read_text missing encoding=utf-8 x4 (test-side GBK drift); 12 passed |
| 2 | tests/run_agent/test_start_order_gate.py | 4 | FIXED — patch lambdas stale vs evolved `_resolve_concurrent_tool_timeout(tool_names)` signature; `lambda *a, **kw` x4; 4 passed |
| 3 | tests/skills/test_darwinian_evolver_skill.py | 4 | FIXED — read_text encoding=utf-8 x3 (skill files valid UTF-8; fixture ERRORs same root cause); 8 passed |
| 4 | tests/cli/test_cli_init.py | 2 | FIXED — POSIX-premise tests gated with skipif(win32) per conftest OS-mark doctrine; win32 twins already exist (windows_only lane); 34 passed, 2 skipped |
| 5 | tests/agent/test_bedrock_integration.py | 2 | FIXED — pyproject read_text encoding=utf-8 in shared helper; 31 passed |
| 6 | tests/agent/test_curator_classification.py | 2 | FIXED — run.json/REPORT.md read_text encoding=utf-8 x4; 13 passed |
| 7 | tests/test_trajectory_compressor_async.py | 2 | FIXED — open() missing encoding=utf-8 in _read_file helper; 8 passed |
| 8 | tests/cli/test_cli_provider_resolution.py | 1 | FIXED — config read_text encoding=utf-8 x4 (whole class incl. latent sites); 15 passed |
| 9 | tests/agent/test_outbound_webhooks.py | 1 | FIXED — subprocess script write_text encoding=utf-8 (child parses source as UTF-8, PEP 3120); 31 passed |
| 10 | tests/agent/test_system_prompt.py | 1 | PENDING |
| 11 | tests/run_agent/test_openai_client_lifecycle.py | 1 | PENDING |
| 12 | tests/honcho_plugin/test_oauth_flow.py | 1 | PENDING |
| 13 | tests/plugins/video_gen/test_deepinfra_provider.py | 1 | PENDING |
| 14 | tests/skills/test_mcp_oauth_remote_gateway_skill.py | 1 | PENDING |
