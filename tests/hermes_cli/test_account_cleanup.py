import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli import account_cleanup


def _write_config(path: Path, value: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.yaml").write_text(
        yaml.safe_dump(value, sort_keys=False),
        encoding="utf-8",
    )


def _make_dir_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"directory junctions unavailable: {result.stderr.strip()}")
    else:
        link.symlink_to(target, target_is_directory=True)


def test_model_cleanup_visits_every_profile_and_preserves_unrelated_config(
    tmp_path,
    monkeypatch,
):
    default = tmp_path / "default"
    worker = default / "profiles" / "worker"
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
        lambda: [
            SimpleNamespace(path=worker, name="worker", is_default=False),
            SimpleNamespace(path=default, name="default", is_default=True),
        ],
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


def test_model_cleanup_skips_linked_and_out_of_layout_profile_roots(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    linked = home / "profiles" / "linked"
    _write_config(home, {"model": {"provider": "owner"}})
    _write_config(outside, {"model": {"provider": "external"}})
    linked.parent.mkdir(parents=True, exist_ok=True)
    try:
        _make_dir_link(linked, outside)
    except OSError:
        # The out-of-layout regular root below still exercises the rejection
        # on Windows installations without symlink privilege.
        linked = None
    stray = tmp_path / "stray"
    _write_config(stray, {"model": {"provider": "stray"}})

    monkeypatch.setattr(account_cleanup, "get_hermes_home", lambda: home)
    profile_entries = [
        SimpleNamespace(path=home, name="default", is_default=True),
        SimpleNamespace(path=outside, name="outside", is_default=False),
        SimpleNamespace(path=stray, name="stray", is_default=False),
    ]
    if linked is not None:
        profile_entries.append(
            SimpleNamespace(path=linked, name="linked", is_default=False)
        )
    monkeypatch.setattr(account_cleanup, "list_profiles", lambda: profile_entries)

    result = account_cleanup.purge_owner_model_configuration("owner")

    assert result["profiles_changed"] == 1
    assert yaml.safe_load((outside / "config.yaml").read_text(encoding="utf-8")) == {
        "model": {"provider": "external"}
    }
    assert yaml.safe_load((stray / "config.yaml").read_text(encoding="utf-8")) == {
        "model": {"provider": "stray"}
    }


def test_model_cleanup_rejects_external_default_profile_root(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    external_default = tmp_path / "external-default"
    _write_config(home, {"model": {"provider": "managed"}})
    _write_config(external_default, {"model": {"provider": "external"}})

    monkeypatch.setattr(account_cleanup, "get_hermes_home", lambda: home)
    monkeypatch.setattr(
        account_cleanup,
        "list_profiles",
        lambda: [
            SimpleNamespace(
                path=external_default,
                name="default",
                is_default=True,
            )
        ],
    )

    result = account_cleanup.purge_owner_model_configuration("owner")

    assert result["profiles_changed"] == 1
    assert yaml.safe_load(
        (external_default / "config.yaml").read_text(encoding="utf-8")
    ) == {"model": {"provider": "external"}}


def test_model_cleanup_skips_linked_config_leaf(tmp_path, monkeypatch):
    home = tmp_path / "home"
    outside = tmp_path / "external"
    home.mkdir()
    _write_config(outside, {"model": {"provider": "external"}})
    config_link = home / "config.yaml"
    try:
        config_link.symlink_to(outside / "config.yaml")
    except OSError:
        pytest.skip("file symlinks unavailable")

    monkeypatch.setattr(account_cleanup, "get_hermes_home", lambda: home)
    monkeypatch.setattr(
        account_cleanup,
        "list_profiles",
        lambda: [SimpleNamespace(path=home, name="default", is_default=True)],
    )

    result = account_cleanup.purge_owner_model_configuration("owner")

    assert result["profiles_changed"] == 0
    assert config_link.is_symlink()
    assert yaml.safe_load((outside / "config.yaml").read_text(encoding="utf-8")) == {
        "model": {"provider": "external"}
    }


def test_model_cleanup_fails_closed_for_link_like_hermes_home(
    tmp_path, monkeypatch
):
    real_home = tmp_path / "real-home"
    linked_home = tmp_path / "linked-home"
    _write_config(real_home, {"model": {"provider": "keep"}})
    try:
        _make_dir_link(linked_home, real_home)
    except OSError:
        pytest.skip("directory symlinks unavailable")

    monkeypatch.setattr(account_cleanup, "get_hermes_home", lambda: linked_home)
    monkeypatch.setattr(
        account_cleanup,
        "list_profiles",
        lambda: [SimpleNamespace(path=linked_home, name="default", is_default=True)],
    )

    result = account_cleanup.purge_owner_model_configuration("owner")

    assert result["profiles_changed"] == 0
    assert yaml.safe_load((real_home / "config.yaml").read_text(encoding="utf-8")) == {
        "model": {"provider": "keep"}
    }


def test_global_cleanup_fails_closed_for_link_like_hermes_home(
    tmp_path, monkeypatch
):
    real_home = tmp_path / "real-home"
    linked_home = tmp_path / "linked-home"
    _write_config(real_home, {"model": {"provider": "keep"}})
    try:
        _make_dir_link(linked_home, real_home)
    except OSError:
        pytest.skip("directory junctions unavailable")

    monkeypatch.setattr(account_cleanup, "get_hermes_home", lambda: linked_home)
    monkeypatch.setattr(
        account_cleanup,
        "_account_cleanup_plugins",
        lambda: pytest.fail("linked HERMES_HOME reached collaboration cleanup"),
    )

    with pytest.raises(RuntimeError, match="link-like HERMES_HOME"):
        account_cleanup.purge_account_owned_cloud_data(
            "owner", account_generation="generation-1"
        )


def test_late_old_generation_model_cleanup_preserves_replacement_config(
    tmp_path,
    monkeypatch,
):
    from hermes_cli.dashboard_auth import mobile_device_store

    home = tmp_path / "home"
    mobile_db = tmp_path / "mobile-auth.db"
    monkeypatch.setattr(account_cleanup, "get_hermes_home", lambda: home)
    monkeypatch.setattr(account_cleanup, "list_profiles", lambda: [])
    monkeypatch.setattr(mobile_device_store, "mobile_auth_db_path", lambda: mobile_db)
    mobile = mobile_device_store.MobileDeviceStore()
    old_generation = mobile.account_generation("owner", create=True)
    mobile.begin_account_deletion("owner", "owner-scope", old_generation)
    new_generation = mobile.activate_account_generation(
        "owner",
        replace_deleting=True,
    )
    _write_config(
        home,
        {
            "model": {"provider": "replacement", "api_key": "new-secret"},
            "dashboard": {"account_generation": new_generation},
        },
    )

    result = account_cleanup.purge_owner_model_configuration(
        "owner",
        account_generation=old_generation,
    )

    assert result["skipped_stale_generation"] is True
    assert yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) == {
        "model": {"provider": "replacement", "api_key": "new-secret"},
        "dashboard": {"account_generation": new_generation},
    }


def test_global_cleanup_combines_collaboration_and_model_domains(monkeypatch):
    from plugins.collaboration.dashboard import plugin_api

    monkeypatch.setattr(
        plugin_api,
        "delete_owner_account_data",
        lambda owner_id, *, account_generation: {
            "owner_id": owner_id,
            "account_generation": account_generation,
            "conversations": 2,
        },
    )
    monkeypatch.setattr(
        account_cleanup,
        "purge_owner_model_configuration",
        lambda owner_id, *, account_generation: {
            "owner_id": owner_id,
            "account_generation": account_generation,
            "profiles_changed": 1,
        },
    )
    monkeypatch.setattr(
        account_cleanup,
        "purge_owner_operational_state",
        lambda owner_id, *, account_generation: {
            "owner_id": owner_id,
            "account_generation": account_generation,
            "workflow_runs": 3,
        },
    )

    result = account_cleanup.purge_account_owned_cloud_data(
        "owner",
        account_generation="owner-generation",
    )

    assert result == {
        "collaboration": {
            "owner_id": "owner",
            "account_generation": "owner-generation",
            "conversations": 2,
        },
        "models": {
            "owner_id": "owner",
            "account_generation": "owner-generation",
            "profiles_changed": 1,
        },
        "operational": {
            "owner_id": "owner",
            "account_generation": "owner-generation",
            "workflow_runs": 3,
        },
    }


def test_global_cleanup_holds_account_lifecycle_guard(monkeypatch):
    lifecycle = __import__(
        "hermes_cli.account_lifecycle", fromlist=["account_lifecycle_commit_guard"]
    )
    entered: list[str] = []

    class Guard:
        def __enter__(self):
            entered.append("enter")
            return self

        def __exit__(self, *_args):
            entered.append("exit")

    monkeypatch.setattr(
        lifecycle,
        "account_lifecycle_commit_guard",
        lambda: Guard(),
    )
    monkeypatch.setattr(
        account_cleanup,
        "_account_cleanup_plugins",
        lambda: SimpleNamespace(
            plugin_api=SimpleNamespace(
                delete_owner_account_data=lambda owner_id, *, account_generation: {
                    "owner_id": owner_id,
                    "account_generation": account_generation,
                }
            )
        ),
    )
    monkeypatch.setattr(
        account_cleanup,
        "purge_owner_model_configuration",
        lambda owner_id, *, account_generation: {
            "owner_id": owner_id,
            "account_generation": account_generation,
        },
    )
    monkeypatch.setattr(
        account_cleanup,
        "purge_owner_operational_state",
        lambda owner_id, *, account_generation: {
            "owner_id": owner_id,
            "account_generation": account_generation,
        },
    )

    account_cleanup.purge_account_owned_cloud_data(
        "owner", account_generation="generation-1"
    )

    assert entered == ["enter", "exit"]


def test_real_cleanup_resolves_mobile_generation_and_tombstones_empty_pi_stores(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import managed_installations
    from hermes_cli.dashboard_auth import mobile_device_store
    from hermes_services.tool_output_artifacts import EncryptedToolArtifactStore
    from plugins.collaboration.dashboard import plugin_api

    mobile_db = tmp_path / "mobile-auth.db"
    managed_db = tmp_path / "managed-installations.db"
    monkeypatch.setattr(mobile_device_store, "mobile_auth_db_path", lambda: mobile_db)
    mobile = mobile_device_store.MobileDeviceStore()
    deletion = mobile.begin_account_deletion("owner", "owner-scope")
    generation = deletion["account_generation"]

    monkeypatch.setattr(plugin_api, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        plugin_api,
        "load_single_state",
        lambda **_kwargs: {"conversations": []},
    )
    monkeypatch.setattr(plugin_api, "save_single_state", lambda _state: None)
    monkeypatch.setattr(plugin_api, "load_state", lambda: {"rooms": []})
    monkeypatch.setattr(plugin_api, "save_state", lambda _state: None)
    monkeypatch.setattr(
        plugin_api,
        "_file_library",
        lambda: SimpleNamespace(
            delete_owner=lambda _owner, *, account_generation: (
                {"files": 0}
                if account_generation == generation
                else pytest.fail("cleanup used the wrong account generation")
            )
        ),
    )
    monkeypatch.setattr(
        managed_installations,
        "managed_installations_db_path",
        lambda: managed_db,
    )
    monkeypatch.setattr(managed_installations, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: tmp_path / "profiles" / name,
    )
    monkeypatch.setattr(
        account_cleanup,
        "purge_owner_model_configuration",
        lambda _owner, *, account_generation: {
            "profiles_changed": 0,
            "account_generation": account_generation,
        },
    )
    monkeypatch.setattr(
        account_cleanup,
        "purge_owner_operational_state",
        lambda _owner, *, account_generation: {
            "workflows": 0,
            "account_generation": account_generation,
        },
    )

    result = account_cleanup.purge_account_owned_cloud_data("owner")

    assert result["collaboration"]["tool_output_artifacts"] == {"artifacts": 0}
    assert result["collaboration"]["managed_resources"] == {
        "resources": 0,
        "events": 0,
        "operations": 0,
    }
    artifact_store = EncryptedToolArtifactStore(tmp_path)
    with artifact_store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tool_output_owner_tombstones "
            "WHERE owner_id=? AND account_generation=?",
            ("owner", generation),
        ).fetchone()[0] == 1
    with managed_installations.closing(managed_installations._connect(managed_db)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM managed_owner_tombstones "
            "WHERE owner_id=? AND account_generation=?",
            ("owner", generation),
        ).fetchone()[0] == 1
