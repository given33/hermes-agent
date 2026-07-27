"""Hard dependency rules for the extracted runtime foundations.

The historical six-package mesh is being reduced behind ratchets.  New
foundation packages have no legacy exception: they may never grow an import
back into an entry point, transport, plugin, tool, or model-loop package.
"""

import ast
from pathlib import Path

from tests.architecture import archlint


UPPER_PACKAGES = {
    "agent",
    "tools",
    "gateway",
    "hermes_cli",
    "plugins",
    "tui_gateway",
}
ROOT = Path(__file__).resolve().parents[2]


def test_runtime_foundations_have_no_upward_edges():
    edges = archlint.measure_dependency_direction()[
        "cross_package_import_statements"
    ]
    forbidden = {
        edge: count
        for edge, count in edges.items()
        if (
            edge.split("->", 1)[0] == "hermes_runtime"
            and edge.split("->", 1)[1] in UPPER_PACKAGES | {"hermes_services"}
        )
        or (
            edge.split("->", 1)[0] == "hermes_services"
            and edge.split("->", 1)[1] in UPPER_PACKAGES
        )
    }
    assert not forbidden, (
        "Low-level runtime packages imported an upper layer: "
        f"{forbidden}. Inject a protocol/callback or move the shared contract "
        "down instead."
    )


def test_legacy_cli_config_snapshot_is_private_to_cli_entrypoint():
    violations: list[str] = []
    for package in sorted(UPPER_PACKAGES):
        root = ROOT / package
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.module != "cli":
                    continue
                if any(alias.name == "CLI_CONFIG" for alias in node.names):
                    violations.append(str(path.relative_to(ROOT)))
    assert not violations, (
        "cli.CLI_CONFIG is an import-time compatibility snapshot and may not "
        "escape cli.py; inject the active instance config or use "
        f"hermes_runtime.config instead: {violations}"
    )
