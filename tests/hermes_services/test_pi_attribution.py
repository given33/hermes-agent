import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_pi_reference_has_license_notice_and_architecture_boundary():
    notice = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    license_text = (ROOT / "licenses" / "pi-MIT.txt").read_text(encoding="utf-8")
    adr = (ROOT / "docs" / "architecture" / "ADR-002-pi-reference-boundary.md").read_text(
        encoding="utf-8"
    )

    assert "earendil-works/pi" in notice
    for target in (
        "hermes_services/hosted_event_protocol.py",
        "hermes_services/tool_contract.py",
        "hermes_services/tool_output_artifacts.py",
        "hermes_services/resource_catalog.py",
        "hermes_services/session_entries.py",
        "hermes_cli/mobile_console.py",
        "hermes_services/behavior_eval.py",
        "hermes_services/internal_hooks.py",
    ):
        assert f"`{target}`" in notice
    assert "MIT License" in license_text
    assert "main Hermes server remains the durable authority" in adr
    assert "DBB3 and WSL are execution nodes" in adr
    assert "Web, TUI, and desktop surfaces are outside" in adr


def test_adopted_service_modules_have_concrete_product_consumers():
    expected_edges = {
        "plugins/collaboration/dashboard/plugin_api.py": {
            "hermes_services.hosted_event_protocol",
            "hermes_services.session_entries",
            "hermes_services.tool_output_artifacts",
        },
        "agent/tool_dispatch_helpers.py": {"hermes_services.tool_contract"},
        "tools/tool_result_storage.py": {
            "hermes_services.internal_hooks",
            "hermes_services.tool_contract",
            "hermes_services.tool_output_artifacts",
        },
        "hermes_cli/managed_installations.py": {"hermes_services.resource_catalog"},
    }
    for relative_path, expected_imports in expected_edges.items():
        tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        actual = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert expected_imports <= actual
