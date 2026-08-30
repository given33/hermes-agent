import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_hk_recovery_has_one_fixed_worker_action():
    config = json.loads(_read("deploy/recovery/managed-nodes.hk.json"))
    receiver = config["recovery_receiver"]
    assert config["nodes"] == []
    assert receiver == {
        "node_id": "hk",
        "token_file": "/etc/hk-team/recovery_token",
        "command": ["/usr/local/sbin/hermes-recover-hk"],
        "state_file": "/var/lib/hermes-hk-recovery/receiver-state.json",
    }

    command = _read("deploy/recovery/recover-hk.sh")
    assert "systemctl restart hermes-fabric-update.timer" in command
    assert "systemctl enable" not in command
    assert "systemctl --user restart hk-cloud-connector.service" in command
    for forbidden in (
        "hermes-gateway",
        "dashboard",
        "supervisor",
        "reviewer",
        "dispatcher",
        "pc-cloud-connector",
        "dbb3-cloud-connector",
    ):
        assert forbidden not in command.lower()


def test_hk_recovery_receiver_and_tunnel_use_fixed_runtime_and_ports():
    receiver = _read("deploy/recovery/hermes-hk-managed-node-recovery.service")
    tunnel = _read("deploy/recovery/hermes-hk-managed-node-recovery-tunnel.service")
    assert "/opt/hk-team/hermes-agent/.venv/bin/python" in receiver
    assert "--host 127.0.0.1 --port 9121" in receiver
    assert "--config /etc/hk-team/managed-nodes.json" in receiver
    assert "User=root" in receiver
    assert "ReadWritePaths=/var/lib/hermes-hk-recovery" in receiver
    assert "/etc/systemd/system" not in receiver

    fabric_installer = _read("deploy/automation/install-fabric-auto-update.sh")
    assert "systemctl enable --now hermes-fabric-update.timer" in fabric_installer
    assert "User=hermes" in tunnel
    assert "StrictHostKeyChecking=yes" in tunnel
    assert "ExitOnForwardFailure=yes" in tunnel
    assert "-R 127.0.0.1:19124:127.0.0.1:9121" in tunnel
    assert "admin@10.66.0.1" in tunnel


def test_public_hk_recovery_route_uses_independent_token_and_loopback_tunnel():
    managed = json.loads(_read("deploy/public/managed-nodes.server.json"))["nodes"][0]
    assert managed["recovery_urls"]["hk"].endswith("/_hermes/recovery/hk")
    assert managed["recovery_token_files"]["hk"] == (
        "/etc/hermes-agent/hk-recovery-token"
    )

    nginx = _read("deploy/public/nginx-daxueshenmai.top.conf")
    location = nginx.split("location = /_hermes/recovery/hk {", 1)[1].split(
        "location = /_hermes/installations/dbb3 {", 1
    )[0]
    assert "proxy_pass http://127.0.0.1:19124/recover;" in location
    assert "limit_except POST" in location
    assert "proxy_set_header X-DBB3-Token $http_x_dbb3_token;" in location

    sshd = _read("deploy/recovery/sshd-hermes-recovery.conf")
    assert "PermitListen" in sshd
    assert "127.0.0.1:19124" in sshd


def test_fabric_updater_installs_and_rolls_back_hk_recovery_transaction():
    updater = _read("deploy/automation/update-fabric-node.sh")
    installer = "deploy/recovery/install-hk-managed-recovery.sh"
    for asset in (
        installer,
        "deploy/recovery/hermes-hk-managed-node-recovery.service",
        "deploy/recovery/hermes-hk-managed-node-recovery-tunnel.service",
        "deploy/recovery/managed-nodes.hk.json",
        "deploy/recovery/recover-hk.sh",
    ):
        assert f'"{asset}"' in updater
    assert '"--handle-file=${receiver_handle}"' in updater
    assert '"--rollback-backup=${receiver_backup}"' in updater


def test_hk_github_deploy_verifies_recovery_services():
    workflow = _read(".github/workflows/deploy-three-endpoints.yml")
    assert "hermes-hk-managed-node-recovery.service" in workflow
    assert "hermes-hk-managed-node-recovery-tunnel.service" in workflow
