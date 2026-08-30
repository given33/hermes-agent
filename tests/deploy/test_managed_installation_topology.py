from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from hermes_cli.managed_installations import (
    PROBE_IDENTIFIER,
    accept_managed_installation,
    get_received_managed_installation,
    load_managed_installation_receiver_config,
    require_managed_installation_topology,
    resolve_installation_targets,
)
from hermes_cli.managed_node_recovery_service import RecoveryHTTPServer
from hermes_cli.managed_nodes import load_managed_nodes_config


ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "deploy" / "public"


def _private_token(path: Path, value: str) -> Path:
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _server_topology(tmp_path: Path) -> Path:
    status = _private_token(tmp_path / "status.token", "s" * 48)
    installation = _private_token(tmp_path / "installation.token", "i" * 48)
    payload = json.loads(
        (PUBLIC / "managed-nodes.server.json").read_text(encoding="utf-8")
    )
    node = payload["nodes"][0]
    node["token_file"] = str(status)
    node["installation_token_file"] = str(installation)
    node["recovery_token_files"]["hk"] = str(
        _private_token(tmp_path / "hk-recovery.token", "h" * 48)
    )
    path = tmp_path / "managed-nodes.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _receiver_config(tmp_path: Path, *, node_id: str = "dbb3") -> tuple[Path, str]:
    token = "receiver-token-" + "x" * 48
    token_path = _private_token(tmp_path / "receiver.token", token)
    payload = {
        "installation_receiver": {
            "node_id": node_id,
            "token_file": str(token_path),
            "state_file": str(tmp_path / "receiver.db"),
            "project_root": str(tmp_path / "projects"),
        }
    }
    path = tmp_path / "receiver.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, token


def _request_json(request: Request) -> tuple[int, dict]:
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))
    with closing(response):
        return response.status, json.loads(response.read().decode("utf-8"))


def test_product_policy_resolves_skill_mcp_and_project_targets():
    assert resolve_installation_targets("skill") == ["server", "dbb3", "wsl"]
    assert resolve_installation_targets("project") == ["dbb3", "wsl"]
    assert resolve_installation_targets("mcp", locality="portable") == [
        "server",
        "dbb3",
        "wsl",
    ]
    assert resolve_installation_targets("mcp", locality="workers") == [
        "dbb3",
        "wsl",
    ]
    with pytest.raises(ValueError, match="explicit targets"):
        resolve_installation_targets("mcp", locality="node")


def test_public_topology_loads_both_authenticated_installation_routes(tmp_path):
    config_path = _server_topology(tmp_path)
    nodes = load_managed_nodes_config(config_path)

    assert len(nodes) == 1
    node = nodes[0]
    assert set(node["installation_urls"]) == {"dbb3", "wsl"}
    assert node["installation_token_file"] != node["token_file"]
    require_managed_installation_topology(
        ["server", "dbb3", "wsl"],
        config_path=config_path,
    )


def test_public_topology_rejects_reused_status_and_installation_secret(tmp_path):
    config_path = _server_topology(tmp_path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    node = payload["nodes"][0]
    Path(node["installation_token_file"]).write_text("s" * 48 + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="different status and installation"):
        load_managed_nodes_config(config_path)


def test_receiver_probe_is_persisted_and_idempotent(tmp_path):
    config_path, token = _receiver_config(tmp_path)
    config = load_managed_installation_receiver_config(config_path)
    assert config is not None
    assert config["node_id"] == "dbb3"

    payload = {
        "id": "mi-probe-persisted",
        "request_id": "mi-probe-persisted",
        "node_id": "dbb3",
        "kind": "probe",
        "identifier": PROBE_IDENTIFIER,
        "probe": True,
    }
    first = accept_managed_installation(payload, token, config_path)
    second = accept_managed_installation(payload, token, config_path)
    persisted = get_received_managed_installation(
        "mi-probe-persisted",
        token,
        config_path,
    )

    assert first == second
    assert persisted["state"] == "completed"
    assert persisted["detail"] == {
        "probe": True,
        "persisted": True,
        "node_id": "dbb3",
    }


def test_receiver_http_health_and_probe_require_dedicated_credential(tmp_path):
    config_path, token = _receiver_config(tmp_path, node_id="wsl")
    server = RecoveryHTTPServer(("127.0.0.1", 0), config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, body = _request_json(Request(f"{base}/health"))
        assert (status, body["error"]) == (401, "invalid_credential")

        status, body = _request_json(
            Request(f"{base}/health", headers={"X-DBB3-Token": token})
        )
        assert status == 200
        assert body == {
            "ok": True,
            "node_id": "wsl",
            "recovery": False,
            "installations": True,
        }

        operation_id = "mi-http-probe"
        payload = json.dumps(
            {
                "id": operation_id,
                "request_id": operation_id,
                "node_id": "wsl",
                "kind": "probe",
                "identifier": PROBE_IDENTIFIER,
                "probe": True,
            }
        ).encode("utf-8")
        status, accepted = _request_json(
            Request(
                f"{base}/installations",
                data=payload,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-DBB3-Token": token,
                },
            )
        )
        assert status == 202
        assert accepted["accepted"] is True

        status, persisted = _request_json(
            Request(
                f"{base}/installations/{operation_id}",
                headers={"X-DBB3-Token": token},
            )
        )
        assert status == 200
        assert persisted["state"] == "completed"
        assert persisted["detail"]["persisted"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_receiver_health_exposes_only_valid_node_release_identity(tmp_path):
    config_path, token = _receiver_config(tmp_path, node_id="wsl")
    evidence_path = tmp_path / "fabric-release.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["installation_receiver"]["release_evidence_file"] = str(evidence_path)
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    commit = "a" * 40
    evidence_path.write_text(
        json.dumps({
            "schema": "hermes.fabric-release.v1",
            "node_id": "wsl",
            "commit": commit,
            "version": "1.2.3",
        }),
        encoding="utf-8",
    )

    server = RecoveryHTTPServer(("127.0.0.1", 0), config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, body = _request_json(
            Request(f"{base}/health", headers={"X-DBB3-Token": token})
        )
        assert status == 200
        assert body["release"] == {"commit": commit, "version": "1.2.3"}

        evidence_path.write_text('{"schema":"wrong"}', encoding="utf-8")
        status, body = _request_json(
            Request(f"{base}/health", headers={"X-DBB3-Token": token})
        )
        assert status == 503
        assert body == {"error": "release evidence schema is invalid"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
