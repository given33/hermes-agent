import importlib.util
from pathlib import Path
from unittest.mock import patch


def test_non_bedrock_import_never_installs_optional_provider_dependencies():
    path = Path(__file__).resolve().parents[2] / "agent" / "bedrock_adapter.py"
    spec = importlib.util.spec_from_file_location("bedrock_startup_probe", path)
    module = importlib.util.module_from_spec(spec)
    with patch("tools.lazy_deps.ensure") as ensure:
        spec.loader.exec_module(module)
        ensure.assert_not_called()


def test_assigned_worker_does_not_start_a_competing_title_model(monkeypatch):
    from agent.title_generator import _auto_title_enabled
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_assigned")
    with patch("hermes_cli.config.load_config_readonly", side_effect=AssertionError("No model configuration needed")):
        assert not _auto_title_enabled()
