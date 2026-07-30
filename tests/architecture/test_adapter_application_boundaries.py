"""Production protocol adapters must use the shared application boundaries.

FastAPI, aiohttp, and JSON-RPC are intentionally different wire protocols.
The architecture contract is that they adapt the same framework-neutral
security and use-case layer instead of copying business decisions.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _imported_from(source: str, module: str) -> set[str]:
    tree = ast.parse(source)
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == module
        for alias in node.names
    }


def test_fastapi_and_aiohttp_use_shared_http_and_cron_boundaries() -> None:
    dashboard = _source("hermes_cli/web_server.py")
    api_server = _source("gateway/platforms/api_server.py")

    for name, source in {
        "FastAPI dashboard": dashboard,
        "aiohttp API server": api_server,
    }.items():
        imports = _imported_from(source, "hermes_services")
        assert {"HermesApplicationKernel", "accept_cron_fire_request"} <= imports, name
        assert "HermesApplicationKernel.for_http(" in source, name
        assert 'os.environ.get("HERMES_HTTP_CONTRACT_MODE", "dual")' in source, name
        assert "accept_cron_fire_request(" in source, name
        assert "get_fire_verifier" not in source, (
            f"{name} reintroduced transport-local Chronos JWT verification"
        )

    assert "_DASHBOARD_HTTP_BOUNDARY.authorize(" in dashboard
    assert "self._http_boundary.authorize(" in api_server
    assert "_DASHBOARD_APPLICATION.require_http_boundary()" in dashboard
    assert "self._application.require_http_boundary()" in api_server


def test_kernel_declares_bounded_contexts_and_compatibility_migration() -> None:
    application = _source("hermes_services/application.py")
    contexts = _source("hermes_services/contexts.py")
    boundary = _source("hermes_services/http_boundary.py")

    assert "contexts: BoundedContextRegistry" in application
    for name in (
        "account",
        "hosted_task",
        "resource_catalog",
        "notification",
        "intelligence",
    ):
        assert f'"{name}": BoundedContext(' in contexts
    assert 'mode: HttpContractMode = "dual"' in boundary
    assert 'error_code="http_contract_mismatch"' in boundary


def test_jsonrpc_adapter_uses_shared_registry_without_a_second_method_table() -> None:
    tui = _source("tui_gateway/server.py")

    assert "HermesApplicationKernel," in tui
    assert "_APPLICATION = HermesApplicationKernel.for_local_rpc()" in tui
    assert "_rpc_registry: JsonRpcMethodRegistry = _APPLICATION.rpc" in tui
    assert "_sessions: LiveSessionRegistry[dict[str, Any]] = _APPLICATION.sessions" in tui
    assert "_methods = _rpc_registry.methods" in tui
    assert "return _rpc_registry.handle(req)" in tui


def test_foundation_services_stay_framework_neutral() -> None:
    forbidden = {"aiohttp", "fastapi", "starlette", "uvicorn"}
    imported: set[str] = set()
    for path in sorted((ROOT / "hermes_services").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
    assert not imported & forbidden, (
        "hermes_services imported adapter frameworks: "
        f"{sorted(imported & forbidden)}"
    )
