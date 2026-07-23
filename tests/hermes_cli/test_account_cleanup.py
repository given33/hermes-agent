from pathlib import Path
from types import SimpleNamespace

import yaml

from hermes_cli import account_cleanup


def _write_config(path: Path, value: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.yaml").write_text(
        yaml.safe_dump(value, sort_keys=False),
        encoding="utf-8",
    )


def test_model_cleanup_visits_every_profile_and_preserves_unrelated_config(
    tmp_path,
    monkeypatch,
):
    default = tmp_path / "default"
    worker = tmp_path / "worker"
    _write_config(default, {
        "model": {"provider": "custom", "api_key": "secret-a"},
        "fallback_model": {"provider": "openai", "key": "secret-b"},
        "dashboard": {"theme": "dark"},
    })
    _write_config(worker, {
        "auxiliary": [{"provider": "custom", "token": "secret-c"}],
        "gateway": {"enabled": True},
    })
    monkeypatch.setattr(account_cleanup, "get_hermes_home", lambda: default)
    monkeypatch.setattr(
        account_cleanup,
        "list_profiles",
        lambda: [SimpleNamespace(path=worker), SimpleNamespace(path=default)],
    )

    result = account_cleanup.purge_owner_model_configuration("owner")

    assert result == {
        "profiles_changed": 2,
        "sections_removed": 3,
        "credentials_removed": 3,
    }
    default_config = yaml.safe_load((default / "config.yaml").read_text(encoding="utf-8"))
    worker_config = yaml.safe_load((worker / "config.yaml").read_text(encoding="utf-8"))
    assert default_config == {"dashboard": {"theme": "dark"}}
    assert worker_config == {"gateway": {"enabled": True}}


def test_global_cleanup_combines_collaboration_and_model_domains(monkeypatch):
    from plugins.collaboration.dashboard import plugin_api

    monkeypatch.setattr(
        plugin_api,
        "delete_owner_account_data",
        lambda owner_id: {"owner_id": owner_id, "conversations": 2},
    )
    monkeypatch.setattr(
        account_cleanup,
        "purge_owner_model_configuration",
        lambda owner_id: {"owner_id": owner_id, "profiles_changed": 1},
    )
    monkeypatch.setattr(
        account_cleanup,
        "purge_owner_operational_state",
        lambda owner_id: {"owner_id": owner_id, "workflow_runs": 3},
    )

    result = account_cleanup.purge_account_owned_cloud_data("owner")

    assert result == {
        "collaboration": {"owner_id": "owner", "conversations": 2},
        "models": {"owner_id": "owner", "profiles_changed": 1},
        "operational": {"owner_id": "owner", "workflow_runs": 3},
    }
