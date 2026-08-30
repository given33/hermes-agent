import json

import pytest

from hermes_cli import managed_nodes


def _write_config(tmp_path, *, status_value: str, recovery_value: str):
    status_token = tmp_path / "status-token"
    recovery_token = tmp_path / "hk-recovery-token"
    status_token.write_text(status_value + "\n", encoding="utf-8")
    recovery_token.write_text(recovery_value + "\n", encoding="utf-8")
    config_path = tmp_path / "managed-nodes.json"
    config_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "fabric",
                        "status_url": "https://status.example/live",
                        "token_file": str(status_token),
                        "recovery_urls": {
                            "hk": "https://status.example/_hermes/recovery/hk"
                        },
                        "recovery_token_files": {"hk": str(recovery_token)},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return config_path, recovery_token


def test_hk_recovery_route_uses_its_dedicated_credential(tmp_path, monkeypatch):
    config_path, recovery_token = _write_config(
        tmp_path,
        status_value="status-credential-000000000000000000000001",
        recovery_value="hk-recovery-credential-0000000000000000001",
    )
    config = managed_nodes.load_managed_nodes_config(config_path)[0]
    calls = []

    def route(route_config, *, recovery_url, targets, reason, force):
        calls.append((route_config["token_file"], recovery_url, targets))
        return {"state": "recovering", "accepted": True}

    monkeypatch.setattr(managed_nodes, "_request_recovery_route", route)
    outcome = managed_nodes._request_recovery(
        config,
        targets=["hk"],
        reason="worker_channel_offline",
        force=True,
    )

    assert outcome["accepted"] is True
    assert calls == [
        (
            str(recovery_token),
            "https://status.example/_hermes/recovery/hk",
            ["hk"],
        )
    ]


def test_hk_recovery_credential_must_not_reuse_status_credential(tmp_path):
    config_path, _ = _write_config(
        tmp_path,
        status_value="same-credential-0000000000000000000000001",
        recovery_value="same-credential-0000000000000000000000001",
    )

    with pytest.raises(ValueError, match="dedicated hk recovery credential"):
        managed_nodes.load_managed_nodes_config(config_path)
