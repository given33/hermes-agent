from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
DBB3 = ROOT / "deploy" / "dbb3"
PC = ROOT / "deploy" / "pc"
PUBLIC = ROOT / "deploy" / "public"


def _posix_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    wsl = shutil.which("wsl.exe")
    if not wsl:
        raise RuntimeError("WSL is required for deployment script tests")
    return subprocess.check_output(
        [wsl, "wslpath", "-a", str(path).replace("\\", "/")],
        text=True,
    ).strip()


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _load_connector():
    path = DBB3 / "dbb3_cloud_connector.py"
    spec = importlib.util.spec_from_file_location("dbb3_cloud_connector_deploy_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_deployment_shell_scripts_have_valid_syntax():
    bash = shutil.which("bash")
    if not bash:
        return
    for path in (
        DBB3 / "install-dbb3-cloud-connector-user.sh",
        PC / "install-pc-cloud-connector-user.sh",
        PC / "run-pc-cloud-connector.sh",
        PUBLIC / "install-collaboration-backend.sh",
        PUBLIC / "test-install-collaboration-backend.sh",
        PUBLIC / "deploy-collaboration-backend.sh",
        PUBLIC / "configure-connector-credential.sh",
    ):
        if os.name == "nt":
            wsl = shutil.which("wsl.exe")
            if not wsl:
                continue
            # wsl.exe can consume backslashes while forwarding argv to Linux.
            # wslpath accepts forward-slash Windows paths without ambiguity.
            windows_path = str(path).replace("\\", "/")
            posix_path = subprocess.check_output(
                [wsl, "wslpath", "-a", windows_path],
                text=True,
            ).strip()
            command = [wsl, "bash", "-n", posix_path]
        else:
            command = [bash, "-n", str(path)]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (path, result.stderr)


def test_transactional_installers_serialize_deployments_and_use_unique_backups():
    public = (PUBLIC / "install-collaboration-backend.sh").read_text(encoding="utf-8")
    connector = (DBB3 / "install-dbb3-cloud-connector-user.sh").read_text(encoding="utf-8")

    assert 'flock -n 8 || die "another collaboration deployment is already running"' in public
    assert 'mktemp -d "${backup_root}/collaboration-${version}-${stamp}.XXXXXX"' in public
    assert 'flock -n 8 || die "another connector deployment is already running"' in connector


def test_pc_connector_delegates_complete_runtime_contract(tmp_path):
    layout = tmp_path / "deploy"
    pc = layout / "pc"
    dbb3 = layout / "dbb3"
    pc.mkdir(parents=True)
    dbb3.mkdir(parents=True)
    shutil.copy2(PC / "install-pc-cloud-connector-user.sh", pc)
    shutil.copy2(PC / "pc-cloud-connector.service", pc)
    (dbb3 / "dbb3_cloud_connector.py").write_text(
        "# connector fixture\n",
        encoding="utf-8",
        newline="\n",
    )
    capture = tmp_path / "connector-contract.txt"
    user_home = tmp_path / "user-home"
    hermes_home = tmp_path / "hermes-home"
    user_home.mkdir()
    hermes_home.mkdir()
    _write_executable(
        dbb3 / "install-dbb3-cloud-connector-user.sh",
        """#!/usr/bin/env bash
printf '%s\n' \
  "$DBB3_CONNECTOR_ID" \
  "$DBB3_CONNECTOR_SOURCE_TARGET" \
  "$DBB3_CONNECTOR_UNIT_TEMPLATE" \
  "$HERMES_CONNECTOR_UNIT_NAME" \
  "$HERMES_CONNECTOR_CONFIG_DIR" \
  "$HERMES_CONNECTOR_STATE_DIR" \
  "$HERMES_CONNECTOR_HERMES_HOME" >"$PC_TEST_CAPTURE"
""",
    )
    fake_bin = tmp_path / "bin"
    _write_executable(
        fake_bin / "id",
        """#!/usr/bin/env bash
if [[ "${1:-}" == "-u" ]]; then printf '0\n'; else exit 0; fi
""",
    )
    _write_executable(
        fake_bin / "getent",
        """#!/usr/bin/env bash
printf '%s:x:1000:1000:test:%s:/bin/bash\n' "$2" "$PC_TEST_USER_HOME"
""",
    )
    values = {
        "PATH": f"{_posix_path(fake_bin)}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PC_CONNECTOR_USER": "test-user",
        "PC_CONNECTOR_HERMES_HOME": _posix_path(hermes_home),
        "PC_TEST_CAPTURE": _posix_path(capture),
        "PC_TEST_USER_HOME": _posix_path(user_home),
    }
    command = [
        "env",
        *(f"{key}={value}" for key, value in values.items()),
        "bash",
        _posix_path(pc / "install-pc-cloud-connector-user.sh"),
    ]
    if os.name == "nt":
        command.insert(0, shutil.which("wsl.exe"))
    result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    contract = capture.read_text(encoding="utf-8").splitlines()
    assert contract[0] == "pc-primary"
    assert contract[1] == "/opt/pc-team/pc_cloud_connector.py"
    assert contract[2].endswith("/deploy/pc/pc-cloud-connector.service")
    assert contract[3] == "pc-cloud-connector.service"
    assert contract[4] == f"{_posix_path(user_home)}/.config/pc-team"
    assert contract[5] == f"{_posix_path(user_home)}/.local/state/pc-cloud-connector"
    assert contract[6] == _posix_path(hermes_home)


def test_public_deployer_uploads_the_complete_runtime_snapshot(tmp_path):
    fake_bin = tmp_path / "bin"
    capture = tmp_path / "deploy.log"
    fake_command = """#!/usr/bin/env bash
printf '%s|%s\n' "$(basename "$0")" "$*" >>"$DEPLOY_CAPTURE"
"""
    _write_executable(fake_bin / "ssh", fake_command)
    _write_executable(fake_bin / "scp", fake_command)
    values = {
        "PATH": f"{_posix_path(fake_bin)}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "DEPLOY_CAPTURE": _posix_path(capture),
        "HERMES_PUBLIC_REMOTE": "admin@test-host",
        "HERMES_REPO": _posix_path(ROOT),
    }
    command = [
        "env",
        *(f"{key}={value}" for key, value in values.items()),
        "bash",
        _posix_path(PUBLIC / "deploy-collaboration-backend.sh"),
    ]
    if os.name == "nt":
        command.insert(0, shutil.which("wsl.exe"))
    result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    deployed = capture.read_text(encoding="utf-8")
    for relative in (
        "plugins/collaboration/dashboard/plugin_api.py",
        "plugins/collaboration/dashboard/manifest.json",
        "plugins/collaboration/dashboard/dist/index.js",
        "hermes_cli/cloud_file_library.py",
        "hermes_cli/dashboard_auth/token_auth.py",
        "hermes_cli/dashboard_auth/mobile_device_store.py",
        "hermes_cli/dashboard_auth/mobile_notifications.py",
        "hermes_cli/web_server.py",
        "tui_gateway/server.py",
        "hermes_cli/ios_intelligence.py",
        "hermes_cli/ios_intelligence_config.py",
        "hermes_cli/ios_intelligence_scheduler.py",
        "hermes_cli/ios_intelligence_supervisor.py",
        "hermes_cli/ios_mcp_supervisor.py",
        "hermes_cli/ios_mcp_server.py",
        "plugins/ios-intelligence/dashboard/plugin_api.py",
        "plugins/ios-intelligence/dashboard/manifest.json",
        "hermes_cli/dashboard_auth/__init__.py",
        "hermes_cli/dashboard_auth/owner_mobile.py",
        "hermes_cli/dashboard_auth/registry.py",
        "hermes_cli/profiles.py",
        "plugins/dashboard_auth/basic/__init__.py",
        "tools/mcp_tool.py",
    ):
        assert f"{_posix_path(ROOT)}/{relative}" in deployed


def test_public_installer_rolls_back_and_installs_every_runtime_file():
    harness = PUBLIC / "test-install-collaboration-backend.sh"
    if os.name == "nt":
        wsl = shutil.which("wsl.exe")
        if not wsl:
            return
        command = [wsl, "sudo", "-n", "bash", _posix_path(harness)]
    elif os.geteuid() == 0:
        command = ["bash", str(harness)]
    elif subprocess.run(
        ["sudo", "-n", "true"],
        capture_output=True,
        check=False,
    ).returncode == 0:
        command = ["sudo", "-n", "bash", str(harness)]
    else:
        return

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "public installer transaction test passed" in result.stdout


def test_public_installer_quiesces_state_during_snapshot_and_rollback():
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(encoding="utf-8")
    stop = installer.index('systemctl stop "${service}"', installer.index("trap rollback EXIT"))
    state_backup = installer.index('backup_one "${state_target}"', stop)
    intelligence_backup = installer.index(
        'backup_sqlite "${ios_database_target}"', state_backup
    )
    supervisor_backup = installer.index(
        'backup_sqlite "${ios_supervisor_target}"', intelligence_backup
    )
    first_install = installer.index("install_atomic()", state_backup)
    start = installer.index('systemctl start "${service}"', first_install)

    assert (
        stop
        < state_backup
        < intelligence_backup
        < supervisor_backup
        < first_install
        < start
    )
    rollback = installer[installer.index("rollback() {"):installer.index("restore_one() {")]
    assert rollback.index('systemctl stop "${service}"') < rollback.index("restore_one")
    assert 'restore_sqlite "${backup}/state/ios-intelligence.db"' in rollback
    assert 'restore_sqlite "${backup}/state/ios-mcp-supervisor.db"' in rollback
    assert 'restore_sqlite "${backup}/state/mobile-auth.db"' in rollback
    assert 'backup_sqlite "${mobile_auth_target}" "${backup}/state/mobile-auth.db"' in installer
    assert rollback.index("restore_state") < rollback.index('systemctl start "${service}"')
    assert '/api/plugins/ios-intelligence/health' in installer
    assert '--config "${curl_cfg}"' in installer[installer.index('/api/plugins/ios-intelligence/health') - 160:]
    assert 'healthy_count") == 21' in installer
    assert 'sum(len(item.get("tools") or []) for item in services) == 44' in installer


def test_public_installer_uses_only_a_root_controlled_install_lock():
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(encoding="utf-8")

    assert "install lock directory must be root-owned" in installer
    assert "install lock directory must not be group/world-writable" in installer
    assert '[[ -f "${install_lock}" && ! -L "${install_lock}" ]]' in installer
    assert "install lock file must be root-owned" in installer
    assert 'chmod 0600 "${install_lock}"' in installer


def test_public_installer_validates_the_root_owned_snapshot_it_installs():
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )
    snapshot_copy = installer.index('tar --no-same-owner -C "${snapshot}" -xf -')
    snapshot_check = installer.index('[[ -f "${snapshot}/${relative}"')
    manifest_check = installer.index(
        '"${snapshot}/plugins/collaboration/dashboard/manifest.json"'
    )
    compile_check = installer.index(
        '"${snapshot}/plugins/collaboration/dashboard/plugin_api.py"'
    )
    first_install = installer.index("install_atomic()")

    assert snapshot_copy < snapshot_check < manifest_check < compile_check < first_install
    validation_section = installer[snapshot_check:first_install]
    assert '"${stage_root}/plugins/collaboration/dashboard/manifest.json"' not in validation_section


def test_public_installer_registers_ios_mcps_in_the_service_hermes_home():
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )

    assert (
        'sudo -u "${service_user}" -- env HERMES_HOME="${runtime_home}" \\\n'
        '    "${runtime_python}" -m hermes_cli.ios_mcp_server --install'
    ) in installer
    assert (
        'sudo -u "${service_user}" -- env HERMES_HOME="${runtime_home}" \\\n'
        '    "${runtime_python}" -m hermes_cli.ios_mcp_supervisor --register'
    ) in installer
    assert "AESGCM" in installer
    assert "from agent.plugin_llm import PluginLlm" in installer


def test_public_installer_transactions_mcp_discovery_with_ios_release():
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )
    ios_assets = installer[
        installer.index("ios_optional=("):installer.index(
            'for relative in "${required[@]}"'
        )
    ]

    assert '"tools/mcp_tool.py"' in ios_assets
    assert '"hermes_cli/dashboard_auth/owner_mobile.py"' in ios_assets
    assert '"hermes_cli/dashboard_auth/registry.py"' in ios_assets
    assert '"hermes_cli/profiles.py"' in ios_assets
    assert '"plugins/dashboard_auth/basic/__init__.py"' in ios_assets
    assert '"${snapshot}/tools/mcp_tool.py"' in installer
    assert '"${target_root}/tools"' in installer
    assert '"${backup}/tools"' in installer
    assert (
        'backup_one "${destination}" "${backup}/${relative}"'
    ) in installer
    assert (
        'restore_one "${backup}/${relative}" "${target_root}/${relative}"'
    ) in installer
    assert (
        'install_atomic "${snapshot}/${relative}" "${target_root}/${relative}"'
    ) in installer


def test_dbb3_installer_uses_only_a_root_controlled_install_lock():
    installer = (DBB3 / "install-dbb3-cloud-connector-user.sh").read_text(
        encoding="utf-8"
    )

    assert "install lock directory must be root-owned" in installer
    assert "install lock directory must not be group/world-writable" in installer
    assert '[[ -f "${install_lock}" && ! -L "${install_lock}" ]]' in installer
    assert "install lock file must be root-owned" in installer
    assert 'chmod 0600 "${install_lock}"' in installer


def test_dbb3_user_installer_rolls_back_each_mutating_failure_stage():
    harness = DBB3 / "test-install-dbb3-cloud-connector-user-rollback.sh"
    if os.name == "nt":
        wsl = shutil.which("wsl.exe")
        if not wsl:
            return
        harness_path = subprocess.check_output(
            [wsl, "wslpath", "-a", str(harness).replace("\\", "/")],
            text=True,
        ).strip()
        command = [wsl, "sudo", "-n", "bash", harness_path]
    else:
        if os.geteuid() == 0:
            command = ["bash", str(harness)]
        elif subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True,
            check=False,
        ).returncode == 0:
            command = ["sudo", "-n", "bash", str(harness)]
        else:
            return

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


class _FakeCloud:
    connector_id = "dbb3-primary"

    def __init__(self, run):
        self.run = run
        self.acks = []
        self.statuses = []
        self.uploads = []
        self.failures = []
        self.attachments = []
        self.attachment_bytes = {}
        self.pull_count = 0

    def pull_runs(self, limit=5, lease_seconds=90):
        self.pull_count += 1
        return [self.run]

    def acknowledge_run(self, run, local, lease_seconds=90):
        self.acks.append((run, dict(local)))

    def report_status(self, remote_id, payload):
        self.statuses.append((remote_id, dict(payload)))
        return {"applied": True}

    def fail_run(self, remote_id, payload):
        self.failures.append((remote_id, dict(payload)))
        return {"applied": True}

    def pull_cancellations(self, limit=5, lease_seconds=90):
        return []

    def acknowledge_cancel(self, item, payload):
        raise AssertionError("unexpected cancellation")

    def upload_artifact(self, remote_id, **kwargs):
        self.uploads.append((remote_id, kwargs))
        return {"applied": True, "artifact": {"id": "artifact-1"}}

    def list_run_attachments(self, remote_id):
        return list(self.attachments)

    def download_run_attachment(
        self,
        remote_id,
        file_id,
        *,
        target,
        expected_sha256,
        expected_size,
    ):
        content = self.attachment_bytes[file_id]
        assert len(content) == expected_size
        assert hashlib.sha256(content).hexdigest() == expected_sha256
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target


def test_connector_checkpoint_status_and_raw_artifact_are_idempotent(tmp_path):
    connector = _load_connector()
    artifact = tmp_path / "report.pdf"
    artifact.write_bytes(b"%PDF-connector-test")
    run = {
        "remote_run_id": "run-1",
        "idempotency_key": "idem-1",
        "profile": "dbb3-worker",
        "title": "Build report",
        "objective": "Build a report",
        "max_runtime_seconds": 900,
    }
    fake = _FakeCloud(run)
    show_count = {"value": 0}

    def command_runner(command, timeout=30):
        if command[:3] == ["hermes", "kanban", "create"]:
            assert "--idempotency-key" in command
            assert command[command.index("--idempotency-key") + 1] == "idem-1"
            return 0, json.dumps({"id": "t-root"})
        if command[:3] == ["hermes", "kanban", "show"]:
            show_count["value"] += 1
            return 0, json.dumps(
                {
                    "task": {"id": "t-root", "status": "done", "result": "ready"},
                    "latest_summary": "ready",
                    "events": [
                        {
                            "kind": "completed",
                            "created_at": 100,
                            "payload": {"artifacts": [str(artifact)]},
                        }
                    ],
                    "runs": [],
                }
            )
        raise AssertionError(command)

    state_file = tmp_path / "state" / "checkpoint.json"
    first = connector.DBB3CloudConnector(
        fake,
        command_runner=command_runner,
        state_file=state_file,
        artifact_roots=[tmp_path],
    )
    result = first.sync_once()
    assert result["created"] == 1
    assert result["statuses"] == 1
    assert result["artifacts"] == 1
    assert len(fake.acks) == 1
    assert len(fake.statuses) == 1
    assert fake.statuses[0][1]["checkpoint_cursor"] == 1
    assert fake.statuses[0][1]["status"] == "completed"
    assert fake.statuses[0][1]["terminal"] is True
    assert len(fake.uploads) == 1
    assert fake.uploads[0][1]["sha256"]
    assert fake.uploads[0][1]["path"] == artifact

    second = connector.DBB3CloudConnector(
        fake,
        command_runner=command_runner,
        state_file=state_file,
        artifact_roots=[tmp_path],
    )
    second.sync_once()
    assert len(fake.acks) == 1
    assert len(fake.statuses) == 1
    assert len(fake.uploads) == 1
    assert show_count["value"] == 2


def test_connector_rejects_artifacts_outside_allowlisted_roots(tmp_path):
    connector = _load_connector()
    path = tmp_path / "outside.txt"
    path.write_text("outside", encoding="utf-8")
    assert connector._safe_filename(path) == "outside.txt"
    fake = _FakeCloud({"remote_run_id": "run", "idempotency_key": "key"})
    instance = connector.DBB3CloudConnector(fake, state_file=tmp_path / "state.json", artifact_roots=[tmp_path / "allowed"])
    assert instance._allowed_artifact(str(path)) is None


def test_connector_downloads_and_injects_verified_run_attachments(tmp_path):
    connector = _load_connector()
    content = b"cloud attachment content"
    digest = hashlib.sha256(content).hexdigest()
    run = {
        "remote_run_id": "run-with-attachment",
        "idempotency_key": "idem-attachment",
        "profile": "dbb3-worker",
        "title": "Inspect input",
        "objective": "Inspect the supplied input",
        "attachment_ids": ["file_input"],
    }
    fake = _FakeCloud(run)
    fake.attachments = [
        {
            "id": "file_input",
            "name": "input.txt",
            "sha256": digest,
            "size": len(content),
            "mime_type": "text/plain",
        }
    ]
    fake.attachment_bytes["file_input"] = content
    captured_body = {"value": ""}

    def command_runner(command, timeout=30):
        if command[:3] == ["hermes", "kanban", "create"]:
            captured_body["value"] = command[command.index("--body") + 1]
            return 0, json.dumps({"id": "t-attachment"})
        if command[:3] == ["hermes", "kanban", "show"]:
            return 0, json.dumps(
                {
                    "task": {"id": "t-attachment", "status": "done", "result": "read"},
                    "events": [],
                    "runs": [],
                }
            )
        raise AssertionError(command)

    instance = connector.DBB3CloudConnector(
        fake,
        command_runner=command_runner,
        state_file=tmp_path / "state" / "checkpoint.json",
        artifact_roots=[tmp_path],
    )
    result = instance.sync_once()

    assert result["created"] == 1
    assert "input.txt" in captured_body["value"]
    local_path = next((tmp_path / "state" / "attachments").rglob("*input.txt"))
    assert str(local_path) in captured_body["value"]
    assert local_path.read_bytes() == content


def test_connector_keeps_authoritative_objective_in_utf8_control_file(tmp_path):
    connector = _load_connector()
    run = {
        "remote_run_id": "run-unicode",
        "idempotency_key": "idem-unicode",
        "profile": "dbb3-worker",
        "title": "中文标题",
        "objective": "用户任务：检查中文输入并保留原文",
    }
    fake = _FakeCloud(run)
    captured = {"body": ""}

    def command_runner(command, timeout=30):
        if command[:3] == ["hermes", "kanban", "create"]:
            captured["body"] = command[command.index("--body") + 1]
            return 0, json.dumps({"id": "t-unicode"})
        if command[:3] == ["hermes", "kanban", "show"]:
            return 0, json.dumps({"task": {"status": "done", "result": "ok"}, "events": [], "runs": []})
        raise AssertionError(command)

    instance = connector.DBB3CloudConnector(
        fake,
        command_runner=command_runner,
        state_file=tmp_path / "state" / "checkpoint.json",
        artifact_roots=[tmp_path],
    )
    instance.sync_once()
    assert "Read the authoritative UTF-8 user objective" in captured["body"]
    assert "kanban_complete" in captured["body"]
    assert "kanban_block" in captured["body"]
    objective_path = next((tmp_path / "state" / "attachments").rglob("objective.txt"))
    assert objective_path.read_text(encoding="utf-8") == run["objective"] + "\n"
    assert "中文标题" not in captured["body"]


def test_connector_cancellation_advances_the_server_cursor_and_requires_terminal_ack(tmp_path):
    connector = _load_connector()
    commands = []

    class CancellationCloud:
        connector_id = "dbb3-primary"

        def __init__(self):
            self.responses = [
                {
                    "applied": False,
                    "run": {"status": "running", "checkpoint_cursor": 11},
                },
                {
                    "applied": True,
                    "run": {"status": "cancelled", "checkpoint_cursor": 12},
                },
            ]
            self.payloads = []

        def acknowledge_cancel(self, item, payload):
            self.payloads.append(dict(payload))
            return self.responses.pop(0)

    cloud = CancellationCloud()
    instance = connector.DBB3CloudConnector(
        cloud,
        command_runner=lambda command, timeout=30: commands.append(command)
        or (0, "Cancellation applied"),
        state_file=tmp_path / "state" / "checkpoint.json",
        artifact_roots=[tmp_path],
    )
    state = {
        "version": 1,
        "runs": {
            "remote-cancel": {
                "root_task_id": "task-cancel",
                "checkpoint_cursor": 3,
            }
        },
        "cancellations": {},
    }
    item = {
        "remote_run_id": "remote-cancel",
        "root_task_id": "task-cancel",
        "checkpoint_cursor": 7,
        "reason": "user cancelled",
    }

    assert instance._process_cancellation(item, state) == 0
    assert commands[0] == [
        "hermes",
        "kanban",
        "block",
        "task-cancel",
        "user cancelled",
    ]
    local = state["runs"]["remote-cancel"]
    assert cloud.payloads[0]["checkpoint_cursor"] == 8
    assert local["checkpoint_cursor"] == 11
    assert "cancel_acked" not in local

    assert instance._process_cancellation(item, state) == 1
    assert cloud.payloads[1]["checkpoint_cursor"] == 12
    assert local["checkpoint_cursor"] == 12
    assert local["cancel_acked"] is True
    assert local["status"] == "cancelled"


def test_artifact_run_uses_a_persistent_private_connector_workspace(tmp_path):
    connector = _load_connector()
    run = {
        "remote_run_id": "run-artifact-workspace",
        "idempotency_key": "idem-artifact-workspace",
        "profile": "dbb3-worker",
        "title": "Create deliverable",
        "objective": "Create the requested deliverable",
        "artifact_required": True,
    }
    fake = _FakeCloud(run)
    captured = {"workspace": None, "artifact": None}

    def command_runner(command, timeout=30):
        if command[:3] == ["hermes", "kanban", "create"]:
            workspace_arg = command[command.index("--workspace") + 1]
            assert workspace_arg.startswith("dir:")
            workspace = Path(workspace_arg.removeprefix("dir:"))
            assert workspace.is_dir()
            artifact = workspace / "cloud-report.txt"
            artifact.write_text("cloud artifact", encoding="utf-8")
            captured.update({"workspace": workspace, "artifact": artifact})
            return 0, json.dumps({"id": "t-artifact-workspace"})
        if command[:3] == ["hermes", "kanban", "show"]:
            return 0, json.dumps(
                {
                    "task": {
                        "id": "t-artifact-workspace",
                        "status": "done",
                        "result": "created",
                    },
                    "events": [
                        {
                            "kind": "completed",
                            "payload": {"artifacts": [str(captured["artifact"])]},
                        }
                    ],
                    "runs": [],
                }
            )
        raise AssertionError(command)

    state_file = tmp_path / "state" / "checkpoint.json"
    instance = connector.DBB3CloudConnector(
        fake,
        command_runner=command_runner,
        state_file=state_file,
        artifact_roots=[tmp_path / "not-the-private-root"],
    )
    result = instance.sync_once()

    assert result["artifacts"] == 1
    assert captured["workspace"].is_relative_to(
        state_file.parent / "attachments" / run["remote_run_id"]
    )
    assert captured["artifact"].is_file()
    assert fake.uploads[0][1]["path"] == captured["artifact"]


def test_connector_polls_acked_local_root_without_waiting_for_cloud_repull(tmp_path):
    connector = _load_connector()
    run = {
        "remote_run_id": "run-local-poll",
        "idempotency_key": "idem-local-poll",
        "profile": "dbb3-worker",
        "title": "Poll local task",
        "objective": "Finish quickly",
    }
    fake = _FakeCloud(run)
    pulls = [[run], []]
    fake.pull_runs = lambda limit=5, lease_seconds=90: pulls.pop(0)
    show_count = {"value": 0}

    def command_runner(command, timeout=30):
        if command[:3] == ["hermes", "kanban", "create"]:
            return 0, json.dumps({"id": "t-local-poll"})
        if command[:3] == ["hermes", "kanban", "show"]:
            show_count["value"] += 1
            status = "running" if show_count["value"] == 1 else "done"
            return 0, json.dumps(
                {
                    "task": {
                        "id": "t-local-poll",
                        "status": status,
                        "result": "finished" if status == "done" else "",
                    },
                    "events": [],
                    "runs": [],
                }
            )
        raise AssertionError(command)

    instance = connector.DBB3CloudConnector(
        fake,
        command_runner=command_runner,
        state_file=tmp_path / "state" / "checkpoint.json",
        artifact_roots=[tmp_path],
    )

    first = instance.sync_once()
    second = instance.sync_once()

    assert first["statuses"] == 1
    assert second["statuses"] == 1
    assert show_count["value"] == 2
    assert fake.statuses[-1][1]["status"] == "completed"
    assert fake.statuses[-1][1]["terminal"] is True


def test_connector_client_uses_the_connector_route_prefix():
    connector = _load_connector()
    client = connector.CloudRelayClient("https://example.test/api/plugins/collaboration", "x" * 64)
    calls = []

    def request(path, **kwargs):
        calls.append(path)
        if path == "/connector/health":
            return {"ok": True, "contract_version": 1}
        if path.endswith("/pull"):
            return {"runs": [], "cancellations": []}
        return {}

    client._request = request
    client.probe()
    client.pull_runs()
    client.acknowledge_run({"remote_run_id": "r"}, {})
    client.report_status("r", {"status": "running"})
    client.fail_run("r", {"status": "failed"})
    client.pull_cancellations()
    client.acknowledge_cancel({"remote_run_id": "r"}, {})
    client.list_run_attachments("r")
    assert calls == [
        "/connector/health",
        "/connector/runs/pull",
        "/connector/runs/r/ack",
        "/connector/runs/r/status",
        "/connector/runs/r/fail",
        "/connector/cancellations/pull",
        "/connector/runs/r/cancel-ack",
        "/connector/runs/r/attachments",
    ]


def test_connector_client_sends_bound_connector_identity_header():
    connector = _load_connector()
    client = connector.CloudRelayClient(
        "https://example.test/api/plugins/collaboration",
        "x" * 64,
        connector_id="dbb3-primary",
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok":true,"contract_version":1}'

    with mock.patch.object(connector.urllib.request, "urlopen", return_value=Response()) as urlopen:
        client.probe()

    request = urlopen.call_args.args[0]
    assert request.get_header("X-connector-id") == "dbb3-primary"


def test_terminal_artifact_waits_for_transient_upload_then_reports(tmp_path):
    connector = _load_connector()
    artifact = tmp_path / "deliverable.txt"
    artifact.write_text("ready", encoding="utf-8")
    run = {
        "remote_run_id": "run-flaky-artifact",
        "idempotency_key": "idem-flaky-artifact",
        "profile": "dbb3-worker",
        "title": "Create deliverable",
        "objective": "Create a deliverable",
        "artifact_required": True,
    }

    class FlakyCloud(_FakeCloud):
        def __init__(self, value):
            super().__init__(value)
            self.attempts = 0

        def upload_artifact(self, remote_id, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise connector.urllib.error.URLError("temporary")
            return super().upload_artifact(remote_id, **kwargs)

    cloud = FlakyCloud(run)

    def command_runner(command, timeout=30):
        if command[:3] == ["hermes", "kanban", "create"]:
            return 0, json.dumps({"id": "task-flaky-artifact"})
        if command[:3] == ["hermes", "kanban", "show"]:
            return 0, json.dumps(
                {
                    "task": {"status": "done", "result": "ready"},
                    "events": [{"kind": "completed", "payload": {"artifacts": [str(artifact)]}}],
                    "comments": [],
                    "runs": [],
                }
            )
        raise AssertionError(command)

    instance = connector.DBB3CloudConnector(
        cloud,
        command_runner=command_runner,
        state_file=tmp_path / "state" / "checkpoint.json",
        artifact_roots=[tmp_path],
    )

    first = instance.sync_once()
    assert first["statuses"] == 0
    assert cloud.statuses == []
    second = instance.sync_once()
    assert second["artifacts"] == 1
    assert len(cloud.statuses) == 1
    assert cloud.statuses[0][1]["status"] == "completed"


def test_missing_required_artifact_reports_failed_terminal(tmp_path):
    connector = _load_connector()
    run = {
        "remote_run_id": "run-missing-artifact",
        "idempotency_key": "idem-missing-artifact",
        "profile": "dbb3-worker",
        "title": "Create deliverable",
        "objective": "Create a deliverable",
        "artifact_required": True,
    }
    cloud = _FakeCloud(run)

    def command_runner(command, timeout=30):
        if command[:3] == ["hermes", "kanban", "create"]:
            return 0, json.dumps({"id": "task-missing-artifact"})
        if command[:3] == ["hermes", "kanban", "show"]:
            return 0, json.dumps(
                {
                    "task": {"status": "done", "result": "claimed completion"},
                    "events": [],
                    "comments": [],
                    "runs": [],
                }
            )
        raise AssertionError(command)

    instance = connector.DBB3CloudConnector(
        cloud,
        command_runner=command_runner,
        state_file=tmp_path / "state" / "checkpoint.json",
        artifact_roots=[tmp_path],
    )
    instance.sync_once()

    assert cloud.statuses[0][1]["status"] == "failed"
    assert cloud.statuses[0][1]["terminal"] is True
    assert "Required artifact" in cloud.statuses[0][1]["error"]


def test_connector_cancellation_accepts_terminal_race_and_legacy_conflict(tmp_path):
    connector = _load_connector()

    class TerminalCloud:
        connector_id = "dbb3-primary"

        def __init__(self):
            self.legacy = False

        def acknowledge_cancel(self, _item, _payload):
            if self.legacy:
                raise connector.ConnectorContractError(409, "already terminal")
            return {
                "applied": False,
                "run": {"status": "completed", "checkpoint_cursor": 9},
            }

    cloud = TerminalCloud()
    instance = connector.DBB3CloudConnector(
        cloud,
        command_runner=lambda _command, timeout=30: (0, "local cancellation applied"),
        state_file=tmp_path / "state" / "checkpoint.json",
        artifact_roots=[tmp_path],
    )
    state = {
        "version": 1,
        "runs": {"race": {"root_task_id": "root", "checkpoint_cursor": 3}},
        "cancellations": {},
    }
    item = {"remote_run_id": "race", "root_task_id": "root", "checkpoint_cursor": 4}

    assert instance._process_cancellation(item, state) == 1
    assert state["runs"]["race"]["status"] == "completed"
    assert state["runs"]["race"]["cancel_acked"] is True

    cloud.legacy = True
    state["runs"]["legacy"] = {"root_task_id": "root-legacy", "checkpoint_cursor": 1}
    legacy = {"remote_run_id": "legacy", "root_task_id": "root-legacy", "checkpoint_cursor": 1}
    assert instance._process_cancellation(legacy, state) == 1
    assert state["runs"]["legacy"]["cancel_acked"] is True


def test_compact_status_keeps_rich_activity_fields_and_redacts_credentials(tmp_path):
    connector = _load_connector()
    cloud = _FakeCloud({"remote_run_id": "rich", "idempotency_key": "rich-key"})
    instance = connector.DBB3CloudConnector(
        cloud,
        state_file=tmp_path / "state.json",
        artifact_roots=[tmp_path],
    )
    detail = {
        "task": {"status": "running", "model_override": "MODEL"},
        "events": [
            {
                "kind": "tool_completed",
                "created_at": 10,
                "run_id": 7,
                "payload": {
                    "name": "terminal",
                    "tool_name": "terminal",
                    "args": {"command": "curl -H 'Authorization: Bearer super-secret-token' TARGET"},
                    "result": "Set-Cookie: session=private-cookie",
                    "status": "completed",
                    "model": "MODEL",
                    "provider": "PROVIDER",
                    "started_at": 10,
                    "ended_at": 11,
                },
            }
        ],
        "comments": [{"author": "worker", "body": "阶段完成", "created_at": 12}],
        "runs": [
            {
                "id": 7,
                "profile": "dbb3-worker",
                "status": "completed",
                "summary": "done",
                "metadata": {"api_key": "private-api-key", "provider": "PROVIDER"},
                "started_at": 10,
                "ended_at": 13,
            }
        ],
    }

    payload, _paths = instance._compact_status(detail, {"checkpoint_cursor": 0})
    encoded = json.dumps(payload, ensure_ascii=False)
    assert len(payload["activities"]) == 3
    tool = next(item for item in payload["activities"] if item["kind"] == "tool_completed")
    assert tool["tool_name"] == "terminal"
    assert tool["status"] == "completed"
    assert tool["model"] == "MODEL"
    assert tool["provider"] == "PROVIDER"
    assert tool["duration_ms"] == 1000
    assert "[REDACTED]" in encoded
    assert "super-secret-token" not in encoded
    assert "private-cookie" not in encoded
    assert "private-api-key" not in encoded


def test_official_session_export_projects_reasoning_tools_model_and_timing():
    connector = _load_connector()
    record = {
        "id": "20260717_140414_2b6bcf",
        "model": "hybrid-56",
        "billing_provider": "moa",
        "started_at": 100.0,
        "ended_at": None,
        "message_count": 5,
        "tool_call_count": 1,
        "api_call_count": 2,
        "system_prompt": "must never leave the device",
        "messages": [
            {
                "id": 1,
                "role": "user",
                "content": "private user objective",
                "timestamp": 100.1,
            },
            {
                "id": 2,
                "role": "assistant",
                "content": "正在验证命令。",
                "reasoning_content": "先读取状态，再执行只读检查。",
                "timestamp": 101.0,
                "tool_calls": [
                    {
                        "id": "call-terminal",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps(
                                {
                                    "command": (
                                        "curl -H 'Authorization: Bearer "
                                        "session-secret-value' TARGET"
                                    )
                                }
                            ),
                        },
                    }
                ],
            },
            {
                "id": 3,
                "role": "tool",
                "tool_name": "terminal",
                "tool_call_id": "call-terminal",
                "content": json.dumps(
                    {
                        "output": "ok",
                        "exit_code": 0,
                        "error": None,
                        "api_key": "tool-result-secret",
                    }
                ),
                "timestamp": 102.5,
            },
            {
                "id": 4,
                "role": "assistant",
                "content": "验证完成。",
                "timestamp": 103.0,
            },
        ],
    }

    activities = connector._session_record_activities(
        record,
        profile="pc-worker",
        terminal=True,
    )
    encoded = json.dumps(activities, ensure_ascii=False)
    summary = next(item for item in activities if item["kind"] == "session")
    reasoning = next(item for item in activities if item["kind"] == "reasoning")
    tool = next(item for item in activities if item["tool_name"] == "terminal")

    assert summary["status"] == "completed"
    assert summary["duration_ms"] == 3000
    assert summary["model"] == "hybrid-56"
    assert summary["provider"] == "moa"
    assert reasoning["category"] == "reasoning"
    assert tool["status"] == "completed"
    assert tool["duration_ms"] == 1500
    assert tool["model"] == "hybrid-56"
    assert "[REDACTED]" in encoded
    assert "session-secret-value" not in encoded
    assert "tool-result-secret" not in encoded
    assert "must never leave the device" not in encoded
    assert "private user objective" not in encoded
    for raw_secret in (
        "OPENAI_API_KEY=private-openai-key",
        "DATABASE_PASSWORD='private-database-password'",
        "TOKEN: private-token-value",
        'password="private secret password"',
        "credential='private credential phrase'",
    ):
        redacted = connector._redact_sensitive(raw_secret)
        assert "private-" not in redacted
        assert "private secret password" not in redacted
        assert "private credential phrase" not in redacted
        assert "[REDACTED]" in redacted


def test_compact_status_uses_profile_scoped_official_session_export(tmp_path):
    connector = _load_connector()
    cloud = _FakeCloud({"remote_run_id": "session-run", "idempotency_key": "key"})
    session_id = "20260717_140414_2b6bcf"
    record = {
        "id": session_id,
        "model": "hybrid-56",
        "billing_provider": "moa",
        "started_at": 100.0,
        "ended_at": 104.0,
        "message_count": 2,
        "tool_call_count": 0,
        "messages": [
            {"id": 1, "role": "user", "content": "work task", "timestamp": 100.0},
            {"id": 2, "role": "assistant", "content": "done", "timestamp": 104.0},
        ],
    }
    commands = []

    def command_runner(command, timeout=30):
        commands.append((command, timeout))
        if command[3:5] == ["sessions", "export"]:
            return 0, json.dumps(record, ensure_ascii=False)
        raise AssertionError(command)

    instance = connector.DBB3CloudConnector(
        cloud,
        command_runner=command_runner,
        state_file=tmp_path / "state.json",
        artifact_roots=[tmp_path],
    )
    local = {
        "remote_run_id": "session-run",
        "root_task_id": "task-session",
        "profile": "pc-worker",
        "checkpoint_cursor": 0,
    }
    detail = {
        "task": {
            "id": "task-session",
            "status": "done",
            "workspace_path": "/tmp/task-session",
        },
        "events": [],
        "comments": [],
        "runs": [
            {
                "id": 1,
                "profile": "pc-worker",
                "status": "done",
                "metadata": {"worker_session_id": session_id},
            }
        ],
    }

    payload, _paths = instance._compact_status(detail, local)

    assert commands == [
        (
            [
                "hermes",
                "-p",
                "pc-worker",
                "sessions",
                "export",
                "-",
                "--format",
                "jsonl",
                "--session-id",
                session_id,
                "--redact",
            ],
            30,
        )
    ]
    assert payload["session_id"] == session_id
    assert payload["actual_model"] == "hybrid-56"
    assert payload["actual_provider"] == "moa"
    assert any(item["kind"] == "message" for item in payload["activities"])


def test_running_session_is_discovered_from_unique_task_workspace(tmp_path):
    connector = _load_connector()
    cloud = _FakeCloud({"remote_run_id": "live-run", "idempotency_key": "key"})
    session_id = "20260717_140414_2b6bcf"
    commands = []
    record = {
        "id": session_id,
        "model": "MODEL",
        "billing_provider": "PROVIDER",
        "started_at": 100.0,
        "message_count": 1,
        "tool_call_count": 0,
        "messages": [],
    }

    def command_runner(command, timeout=30):
        commands.append(command)
        if command[3:5] == ["sessions", "list"]:
            return 0, (
                "Preview Workspace Last Active Src ID\n"
                f"work kanban task task-live task-live now cli {session_id}"
            )
        if command[3:5] == ["sessions", "export"]:
            return 0, json.dumps(record)
        raise AssertionError(command)

    instance = connector.DBB3CloudConnector(
        cloud,
        command_runner=command_runner,
        state_file=tmp_path / "state.json",
        artifact_roots=[tmp_path],
    )
    local = {
        "remote_run_id": "live-run",
        "root_task_id": "task-live",
        "profile": "pc-worker",
        "checkpoint_cursor": 0,
    }
    detail = {
        "task": {
            "id": "task-live",
            "status": "running",
            "workspace_path": "/tmp/task-live",
        },
        "events": [],
        "comments": [],
        "runs": [],
    }

    payload, _paths = instance._compact_status(detail, local)

    assert local["worker_session_id"] == session_id
    assert commands[0][3:5] == ["sessions", "list"]
    assert commands[1][3:5] == ["sessions", "export"]
    assert payload["actual_model"] == "MODEL"


def test_session_discovery_falls_back_for_older_hermes_cli(tmp_path):
    connector = _load_connector()
    cloud = _FakeCloud({"remote_run_id": "legacy-cli", "idempotency_key": "key"})
    session_id = "20260717_152220_ecec4f"
    commands = []

    def command_runner(command, timeout=30):
        commands.append(command)
        if "--workspace" in command:
            return 2, "unrecognized arguments: --workspace"
        return 0, f"work kanban task task-legacy scratch now cli {session_id}"

    instance = connector.DBB3CloudConnector(
        cloud,
        command_runner=command_runner,
        state_file=tmp_path / "state.json",
        artifact_roots=[tmp_path],
    )
    local = {
        "remote_run_id": "legacy-cli",
        "root_task_id": "task-legacy",
        "profile": "reviewer",
    }
    detail = {
        "task": {
            "id": "task-legacy",
            "workspace_path": "/tmp/task-legacy",
        },
        "runs": [],
    }

    discovered = instance._discover_session_id(detail, local)

    assert discovered == session_id
    assert commands[0][-2:] == ["--workspace", "/tmp/task-legacy"]
    assert "--workspace" not in commands[1]
