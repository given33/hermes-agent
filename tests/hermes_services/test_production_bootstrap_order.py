"""Runtime proof that trusted hooks seal before dynamic extension code."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


def _subprocess_env(home: Path, marker: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "HERMES_HOME": str(home),
            "HERMES_BOOTSTRAP_ORDER_MARKER": str(marker),
            # Keep the probe isolated from the checkout's bundled plugins; the
            # user plugin below is the only dynamic code that needs to load.
            "HERMES_BUNDLED_PLUGINS": str(home / "empty-bundled-plugins"),
        }
    )
    return env


def _assert_probe_saw_sealed_registry(marker: Path) -> None:
    observation = json.loads(marker.read_text(encoding="utf-8"))
    assert observation == {"module_loaded": True, "sealed": True}


def test_agent_plugin_discovery_seals_hooks_before_register_callback(tmp_path):
    home = tmp_path / "home"
    plugin = home / "plugins" / "order-probe"
    plugin.mkdir(parents=True)
    (home / "empty-bundled-plugins").mkdir(parents=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["order-probe"]}}),
        encoding="utf-8",
    )
    (plugin / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "order-probe",
                "version": "1.0.0",
                "description": "startup order probe",
            }
        ),
        encoding="utf-8",
    )
    (plugin / "__init__.py").write_text(
        textwrap.dedent(
            """
            import json
            import os
            from pathlib import Path
            import sys

            def register(_context):
                module = sys.modules.get("hermes_services.internal_hooks")
                Path(os.environ["HERMES_BOOTSTRAP_ORDER_MARKER"]).write_text(
                    json.dumps({
                        "module_loaded": module is not None,
                        "sealed": bool(getattr(module, "_SEALED", False)),
                    }),
                    encoding="utf-8",
                )
            """
        ),
        encoding="utf-8",
    )
    marker = tmp_path / "plugin-order.json"

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hermes_cli.plugins import discover_plugins; discover_plugins()",
        ],
        cwd=REPO_ROOT,
        env=_subprocess_env(home, marker),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert child.returncode == 0, child.stderr
    _assert_probe_saw_sealed_registry(marker)

def test_dashboard_api_load_seals_hooks_before_module_execution(tmp_path):
    home = tmp_path / "home"
    dashboard = home / "plugins" / "dashboard-order-probe" / "dashboard"
    dashboard.mkdir(parents=True)
    (home / "empty-bundled-plugins").mkdir(parents=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {"plugins": {"enabled": ["dashboard-order-probe"]}}
        ),
        encoding="utf-8",
    )
    (dashboard / "manifest.json").write_text(
        json.dumps(
            {
                "name": "dashboard-order-probe",
                "version": "1.0.0",
                "entry": "index.js",
                "api": "plugin_api.py",
            }
        ),
        encoding="utf-8",
    )
    (dashboard / "plugin_api.py").write_text(
        textwrap.dedent(
            """
            import json
            import os
            from pathlib import Path
            import sys

            module = sys.modules.get("hermes_services.internal_hooks")
            Path(os.environ["HERMES_BOOTSTRAP_ORDER_MARKER"]).write_text(
                json.dumps({
                    "module_loaded": module is not None,
                    "sealed": bool(getattr(module, "_SEALED", False)),
                }),
                encoding="utf-8",
            )

            from fastapi import APIRouter
            router = APIRouter()
            """
        ),
        encoding="utf-8",
    )
    marker = tmp_path / "dashboard-order.json"

    child = subprocess.run(
        [sys.executable, "-c", "import hermes_cli.web_server"],
        cwd=REPO_ROOT,
        env=_subprocess_env(home, marker),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert child.returncode == 0, child.stderr
    _assert_probe_saw_sealed_registry(marker)
