from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[2]
DBB3 = ROOT / "deploy" / "dbb3"
PC = ROOT / "deploy" / "pc"
PUBLIC = ROOT / "deploy" / "public"
RECOVERY = ROOT / "deploy" / "recovery"
AUTOMATION = ROOT / "deploy" / "automation"
UPSTREAM_REPORT = ROOT / "scripts" / "upstream_change_report.py"
UPSTREAM_WORKFLOW = ROOT / ".github" / "workflows" / "upstream-sync.yml"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy-three-endpoints.yml"
SITE_WORKFLOW = ROOT / ".github" / "workflows" / "deploy-site.yml"


def _posix_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    wsl = shutil.which("wsl.exe")
    if not wsl:
        pytest.skip("WSL is required for deployment script tests")
    try:
        result = subprocess.run(
            [wsl, "wslpath", "-a", str(path).replace("\\", "/")],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"WSL is unavailable: {exc}")
    stdout = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode != 0 or not stdout:
        pytest.skip("WSL service is unavailable for deployment script tests")
    return stdout


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


def _load_upstream_report():
    spec = importlib.util.spec_from_file_location(
        "upstream_change_report_test", UPSTREAM_REPORT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_upstream_report_flags_product_ios_and_deployment_overlap():
    module = _load_upstream_report()

    def fake_git(*args: str) -> str:
        if args[:1] == ("merge-base",):
            return "base-sha"
        if args[:2] == ("diff", "--name-only"):
            if args[2] == "base-sha..origin/main":
                return "plugins/collaboration/dashboard/plugin_api.py\nlocal-only.py"
            return (
                "plugins/collaboration/dashboard/plugin_api.py\n"
                "gateway/run.py\n.github/workflows/tests.yml\nupstream-only.py"
            )
        if args[:1] == ("log",):
            return "abc123 upstream change"
        raise AssertionError(args)

    with mock.patch.object(module, "git", side_effect=fake_git):
        report = module.build_report("origin/main", "v2026.8.1")

    assert "Direct file overlap: `1`" in report
    assert "`plugins/collaboration/dashboard/plugin_api.py`" in report
    assert "`gateway/run.py`" in report
    assert "`.github/workflows/tests.yml`" in report
    assert "Manual Codex review required before merge" in report


def test_upstream_sync_creates_a_reviewed_pr_without_direct_merge_or_deploy():
    workflow = UPSTREAM_WORKFLOW.read_text(encoding="utf-8")
    assert "cron: '17 18 * * *'" in workflow
    assert "https://github.com/NousResearch/hermes-agent.git" in workflow
    assert 'branch="upstream-sync/$tag"' in workflow
    assert "scripts/upstream_change_report.py" in workflow
    assert "tests/plugins/test_collaboration_dashboard.py" in workflow
    assert "tests/deploy/test_cloud_deployment_assets.py" in workflow
    assert "codex-review-required" in workflow
    assert "@codex review" in workflow
    assert "gh pr create" in workflow
    assert "gh pr merge" not in workflow
    assert "ssh " not in workflow


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
        PUBLIC / "verify-fabric-release.sh",
        RECOVERY / "install-dbb3-managed-installation-receiver.sh",
        RECOVERY / "install-wsl-managed-installation.sh",
        RECOVERY / "configure-main-managed-installation-ssh.sh",
        AUTOMATION / "install-fabric-auto-update.sh",
        AUTOMATION / "update-fabric-node.sh",
        AUTOMATION / "test-update-fabric-node.sh",
    ):
        if os.name == "nt":
            wsl = shutil.which("wsl.exe")
            if not wsl:
                continue
            # `wsl.exe` can be present while the service is unavailable (for
            # example when the current Windows account cannot start a distro).
            # Treat that as an environment limitation instead of reporting a
            # false shell syntax failure with WSL's unsigned exit code.
            try:
                probe = subprocess.run(
                    [wsl, "-e", "true"],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                pytest.skip(f"WSL is unavailable for deployment script tests: {exc}")
            if probe.returncode != 0:
                pytest.skip("WSL service is unavailable for deployment script tests")
            # wsl.exe can consume backslashes while forwarding argv to Linux.
            # wslpath accepts forward-slash Windows paths without ambiguity.
            posix_path = _posix_path(path)
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


def test_three_endpoint_updates_follow_only_a_committed_main_release():
    updater = (AUTOMATION / "update-fabric-node.sh").read_text(encoding="utf-8")
    bootstrap = (AUTOMATION / "install-fabric-auto-update.sh").read_text(
        encoding="utf-8"
    )
    service = (AUTOMATION / "hermes-fabric-update.service").read_text(
        encoding="utf-8"
    )
    timer = (AUTOMATION / "hermes-fabric-update.timer").read_text(encoding="utf-8")
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    deployer = (PUBLIC / "deploy-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )

    assert "connector/deployment-health" in updater
    assert 'payload.get("ok") is True' in updater
    assert 're.fullmatch(r"[0-9a-f]{40}", commit)' in updater
    assert "merge-base --is-ancestor" in updater
    assert "refs/remotes/origin/main" in updater
    archive = updater.index('archive --format=tar "${release_commit}"')
    readable_stage = updater.index('chmod -R a+rX "${stage}"')
    install_dispatch = updater.rindex('case "${role}" in')
    node_install = updater.index(
        'bash "${preflight_root}/deploy/dbb3/install-dbb3-cloud-connector-user.sh"',
        install_dispatch,
    )
    assert archive < readable_stage < node_install
    assert 'preflight_root="${state_root}/preflight.$$"' in updater
    assert '-- "${archive_paths[@]}"' in updater
    for relative in (
        "deploy/automation/update-fabric-node.sh",
        "deploy/automation/hermes-fabric-update.service",
        "deploy/automation/hermes-fabric-update.timer",
        "deploy/dbb3/install-dbb3-cloud-connector-user.sh",
        "deploy/dbb3/dbb3_cloud_connector.py",
        "deploy/dbb3/dbb3-cloud-connector.service",
        "deploy/pc/install-pc-cloud-connector-user.sh",
        "deploy/pc/pc-cloud-connector.service",
        "hermes_cli/__init__.py",
        "hermes_runtime",
        "hermes_services",
        "hermes_constants.py",
        "hermes_secret_compare.py",
        "utils.py",
    ):
        assert f'"{relative}"' in updater
    assert (
        'bash "${stage}/deploy/recovery/install-dbb3-managed-installation-receiver.sh"'
        in updater
    )
    assert 'bash "${stage}/deploy/recovery/install-wsl-managed-installation.sh"' in updater
    assert '"hermes_cli/managed_node_recovery_service.py"' in updater
    assert "hermes.fabric-release.v1" in updater
    updater_refresh = updater.index(
        '"${stage}/deploy/automation/update-fabric-node.sh"'
    )
    deployed_commit = updater.index('mv -f -- "${deployed_file}.new.$$"')
    assert node_install < updater_refresh < deployed_commit
    assert "systemctl daemon-reload" in updater[updater_refresh:deployed_commit]
    assert (
        "systemctl enable --now hermes-fabric-update.timer"
        in updater[updater_refresh:deployed_commit]
    )
    assert '--config "${curl_config}"' in updater
    assert 'Authorization: Bearer $(cat' not in updater
    assert "^[A-Za-z0-9._~+/-]+={0,3}$" in updater
    assert node_install < updater.index(
        'mv -f -- "${deployed_file}.new.$$"'
    )
    assert "systemctl enable --now hermes-fabric-update.timer" in bootstrap
    assert "initial_state=pending" in bootstrap
    assert (
        "install -d -o root -g root -m 0755 /var/lib/hermes-agent-fabric-update"
        in bootstrap
    )
    assert "Persistent=true" in timer
    assert "ProtectSystem=strict" in service
    assert "TimeoutStartSec=5min" in service
    assert "TimeoutStopSec=30s" in service
    assert "KillMode=control-group" in service
    assert 'git_network_timeout="${HERMES_FABRIC_GIT_TIMEOUT_SECONDS:-90}"' in updater
    assert "run_network_git clone --mirror" in updater
    assert 'run_network_git --git-dir="${mirror}" fetch' in updater
    assert "/var/lib/systemd/linger" in service
    assert "/etc/systemd/system" in service
    assert "/usr/local/lib/hermes-agent" in service
    assert "push:" in workflow
    assert "branches: [main]" in workflow
    assert "release:" in workflow
    assert "types: [published]" in workflow
    assert "needs: verify" in workflow
    assert "actions: read" in workflow
    assert "Wait for the complete CI gate" in workflow
    assert "--workflow ci.yml" in workflow
    assert "workflow_run:" not in workflow
    assert "tests/hermes_cli/test_config_read_guard.py" in workflow
    assert "tests/test_sqlite_wal_reset_gate.py" in workflow
    assert "tests/hermes_cli/test_ios_intelligence.py" in workflow
    assert "tests/plugins/test_collaboration_ios_contract.py" in workflow
    assert "schedule:" in workflow
    assert "environment: production" in workflow
    assert "HERMES_PUBLIC_SSH_KEY_B64" in workflow
    assert "base64 --decode" in workflow
    assert 'ssh-keygen -y -f "$RUNNER_TEMP/ssh/id_ed25519"' in workflow
    assert "HERMES_REQUIRE_PINNED_SSH_HOST_KEY: '1'" in workflow
    assert "HERMES_SSH_KNOWN_HOSTS" in workflow
    assert "HERMES_SSH_KNOWN_HOSTS" in deployer
    assert "StrictHostKeyChecking=yes" in deployer


def test_public_deployer_selects_a_remote_staging_filesystem_with_space():
    deployer = (PUBLIC / "deploy-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )

    assert 'stage_root="${HERMES_PUBLIC_STAGE_ROOT:-}"' in deployer
    assert 'HERMES_PUBLIC_STAGE_ROOT must be absolute' in deployer
    assert "/dev/shm/hermes-agent-deploy" in deployer
    assert "/tmp/hermes-agent-deploy" in deployer
    assert "/home/admin/.cache/hermes-agent-deploy" in deployer
    assert 'df -Pk -- "$root"' in deployer
    assert 'remote staging filesystems have insufficient free space' in deployer


def test_fabric_diagnostics_are_manual_read_only_and_keep_deploy_gated():
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    diagnostics = workflow.split("\n  diagnose-fabric:\n", 1)[1].split(
        "\n  verify:", 1
    )[0]
    assert "github.event_name == 'workflow_dispatch'" in diagnostics
    assert "inputs.operation == 'diagnose-fabric'" in diagnostics
    assert "StrictHostKeyChecking=yes" in diagnostics
    assert "systemctl show hermes-fabric-update.timer" in diagnostics
    assert "systemctl show hermes-fabric-update.service" in diagnostics
    assert "journalctl -u hermes-fabric-update.service" in diagnostics
    assert "systemctl restart" not in diagnostics
    assert "systemctl enable" not in diagnostics
    assert "StrictHostKeyChecking=no" not in diagnostics
    assert (
        "if: github.event_name != 'workflow_dispatch' || inputs.operation == 'deploy'"
        in workflow
    )


def test_production_release_synchronizes_the_ios_workflow_observably():
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert "dispatch-ios:" in workflow
    assert "needs: deploy-public" in workflow
    assert "github.event_name == 'push'" in workflow
    assert "github.event_name == 'release'" in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "github.event_name == 'schedule'" not in workflow.split(
        "dispatch-ios:", 1
    )[1]
    assert "HERMES_IOS_WORKFLOW_TOKEN" in workflow
    assert "HERMES_IOS_SIGNED_BUILD" in workflow
    assert "hermes-backend-release-unsigned" in workflow
    assert "IOS_REPOSITORY" in workflow
    assert "given33/hermes-ios" in workflow
    assert "ios-unsigned.yml" in workflow
    assert 'gh api --method POST "repos/${IOS_REPOSITORY}/dispatches"' in workflow
    assert '--arg event_type "${event_type}"' in workflow
    assert '--arg commit "${RELEASE_COMMIT}"' in workflow
    assert '--arg version "${release_version}"' in workflow
    assert "release_name=\"Backend release ${RELEASE_COMMIT}\"" in workflow
    assert "production_release_name=\"Backend release signed ${RELEASE_COMMIT}\"" in workflow
    assert 'find_release_run ios-production-eas.yml "${production_release_name}"' in workflow
    assert "Dashboard release version contains unsupported characters" in workflow
    assert "jq -n" in workflow
    assert "ios-production-eas.yml" in workflow
    assert "already has unsigned run" in workflow
    assert "find_failed_release_run" in workflow
    assert "--json databaseId,displayTitle,status,conclusion" in workflow
    assert ".displayTitle == $name" in workflow
    assert ".name == $name" not in workflow
    assert 'if [ -z "${run_id}" ] && [ -n "${failed_run_id}" ]; then' in workflow
    assert 'if [ -z "${production_run_id}" ] && [ -n "${failed_production_run_id}" ]; then' in workflow
    assert 'rerun_release "${failed_production_run_id}"' in workflow
    assert 'if [ -z "${run_id}" ] && [ -z "${production_run_id}" ]; then' in workflow
    assert "dispatch_release()" in workflow
    assert "run_list()" in workflow
    assert "run-list attempt ${attempt}/3" in workflow
    assert "repository-dispatch attempt ${attempt}/3" in workflow
    assert "rerun_release()" in workflow
    assert "rerun attempt ${attempt}/3" in workflow
    assert "sleep $((attempt * 5))" in workflow
    assert "watch_release()" in workflow
    assert "watch attempt ${attempt}/3" in workflow
    assert "failure|cancelled|timed_out|action_required|startup_failure|stale" in workflow
    assert 'watch_release "${run_id}" unsigned' in workflow
    assert 'watch_release "${production_run_id}" production' in workflow
    assert 'dispatch_release "hermes-backend-release"' in workflow
    assert 'dispatch_release "hermes-backend-release-unsigned"' in workflow
    assert "Unsigned iOS IPA is the configured delivery artifact; signed EAS build is opt-in" in workflow
    assert 'elif [ -z "${run_id}" ] || [ -z "${production_run_id}" ]; then' in workflow
    assert "if [ -z \"${run_id}\" ] || [ -z \"${production_run_id}\" ]; then" in workflow
    assert "refusing a duplicate dispatch" in workflow
    assert "reusing existing dispatch" in workflow
    assert "production EAS run could not be observed" in workflow
    assert 'echo "Observing iOS production EAS run ${production_run_id}"' in workflow
    assert "--event repository_dispatch" in workflow
    assert "gh run watch" in workflow
    assert "--exit-status" in workflow
    assert "repository dispatch was accepted but its unsigned run could not be observed" in workflow

    operations = (ROOT / "docs" / "spec" / "OPERATIONS.md").read_text(encoding="utf-8")
    assert "Contents write and Actions write access" in operations


def test_site_pushes_trigger_vercel_and_pages_deployments():
    workflow = SITE_WORKFLOW.read_text(encoding="utf-8")

    assert "release:" in workflow
    assert "types: [published]" in workflow
    assert "branches: [main]" in workflow
    assert "website/**" in workflow
    vercel_job = workflow[workflow.index("deploy-vercel:") : workflow.index("deploy-docs:")]
    assert "github.repository == 'NousResearch/hermes-agent'" in vercel_job
    assert "github.event_name == 'push'" in vercel_job
    assert "VERCEL_DEPLOY_HOOK" in vercel_job
    assert 'curl -fsS --retry 3 --retry-delay 10 -X POST "$VERCEL_DEPLOY_HOOK"' in vercel_job
    assert "secrets.VERCEL_DEPLOY_HOOK" in vercel_job
    assert '"${{ secrets.VERCEL_DEPLOY_HOOK }}"' not in vercel_job
    assert "github.event_name == 'release' || github.event_name == 'push' || github.event_name == 'workflow_dispatch'" in vercel_job


def test_fabric_updater_transaction_and_rollback_behavior():
    harness = AUTOMATION / "test-update-fabric-node.sh"
    if os.name == "nt":
        wsl = shutil.which("wsl.exe")
        if not wsl:
            return
        command = [wsl, "sudo", "-n", "bash", _posix_path(harness)]
    elif os.geteuid() == 0:
        command = ["bash", str(harness)]
    elif subprocess.run(
        ["sudo", "-n", "true"], capture_output=True, check=False
    ).returncode == 0:
        command = ["sudo", "-n", "bash", str(harness)]
    else:
        return
    result = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "fabric updater transaction harness passed" in result.stdout


def test_fabric_updater_stages_sqlite_fallback_dependency():
    updater = (AUTOMATION / "update-fabric-node.sh").read_text(encoding="utf-8")
    harness = (AUTOMATION / "test-update-fabric-node.sh").read_text(encoding="utf-8")

    assert '"hermes_cli/sqlite_util.py"' in updater
    assert '"utils.py"' in updater
    assert "managed_node_recovery_service.py sqlite_util.py" in harness
    assert "import hermes_cli.managed_node_recovery_service" in updater


def test_legacy_fabric_manifest_can_bootstrap_the_current_receiver(tmp_path):
    """The updater deployed before 0.20 must be able to install its successor."""
    legacy_snapshot = tmp_path / "legacy-fabric-snapshot"
    legacy_snapshot.mkdir()
    for package in ("hermes_runtime", "hermes_services"):
        shutil.copytree(ROOT / package, legacy_snapshot / package)
    for relative in (
        "hermes_cli/__init__.py",
        "hermes_cli/managed_installations.py",
        "hermes_cli/managed_nodes.py",
        "hermes_cli/managed_node_recovery_service.py",
        "hermes_constants.py",
        "hermes_secret_compare.py",
        "utils.py",
    ):
        source = ROOT / relative
        target = legacy_snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(legacy_snapshot)
    result = subprocess.run(
        [sys.executable, "-I", "-c", (
            "import sys; "
            f"sys.path.insert(0, {str(legacy_snapshot)!r}); "
            "import hermes_cli.managed_node_recovery_service"
        )],
        cwd=legacy_snapshot,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


def test_public_release_contains_the_complete_application_service_layer():
    deployer = (PUBLIC / "deploy-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )
    harness = (PUBLIC / "test-install-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )

    service_files = sorted((ROOT / "hermes_services").glob("*.py"))
    assert service_files
    for path in service_files:
        relative = path.relative_to(ROOT).as_posix()
        assert relative in deployer
        assert relative in installer
        assert relative in harness
    runtime_files = sorted((ROOT / "hermes_runtime").glob("*.py"))
    assert runtime_files
    for path in runtime_files:
        relative = path.relative_to(ROOT).as_posix()
        assert relative in deployer
        assert relative in installer
        assert relative in harness
    root_runtime_dependencies = (
        "hermes_constants.py",
        "hermes_logging.py",
        "hermes_secret_compare.py",
        "utils.py",
    )
    for relative in root_runtime_dependencies:
        assert relative in deployer
        assert relative in installer
        assert relative in harness
    for relative in (
        "hermes_cli/account_identity.py",
        "hermes_cli/account_lifecycle.py",
        "hermes_cli/collaboration_plugin_backend.py",
        "hermes_cli/ios_plugin_backend.py",
        "hermes_cli/account_session_facade.py",
        "hermes_cli/account_write_approvals.py",
        "hermes_cli/mobile_console.py",
        "plugins/account_cleanup_backend.py",
    ):
        assert relative in deployer
        assert relative in installer
        assert relative in harness

    for relative in (
        "agent/conversation_loop.py",
        "agent/tool_executor.py",
        "agent/transports/hermes_tools_mcp_server.py",
        "gateway/platforms/api_server.py",
        "hermes_cli/dashboard_auth/client_ip.py",
        "hermes_cli/mcp_config.py",
        "plugins/memory/config_schema.py",
        "run_agent.py",
        "tools/file_operations.py",
        "tools/mcp_oauth_manager.py",
        "tools/registry.py",
        "tools/skills_guard.py",
        "tools/terminal_tool.py",
    ):
        assert relative in deployer
        assert relative in installer
        assert relative in harness

    assert 'required+=("${runtime_service_assets[@]}")' in installer
    assert 'git -C "${repo}" ls-files -z --' in deployer
    assert 'tar -C "${repo}" --null -T "${runtime_source_manifest}" -cf -' in deployer
    assert 'runtime-source-files.nul' in deployer
    assert 'runtime-source-files.nul' in installer
    assert 'runtime source path is outside approved roots' in installer
    assert 'runtime source manifest contains a test or cache path' in installer
    assert 'runtime_compile_assets+=("${snapshot}/${relative}")' in installer
    assert 'destination_parent="$(dirname "${target_root}/${relative}")"' in installer
    assert '"hermes_cli/sqlite_util.py"' in installer
    assert '"hermes_cli/sqlite_util.py"' in deployer
    assert '"hermes_cli/sqlite_util.py"' in (AUTOMATION / "update-fabric-node.sh").read_text(
        encoding="utf-8"
    )
    assert 'backup_one "${target_root}/${relative}"' in installer
    assert 'install_atomic "${snapshot}/${relative}"' in installer
    assert 'restore_one "${backup}/${relative}"' in installer
    assert "runtime-requirements.lock" in deployer
    assert "runtime-requirements.lock" in installer
    assert "runtime-requirements.lock" in harness
    assert "from mcp.server.fastmcp import FastMCP" in installer
    assert "from starlette.concurrency import run_in_threadpool" in installer
    assert 'mv -f -- "${runtime_venv}" "${previous_venv}"' in installer
    assert 'mv -f -- "${previous_venv}" "${runtime_venv}"' in installer


def test_managed_installation_receivers_probe_their_real_bind_and_rollback_safely():
    dbb3 = (RECOVERY / "install-dbb3-managed-installation-receiver.sh").read_text(
        encoding="utf-8"
    )
    wsl = (RECOVERY / "install-wsl-managed-installation.sh").read_text(
        encoding="utf-8"
    )

    safe_restore = (
        'local current="$1"\n'
        '  local name="$2"\n'
        '  local temporary="${current}.rollback.$$"'
    )
    assert safe_restore in dbb3
    assert safe_restore in wsl
    assert 'install -o "${receiver_user}" -g "${receiver_user}" -m 0600' in wsl
    assert '"${receiver_user}:${receiver_user}:600"' in wsl
    assert 'install -o root -g "${receiver_user}" -m 0640' not in wsl
    assert 'local current="$1" name="$2" temporary="${current}.rollback.$$"' not in dbb3
    assert 'local current="$1" name="$2" temporary="${current}.rollback.$$"' not in wsl
    assert dbb3.count("http://10.66.0.2:9122/") == 3
    assert "http://127.0.0.1:9122/" not in dbb3
    assert wsl.count("http://127.0.0.1:9122/") == 3


def test_public_nginx_contract_separates_refresh_and_returns_json_errors():
    security = (PUBLIC / "nginx-00-hermes-security.conf").read_text(
        encoding="utf-8"
    )
    site = (PUBLIC / "nginx-daxueshenmai.top.conf").read_text(encoding="utf-8")

    assert "$hermes_login_limit_key" in security
    assert "$hermes_refresh_limit_key" in security
    assert "password-login|mobile/(token|registration-code|register)" in security
    assert "~^POST:/auth/mobile/refresh$" in security
    assert "zone=hermes_login:10m rate=10r/m" in security
    assert "zone=hermes_refresh:10m rate=60r/m" in security
    assert "limit_req zone=hermes_login burst=10 nodelay" in site
    assert "limit_req zone=hermes_refresh burst=60 nodelay" in site
    assert "error_page 429 = @hermes_json_rate_limited" in site
    assert "error_page 502 503 504 = @hermes_json_unavailable" in site
    assert '"code":"rate_limited"' in site
    assert '"code":"service_unavailable"' in site
    assert "proxy_intercept_errors on" in site
    assert "<html>" not in site.lower()

    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )
    deployer = (PUBLIC / "deploy-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )
    for name in ("nginx-00-hermes-security.conf", "nginx-daxueshenmai.top.conf"):
        assert f'"${{repo}}/deploy/public/{name}"' in deployer
        assert f'"deploy/public/{name}"' in installer
    assert 'backup_one "${nginx_security_target}"' in installer
    assert 'backup_one "${nginx_site_target}"' in installer
    assert 'restore_root_file "${backup}/nginx/00-hermes-security.conf"' in installer
    assert 'restore_root_file "${backup}/nginx/daxueshenmai.top.conf"' in installer
    assert 'install_root_atomic "${snapshot}/deploy/public/nginx-00-hermes-security.conf"' in installer
    assert 'install_root_atomic "${snapshot}/deploy/public/nginx-daxueshenmai.top.conf"' in installer
    assert '"${nginx_binary}" -t' in installer
    assert 'systemctl reload "${nginx_service}"' in installer


def test_transactional_installers_serialize_deployments_and_use_unique_backups():
    public = (PUBLIC / "install-collaboration-backend.sh").read_text(encoding="utf-8")
    connector = (DBB3 / "install-dbb3-cloud-connector-user.sh").read_text(encoding="utf-8")

    assert 'lock_wait_seconds="${HERMES_INSTALL_LOCK_WAIT_SECONDS:-900}"' in public
    assert 'flock --wait "${lock_wait_seconds}" 8' in public
    assert 'another collaboration deployment is still running after ${lock_wait_seconds}s' in public
    assert 'mktemp -d "${backup_root}/collaboration-${version}-${stamp}.XXXXXX"' in public
    assert 'flock -n 8 || die "another connector deployment is already running"' in connector


def test_public_installer_reclaims_only_bounded_deployment_artifacts_on_disk_pressure():
    public = (PUBLIC / "install-collaboration-backend.sh").read_text(encoding="utf-8")

    assert "reclaim_runtime_disk_pressure()" in public
    assert "reclaim_stale_runtime_artifacts()" in public
    assert "HERMES_DEPLOY_MIN_FREE_KIB" in public
    assert "HERMES_DEPLOY_VENV_HEADROOM_KIB" in public
    assert "HERMES_BACKUP_RETENTION" in public
    assert "-name 'collaboration-*'" in public
    assert "-name '.venv.candidate.*'" in public
    assert "-name '.venv.failed.*'" in public
    assert "-name '.venv.rollback-*'" in public
    assert "-name '.collaboration-install.*'" in public
    assert 'du -sk -- "${target_root}/.venv"' in public
    assert 'rm -rf -- "${previous_venv}"' in public
    assert "journalctl --vacuum-size" in public
    assert 'business databases and runtime objects are never deleted' in public


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
if [[ "$*" == *"for root in /dev/shm/hermes-agent-deploy"* ]]; then
  printf '%s\n' '/tmp/hermes-agent-deploy'
  exit 0
fi
cat >/dev/null
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
        "hermes_cli/dashboard_auth/public_paths.py",
        "hermes_cli/dashboard_auth/token_auth.py",
        "hermes_cli/dashboard_auth/mobile_device_store.py",
        "hermes_cli/dashboard_auth/mobile_notifications.py",
        "hermes_cli/web_server.py",
        "agent/agent_init.py",
        "tui_gateway/server.py",
        "hermes_cli/account_cleanup.py",
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
        "hermes_cli/dashboard_auth/routes.py",
        "hermes_cli/profiles.py",
        "hermes_cli/managed_nodes.py",
        "hermes_cli/managed_node_recovery_service.py",
        "plugins/dashboard_auth/basic/__init__.py",
        "tools/mcp_tool.py",
        "deploy/recovery/configure-main-managed-installation-ssh.sh",
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
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "public installer transaction test passed" in result.stdout


def test_public_paths_is_transactional_deployment_asset():
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )
    deployer = (PUBLIC / "deploy-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )

    assert '"${repo}/hermes_cli/dashboard_auth/public_paths.py"' in deployer
    assert '"hermes_cli/dashboard_auth/public_paths.py"' in installer
    assert '"${snapshot}/hermes_cli/dashboard_auth/public_paths.py"' in installer
    assert (
        'public_paths_target="${target_root}/hermes_cli/dashboard_auth/public_paths.py"'
        in installer
    )
    assert 'backup_one "${public_paths_target}"' in installer
    assert (
        'restore_one "${backup}/hermes_cli/dashboard_auth/public_paths.py"' in installer
    )
    assert (
        'install_atomic "${snapshot}/hermes_cli/dashboard_auth/public_paths.py"'
        in installer
    )
    assert "api/mobile/v1/handshake" in installer
    assert 'data.get("api_version") == 1' in installer
    assert 'isinstance(data.get("profiles"), list)' in installer
    assert 'isinstance(data.get("capabilities"), list)' in installer
    assert 'data.get("server_time")' in installer


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
    assert "prepare_sqlite_runtime_target()" in installer
    assert '"/tmp/hermes-agent-deploy/"*' in installer
    assert '"/dev/shm/hermes-agent-deploy/"*' in installer
    assert "approved Hermes deployment staging root" in installer
    assert 'for suffix in -wal -shm -journal' in installer
    assert 'chown "${service_user}:${service_group}" "${sidecar}"' in installer
    assert 'chmod 0600 "${sidecar}"' in installer
    permission_normalization = installer.index(
        'prepare_sqlite_runtime_target "${sqlite_target}"'
    )
    assert permission_normalization > installer.index("mutated=1")
    assert permission_normalization < installer.index(
        'install_atomic "${snapshot}/plugins/collaboration/dashboard/plugin_api.py"'
    )
    mutated_start = rollback.index(
        'systemctl start "${service}"', rollback.index("restore_state")
    )
    assert rollback.index("restore_state") < mutated_start
    assert '/api/plugins/ios-intelligence/health' in installer
    assert '--config "${curl_cfg}"' in installer[installer.index('/api/plugins/ios-intelligence/health') - 160:]
    ios_plugin = (
        ROOT / "plugins" / "ios-intelligence" / "dashboard" / "plugin_api.py"
    ).read_text(encoding="utf-8")
    assert '"/api/plugins/ios-intelligence/health"' in ios_plugin
    assert 'required_scope="collaboration:connector"' in ios_plugin
    assert 'required_count = int(runtime.get("required_count") or 0)' in installer
    assert 'runtime.get("healthy_count") == required_count' in installer
    assert 'len(services) == required_count' in installer
    assert 'HERMES_IOS_HEALTH_ATTEMPTS:-180' in installer
    assert 'runtime.get("running") is True' in installer
    assert 'runtime.get("starting") is not True' in installer
    assert 'for _ in $(seq 1 "${ios_health_attempts}")' in installer
    assert 'validate_ios_health "${ios_health_file}"' in installer
    assert (
        "iOS intelligence runtime did not reach all required healthy MCPs and tools"
        in installer
    )


def test_public_installer_uses_only_a_root_controlled_install_lock():
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(encoding="utf-8")

    assert "install lock directory must be root-owned" in installer
    assert "install lock directory must not be group/world-writable" in installer
    assert '[[ -f "${install_lock}" && ! -L "${install_lock}" ]]' in installer
    assert "install lock file must be root-owned" in installer
    assert 'chmod 0600 "${install_lock}"' in installer


def test_public_installer_keeps_post_restart_diagnostics_parseable():
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )

    # journalctl on older systemd releases rejects ISO-8601's ``T`` and
    # numeric offset in --since, which previously hid the service crash log.
    assert "service_start_since=\"$(date '+%Y-%m-%d %H:%M:%S')\"" in installer
    assert 'systemctl cat "${service}" --no-pager' in installer
    assert "ss --listening --numeric --tcp --process" in installer
    assert "lsof -nP -iTCP:9119 -sTCP:LISTEN" in installer


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
        '    PYTHONPATH="${target_root}${PYTHONPATH:+:${PYTHONPATH}}" \\\n'
        '    "${runtime_python}" -m hermes_cli.ios_mcp_server --install'
    ) in installer
    assert (
        'sudo -u "${service_user}" -- env HERMES_HOME="${runtime_home}" \\\n'
        '    PYTHONPATH="${target_root}${PYTHONPATH:+:${PYTHONPATH}}" \\\n'
        '    "${runtime_python}" -m hermes_cli.ios_mcp_supervisor --register'
    ) in installer
    assert "AESGCM" in installer
    assert "from agent.plugin_llm import PluginLlm" in installer


def test_public_installer_validates_locked_fastmcp_in_the_dependency_candidate():
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )

    candidate_install = installer.index('"${candidate_venv}/bin/python" -m pip install')
    select_candidate = installer.index(
        'dependency_validation_python="${candidate_venv}/bin/python"'
    )
    mcp_validation = installer.index(
        '"${dependency_validation_python}" -c \'from mcp.server.fastmcp import FastMCP; assert FastMCP\''
    )
    service_stop = installer.index('systemctl stop "${service}"', mcp_validation)

    assert candidate_install < select_candidate < mcp_validation < service_stop
    pre_candidate = installer[:candidate_install]
    assert '"${runtime_python}" -c \'from mcp.server.fastmcp import FastMCP' not in pre_candidate


def test_public_installer_makes_candidate_dependencies_readable_to_service_user():
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )

    candidate_copy = installer.index('cp -a -- "${runtime_venv}" "${candidate_venv}"')
    readable_umask = installer.index("    umask 022", candidate_copy)
    candidate_install = installer.index(
        '"${candidate_venv}/bin/python" -m pip install', readable_umask
    )
    service_validation = installer.index(
        'sudo -u "${service_user}" -- "${candidate_venv}/bin/python" -',
        candidate_install,
    )
    service_stop = installer.index('systemctl stop "${service}"', service_validation)

    assert candidate_copy < readable_umask < candidate_install
    assert candidate_install < service_validation < service_stop
    service_imports = installer[service_validation:service_stop]
    assert "import requests" in service_imports
    assert "from fastapi import (" in service_imports
    assert "from mcp.server.fastmcp import FastMCP" in service_imports


def test_public_runtime_dependency_lock_matches_the_canonical_uv_lock():
    locked_packages = {
        package["name"].lower().replace("_", "-"): package["version"]
        for package in tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
    }
    runtime_lock = (PUBLIC / "runtime-requirements.lock").read_text(encoding="utf-8")
    runtime_packages: dict[str, str] = {}
    for line in runtime_lock.splitlines():
        requirement = line.split(";", 1)[0].strip()
        if "==" not in requirement or requirement.startswith("--"):
            continue
        name, version = requirement.split("==", 1)
        runtime_packages[name.lower().replace("_", "-")] = version.rstrip(" \\")

    assert runtime_packages["mcp"] == locked_packages["mcp"]
    assert runtime_packages["starlette"] == locked_packages["starlette"]
    assert {
        name: (version, locked_packages.get(name))
        for name, version in runtime_packages.items()
        if locked_packages.get(name) != version
    } == {}


def test_public_installer_imports_installed_dashboard_before_service_restart():
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )

    install_runtime = installer.index(
        'install_atomic "${snapshot}/${relative}" "${target_root}/${relative}"'
    )
    dashboard_preflight = installer.index(
        '"${runtime_python}" -c \'from hermes_cli.web_server import app; assert app\'',
        install_runtime,
    )
    ios_registration = installer.index(
        '"${runtime_python}" -m hermes_cli.ios_mcp_supervisor --register',
        install_runtime,
    )
    service_start = installer.index('systemctl start "${service}"', dashboard_preflight)

    assert install_runtime < ios_registration < dashboard_preflight < service_start
    preflight = installer[install_runtime:service_start]
    assert 'if [[ "${dependency_update_enabled}" == 1 ]]; then' in preflight
    assert 'sudo -u "${service_user}" -- env HERMES_HOME="${runtime_home}"' in preflight
    assert 'PYTHONPATH="${target_root}${PYTHONPATH:+:${PYTHONPATH}}"' in preflight


def test_public_installer_allows_recovery_when_service_is_inactive():
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )

    preflight = installer[installer.index('preflight_health='):installer.index(
        'rm -f -- "${preflight_health}"'
    )]

    assert 'if ! curl --fail --silent --show-error --max-time 8' in preflight
    assert 'elif ! validate_connector_health_payload "${preflight_health}" 0; then' in preflight
    assert 'if systemctl is-active --quiet "${service}"; then' in preflight
    assert 'connector health preflight failed while ${service} is active' in preflight
    assert 'connector health endpoint is unreachable; continuing with recovery transaction' in preflight
    assert 'connector health preflight returned an invalid contract while ${service} is inactive' in preflight


def test_public_installer_bounds_fabric_checks_and_bypasses_proxy_for_loopback():
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )
    verifier = (PUBLIC / "verify-fabric-release.sh").read_text(encoding="utf-8")

    assert 'fabric_health_attempts="${HERMES_FABRIC_HEALTH_ATTEMPTS:-360}"' in installer
    assert installer.count("--noproxy '*'") >= 7
    assert 'attempts="${HERMES_FABRIC_VERIFY_ATTEMPTS:-60}"' in verifier
    assert "--noproxy '*'" in verifier


def test_public_deployer_keeps_desired_fabric_release_and_retries_transaction():
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )
    deployer = (PUBLIC / "deploy-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )

    assert "fabric_release_published=0" in installer
    assert "fabric_release_published=1" in installer
    assert 'if [[ "${fabric_release_published}" == 0 ]]; then' in installer
    assert "fabric recovery remains pending" in installer
    assert '[[ "${installed}" != 1 && "${fabric_release_published}" == 1 ]]' in installer
    assert "exit_code=75" in installer
    assert 'recovery_attempts="${HERMES_PUBLIC_FABRIC_RECOVERY_ATTEMPTS:-2}"' in deployer
    assert 'installer_status}" != 75' in deployer
    assert "fabric convergence is pending; retrying public transaction" in deployer


def test_public_installer_uses_effective_systemd_hermes_home_before_env_fallback():
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )

    systemd_read = installer.index(
        'systemctl show "${service}" --property=Environment --value'
    )
    runtime_choice = installer.index(
        'runtime_home="${HERMES_HOME_DIR:-${systemd_runtime_home:-${env_runtime_home:-${service_home}/.hermes}}}"'
    )
    registration = installer.index(
        'env HERMES_HOME="${runtime_home}"', runtime_choice
    )
    assert systemd_read < runtime_choice < registration
    assert 'sed -n \'s/^HERMES_HOME=//p\'' in installer[systemd_read:runtime_choice]
    assert 'Hermes runtime home must be an absolute path' in installer


def test_public_installer_transactions_mcp_discovery_with_ios_release():
    installer = (PUBLIC / "install-collaboration-backend.sh").read_text(
        encoding="utf-8"
    )
    ios_assets = installer[
        installer.index("ios_optional=("):installer.index(
            'for relative in "${required[@]}"'
        )
    ]
    required_assets = installer[
        installer.index("required=("):installer.index("ios_optional=(")
    ]

    assert '"tools/mcp_tool.py"' in ios_assets
    assert '"hermes_cli/dashboard_auth/owner_mobile.py"' in ios_assets
    assert '"hermes_cli/dashboard_auth/registry.py"' in ios_assets
    assert '"hermes_cli/dashboard_auth/routes.py"' in ios_assets
    assert '"hermes_cli/profiles.py"' in ios_assets
    assert '"hermes_cli/account_cleanup.py"' in ios_assets
    assert '"hermes_cli/managed_nodes.py"' not in ios_assets
    assert '"hermes_cli/managed_nodes.py"' in required_assets
    assert '"hermes_cli/managed_node_recovery_service.py"' in ios_assets
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
        harness_path = _posix_path(harness)
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
        run.setdefault("claim_token", "claim-v2-test-token")
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
        assert run["claim_token"]
        self.acks.append((run, dict(local)))

    def report_status(self, remote_id, payload):
        assert payload["claim_token"]
        self.statuses.append((remote_id, dict(payload)))
        return {"applied": True}

    def fail_run(self, remote_id, payload):
        assert payload["claim_token"]
        self.failures.append((remote_id, dict(payload)))
        return {
            "applied": True,
            "run": {
                "status": "failed",
                "checkpoint_cursor": payload.get("checkpoint_cursor", 0),
                "claim_token": payload["claim_token"],
            },
        }

    def get_run(self, remote_id):
        assert remote_id == self.run["remote_run_id"]
        return dict(self.run)

    def pull_cancellations(self, limit=5, lease_seconds=90):
        return []

    def acknowledge_cancel(self, item, payload):
        raise AssertionError("unexpected cancellation")

    def upload_artifact(self, remote_id, **kwargs):
        assert kwargs["claim_token"]
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


def test_account_remote_run_executes_in_private_overlay_profile(tmp_path, monkeypatch):
    connector = _load_connector()
    run = {
        "remote_run_id": "run-account-overlay",
        "idempotency_key": "idem-account-overlay",
        "profile": "dbb3-worker",
        "owner_id": "alice@example.test",
        "account_generation": "generation-7",
        "title": "Private task",
        "objective": "Use my installed resources",
    }
    overlay = tmp_path / "profiles" / "acct-private-worker"
    overlay.mkdir(parents=True)
    resolved = []

    from hermes_cli import managed_installations

    def resolve(owner_id, account_generation, profile):
        resolved.append((owner_id, account_generation, profile))
        return overlay

    monkeypatch.setattr(
        managed_installations,
        "managed_account_runtime_home",
        resolve,
    )
    commands = []

    def command_runner(command, timeout=30):
        commands.append(command)
        assert command[:4] == ["hermes", "-p", overlay.name, "kanban"]
        if command[4] == "create":
            assert command[command.index("--assignee") + 1] == overlay.name
            return 0, json.dumps({"id": "task-private"})
        if command[4] == "show":
            return 0, json.dumps(
                {
                    "task": {
                        "id": "task-private",
                        "status": "done",
                        "result": "complete",
                    },
                    "events": [],
                    "runs": [],
                }
            )
        raise AssertionError(command)

    cloud = _FakeCloud(run)
    instance = connector.DBB3CloudConnector(
        cloud,
        command_runner=command_runner,
        state_file=tmp_path / "state" / "checkpoint.json",
        artifact_roots=[tmp_path],
    )

    result = instance.sync_once()

    assert result["created"] == 1
    assert result["statuses"] == 1
    assert resolved == [
        ("alice@example.test", "generation-7", "dbb3-worker"),
    ]
    assert run["profile"] == "dbb3-worker"
    assert cloud.acks[0][1]["profile"] == "dbb3-worker"
    assert cloud.acks[0][1]["execution_profile"] == overlay.name
    assert len(commands) == 2


def test_deleted_account_remote_run_fails_without_local_execution(tmp_path, monkeypatch):
    connector = _load_connector()
    run = {
        "remote_run_id": "run-deleted-account",
        "idempotency_key": "idem-deleted-account",
        "profile": "pc-worker",
        "owner_id": "deleted@example.test",
        "account_generation": "old-generation",
        "title": "Stale task",
        "objective": "Must not execute",
    }
    from hermes_cli import managed_installations

    def deleted(*_args, **_kwargs):
        raise PermissionError("account generation is deleted")

    monkeypatch.setattr(
        managed_installations,
        "managed_account_runtime_home",
        deleted,
    )
    commands = []
    cloud = _FakeCloud(run)
    instance = connector.DBB3CloudConnector(
        cloud,
        command_runner=lambda command, timeout=30: (
            commands.append(command) or (0, "")
        ),
        state_file=tmp_path / "state" / "checkpoint.json",
        artifact_roots=[tmp_path],
    )

    result = instance.sync_once()

    assert result == {
        "created": 0,
        "statuses": 1,
        "artifacts": 0,
        "cancelled": 0,
        "steered": 0,
        "terminal_pushed": 0,
    }
    assert commands == []
    assert len(cloud.failures) == 1
    assert cloud.failures[0][1]["claim_token"] == run["claim_token"]
    assert "deleted" in cloud.failures[0][1]["error"].lower()


def test_account_deletion_stops_an_already_accepted_remote_root(tmp_path, monkeypatch):
    connector = _load_connector()
    run = {
        "remote_run_id": "run-deleted-after-ack",
        "idempotency_key": "idem-deleted-after-ack",
        "profile": "dbb3-worker",
        "owner_id": "alice@example.test",
        "account_generation": "generation-live",
        "title": "Long task",
        "objective": "Keep working",
    }
    overlay = tmp_path / "profiles" / "acct-live-worker"
    overlay.mkdir(parents=True)
    deleted = {"value": False}
    from hermes_cli import managed_installations

    def resolve(*_args, **_kwargs):
        if deleted["value"]:
            raise PermissionError("account generation is deleted")
        return overlay

    monkeypatch.setattr(
        managed_installations,
        "managed_account_runtime_home",
        resolve,
    )
    commands = []

    def command_runner(command, timeout=30):
        commands.append(command)
        if command[3:5] == ["kanban", "create"]:
            return 0, json.dumps({"id": "task-long"})
        if command[3:5] == ["kanban", "show"]:
            return 0, json.dumps(
                {
                    "task": {"id": "task-long", "status": "running"},
                    "events": [],
                    "runs": [],
                }
            )
        if command[3:5] == ["kanban", "block"]:
            return 0, "blocked"
        raise AssertionError(command)

    cloud = _FakeCloud(run)
    state_file = tmp_path / "state" / "checkpoint.json"
    instance = connector.DBB3CloudConnector(
        cloud,
        command_runner=command_runner,
        state_file=state_file,
        artifact_roots=[tmp_path],
    )
    first = instance.sync_once()
    assert first["created"] == 1

    deleted["value"] = True
    second = instance.sync_once()

    assert second["created"] == 0
    assert commands[-1] == [
        "hermes",
        "-p",
        overlay.name,
        "kanban",
        "block",
        "task-long",
        "Account generation is no longer active",
    ]
    checkpoint = json.loads(state_file.read_text(encoding="utf-8"))
    local = checkpoint["runs"][run["remote_run_id"]]
    assert local["status"] == "failed"
    assert local["terminal_acked"] is True
    assert "pending_terminal_failure" not in local
    assert len(cloud.failures) == 1
    assert cloud.failures[0][1]["claim_token"] == run["claim_token"]


def test_account_deletion_terminal_report_survives_network_failure_and_restart(
    tmp_path,
    monkeypatch,
):
    connector = _load_connector()
    run = {
        "remote_run_id": "run-deleted-terminal-retry",
        "idempotency_key": "idem-deleted-terminal-retry",
        "profile": "dbb3-worker",
        "owner_id": "alice@example.test",
        "account_generation": "generation-live",
        "title": "Long task",
        "objective": "Keep working",
    }
    overlay = tmp_path / "profiles" / "acct-live-worker"
    overlay.mkdir(parents=True)
    deleted = {"value": False}
    from hermes_cli import managed_installations

    def resolve(*_args, **_kwargs):
        if deleted["value"]:
            raise PermissionError("account generation is deleted")
        return overlay

    monkeypatch.setattr(managed_installations, "managed_account_runtime_home", resolve)

    commands = []

    def command_runner(command, timeout=30):
        commands.append(command)
        if command[3:5] == ["kanban", "create"]:
            return 0, json.dumps({"id": "task-terminal-retry"})
        if command[3:5] == ["kanban", "show"]:
            return 0, json.dumps(
                {
                    "task": {"id": "task-terminal-retry", "status": "running"},
                    "events": [],
                    "runs": [],
                }
            )
        if command[3:5] == ["kanban", "block"]:
            return 0, "blocked"
        raise AssertionError(command)

    class FlakyCloud(_FakeCloud):
        def __init__(self, payload):
            super().__init__(payload)
            self.emit_run = True
            self.failure_attempts = 0

        def pull_runs(self, limit=5, lease_seconds=90):
            self.pull_count += 1
            return [self.run] if self.emit_run else []

        def fail_run(self, remote_id, payload):
            self.failure_attempts += 1
            if self.failure_attempts == 1:
                raise OSError("cloud unavailable")
            return super().fail_run(remote_id, payload)

    cloud = FlakyCloud(run)
    state_file = tmp_path / "state" / "checkpoint.json"
    first = connector.DBB3CloudConnector(
        cloud,
        command_runner=command_runner,
        state_file=state_file,
        artifact_roots=[tmp_path],
    )
    assert first.sync_once()["created"] == 1

    deleted["value"] = True
    assert first.sync_once()["statuses"] == 0
    pending_state = json.loads(state_file.read_text(encoding="utf-8"))["runs"][
        run["remote_run_id"]
    ]
    assert pending_state["status"] == "terminal_pending"
    assert pending_state["pending_terminal_failure"]["claim_token"] == run["claim_token"]
    assert pending_state["terminal_local_stopped"] is True
    assert pending_state.get("terminal_acked") is not True

    cloud.emit_run = False
    restarted = connector.DBB3CloudConnector(
        cloud,
        command_runner=command_runner,
        state_file=state_file,
        artifact_roots=[tmp_path],
    )
    assert restarted.sync_once()["statuses"] == 1

    final_state = json.loads(state_file.read_text(encoding="utf-8"))["runs"][
        run["remote_run_id"]
    ]
    assert final_state["status"] == "failed"
    assert final_state["terminal_acked"] is True
    assert "pending_terminal_failure" not in final_state
    assert cloud.failure_attempts == 2
    assert len(cloud.failures) == 1
    assert sum(command[3:5] == ["kanban", "block"] for command in commands) == 1


def test_terminal_conflict_with_rotated_claim_waits_for_new_claim(tmp_path):
    connector = _load_connector()
    run = {
        "remote_run_id": "run-claim-rotated",
        "idempotency_key": "idem-claim-rotated",
        "profile": "dbb3-worker",
        "owner_id": "server-admin",
        "account_generation": "",
        "claim_token": "claim-old",
    }

    class RotatingCloud(_FakeCloud):
        def __init__(self, payload):
            super().__init__(payload)
            self.conflict = True

        def fail_run(self, remote_id, payload):
            if self.conflict:
                raise connector.ConnectorContractError(409, "claim lost")
            return super().fail_run(remote_id, payload)

        def get_run(self, remote_id):
            assert remote_id == self.run["remote_run_id"]
            return {
                **self.run,
                "claim_token": "claim-new",
                "status": "running",
                "checkpoint_cursor": 4,
            }

    cloud = RotatingCloud(run)
    state_file = tmp_path / "state" / "checkpoint.json"
    instance = connector.DBB3CloudConnector(
        cloud,
        state_file=state_file,
        artifact_roots=[tmp_path],
    )
    state = {
        "version": 1,
        "runs": {
            run["remote_run_id"]: {
                **run,
                "root_task_id": "root-claim-rotated",
                "status": "terminal_pending",
                "pending_terminal_failure": {
                    "claim_token": "claim-old",
                    "checkpoint_cursor": 5,
                    "error": "account deleted",
                    "summary": "stopped",
                },
            }
        },
        "cancellations": {},
    }
    local = state["runs"][run["remote_run_id"]]

    assert instance._flush_terminal_failure(run["remote_run_id"], local, state) is False
    assert local["status"] == "awaiting_claim"
    assert local["claim_stale"] is True
    assert local.get("terminal_acked") is not True
    assert "pending_terminal_failure" in local

    instance._accept_run({**run, "claim_token": "claim-new"}, state)
    assert local["claim_token"] == "claim-new"
    assert local["pending_terminal_failure"]["claim_token"] == "claim-new"
    cloud.conflict = False
    assert instance._flush_terminal_failure(run["remote_run_id"], local, state) is True
    assert local["status"] == "failed"
    assert local["terminal_acked"] is True
    assert "pending_terminal_failure" not in local


def test_terminal_conflict_seals_only_authoritative_terminal_snapshot(tmp_path):
    connector = _load_connector()
    run = {
        "remote_run_id": "run-already-completed",
        "idempotency_key": "idem-already-completed",
        "profile": "dbb3-worker",
        "claim_token": "claim-old",
    }

    class CompletedCloud(_FakeCloud):
        def fail_run(self, remote_id, payload):
            raise connector.ConnectorContractError(409, "already terminal")

        def get_run(self, remote_id):
            return {
                **self.run,
                "status": "completed",
                "checkpoint_cursor": 9,
            }

    cloud = CompletedCloud(run)
    instance = connector.DBB3CloudConnector(
        cloud,
        state_file=tmp_path / "state" / "checkpoint.json",
        artifact_roots=[tmp_path],
    )
    state = {
        "version": 1,
        "runs": {
            run["remote_run_id"]: {
                **run,
                "status": "terminal_pending",
                "pending_terminal_failure": {
                    "claim_token": "claim-old",
                    "checkpoint_cursor": 5,
                    "error": "account deleted",
                    "summary": "stopped",
                },
            }
        },
        "cancellations": {},
    }
    local = state["runs"][run["remote_run_id"]]

    assert instance._flush_terminal_failure(run["remote_run_id"], local, state) is True
    assert local["status"] == "completed"
    assert local["checkpoint_cursor"] == 9
    assert local["terminal_acked"] is True


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
        "show",
        "task-cancel",
        "--json",
    ]
    assert commands[1] == [
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
            return {"ok": True, "contract_version": 2}
        if path.endswith("/pull"):
            return {"runs": [], "cancellations": []}
        if path.endswith("/attachments"):
            return {"attachments": []}
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
            return b'{"ok":true,"contract_version":2}'

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
