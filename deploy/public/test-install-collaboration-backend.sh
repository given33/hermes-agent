#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ "$(id -u)" == 0 ]] || {
  printf '%s\n' "test-install-collaboration-backend: root is required" >&2
  exit 1
}

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
installer="${here}/install-collaboration-backend.sh"
runtime_python="${HERMES_TEST_RUNTIME_PYTHON:-${repo}/venv/bin/python}"
if [[ -n "${HERMES_TEST_RUNTIME_PYTHON:-}" ]]; then
  [[ -x "${runtime_python}" ]] || {
    printf 'HERMES_TEST_RUNTIME_PYTHON is not executable: %s\n' \
      "${runtime_python}" >&2
    exit 1
  }
elif [[ ! -x "${runtime_python}" ]]; then
  if [[ -x "${repo}/.venv/bin/python" ]]; then
    runtime_python="${repo}/.venv/bin/python"
  else
    runtime_python="$(command -v python3)"
  fi
fi
bootstrap_python="${HERMES_TEST_BOOTSTRAP_PYTHON:-$(command -v python3)}"
bootstrap_python="$(realpath -e -- "${bootstrap_python}")"
version="$(python3 - "${repo}/plugins/collaboration/dashboard/manifest.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])
PY
)"
ios_capabilities_count="$(python3 - "${repo}/hermes_cli/ios_mcp_server.py" <<'PY'
import ast
import sys
from pathlib import Path

tree = ast.parse(Path(sys.argv[1]).read_text(encoding="utf-8"))
for node in tree.body:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == "CAPABILITIES" for target in targets):
            value = ast.literal_eval(node.value)
            print(len(value))
            break
else:
    raise SystemExit("CAPABILITIES declaration not found")
PY
)"
[[ "${ios_capabilities_count}" =~ ^[1-9][0-9]*$ ]] || {
  printf '%s\n' "invalid iOS capability count" >&2
  exit 1
}
work="$(mktemp -d /tmp/hermes-public-installer-test.XXXXXX)"
stage="/home/root/.cache/hermes-agent-deploy/${version}-test-$$"
last_error_status=0
last_error_line="unknown"
last_error_command="unknown"
record_error() {
  local status=$?
  last_error_status="${status}"
  last_error_line="${BASH_LINENO[0]:-unknown}"
  last_error_command="${BASH_COMMAND:-unknown}"
}
cleanup() {
  local status=$?
  if (( status != 0 )); then
    printf 'public installer harness failed: status=%s last_error_status=%s line=%s command=%q\n' \
      "${status}" "${last_error_status}" "${last_error_line}" \
      "${last_error_command}" >&2
    local diagnostic
    for diagnostic in \
      "${work}/deployer-failure.stderr" \
      "${work}/deployer-upload.stderr" \
      "${work}"/credential-*.stderr \
      "${work}/failure.stderr" \
      "${work}/effective-unit-reset.stderr" \
      "${work}/nginx-failure.stderr" \
      "${work}/candidate-reboot-start.stderr" \
      "${work}/candidate-running.stderr" \
      "${work}/candidate-marker-committed.stderr" \
      "${work}/watchdog-detached.stderr" \
      "${work}/handshake.stderr" \
      "${work}/signal.stderr" \
      "${work}/systemctl.log" \
      "${work}/systemctl-condition.log" \
      "${work}/systemd-run.log" \
      "${work}/migration-systemctl.log" \
      "${work}"/phase-*.stderr \
      "${work}"/migration-*.stderr \
      "${work}/success.stderr"; do
      [[ -s "${diagnostic}" ]] || continue
      printf '%s\n' "--- $(basename "${diagnostic}") (tail) ---" >&2
      tail -n 120 -- "${diagnostic}" >&2 || true
    done
  fi
  if [[ "${HERMES_TEST_KEEP_WORK:-0}" == 1 ]]; then
    printf 'public installer harness work directory preserved: %s\n' "${work}" >&2
  else
    rm -rf -- "${work}" "${stage}"
  fi
  exit "${status}"
}
trap record_error ERR
trap cleanup EXIT

runtime_files=(
  "hermes_auth_errors.py"
  "hermes_cli/web_models.py"
  "agent/interrupt_compat.py"
  "gateway/streaming_tts_consumer.py"
  "plugins/collaboration/dashboard/hosted_tui_runtime.py"
  "agent/agent_runtime_helpers.py"
  "agent/chat_completion_helpers.py"
  "agent/codex_runtime.py"
  "agent/conversation_compression.py"
  "agent/conversation_loop.py"
  "agent/curator_backup.py"
  "agent/memory_provider.py"
  "agent/turn_context.py"
  "agent/lsp/workspace.py"
  "agent/image_routing.py"
  "agent/model_metadata.py"
  "agent/models_dev.py"
  "agent/shell_hooks.py"
  "agent/tool_dispatch_helpers.py"
  "agent/tool_executor.py"
  "agent/transports/hermes_tools_mcp_server.py"
  "gateway/hooks.py"
  "gateway/platforms/api_server.py"
  "gateway/run.py"
  "hermes_runtime/__init__.py"
  "hermes_runtime/capabilities.py"
  "hermes_runtime/collaboration.py"
  "hermes_runtime/colors.py"
  "hermes_runtime/config.py"
  "hermes_runtime/console_output.py"
  "hermes_runtime/credential_persistence.py"
  "hermes_runtime/default_soul.py"
  "hermes_runtime/evidence.py"
  "hermes_runtime/golden_path.py"
  "hermes_runtime/managed_scope.py"
  "hermes_runtime/mcp_security.py"
  "hermes_runtime/model_catalog_cache.py"
  "hermes_runtime/package_install.py"
  "hermes_runtime/plugin_compatibility.py"
  "hermes_runtime/process_probe.py"
  "hermes_runtime/profile_identity.py"
  "hermes_runtime/prompt_runtime.py"
  "hermes_runtime/redaction.py"
  "hermes_runtime/runtime_cwd.py"
  "hermes_runtime/secret_prompt.py"
  "hermes_runtime/secret_provenance.py"
  "hermes_runtime/secret_scope.py"
  "hermes_runtime/session_context.py"
  "hermes_runtime/session_trace.py"
  "hermes_runtime/skill_utils.py"
  "hermes_runtime/subprocess_compat.py"
  "hermes_runtime/text_safety.py"
  "hermes_runtime/timeouts.py"
  "hermes_runtime/tool_execution.py"
  "hermes_runtime/toolset_validation.py"
  "hermes_runtime/trajectory.py"
  "hermes_runtime/urllib_security.py"
  "hermes_runtime/version.py"
  "hermes_runtime/viewer_registry.py"
  "hermes_runtime/visual_evidence.py"
  "hermes_cli/backup.py"
  "hermes_cli/sqlite_util.py"
  "hermes_cli/dashboard_auth/base.py"
  "hermes_cli/dashboard_auth/client_ip.py"
  "hermes_cli/main.py"
  "hermes_cli/mcp_config.py"
  "hermes_cli/plugins.py"
  "hermes_cli/profile_distribution.py"
  "hermes_cli/runtime_provider.py"
  "hermes_constants.py"
  "hermes_logging.py"
  "hermes_secret_compare.py"
  "hermes_state.py"
  "mcp_serve.py"
  "model_tools.py"
  "plugins/context_engine/__init__.py"
  "plugins/cron_providers/__init__.py"
  "plugins/memory/__init__.py"
  "plugins/memory/config_schema.py"
  "plugins/memory/honcho/__init__.py"
  "providers/__init__.py"
  "run_agent.py"
  "tui_gateway/entry.py"
  "tools/code_execution_tool.py"
  "tools/computer_use/cua_backend.py"
  "tools/credential_files.py"
  "tools/file_operations.py"
  "tools/file_tools.py"
  "tools/lazy_deps.py"
  "tools/mcp_oauth.py"
  "tools/mcp_oauth_manager.py"
  "tools/mcp_schema_cache.py"
  "tools/registry.py"
  "tools/skills_guard.py"
  "tools/skills_hub.py"
  "tools/terminal_tool.py"
  "tools/tool_result_storage.py"
  "utils.py"
  "plugins/collaboration/dashboard/plugin_api.py"
  "plugins/collaboration/dashboard/manifest.json"
  "plugins/collaboration/dashboard/dist/index.js"
  "hermes_cli/cloud_file_library.py"
  "hermes_cli/dashboard_auth/public_paths.py"
  "hermes_cli/dashboard_auth/token_auth.py"
  "hermes_cli/dashboard_auth/mobile_device_store.py"
  "hermes_cli/dashboard_auth/mobile_notifications.py"
  "hermes_cli/managed_installations.py"
  "hermes_cli/managed_nodes.py"
  "hermes_cli/web_server.py"
  "tools/managed_installation_tool.py"
  "toolsets.py"
  "agent/agent_init.py"
  "agent/prompt_builder.py"
  "agent/system_prompt.py"
  "agent/context_diagnostics.py"
  "hermes_cli/doctor.py"
  "tui_gateway/server.py"
  "hermes_services/__init__.py"
  "hermes_services/application.py"
  "hermes_services/auth.py"
  "hermes_services/behavior_eval.py"
  "hermes_services/bounded_dict.py"
  "hermes_services/contexts.py"
  "hermes_services/contracts.py"
  "hermes_services/cron_fire.py"
  "hermes_services/hosted_event_protocol.py"
  "hermes_services/hosted_role_migration.py"
  "hermes_services/http_boundary.py"
  "hermes_services/http_policy.py"
  "hermes_services/internal_hooks.py"
  "hermes_services/jsonrpc.py"
  "hermes_services/latency_trace.py"
  "hermes_services/low_latency_protocol.py"
  "hermes_services/middleware.py"
  "hermes_services/resource_catalog.py"
  "hermes_services/session_entries.py"
  "hermes_services/session_registry.py"
  "hermes_services/startup.py"
  "hermes_services/tool_contract.py"
  "hermes_services/tool_isolation.py"
  "hermes_services/tool_output_artifacts.py"
  "hermes_services/worker_channel.py"
  "hermes_cli/account_identity.py"
  "hermes_cli/account_lifecycle.py"
  "hermes_cli/collaboration_plugin_backend.py"
  "hermes_cli/ios_plugin_backend.py"
  "hermes_cli/account_session_facade.py"
  "hermes_cli/account_write_approvals.py"
  "hermes_cli/mobile_console.py"
  "plugins/account_cleanup_backend.py"
)
nginx_files=(
  "deploy/public/nginx-00-hermes-security.conf"
  "deploy/public/nginx-daxueshenmai.top.conf"
)
managed_nodes_template="deploy/public/managed-nodes.server.json"
candidate_start_guard_asset="deploy/public/candidate-start-guard.py"
runtime_home_guard_asset="deploy/public/runtime-home-guard.py"
profile_runtime_io_asset="deploy/public/profile-runtime-io.py"
candidate_start_guard_sha256="$(
  sha256sum "${repo}/${candidate_start_guard_asset}" | cut -d' ' -f1
)"

target="${work}/target"
backup="${work}/backups"
fake_bin="${work}/bin"
sshd_config="${work}/sshd_config"
sshd_original="${work}/sshd_config.original"
token_file="${work}/connector.token"
status_token_file="${work}/dbb3-status.token"
installation_token_file="${work}/managed-installation.token"
hk_recovery_token_file="${work}/hk-recovery.token"
agent_env_file="${work}/hermes-agent.env"
agent_profile_dropin="${work}/systemd/hermes-agent-test.service.d/10-hermes-dispatcher-profile.conf"
agent_reload_marker="${work}/systemd/daemon-reloaded"
state_file="${work}/state/single.json"
runtime_home="${work}/hermes-home"
# The dispatcher state root is passed to every installer invocation; the
# migration fixture below re-derives its profile tree from the same home.
migration_service_home="${work}/migration-service-home"
managed_installations_db="${runtime_home}/managed-installations.db"
managed_nodes_file="${runtime_home}/managed-nodes.json"
release_evidence_file="${work}/release/release-evidence.json"
release_pending_marker="${work}/release/candidate-pending.json"
release_start_guard="${work}/release/candidate-start-guard.${candidate_start_guard_sha256}.py"
release_start_lease="${work}/release/candidate-start-lease.json"
systemctl_state_file="${work}/systemctl.state"
systemctl_condition_log="${work}/systemctl-condition.log"
systemd_run_log="${work}/systemd-run.log"
nginx_dir="${work}/nginx"
nginx_security_target="${nginx_dir}/00-hermes-security.conf"
nginx_site_target="${nginx_dir}/daxueshenmai.top.conf"
install -d -m 0700 \
  "${stage}" "${target}" "${backup}" "${fake_bin}" "${nginx_dir}" "${runtime_home}"
# Production keeps the checkout root-owned but traversable by the service
# account.  The diagnostics redactor imports installed code as that account.
chmod 0755 "${target}"
[[ "$(stat -c '%u:%g:%a' "${target}")" == "0:0:755" ]]
stale_runtime_artifacts=(
  "${target}/.collaboration-install.stale"
)
install -d -m 0700 "${stale_runtime_artifacts[@]}"
install -d -m 0700 "$(dirname "${state_file}")"
printf '%s' "connector-test-token" >"${token_file}"
printf '%s\n' "status-test-token-00000000000000000001" >"${status_token_file}"
printf '%s\n' "installation-test-token-000000000000001" >"${installation_token_file}"
printf '%s\n' "hk-recovery-test-token-0000000000000001" >"${hk_recovery_token_file}"
printf '%s\n' \
  'HERMES_QWEATHER_API_KEY=qweather-test-key' \
  'HERMES_AMAP_WEB_API_KEY=amap-test-key' \
  'HERMES_IOS_DATA_KEY=ios-data-test-key-0000000000000000' \
  >"${agent_env_file}"
chown root:root "${agent_env_file}"
chmod 0600 "${agent_env_file}"
credential_service_user="$(awk -F: '$3 != 0 { print $1; exit }' /etc/passwd)"
credential_primary_group="$(id -gn "${credential_service_user}")"
credential_service_group="$(awk -F: -v primary="${credential_primary_group}" \
  '$1 != "root" && $1 != primary { print $1; exit }' /etc/group)"
[[ -n "${credential_service_user}" && -n "${credential_service_group}" ]] || {
  printf '%s\n' "test-install-collaboration-backend: no distinct service group fixture is available" >&2
  exit 1
}
chown "${credential_service_user}:${credential_service_group}" "${runtime_home}"
chmod 0700 "${runtime_home}"
credential_source_group="$(awk -F: -v target="${credential_service_group}" \
  '$1 != "root" && $1 != target { print $1; exit }' /etc/group)"
[[ -n "${credential_source_group}" ]] || {
  printf '%s\n' "test-install-collaboration-backend: no distinct source group is available" >&2
  exit 1
}
chown "root:${credential_service_group}" "${work}"
# The installer invokes the runtime-I/O helper as the service account.  Give
# that group read/search access to the hermetic harness root just as the
# production backup namespace does; secrets remain mode 0600 below it.
chmod 0750 "${work}"
chown "root:${credential_source_group}" "${status_token_file}" \
  "${installation_token_file}" "${hk_recovery_token_file}"
chmod 0600 "${status_token_file}" "${installation_token_file}" \
  "${hk_recovery_token_file}"
printf '%s\n' '{"conversations":[{"id":"old-state"}]}' >"${state_file}"
printf '%s\n' '{"nodes":[]}' >"${managed_nodes_file}"
printf '%s\n' active >"${systemctl_state_file}"
: >"${systemctl_condition_log}"
: >"${systemd_run_log}"
assert_state_id() {
  python3 - "$1" "$2" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["conversations"][0]["id"] == sys.argv[2]
PY
}
assert_old_state() { assert_state_id "$1" old-state; }
assert_installed_runtime_files() {
  local relative
  for relative in "${runtime_files[@]}"; do
    cmp -- "${stage}/${relative}" "${target}/${relative}"
  done
}
assert_installed_candidate() {
  assert_installed_runtime_files
  cmp -- "${stage}/deploy/public/nginx-00-hermes-security.conf" \
    "${nginx_security_target}"
  cmp -- "${stage}/deploy/public/nginx-daxueshenmai.top.conf" \
    "${nginx_site_target}"
  python3 - \
    "${managed_nodes_file}" "${status_token_file}" \
    "${installation_token_file}" "${hk_recovery_token_file}" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
node = payload["nodes"][0]
assert node["token_file"] == sys.argv[2]
assert node["installation_token_file"] == sys.argv[3]
assert sorted(node["installation_urls"]) == ["dbb3", "wsl"]
assert "hk" in node["recovery_urls"]
assert node["recovery_token_files"]["hk"] == sys.argv[4]
PY
}
assert_post_start_database_write() {
  python3 - "${managed_installations_db}" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as database:
    old_value = database.execute("SELECT value FROM marker").fetchone()[0]
    post_start_value = database.execute(
        "SELECT value FROM post_start_marker"
    ).fetchone()[0]
assert old_value == "old-managed-installation-state"
assert post_start_value == "status-failure-write"
PY
}
assert_pre_start_database_state() {
  python3 - "${managed_installations_db}" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as database:
    old_value = database.execute("SELECT value FROM marker").fetchone()[0]
    post_start_tables = database.execute(
        "SELECT count(*) FROM sqlite_master "
        "WHERE type = 'table' AND name = 'post_start_marker'"
    ).fetchone()[0]
assert old_value == "old-managed-installation-state"
assert post_start_tables == 0
PY
}
assert_started_only_in_home() {
  local log="$1" expected_home="$2" expected_count="$3"
  local total_count matching_count
  total_count="$(awk 'END { print NR }' "${log}")"
  matching_count="$(grep -Fxc -- "${expected_home}" "${log}" || true)"
  [[ "${total_count}" == "${expected_count}" \
      && "${matching_count}" == "${expected_count}" ]] || {
    printf 'unexpected service start homes (expected %s x%s):\n' \
      "${expected_home}" "${expected_count}" >&2
    sed 's/^/  /' "${log}" >&2
    exit 1
  }
}
assert_systemctl_subsequence() {
  local log="$1"
  shift
  local -a expected=("$@")
  local next=0 action
  while IFS= read -r action; do
    if (( next < ${#expected[@]} )) \
        && [[ "${action}" == "${expected[next]}" ]]; then
      ((next += 1))
    fi
  done <"${log}"
  (( next == ${#expected[@]} )) || {
    printf 'systemctl log did not contain the expected ordered actions: %s\n' \
      "${expected[*]}" >&2
    sed 's/^/  /' "${log}" >&2
    exit 1
  }
}
dispatcher_ready_from_dropin() {
  sed -n 's/^ConditionPathExists=//p' "$1" | tail -n 1
}
release_marker_from_dropin() {
  python3 - "$1" <<'PY'
import pathlib
import shlex
import sys

line = next(
    line for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
    if line.startswith("ExecCondition=")
)
print(shlex.split(line.removeprefix("ExecCondition="))[4])
PY
}
release_lease_from_dropin() {
  python3 - "$1" <<'PY'
import pathlib
import shlex
import sys

line = next(
    line for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
    if line.startswith("ExecCondition=")
)
print(shlex.split(line.removeprefix("ExecCondition="))[5])
PY
}
release_watchdog_from_dropin() {
  sed -n 's/^BindsTo=//p' "$1" | tail -n 1
}
assert_dispatcher_ready_guard() {
  local dropin="$1" expected_home="$2" expected_state="$3"
  local expected_binding="${4:-any}" ready_path marker_path
  [[ "$(grep -c '^ConditionPathExists=' "${dropin}")" == 1 ]]
  [[ "$(grep -c '^ExecCondition=' "${dropin}")" == 1 ]]
  python3 - \
    "${dropin}" "${expected_home}" "${bootstrap_python}" \
    "${target}" "hermes-agent-test.service" \
    "${stage}/${candidate_start_guard_asset}" "${expected_binding}" <<'PY'
import hashlib
import pathlib
import re
import shlex
import stat
import sys

dropin, expected_home, bootstrap, target_root, service, staged_guard, expected_binding = (
    pathlib.Path(sys.argv[1]),
    sys.argv[2],
    sys.argv[3],
    pathlib.Path(sys.argv[4]),
    sys.argv[5],
    pathlib.Path(sys.argv[6]),
    sys.argv[7],
)
lines = dropin.read_text(encoding="utf-8").splitlines()
assert lines[0] == "[Unit]"
index = 1
watchdog = None
if lines[index].startswith("BindsTo="):
    watchdog = lines[index].removeprefix("BindsTo=")
    assert re.fullmatch(r"hermes-release-watchdog-[0-9a-f]{32}\.service", watchdog)
    assert lines[index + 1] == f"After={watchdog}"
    index += 2
if expected_binding == "bound":
    assert watchdog is not None, lines
elif expected_binding == "unbound":
    assert watchdog is None, lines
else:
    assert expected_binding == "any"
assert lines[index].startswith("ConditionPathExists=")
assert lines[index + 1] == "[Service]"
assert lines[index + 2] == f"Environment=HERMES_HOME={expected_home}"
assert lines[index + 3].startswith("ExecCondition=")
assert len(lines) == index + 4, lines

command = shlex.split(lines[index + 3].removeprefix("ExecCondition="))
assert len(command) == 10, command
assert command[0] == f"+{bootstrap}", command
assert command[1] == "-I"
guard = pathlib.Path(command[2])
marker = pathlib.Path(command[4])
lease = pathlib.Path(command[5])
assert command[3] == "check"
assert command[6] == str(target_root)
assert command[7] == f"{target_root.stat().st_dev}:{target_root.stat().st_ino}"
assert command[8] == service
assert command[9] == f"{marker.parent.stat().st_dev}:{marker.parent.stat().st_ino}"
assert guard.parent == marker.parent == lease.parent
expected_guard_sha256 = hashlib.sha256(staged_guard.read_bytes()).hexdigest()
assert guard.name == f"candidate-start-guard.{expected_guard_sha256}.py"
assert marker.name == "candidate-pending.json"
assert lease.name == "candidate-start-lease.json"

metadata = guard.lstat()
assert stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)
assert metadata.st_uid == 0 and metadata.st_gid == 0
assert stat.S_IMODE(metadata.st_mode) == 0o755
assert guard.read_bytes() == staged_guard.read_bytes()
PY
  ready_path="$(dispatcher_ready_from_dropin "${dropin}")"
  marker_path="$(release_marker_from_dropin "${dropin}")"
  case "${ready_path}" in
    "$(dirname -- "${marker_path}")/.hermes-dispatcher-ready."*) ;;
    *)
      printf 'unexpected dispatcher ready guard: %s\n' \
        "${ready_path:-<unset>}" >&2
      exit 1
      ;;
  esac
  if [[ "${expected_state}" == published ]]; then
    [[ -f "${ready_path}" && ! -L "${ready_path}" ]]
    [[ "$(stat -c '%u:%g:%a' "${ready_path}")" == "0:0:600" ]]
    [[ "$(<"${ready_path}")" == \
      hermes-dispatcher-ready:0000000000000000000000000000000000000001:* ]]
  else
    [[ ! -e "${ready_path}" && ! -L "${ready_path}" ]]
  fi
}
assert_release_candidate_marker() {
  python3 - \
    "$1" "$2" "${target}" "hermes-agent-test.service" \
    "${version}" <<'PY'
import json
import pathlib
import re
import stat
import sys

marker = pathlib.Path(sys.argv[1])
expected_home = pathlib.Path(sys.argv[2])
target = pathlib.Path(sys.argv[3])
service = sys.argv[4]
version = sys.argv[5]
metadata = marker.lstat()
assert stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)
assert metadata.st_uid == 0 and metadata.st_gid == 0
assert stat.S_IMODE(metadata.st_mode) == 0o600
payload = json.loads(marker.read_text(encoding="utf-8"))
assert set(payload) == {
    "schema", "phase", "txid", "target_root", "target_identity",
    "runtime_home", "runtime_identity", "service", "version", "commit",
}
assert payload["schema"] == "hermes.release-candidate.v1"
assert payload["phase"] == "candidate"
assert re.fullmatch(r"[0-9a-f]{32}", payload["txid"])
assert payload["target_root"] == str(target)
assert payload["target_identity"] == f"{target.stat().st_dev}:{target.stat().st_ino}"
assert payload["runtime_home"] == str(expected_home)
assert payload["runtime_identity"] == (
    f"{expected_home.stat().st_dev}:{expected_home.stat().st_ino}"
)
assert payload["service"] == service
assert payload["version"] == version
assert payload["commit"] == "0000000000000000000000000000000000000001"
PY
}
assert_release_start_lease() {
  python3 - "$1" "$2" <<'PY'
import json
import pathlib
import re
import stat
import sys

marker = pathlib.Path(sys.argv[1])
lease = pathlib.Path(sys.argv[2])
metadata = lease.lstat()
assert stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)
assert metadata.st_uid == 0 and metadata.st_gid == 0
assert stat.S_IMODE(metadata.st_mode) == 0o600
marker_payload = json.loads(marker.read_text(encoding="utf-8"))
payload = json.loads(lease.read_text(encoding="utf-8"))
assert set(payload) == set(marker_payload) | {
    "installer_pid", "installer_starttime", "boot_id",
}
assert payload["schema"] == "hermes.release-start-lease.v1"
for key, value in marker_payload.items():
    if key == "schema":
        continue
    assert payload[key] == value, key
assert isinstance(payload["installer_pid"], int) and payload["installer_pid"] > 1
assert re.fullmatch(r"[0-9]+", payload["installer_starttime"])
assert re.fullmatch(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    payload["boot_id"],
)
PY
}
assert_orphaned_release_start_lease() {
  python3 - \
    "$1" "$2" "${target}" "hermes-agent-test.service" "${version}" <<'PY'
import json
import pathlib
import re
import stat
import sys

lease = pathlib.Path(sys.argv[1])
runtime_home = pathlib.Path(sys.argv[2])
target = pathlib.Path(sys.argv[3])
service = sys.argv[4]
version = sys.argv[5]
metadata = lease.lstat()
assert stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)
assert metadata.st_uid == 0 and metadata.st_gid == 0
assert stat.S_IMODE(metadata.st_mode) == 0o600
payload = json.loads(lease.read_text(encoding="utf-8"))
assert payload["schema"] == "hermes.release-start-lease.v1"
assert payload["phase"] == "candidate"
assert re.fullmatch(r"[0-9a-f]{32}", payload["txid"])
assert payload["target_root"] == str(target)
assert payload["target_identity"] == f"{target.stat().st_dev}:{target.stat().st_ino}"
assert payload["runtime_home"] == str(runtime_home)
assert payload["runtime_identity"] == (
    f"{runtime_home.stat().st_dev}:{runtime_home.stat().st_ino}"
)
assert payload["service"] == service
assert payload["version"] == version
assert payload["commit"] == "0000000000000000000000000000000000000001"
assert isinstance(payload["installer_pid"], int) and payload["installer_pid"] > 1
PY
}
python3 - "${managed_installations_db}" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as database:
    database.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    database.execute("INSERT INTO marker VALUES ('old-managed-installation-state')")
PY
for relative in "${runtime_files[@]}"; do
  install -D -m 0644 "${repo}/${relative}" "${stage}/${relative}"
  install -D -m 0644 /dev/null "${target}/${relative}"
  printf 'old:%s\n' "${relative}" >"${target}/${relative}"
done
runtime_source_manifest="${stage}/deploy/public/runtime-source-files.nul"
install -d -m 0700 "$(dirname "${runtime_source_manifest}")"
for relative in "${runtime_files[@]}"; do
  case "${relative}" in
    *.py) printf '%s\0' "${relative}" ;;
  esac
done | sort -zu >"${runtime_source_manifest}"
for relative in "${nginx_files[@]}"; do
  install -D -m 0644 "${repo}/${relative}" "${stage}/${relative}"
done
install -D -m 0644 \
  "${repo}/${managed_nodes_template}" "${stage}/${managed_nodes_template}"
install -D -m 0644 \
  "${repo}/deploy/public/runtime-requirements.lock" \
  "${stage}/deploy/public/runtime-requirements.lock"
install -D -m 0755 \
  "${repo}/${candidate_start_guard_asset}" \
  "${stage}/${candidate_start_guard_asset}"
install -D -m 0755 \
  "${repo}/${runtime_home_guard_asset}" \
  "${stage}/${runtime_home_guard_asset}"
install -D -m 0755 \
  "${repo}/${profile_runtime_io_asset}" \
  "${stage}/${profile_runtime_io_asset}"
printf '%s\n' "old:nginx-security" >"${nginx_security_target}"
printf '%s\n' "old:nginx-site" >"${nginx_site_target}"
cat >"${sshd_original}" <<'EOF'
Match User admin
    AllowTcpForwarding yes
    GatewayPorts clientspecified
    PermitListen 127.0.0.1:19122 10.66.0.1:8081
Match all
    AllowTcpForwarding local
EOF
cp "${sshd_original}" "${sshd_config}"

cat >"${fake_bin}/systemctl" <<'SH'
#!/usr/bin/env bash
set -u
effective_systemd_environment() {
  if [[ -n "${FAKE_DAEMON_RELOAD_MARKER:-}" \
      && -e "${FAKE_DAEMON_RELOAD_MARKER}" ]]; then
    printf '%s\n' \
      "${FAKE_SYSTEMD_ENVIRONMENT_AFTER_RELOAD:-${FAKE_SYSTEMD_ENVIRONMENT:-}}"
  else
    printf '%s\n' "${FAKE_SYSTEMD_ENVIRONMENT:-}"
  fi
}
effective_hermes_home() {
  local environment token effective_home=""
  environment="$(effective_systemd_environment)"
  while IFS= read -r token; do
    token="${token#\"}"
    token="${token%\"}"
    if [[ "${token}" == HERMES_HOME=* ]]; then
      effective_home="${token#HERMES_HOME=}"
    fi
  done < <(printf '%s\n' "${environment}" | tr ' ' '\n')
  printf '%s\n' "${effective_home:-<unset>}"
}
requested_unit() {
  local argument unit="${FAKE_MAIN_SYSTEMD_SERVICE:-hermes-agent-test.service}"
  for argument in "$@"; do
    if [[ "${argument}" =~ ^[A-Za-z0-9_.@:-]+\.service$ ]]; then
      unit="${argument}"
    fi
  done
  printf '%s\n' "${unit}"
}
fake_state_path() {
  local unit="$1"
  if [[ "${unit}" == "${FAKE_MAIN_SYSTEMD_SERVICE:-hermes-agent-test.service}" ]]; then
    printf '%s\n' "${FAKE_SYSTEMCTL_STATE_FILE:-}"
  else
    printf '%s.unit.%s\n' "${FAKE_SYSTEMCTL_STATE_FILE:-}" "${unit}"
  fi
}
fake_service_state() {
  local unit="${1:-${FAKE_MAIN_SYSTEMD_SERVICE:-hermes-agent-test.service}}" state_path
  state_path="$(fake_state_path "${unit}")"
  if [[ -n "${state_path}" && -f "${state_path}" ]]; then
    /bin/cat -- "${state_path}"
  else
    if [[ "${unit}" == "${FAKE_MAIN_SYSTEMD_SERVICE:-hermes-agent-test.service}" ]]; then
      printf '%s\n' active
    else
      printf '%s\n' inactive
    fi
  fi
}
set_fake_service_state() {
  local state="$1" unit="${2:-${FAKE_MAIN_SYSTEMD_SERVICE:-hermes-agent-test.service}}"
  local state_path temporary
  [[ -n "${FAKE_SYSTEMCTL_STATE_FILE:-}" ]] || return 0
  state_path="$(fake_state_path "${unit}")"
  temporary="${state_path}.new-$$"
  printf '%s\n' "${state}" >"${temporary}"
  /usr/bin/mv -f -- "${temporary}" "${state_path}"
}
run_loaded_start_conditions() {
  [[ -n "${FAKE_DAEMON_RELOAD_MARKER:-}" \
      && -e "${FAKE_DAEMON_RELOAD_MARKER}" \
      && -n "${FAKE_SYSTEMD_PROFILE_DROPIN:-}" \
      && -f "${FAKE_SYSTEMD_PROFILE_DROPIN}" ]] || return 0
  python3 - \
    "${FAKE_SYSTEMD_PROFILE_DROPIN}" \
    "${FAKE_SYSTEMCTL_CONDITION_LOG:-}" \
    "${FAKE_SYSTEMD_EFFECTIVE_UNIT_FILE:-}" <<'PY'
import os
import pathlib
import shlex
import subprocess
import sys

dropin = pathlib.Path(sys.argv[1])
log_path = pathlib.Path(sys.argv[2]) if sys.argv[2] else None
effective_unit_path = pathlib.Path(sys.argv[3]) if sys.argv[3] else dropin


def record(message: str) -> None:
    if log_path is None:
        return
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(message + "\n")


for raw_line in effective_unit_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if line.startswith("ConditionPathExists="):
        value = line.removeprefix("ConditionPathExists=")
        negate = value.startswith("!")
        path = pathlib.Path(value[1:] if negate else value)
        passed = path.exists() != negate
        record(f"ConditionPathExists passed={int(passed)} path={path}")
        if not passed:
            raise SystemExit(10)
    elif line.startswith("ExecCondition="):
        value = line.removeprefix("ExecCondition=")
        try:
            command = shlex.split(value)
        except ValueError as error:
            record(f"ExecCondition parse-error={error}")
            raise SystemExit(20) from error
        if not command:
            record("ExecCondition empty")
            raise SystemExit(20)
        # systemd executable prefixes alter privilege/error handling and are
        # not part of argv[0].  The harness runs as root, so stripping them is
        # sufficient to exercise the actual guard program and state files.
        command[0] = command[0].lstrip("-@:+!")
        if not command[0]:
            record("ExecCondition missing executable")
            raise SystemExit(20)
        result = subprocess.run(command, check=False)
        record(f"ExecCondition rc={result.returncode} argv={shlex.join(command)}")
        if result.returncode == 0:
            continue
        if 1 <= result.returncode <= 254:
            raise SystemExit(10)
        raise SystemExit(20)
PY
}
if [[ "${1:-}" != "show" && "${1:-}" != "cat" \
    && "${1:-}" != "--version" ]]; then
  printf '%s\n' "${1:-}" >>"${FAKE_SYSTEMCTL_LOG}"
fi
case "${1:-}" in
  --version)
    printf '%s\n' 'systemd 252 (252.38-1~deb12u1)'
    exit 0
    ;;
  cat)
    if [[ -n "${FAKE_DAEMON_RELOAD_MARKER:-}" \
        && -e "${FAKE_DAEMON_RELOAD_MARKER}" ]]; then
      if [[ -n "${FAKE_SYSTEMD_EFFECTIVE_UNIT_FILE:-}" ]]; then
        /bin/cat -- "${FAKE_SYSTEMD_EFFECTIVE_UNIT_FILE}"
      elif [[ -n "${FAKE_SYSTEMD_PROFILE_DROPIN:-}" \
          && -f "${FAKE_SYSTEMD_PROFILE_DROPIN}" ]]; then
        /bin/cat -- "${FAKE_SYSTEMD_PROFILE_DROPIN}"
      fi
    fi
    exit 0
    ;;
  show)
    if [[ "$*" == *"ExecStart"* ]]; then
      printf '%s\n' "${FAKE_SYSTEMD_EXEC_START:-}"
    elif [[ "$*" == *"DropInPaths"* ]]; then
      if [[ -n "${FAKE_DAEMON_RELOAD_MARKER:-}" \
          && -e "${FAKE_DAEMON_RELOAD_MARKER}" ]]; then
        printf '%s\n' "${FAKE_SYSTEMD_DROPIN_PATHS_AFTER_RELOAD:-}"
      else
        printf '%s\n' "${FAKE_SYSTEMD_DROPIN_PATHS:-}"
      fi
    elif [[ "$*" == *"ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,NRestarts"* ]]; then
      state="$(fake_service_state "$(requested_unit "$@")")"
      if [[ "${state}" == activating ]]; then
        printf '%s\n' \
          'ActiveState=activating' \
          'SubState=auto-restart' \
          'Result=exit-code' \
          'ExecMainCode=1' \
          'ExecMainStatus=1' \
          'NRestarts=3'
      else
        printf '%s\n' \
          "ActiveState=${state}" \
          'SubState=dead' \
          'Result=success' \
          'ExecMainCode=0' \
          'ExecMainStatus=0' \
          'NRestarts=0'
      fi
    else
      effective_systemd_environment
    fi
    exit 0
    ;;
  daemon-reload)
    if [[ -n "${FAKE_DAEMON_RELOAD_MARKER:-}" ]]; then
      if [[ -n "${FAKE_SYSTEMD_PROFILE_DROPIN:-}" ]]; then
        if [[ -f "${FAKE_SYSTEMD_PROFILE_DROPIN}" ]]; then
          : >"${FAKE_DAEMON_RELOAD_MARKER}"
        else
          rm -f -- "${FAKE_DAEMON_RELOAD_MARKER}"
        fi
      else
        : >"${FAKE_DAEMON_RELOAD_MARKER}"
      fi
    fi
    exit 0
    ;;
  reload)
    if [[ "${FAKE_SSH_RELOAD_FAIL_ONCE:-0}" == 1 \
      && ! -e "${FAKE_SSH_RELOAD_MARKER:-/nonexistent}" ]]; then
      : >"${FAKE_SSH_RELOAD_MARKER}"
      exit 1
    fi
    exit 0
    ;;
  stop)
    unit="$(requested_unit "$@")"
    set_fake_service_state inactive "${unit}"
    if [[ "${unit}" != "${FAKE_MAIN_SYSTEMD_SERVICE:-hermes-agent-test.service}" \
        && -n "${FAKE_DAEMON_RELOAD_MARKER:-}" \
        && -e "${FAKE_DAEMON_RELOAD_MARKER}" \
        && -n "${FAKE_SYSTEMD_PROFILE_DROPIN:-}" \
        && -f "${FAKE_SYSTEMD_PROFILE_DROPIN}" ]]; then
      binding="$(sed -n 's/^BindsTo=//p' \
        "${FAKE_SYSTEMD_PROFILE_DROPIN}" | tail -n 1)"
      if [[ "${binding}" == "${unit}" ]]; then
        set_fake_service_state inactive \
          "${FAKE_MAIN_SYSTEMD_SERVICE:-hermes-agent-test.service}"
      fi
    fi
    exit 0
    ;;
  start)
    unit="$(requested_unit "$@")"
    if [[ "${unit}" == "${FAKE_MAIN_SYSTEMD_SERVICE:-hermes-agent-test.service}" ]]; then
      set +e
      run_loaded_start_conditions
      condition_status=$?
      set -e
      if [[ "${condition_status}" == 10 ]]; then
        set_fake_service_state inactive "${unit}"
        exit 0
      elif [[ "${condition_status}" != 0 ]]; then
        set_fake_service_state failed "${unit}"
        exit 1
      fi
    fi
    if [[ -n "${FAKE_SYSTEMCTL_START_HOME_LOG:-}" ]]; then
      effective_hermes_home >>"${FAKE_SYSTEMCTL_START_HOME_LOG}"
    fi
    if [[ "${FAKE_STATUS_FAIL:-0}" == 1 ]]; then
      set_fake_service_state activating "${unit}"
    else
      set_fake_service_state active "${unit}"
    fi
    if [[ "${FAKE_STATUS_FAIL:-0}" == 1 \
        && ! -e "${HERMES_COLLABORATION_STATE_FILE}.mutated" ]]; then
      printf '%s\n' '{"conversations":[{"id":"new-state"}]}' \
        >"${HERMES_COLLABORATION_STATE_FILE}"
      python3 - "${HERMES_HOME_DIR}/managed-installations.db" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as database:
    database.execute(
        "CREATE TABLE IF NOT EXISTS post_start_marker (value TEXT NOT NULL)"
    )
    database.execute("DELETE FROM post_start_marker")
    database.execute("INSERT INTO post_start_marker VALUES ('status-failure-write')")
PY
      : >"${HERMES_COLLABORATION_STATE_FILE}.mutated"
    fi
    if [[ "${FAKE_SIGNAL_ON_START:-0}" == 1 \
        && ! -e "${HERMES_COLLABORATION_STATE_FILE}.signaled" ]]; then
      printf '%s\n' '{"conversations":[{"id":"signal-state"}]}' \
        >"${HERMES_COLLABORATION_STATE_FILE}"
      : >"${HERMES_COLLABORATION_STATE_FILE}.signaled"
      kill -TERM "${PPID}"
    fi
    if [[ "${FAKE_DISPATCHER_SENTINEL_ON_START:-0}" == 1 \
        && -d "${FAKE_DISPATCHER_HOME:-/nonexistent}" \
        && -n "$(find "${FAKE_DISPATCHER_HOME}" -maxdepth 1 \
          -name '.hermes-dispatcher-migration.*' -print -quit)" \
        && ! -e "${FAKE_DISPATCHER_HOME}/post-start-write.txt" ]]; then
      printf '%s\n' 'accepted-after-dispatcher-start' \
        >"${FAKE_DISPATCHER_HOME}/post-start-write.txt"
    fi
    exit 0
    ;;
  is-active)
    [[ "$(fake_service_state "$(requested_unit "$@")")" == active ]]
    ;;
  *) exit 0 ;;
esac
SH
cat >"${fake_bin}/systemd-run" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
unit=""
for argument in "$@"; do
  case "${argument}" in
    --unit=*) unit="${argument#--unit=}" ;;
  esac
done
[[ "${unit}" =~ ^[A-Za-z0-9_.@:-]+\.service$ ]]
printf '%s\n' "$*" >>"${FAKE_SYSTEMD_RUN_LOG}"
state_path="${FAKE_SYSTEMCTL_STATE_FILE}.unit.${unit}"
temporary="${state_path}.new-$$"
printf '%s\n' active >"${temporary}"
/usr/bin/mv -f -- "${temporary}" "${state_path}"
SH
cat >"${fake_bin}/getent" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "passwd" && "${2:-}" == "${FAKE_SERVICE_USER:-}" \
    && -n "${FAKE_SERVICE_HOME:-}" ]]; then
  entry="$(/usr/bin/getent passwd "$2")"
  IFS=: read -r name password uid gid gecos _ shell <<<"${entry}"
  printf '%s:%s:%s:%s:%s:%s:%s\n' \
    "${name}" "${password}" "${uid}" "${gid}" "${gecos}" \
    "${FAKE_SERVICE_HOME}" "${shell}"
else
  exec /usr/bin/getent "$@"
fi
SH
cat >"${fake_bin}/journalctl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_JOURNALCTL_LOG}"
printf '%s\n' \
  'dashboard startup failed: token=super-secret-diagnostic-token-value' \
  'Traceback: RuntimeError: failed before bind'
SH
cat >"${fake_bin}/sshd" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
config=""
mode=""
while (($#)); do
  case "$1" in
    -t|-T) mode="$1" ;;
    -f) shift; config="$1" ;;
  esac
  shift
done
[[ -n "${config}" && -f "${config}" ]]
if [[ "${mode}" == "-T" ]]; then
  printf '%s\n' 'allowtcpforwarding yes'
  awk 'tolower($1) == "permitlisten" { $1="permitlisten"; print }' "${config}"
fi
SH
cat >"${fake_bin}/mv" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
target="${!#}"
if [[ "${FAKE_SSH_MV_FAIL_ONCE:-0}" == 1 \
  && "${target}" == "${HERMES_SSHD_CONFIG}" \
  && ! -e "${FAKE_SSH_MV_MARKER}" ]]; then
  /usr/bin/mv "$@"
  : >"${FAKE_SSH_MV_MARKER}"
  exit 1
fi
exec /usr/bin/mv "$@"
SH
cat >"${fake_bin}/ssh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
command="${!#}"
printf '%s\n' "${command}" >>"${FAKE_DEPLOY_SSH_LOG}"
if [[ "${command}" == "tar --no-same-owner "* ]]; then
  cat >/dev/null
fi
if [[ "${command}" == *"for root in /dev/shm/hermes-agent-deploy"* ]]; then
  printf '%s\n' '/tmp/hermes-agent-deploy'
  exit 0
fi
if [[ "${FAKE_DEPLOY_CONFIGURE_FAIL:-0}" == 1 \
  && "${command}" == *"sudo -n /bin/bash"* \
  && "${command}" == *"configure-main-managed-installation-ssh.sh"* ]]; then
  exit 1
fi
SH
cat >"${fake_bin}/scp" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_DEPLOY_SCP_LOG}"
[[ "${FAKE_DEPLOY_SCP_FAIL:-0}" != 1 ]]
SH
cat >"${fake_bin}/nginx" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FAKE_NGINX_LOG}"
[[ "${FAKE_NGINX_FAIL:-0}" != 1 ]]
SH
cat >"${fake_bin}/sleep" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat >"${fake_bin}/curl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
output=""
next_is_output=0
write_out=""
next_is_write_out=0
data_file=""
next_is_data=0
for arg in "$@"; do
  if [[ "${next_is_output}" == 1 ]]; then
    output="${arg}"
    next_is_output=0
  elif [[ "${next_is_write_out}" == 1 ]]; then
    write_out="${arg}"
    next_is_write_out=0
  elif [[ "${arg}" == "-o" ]]; then
    next_is_output=1
  elif [[ "${arg}" == "--write-out" ]]; then
    next_is_write_out=1
  elif [[ "${next_is_data}" == 1 ]]; then
    data_file="${arg#@}"
    next_is_data=0
  elif [[ "${arg}" == "--data-binary" ]]; then
    next_is_data=1
  fi
done
url="${!#}"
if [[ "${url}" == */api/status ]]; then
  [[ "${FAKE_STATUS_FAIL:-0}" != 1 ]] || exit 22
  payload='{"status":"ok"}'
elif [[ "${url}" == */api/mobile/v1/handshake ]]; then
  [[ "${FAKE_HANDSHAKE_FAIL:-0}" != 1 ]] || exit 22
  payload='{"api_version":1,"hermes_version":"test","profiles":[],"capabilities":[],"server_time":1784443200}'
elif [[ "${url}" == */api/plugins/ios-intelligence/health ]]; then
  payload="$(IOS_CAPABILITIES_COUNT="${ios_capabilities_count}" python3 - <<'PY'
import json
import os

capability_count = int(os.environ["IOS_CAPABILITIES_COUNT"])
services = [
    {"name": f"service-{index}", "ok": True, "tools": ["read", "write"] + (["extra"] if index < 2 else [])}
    for index in range(capability_count)
]
print(json.dumps({
    "ok": True,
    "scheduler_running": True,
    "mcp_runtime": {
        "ok": True,
        "running": True,
        "healthy_count": capability_count,
        "required_count": capability_count,
        "services": services,
    },
}))
PY
  )"
elif [[ "${url}" == */api/plugins/collaboration/connector/deployment-health ]]; then
  payload="$(python3 - "${HERMES_AGENT_ROOT}/plugins/collaboration/dashboard/manifest.json" <<'PY'
import hashlib
import json
import sys

manifest_bytes = open(sys.argv[1], "rb").read()
manifest = json.loads(manifest_bytes)
database = {
    "ok": True,
    "code_schema_version": 1,
    "db_user_version": 1,
    "integrity_check": "ok",
    "schema_sha256": "0" * 64,
    "required_tables": [],
    "required_triggers": [],
}
managed = {
    **database,
    "catalog_rows": 0,
    "required_tables": ["managed_resource_catalog"],
    "required_triggers": ["managed_installation_source_lock_immutable"],
}
print(json.dumps({
    "ok": True,
    "connector_id": "dbb3-primary",
    "contract_version": 2,
    "manifest_version": manifest["version"],
    "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    "managed_catalog_readable": True,
    "databases": {
        "cloud_files": database,
        "mobile_auth": database,
        "managed_resources": managed,
    },
}))
PY
)"
elif [[ "${url}" == */_hermes/installations/dbb3/health ]]; then
  release_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "${HERMES_AGENT_ROOT}/plugins/collaboration/dashboard/manifest.json")"
  payload="{\"ok\":true,\"node_id\":\"dbb3\",\"installations\":true,\"recovery\":false,\"release\":{\"commit\":\"0000000000000000000000000000000000000001\",\"version\":\"${release_version}\"}}"
elif [[ "${url}" == */_hermes/installations/wsl/health ]]; then
  release_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "${HERMES_AGENT_ROOT}/plugins/collaboration/dashboard/manifest.json")"
  payload="{\"ok\":true,\"node_id\":\"wsl\",\"installations\":true,\"recovery\":false,\"release\":{\"commit\":\"0000000000000000000000000000000000000001\",\"version\":\"${release_version}\"}}"
elif [[ "${url}" =~ /_hermes/installations/(dbb3|wsl)$ ]]; then
  node="${BASH_REMATCH[1]}"
  probe_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "${data_file}")"
  payload="{\"accepted\":true,\"id\":\"${probe_id}\",\"state\":\"completed\",\"node_id\":\"${node}\"}"
elif [[ "${url}" =~ /_hermes/installations/(dbb3|wsl)/(mi-[0-9a-f]+)$ ]]; then
  node="${BASH_REMATCH[1]}"
  probe_id="${BASH_REMATCH[2]}"
  payload="{\"id\":\"${probe_id}\",\"node_id\":\"${node}\",\"state\":\"completed\",\"detail\":{\"probe\":true,\"persisted\":true}}"
else
  payload='{"ok":true,"contract_version":2,"connector_id":"dbb3-primary","capabilities":["artifact-upload","attachment-download"]}'
fi
if [[ -n "${output}" ]]; then
  printf '%s\n' "${payload}" >"${output}"
else
  printf '%s\n' "${payload}"
fi
if [[ -n "${write_out}" ]]; then
  printf '%s' '200'
fi
SH
chmod 0755 "${fake_bin}/systemctl" "${fake_bin}/systemd-run" \
  "${fake_bin}/getent" \
  "${fake_bin}/sshd" "${fake_bin}/mv" \
  "${fake_bin}/ssh" "${fake_bin}/scp" "${fake_bin}/nginx" \
  "${fake_bin}/sleep" "${fake_bin}/curl" "${fake_bin}/journalctl"

ssh_configurator="${repo}/deploy/recovery/configure-main-managed-installation-ssh.sh"
ssh_reload_marker="${work}/ssh-reload-failed"
ssh_mv_marker="${work}/ssh-mv-failed"
run_ssh_configurator() {
  env \
    PATH="${fake_bin}:${PATH}" \
    FAKE_SSH_RELOAD_FAIL_ONCE="${1:-0}" \
    FAKE_SSH_RELOAD_MARKER="${ssh_reload_marker}" \
    FAKE_SSH_MV_FAIL_ONCE="${2:-0}" \
    FAKE_SSH_MV_MARKER="${ssh_mv_marker}" \
    FAKE_SYSTEMCTL_LOG="${work}/sshd-systemctl.log" \
    HERMES_SSHD_CONFIG="${sshd_config}" \
    HERMES_SSHD_BINARY="${fake_bin}/sshd" \
    HERMES_SSHD_SERVICE="ssh-test.service" \
    HERMES_SSHD_LOCK_FILE="${work}/sshd-install.lock" \
    HERMES_BACKUP_ROOT="${backup}" \
    /bin/bash "${ssh_configurator}"
}

run_ssh_configurator 0 >"${work}/sshd-success.stdout"
grep -Eq 'PermitListen .*127\.0\.0\.1:19123' "${sshd_config}"
grep -Eq 'PermitListen .*127\.0\.0\.1:19124' "${sshd_config}"
sshd_hash="$(sha256sum "${sshd_config}" | cut -d' ' -f1)"
run_ssh_configurator 0 >"${work}/sshd-idempotent.stdout"
[[ "$(sha256sum "${sshd_config}" | cut -d' ' -f1)" == "${sshd_hash}" ]]
cp "${sshd_original}" "${sshd_config}"
rm -f -- "${ssh_reload_marker}"
set +e
run_ssh_configurator 1 >"${work}/sshd-failure.stdout" 2>"${work}/sshd-failure.stderr"
sshd_failure_status=$?
set -e
[[ "${sshd_failure_status}" -ne 0 ]]
cmp -- "${sshd_original}" "${sshd_config}"
cp "${sshd_original}" "${sshd_config}"
rm -f -- "${ssh_mv_marker}"
set +e
run_ssh_configurator 0 1 >"${work}/sshd-mv-failure.stdout" \
  2>"${work}/sshd-mv-failure.stderr"
sshd_mv_failure_status=$?
set -e
[[ "${sshd_mv_failure_status}" -ne 0 ]]
cmp -- "${sshd_original}" "${sshd_config}"

deployer="${repo}/deploy/public/deploy-collaboration-backend.sh"
deploy_ssh_log="${work}/deploy-ssh.log"
deploy_scp_log="${work}/deploy-scp.log"
: >"${deploy_ssh_log}"
: >"${deploy_scp_log}"
set +e
env \
  PATH="${fake_bin}:${PATH}" \
  FAKE_DEPLOY_CONFIGURE_FAIL=1 \
  FAKE_DEPLOY_SSH_LOG="${deploy_ssh_log}" \
  FAKE_DEPLOY_SCP_LOG="${deploy_scp_log}" \
  HERMES_REPO="${repo}" \
  HERMES_LOCAL_PYTHON="${runtime_python}" \
  HERMES_COLLABORATION_VERSION="${version}" \
  HERMES_PUBLIC_REMOTE="admin@test.invalid" \
  /bin/bash "${deployer}" >"${work}/deployer-failure.stdout" \
    2>"${work}/deployer-failure.stderr"
deployer_failure_status=$?
set -e
[[ "${deployer_failure_status}" -ne 0 ]]
grep -Fq "configure-main-managed-installation-ssh.sh" "${deploy_ssh_log}"
grep -Fq "${repo}/${candidate_start_guard_asset}" "${deploy_scp_log}"
if grep -Eq "sudo -n /bin/bash .*install-collaboration-backend\.sh" "${deploy_ssh_log}"; then
  printf '%s\n' "installer ran after SSH configuration failure" >&2
  exit 1
fi
[[ "$(tail -n 1 "${deploy_ssh_log}")" == "rm -rf -- "* ]]

: >"${deploy_ssh_log}"
: >"${deploy_scp_log}"
set +e
env \
  PATH="${fake_bin}:${PATH}" \
  FAKE_DEPLOY_SCP_FAIL=1 \
  FAKE_DEPLOY_SSH_LOG="${deploy_ssh_log}" \
  FAKE_DEPLOY_SCP_LOG="${deploy_scp_log}" \
  HERMES_REPO="${repo}" \
  HERMES_LOCAL_PYTHON="${runtime_python}" \
  HERMES_COLLABORATION_VERSION="${version}" \
  HERMES_PUBLIC_REMOTE="admin@test.invalid" \
  /bin/bash "${deployer}" >"${work}/deployer-upload.stdout" \
    2>"${work}/deployer-upload.stderr"
deployer_upload_status=$?
set -e
[[ "${deployer_upload_status}" -ne 0 ]]
[[ -s "${deploy_scp_log}" ]]
if grep -Eq "sudo -n /bin/bash .*install-collaboration-backend\.sh" "${deploy_ssh_log}"; then
  printf '%s\n' "installer ran after upload failure" >&2
  exit 1
fi
[[ "$(tail -n 1 "${deploy_ssh_log}")" == "rm -rf -- "* ]]

run_installer() {
  local timing_start="${EPOCHREALTIME}" timing_status=0 label="installer"
  if _run_installer_body "$@"; then
    :
  else
    timing_status=$?
  fi
  harness_report_timing "installer ${1:-}" "${timing_status}" "${timing_start}"
  return "${timing_status}"
}
harness_report_timing() {
  printf 'harness-timing: %s status=%s elapsed=%ss\n' "$1" "${2}" "$(awk -v a="$3" -v b="${EPOCHREALTIME}" 'BEGIN { printf "%.2f", b - a }')" >&2
}
_run_installer_body() {
  env \
    PATH="${fake_bin}:${PATH}" \
    FAKE_STATUS_FAIL="$1" \
    FAKE_SIGNAL_ON_START="${2:-0}" \
    FAKE_HANDSHAKE_FAIL="${3:-0}" \
    FAKE_NGINX_FAIL="${4:-0}" \
    HERMES_DEPLOY_FAIL_PHASE="${5:-}" \
    HERMES_DEPLOY_HARD_KILL_PHASE="${7:-}" \
    HERMES_HK_ENABLED="${6:-1}" \
    HERMES_RUNTIME_SOURCE_MIN_FILES=1 \
    HERMES_DEBUG_INSTALLER="${HERMES_DEBUG_INSTALLER:-0}" \
    IOS_CAPABILITIES_COUNT="${ios_capabilities_count}" \
    HERMES_AGENT_ROOT="${target}" \
    HERMES_BOOTSTRAP_PYTHON="${bootstrap_python}" \
    HERMES_RUNTIME_PYTHON="${runtime_python}" \
    HERMES_AGENT_SERVICE="hermes-agent-test.service" \
    HERMES_AGENT_USER="${credential_service_user}" \
    HERMES_AGENT_GROUP="${credential_service_group}" \
    HERMES_DISPATCHER_STATE_ROOT="${migration_service_home}/.hermes" \
    HERMES_SYSTEMCTL_BINARY="${fake_bin}/systemctl" \
    HERMES_SYSTEMD_RUN_BINARY="${fake_bin}/systemd-run" \
    HERMES_SETPRIV_BINARY="$(command -v setpriv)" \
    HERMES_JOURNALCTL_BINARY="${fake_bin}/journalctl" \
    HERMES_CURL_BINARY="${fake_bin}/curl" \
    HERMES_STAGE_OWNER="root" \
    HERMES_BACKUP_ROOT="${backup}" \
    HERMES_INSTALL_LOCK_FILE="${work}/collaboration-install.lock" \
    HERMES_COLLABORATION_STATE_FILE="${state_file}" \
    HERMES_HOME_DIR="${runtime_home}" \
    HERMES_COLLABORATION_CONNECTOR_TOKEN_FILE="${token_file}" \
    HERMES_MANAGED_NODE_TOKEN_FILE="${status_token_file}" \
    HERMES_MANAGED_INSTALLATION_TOKEN_FILE="${installation_token_file}" \
    HERMES_HK_RECOVERY_TOKEN_FILE="${hk_recovery_token_file}" \
    HERMES_AGENT_ENV_FILE="${agent_env_file}" \
    HERMES_AGENT_PROFILE_DROPIN="${agent_profile_dropin}" \
    FAKE_SYSTEMD_ENVIRONMENT="HERMES_HOME=${runtime_home}" \
    FAKE_SYSTEMD_ENVIRONMENT_AFTER_RELOAD="HERMES_HOME=${runtime_home}" \
    FAKE_DAEMON_RELOAD_MARKER="${agent_reload_marker}" \
    FAKE_SYSTEMD_PROFILE_DROPIN="${agent_profile_dropin}" \
    FAKE_SYSTEMD_EFFECTIVE_UNIT_FILE="${8:-${FAKE_SYSTEMD_EFFECTIVE_UNIT_FILE:-}}" \
    FAKE_SYSTEMD_DROPIN_PATHS_AFTER_RELOAD="${agent_profile_dropin}" \
    HERMES_NGINX_SECURITY_TARGET="${nginx_security_target}" \
    HERMES_NGINX_SITE_TARGET="${nginx_site_target}" \
    HERMES_NGINX_SERVICE="nginx-test.service" \
    HERMES_NGINX_BINARY="${fake_bin}/nginx" \
    HERMES_SYSTEMCTL_BINARY="${fake_bin}/systemctl" \
    HERMES_SYSTEMD_RUN_BINARY="${fake_bin}/systemd-run" \
    HERMES_SETPRIV_BINARY="$(command -v setpriv)" \
    HERMES_RELEASE_EVIDENCE_FILE="${release_evidence_file}" \
    HERMES_RELEASE_PENDING_MARKER="${release_pending_marker}" \
    FAKE_SYSTEMCTL_LOG="${work}/systemctl.log" \
    FAKE_SYSTEMCTL_START_HOME_LOG="${work}/systemctl-start-home.log" \
    FAKE_SYSTEMCTL_STATE_FILE="${systemctl_state_file}" \
    FAKE_SYSTEMCTL_CONDITION_LOG="${systemctl_condition_log}" \
    FAKE_MAIN_SYSTEMD_SERVICE="hermes-agent-test.service" \
    FAKE_SYSTEMD_RUN_LOG="${systemd_run_log}" \
    FAKE_JOURNALCTL_LOG="${work}/journalctl.log" \
    FAKE_NGINX_LOG="${work}/nginx.log" \
    /bin/bash "${installer}" "${version}" "${stage}" \
      0000000000000000000000000000000000000001
}

run_fake_main_systemctl() {
  env \
    PATH="${fake_bin}:${PATH}" \
    FAKE_STATUS_FAIL=0 \
    FAKE_SIGNAL_ON_START=0 \
    FAKE_SYSTEMD_ENVIRONMENT="HERMES_HOME=${runtime_home}" \
    FAKE_SYSTEMD_ENVIRONMENT_AFTER_RELOAD="HERMES_HOME=${runtime_home}" \
    FAKE_DAEMON_RELOAD_MARKER="${agent_reload_marker}" \
    FAKE_SYSTEMD_PROFILE_DROPIN="${agent_profile_dropin}" \
    FAKE_SYSTEMD_EFFECTIVE_UNIT_FILE="${FAKE_SYSTEMD_EFFECTIVE_UNIT_FILE:-}" \
    FAKE_SYSTEMCTL_LOG="${work}/systemctl.log" \
    FAKE_SYSTEMCTL_START_HOME_LOG="${work}/systemctl-start-home.log" \
    FAKE_SYSTEMCTL_STATE_FILE="${systemctl_state_file}" \
    FAKE_SYSTEMCTL_CONDITION_LOG="${systemctl_condition_log}" \
    FAKE_MAIN_SYSTEMD_SERVICE="hermes-agent-test.service" \
    HERMES_COLLABORATION_STATE_FILE="${state_file}" \
    HERMES_HOME_DIR="${runtime_home}" \
    systemctl "$@"
}

assert_fixture_credentials_unchanged() {
  for credential_file in "${status_token_file}" "${installation_token_file}" \
    "${hk_recovery_token_file}"; do
    [[ "$(stat -c '%U:%G:%a' "${credential_file}")" == \
      "root:${credential_source_group}:600" ]] || {
      printf 'credential preflight mutated a rejected file: %s\n' \
        "${credential_file}" >&2
      exit 1
    }
  done
}

expect_credential_preflight_failure() {
  local label="$1" expected="$2"
  : >"${work}/systemctl.log"
  set +e
  run_installer 0 0 >"${work}/${label}.stdout" 2>"${work}/${label}.stderr"
  local status=$?
  set -e
  [[ "${status}" -ne 0 ]] || {
    printf 'unsafe credential fixture unexpectedly succeeded: %s\n' "${label}" >&2
    exit 1
  }
  grep -Fq "${expected}" "${work}/${label}.stderr"
  [[ ! -s "${work}/systemctl.log" ]] || {
    printf 'service was touched before credential rejection: %s\n' "${label}" >&2
    exit 1
  }
}

status_fixture_backup="${work}/dbb3-status.original"
mv -- "${status_token_file}" "${status_fixture_backup}"
ln -s -- "${installation_token_file}" "${status_token_file}"
expect_credential_preflight_failure \
  credential-symlink "status credential must be a regular file"
rm -- "${status_token_file}"
mv -- "${status_fixture_backup}" "${status_token_file}"
assert_fixture_credentials_unchanged

mv -- "${status_token_file}" "${status_fixture_backup}"
ln -- "${status_fixture_backup}" "${status_token_file}"
expect_credential_preflight_failure \
  credential-hardlink "status credential must have exactly one hard link"
rm -- "${status_token_file}"
mv -- "${status_fixture_backup}" "${status_token_file}"
assert_fixture_credentials_unchanged

cp --preserve=mode,ownership -- "${status_token_file}" "${status_fixture_backup}"
cp -- "${installation_token_file}" "${status_token_file}"
expect_credential_preflight_failure \
  credential-duplicate "installation credential must use a dedicated value"
rm -- "${status_token_file}"
mv -- "${status_fixture_backup}" "${status_token_file}"
assert_fixture_credentials_unchanged

unsafe_credential_dir="${work}/unsafe-credential-parent"
unsafe_status_token_file="${unsafe_credential_dir}/status.token"
install -d -o root -g root -m 0770 "${unsafe_credential_dir}"
printf '%s\n' "unsafe-status-test-token-000000000000001" \
  >"${unsafe_status_token_file}"
chown "root:${credential_source_group}" "${unsafe_status_token_file}"
chmod 0600 "${unsafe_status_token_file}"
safe_status_token_file="${status_token_file}"
status_token_file="${unsafe_status_token_file}"
expect_credential_preflight_failure credential-parent \
  "status credential parent directories must not be writable by other users"
status_token_file="${safe_status_token_file}"
assert_fixture_credentials_unchanged

# The on-disk drop-in is not authoritative if a later systemd drop-in resets
# either condition.  Model `systemctl cat` returning that merged, weakened unit
# and require the installer to fail before it can publish a candidate marker.
effective_reset_unit="${work}/systemd-effective-reset.conf"
printf '%s\n' \
  '[Unit]' \
  'ConditionPathExists=' \
  '[Service]' \
  "Environment=HERMES_HOME=${runtime_home}" \
  'ExecCondition=' \
  >"${effective_reset_unit}"
: >"${work}/systemctl.log"
: >"${work}/systemctl-start-home.log"
set +e
run_installer 0 0 0 0 "" 1 "" "${effective_reset_unit}" \
  >"${work}/effective-unit-reset.stdout" \
  2>"${work}/effective-unit-reset.stderr"
effective_unit_reset_status=$?
set -e
[[ "${effective_unit_reset_status}" -ne 0 ]]
grep -Fq "effective systemd unit cleared the dispatcher ready guard" \
  "${work}/effective-unit-reset.stderr"
assert_systemctl_subsequence "${work}/systemctl.log" \
  stop daemon-reload stop daemon-reload reset-failed start is-active
[[ "$(<"${systemctl_state_file}")" == active ]]
assert_started_only_in_home \
  "${work}/systemctl-start-home.log" "${runtime_home}" 1
[[ ! -e "${release_pending_marker}" && ! -L "${release_pending_marker}" ]]
[[ ! -e "${agent_profile_dropin}" && ! -L "${agent_profile_dropin}" ]]
[[ "$(stat -c '%u:%g:%a' "${target}")" == "0:0:755" ]]

# nginx validation runs before the candidate-authoritative start boundary, so
# this failure still restores the invocation baseline and restarts it.
: >"${work}/systemctl.log"
: >"${work}/systemctl-start-home.log"
: >"${work}/nginx.log"
set +e
run_installer 0 0 0 1 >"${work}/nginx-failure.stdout" 2>"${work}/nginx-failure.stderr"
nginx_failure_status=$?
set -e
[[ "${nginx_failure_status}" -ne 0 ]] || {
  printf '%s\n' "forced nginx validation failure unexpectedly succeeded" >&2
  exit 1
}
grep -Fq "nginx configuration validation failed" "${work}/nginx-failure.stderr"
[[ "$(<"${nginx_security_target}")" == "old:nginx-security" ]]
[[ "$(<"${nginx_site_target}")" == "old:nginx-site" ]]
for relative in "${runtime_files[@]}"; do
  [[ "$(<"${target}/${relative}")" == "old:${relative}" ]]
done
assert_old_state "${state_file}"
assert_systemctl_subsequence "${work}/systemctl.log" \
  stop daemon-reload stop daemon-reload reset-failed start is-active
[[ "$(grep -c '^start$' "${work}/systemctl.log")" == 1 ]]
[[ "$(<"${systemctl_state_file}")" == active ]]
assert_started_only_in_home \
  "${work}/systemctl-start-home.log" "${runtime_home}" 1
[[ ! -e "${agent_profile_dropin}" && ! -L "${agent_profile_dropin}" ]]
[[ "$(stat -c '%u:%g:%a' "${target}")" == "0:0:755" ]]
[[ -z "$(find "$(dirname -- "${release_pending_marker}")" -maxdepth 1 \
  -name '.hermes-dispatcher-ready.*' -print -quit)" ]]

# This hard-kill point is after the complete candidate and its unique startup
# guard are durable, but immediately before the first service start. A retry
# must see one coherent candidate without any accepted post-start writes.
: >"${work}/systemctl.log"
: >"${work}/systemctl-start-home.log"
: >"${systemd_run_log}"
set +e
run_installer 0 0 0 0 "" 1 candidate-authoritative \
  >"${work}/candidate-authoritative.stdout" \
  2>"${work}/candidate-authoritative.stderr"
candidate_authoritative_status=$?
set -e
[[ "${candidate_authoritative_status}" -eq 137 ]] || {
  printf 'candidate-authoritative hard kill returned %s, expected 137\n' \
    "${candidate_authoritative_status}" >&2
  exit 1
}
assert_installed_candidate
assert_old_state "${state_file}"
assert_pre_start_database_state
assert_dispatcher_ready_guard \
  "${agent_profile_dropin}" "${runtime_home}" published bound
[[ "$(release_marker_from_dropin "${agent_profile_dropin}")" == \
  "${release_pending_marker}" ]]
[[ "$(release_lease_from_dropin "${agent_profile_dropin}")" == \
  "${release_start_lease}" ]]
assert_release_candidate_marker "${release_pending_marker}" "${runtime_home}"
assert_release_start_lease "${release_pending_marker}" "${release_start_lease}"
assert_systemctl_subsequence "${work}/systemctl.log" stop daemon-reload
[[ "$(grep -c '^start$' "${work}/systemctl.log" || true)" == 0 ]]
[[ "$(<"${systemctl_state_file}")" == inactive ]]
[[ ! -s "${work}/systemctl-start-home.log" ]]
[[ ! -e "${release_evidence_file}" ]]

# A reboot or an operator-issued start after the installer was killed must
# evaluate the loaded ExecCondition.  ConditionPathExists alone still passes
# because the ready sentinel is durable, but the dead installer's lease cannot
# authorize a new service process.
: >"${work}/systemctl-condition.log"
run_fake_main_systemctl start \
  >"${work}/candidate-reboot-start.stdout" \
  2>"${work}/candidate-reboot-start.stderr"
set +e
run_fake_main_systemctl is-active --quiet hermes-agent-test.service
candidate_reboot_active_status=$?
set -e
[[ "${candidate_reboot_active_status}" -ne 0 ]]
[[ "$(<"${systemctl_state_file}")" == inactive ]]
[[ ! -s "${work}/systemctl-start-home.log" ]]
grep -Fq "candidate-start-guard:" "${work}/candidate-reboot-start.stderr"
grep -Eq '^ExecCondition rc=[1-9][0-9]* ' "${systemctl_condition_log}"
assert_dispatcher_ready_guard \
  "${agent_profile_dropin}" "${runtime_home}" published bound
candidate_watchdog_unit="$(release_watchdog_from_dropin "${agent_profile_dropin}")"
[[ "${candidate_watchdog_unit}" =~ \
  ^hermes-release-watchdog-[0-9a-f]{32}\.service$ ]]
grep -Fq -- "--unit=${candidate_watchdog_unit}" "${systemd_run_log}"
grep -Fq -- "${release_start_guard} watch" "${systemd_run_log}"
run_fake_main_systemctl stop "${candidate_watchdog_unit}"
[[ "$(<"${systemctl_state_file}")" == inactive ]]
[[ "$(<"${systemctl_state_file}.unit.${candidate_watchdog_unit}")" == inactive ]]

# A kill immediately after `systemctl start` exercises the complementary
# BindsTo guarantee: the transient watchdog dies with the installer and must
# pull the uncommitted main service back to inactive.
: >"${work}/systemctl.log"
: >"${work}/systemctl-start-home.log"
: >"${systemd_run_log}"
set +e
run_installer 0 0 0 0 "" 1 candidate-running \
  >"${work}/candidate-running.stdout" \
  2>"${work}/candidate-running.stderr"
candidate_running_status=$?
set -e
[[ "${candidate_running_status}" -eq 137 ]] || {
  printf 'candidate-running hard kill returned %s, expected 137\n' \
    "${candidate_running_status}" >&2
  exit 1
}
assert_dispatcher_ready_guard \
  "${agent_profile_dropin}" "${runtime_home}" published bound
assert_release_candidate_marker "${release_pending_marker}" "${runtime_home}"
assert_release_start_lease "${release_pending_marker}" "${release_start_lease}"
[[ "$(grep -c '^start$' "${work}/systemctl.log")" == 1 ]]
assert_started_only_in_home \
  "${work}/systemctl-start-home.log" "${runtime_home}" 1
[[ "$(<"${systemctl_state_file}")" == active ]]
candidate_running_watchdog="$(
  release_watchdog_from_dropin "${agent_profile_dropin}"
)"
[[ "${candidate_running_watchdog}" =~ \
  ^hermes-release-watchdog-[0-9a-f]{32}\.service$ ]]
grep -Fq -- "--unit=${candidate_running_watchdog}" "${systemd_run_log}"
run_fake_main_systemctl stop "${candidate_running_watchdog}"
[[ "$(<"${systemctl_state_file}")" == inactive ]]
[[ "$(<"${systemctl_state_file}.unit.${candidate_running_watchdog}")" == inactive ]]

: >"${work}/systemctl.log"
: >"${work}/systemctl-start-home.log"
set +e
run_installer 1 0 >"${work}/failure.stdout" 2>"${work}/failure.stderr"
failure_status=$?
set -e
[[ "${failure_status}" -eq 75 ]] || {
  printf 'forced post-start failure returned %s, expected 75\n' \
    "${failure_status}" >&2
  exit 1
}
for credential_file in "${status_token_file}" "${installation_token_file}" \
  "${hk_recovery_token_file}"; do
  [[ "$(stat -c '%U:%G:%a' "${credential_file}")" == \
    "root:${credential_service_group}:640" ]] || {
    printf 'managed credential permissions were not normalized: %s\n' \
      "${credential_file}" >&2
    exit 1
  }
done
grep -Fq "service_diagnostics_begin" "${work}/failure.stderr"
grep -Fq "SubState=auto-restart" "${work}/failure.stderr"
grep -Fq "Traceback: RuntimeError: failed before bind" "${work}/failure.stderr"
if grep -Fq "super-secret-diagnostic-token-value" "${work}/failure.stderr"; then
  printf '%s\n' "service failure diagnostics leaked a token" >&2
  exit 1
fi
grep -Fq -- "--unit hermes-agent-test.service" "${work}/journalctl.log"
for artifact in "${stale_runtime_artifacts[@]}"; do
  [[ ! -e "${artifact}" && ! -L "${artifact}" ]] || {
    printf 'stale runtime artifact was not reclaimed: %s\n' "${artifact}" >&2
    exit 1
  }
done
[[ -z "$(find -P "${target}" -mindepth 1 -maxdepth 1 \
  -name '.collaboration-install.*' -print -quit)" ]]
assert_installed_candidate
assert_state_id "${state_file}" new-state || {
  cat "${work}/failure.stderr" >&2
  exit 1
}
assert_post_start_database_write
assert_dispatcher_ready_guard \
  "${agent_profile_dropin}" "${runtime_home}" unpublished
assert_release_candidate_marker "${release_pending_marker}" "${runtime_home}"
[[ ! -e "${release_evidence_file}" ]]
assert_systemctl_subsequence "${work}/systemctl.log" \
  stop daemon-reload start is-active stop
[[ "$(grep -c '^start$' "${work}/systemctl.log")" == 1 ]]
[[ "$(<"${systemctl_state_file}")" == inactive ]]
assert_started_only_in_home \
  "${work}/systemctl-start-home.log" "${runtime_home}" 1
grep -Fq "runtime candidate preserved; service remains stopped pending retry" \
  "${work}/failure.stderr"

: >"${work}/systemctl.log"
: >"${work}/systemctl-start-home.log"
set +e
run_installer 0 0 1 >"${work}/handshake.stdout" 2>"${work}/handshake.stderr"
handshake_status=$?
set -e
[[ "${handshake_status}" -eq 75 ]] || {
  printf 'forced mobile handshake failure returned %s, expected 75\n' \
    "${handshake_status}" >&2
  exit 1
}
grep -Fq "anonymous mobile handshake did not respond" "${work}/handshake.stderr"
assert_installed_candidate
assert_state_id "${state_file}" new-state
assert_post_start_database_write
assert_dispatcher_ready_guard \
  "${agent_profile_dropin}" "${runtime_home}" unpublished
assert_release_candidate_marker "${release_pending_marker}" "${runtime_home}"
assert_systemctl_subsequence "${work}/systemctl.log" \
  stop daemon-reload start is-active stop
[[ "$(grep -c '^start$' "${work}/systemctl.log")" == 1 ]]
[[ "$(<"${systemctl_state_file}")" == inactive ]]
assert_started_only_in_home \
  "${work}/systemctl-start-home.log" "${runtime_home}" 1
grep -Fq "runtime candidate preserved; service remains stopped pending retry" \
  "${work}/handshake.stderr"

: >"${work}/systemctl.log"
: >"${work}/systemctl-start-home.log"
set +e
run_installer 0 1 >"${work}/signal.stdout" 2>"${work}/signal.stderr"
signal_status=$?
set -e
[[ "${signal_status}" -eq 75 ]] || {
  printf 'signal interruption returned %s, expected 75\n' "${signal_status}" >&2
  exit 1
}
assert_installed_candidate
assert_state_id "${state_file}" signal-state
assert_dispatcher_ready_guard \
  "${agent_profile_dropin}" "${runtime_home}" unpublished
assert_release_candidate_marker "${release_pending_marker}" "${runtime_home}"
assert_systemctl_subsequence "${work}/systemctl.log" \
  stop daemon-reload start stop
[[ "$(grep -c '^start$' "${work}/systemctl.log")" == 1 ]]
[[ "$(<"${systemctl_state_file}")" == inactive ]]
assert_started_only_in_home \
  "${work}/systemctl-start-home.log" "${runtime_home}" 1
grep -Fq "runtime candidate preserved; service remains stopped pending retry" \
  "${work}/signal.stderr"

for injected_phase in migrate candidate-health traffic-switch drain commit; do
  : >"${work}/systemctl.log"
  : >"${work}/systemctl-start-home.log"
  set +e
  run_installer 0 0 0 0 "${injected_phase}" \
    >"${work}/phase-${injected_phase}.stdout" \
    2>"${work}/phase-${injected_phase}.stderr"
  phase_status=$?
  set -e
  [[ "${phase_status}" -eq 75 ]] || {
    printf 'forced %s failure returned %s, expected 75\n' \
      "${injected_phase}" "${phase_status}" >&2
    exit 1
  }
  grep -Fq "injected deployment failure at ${injected_phase}" \
    "${work}/phase-${injected_phase}.stderr"
  assert_installed_candidate
  assert_state_id "${state_file}" signal-state
  assert_post_start_database_write
  assert_dispatcher_ready_guard \
    "${agent_profile_dropin}" "${runtime_home}" unpublished
  assert_release_candidate_marker "${release_pending_marker}" "${runtime_home}"
  assert_systemctl_subsequence "${work}/systemctl.log" \
    stop daemon-reload start is-active stop
  [[ "$(grep -c '^start$' "${work}/systemctl.log")" == 1 ]]
  [[ "$(<"${systemctl_state_file}")" == inactive ]]
  assert_started_only_in_home \
    "${work}/systemctl-start-home.log" "${runtime_home}" 1
  grep -Fq "runtime candidate preserved; service remains stopped pending retry" \
    "${work}/phase-${injected_phase}.stderr"
  [[ ! -e "${release_evidence_file}" ]]
done

# Marker removal is the durable commit boundary, but the service remains bound
# to the watchdog until a second drop-in reload.  A kill in that interval must
# let watchdog exit pull the main service inactive; the following normal run
# is the marker-aware crash retry which detaches and completes the commit.
: >"${work}/systemctl.log"
: >"${work}/systemctl-start-home.log"
: >"${systemd_run_log}"
set +e
run_installer 0 0 0 0 "" 1 candidate-marker-committed \
  >"${work}/candidate-marker-committed.stdout" \
  2>"${work}/candidate-marker-committed.stderr"
candidate_marker_committed_status=$?
set -e
[[ "${candidate_marker_committed_status}" -eq 137 ]] || {
  printf 'candidate-marker-committed hard kill returned %s, expected 137\n' \
    "${candidate_marker_committed_status}" >&2
  exit 1
}
assert_dispatcher_ready_guard \
  "${agent_profile_dropin}" "${runtime_home}" published bound
[[ ! -e "${release_pending_marker}" && ! -L "${release_pending_marker}" ]]
assert_orphaned_release_start_lease "${release_start_lease}" "${runtime_home}"
[[ "$(<"${systemctl_state_file}")" == active ]]
committed_bound_watchdog="$(
  release_watchdog_from_dropin "${agent_profile_dropin}"
)"
[[ "${committed_bound_watchdog}" =~ \
  ^hermes-release-watchdog-[0-9a-f]{32}\.service$ ]]
grep -Fq -- "--unit=${committed_bound_watchdog}" "${systemd_run_log}"
run_fake_main_systemctl stop "${committed_bound_watchdog}"
[[ "$(<"${systemctl_state_file}")" == inactive ]]

: >"${work}/systemctl.log"
: >"${work}/systemctl-start-home.log"
run_installer 0 0 >"${work}/success.stdout" 2>"${work}/success.stderr" || {
  cat "${work}/success.stdout" >&2
  cat "${work}/success.stderr" >&2
  exit 1
}
for relative in "${runtime_files[@]}"; do
  cmp -- "${stage}/${relative}" "${target}/${relative}"
done
assert_state_id "${state_file}" signal-state
assert_post_start_database_write
assert_dispatcher_ready_guard \
  "${agent_profile_dropin}" "${runtime_home}" published unbound
[[ ! -e "${release_pending_marker}" && ! -L "${release_pending_marker}" ]]
[[ ! -e "${release_start_lease}" && ! -L "${release_start_lease}" ]]
"${runtime_python}" - "${target}" "${work}" <<'PY'
import ast
from pathlib import Path
import sys

target = Path(sys.argv[1]).resolve()
scratch = Path(sys.argv[2]).resolve()

expected_symbols = {
    "agent/prompt_builder.py": {"TOOL_USE_ENFORCEMENT_GUIDANCE"},
    "agent/system_prompt.py": {"build_system_prompt"},
    "agent/context_diagnostics.py": {"analyze_context_sources"},
    "hermes_cli/doctor.py": {"_check_context_engineering"},
}
for relative, symbols in expected_symbols.items():
    installed = target / relative
    source = installed.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(installed))
    compile(tree, str(installed), "exec")
    declared = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    declared.update(
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        if isinstance(target, ast.Name)
    )
    assert symbols <= declared, (relative, symbols - declared)

assert scratch.is_dir()
PY
cmp -- "${stage}/deploy/public/nginx-00-hermes-security.conf" "${nginx_security_target}"
cmp -- "${stage}/deploy/public/nginx-daxueshenmai.top.conf" "${nginx_site_target}"
python3 - \
  "${managed_nodes_file}" "${status_token_file}" "${installation_token_file}" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
node = payload["nodes"][0]
assert node["token_file"] == sys.argv[2]
assert node["installation_token_file"] == sys.argv[3]
assert sorted(node["installation_urls"]) == ["dbb3", "wsl"]
assert "hk" in node["recovery_urls"]
assert node["recovery_token_files"]["hk"].endswith("hk-recovery.token")
PY
assert_post_start_database_write
grep -Fq "service=active" "${work}/success.stdout"
python3 - "${release_evidence_file}" <<'PY'
import json
import sys

evidence = json.load(open(sys.argv[1], encoding="utf-8"))
assert evidence["schema"] == "hermes.release-evidence.v1"
assert evidence["phase"] == "committed"
assert evidence["commit"] == "0000000000000000000000000000000000000001"
assert len(evidence["manifest_sha256"]) == 64
assert evidence["database_snapshot"]["database_count"] >= 1
assert len(evidence["database_snapshot"]["manifest_sha256"]) == 64
assert evidence["probes"]["deployment_health"]["ok"] is True
assert evidence["probes"]["managed_installation_routes"] == {
    "dbb3": True,
    "wsl": True,
}
assert evidence["fabric"]["status"] == "verified"
assert evidence["fabric"]["nodes"] == {
    "dbb3": {"commit": evidence["commit"], "version": evidence["version"]},
    "wsl": {"commit": evidence["commit"], "version": evidence["version"]},
}
assert evidence["probes"]["traffic_switch"]["nginx_reloaded"] is True
PY
assert_systemctl_subsequence "${work}/systemctl.log" \
  stop daemon-reload start is-active reload
[[ "$(grep -c '^start$' "${work}/systemctl.log")" == 1 ]]
assert_started_only_in_home \
  "${work}/systemctl-start-home.log" "${runtime_home}" 1

# Once the candidate marker is durably removed at commit, the same persistent
# drop-in must allow ordinary reboot/operator starts without an installer lease.
: >"${work}/systemctl.log"
: >"${work}/systemctl-start-home.log"
: >"${systemctl_condition_log}"
run_fake_main_systemctl stop hermes-agent-test.service
run_fake_main_systemctl start hermes-agent-test.service
run_fake_main_systemctl is-active --quiet hermes-agent-test.service
[[ "$(<"${systemctl_state_file}")" == active ]]
assert_systemctl_subsequence "${work}/systemctl.log" stop start is-active
assert_started_only_in_home \
  "${work}/systemctl-start-home.log" "${runtime_home}" 1
grep -Fq 'ExecCondition rc=0 ' "${systemctl_condition_log}"

# A kill after the effective unit is durably detached is already committed.
# The watchdog may exit, but without BindsTo it must not stop the main service;
# marker absence must also continue to authorize ordinary starts.
: >"${work}/systemctl.log"
: >"${work}/systemctl-start-home.log"
: >"${systemd_run_log}"
set +e
run_installer 0 0 0 0 "" 1 watchdog-detached \
  >"${work}/watchdog-detached.stdout" \
  2>"${work}/watchdog-detached.stderr"
watchdog_detached_status=$?
set -e
[[ "${watchdog_detached_status}" -eq 137 ]] || {
  printf 'watchdog-detached hard kill returned %s, expected 137\n' \
    "${watchdog_detached_status}" >&2
  exit 1
}
assert_dispatcher_ready_guard \
  "${agent_profile_dropin}" "${runtime_home}" published unbound
[[ ! -e "${release_pending_marker}" && ! -L "${release_pending_marker}" ]]
assert_orphaned_release_start_lease "${release_start_lease}" "${runtime_home}"
[[ "$(<"${systemctl_state_file}")" == active ]]
detached_watchdog_unit="$(
  sed -n 's/.*--unit=\([^ ]*\).*/\1/p' "${systemd_run_log}" | tail -n 1
)"
[[ "${detached_watchdog_unit}" =~ \
  ^hermes-release-watchdog-[0-9a-f]{32}\.service$ ]]
run_fake_main_systemctl stop "${detached_watchdog_unit}"
[[ "$(<"${systemctl_state_file}")" == active ]]
: >"${work}/systemctl.log"
: >"${work}/systemctl-start-home.log"
: >"${systemctl_condition_log}"
run_fake_main_systemctl stop hermes-agent-test.service
run_fake_main_systemctl start hermes-agent-test.service
run_fake_main_systemctl is-active --quiet hermes-agent-test.service
[[ "$(<"${systemctl_state_file}")" == active ]]
assert_started_only_in_home \
  "${work}/systemctl-start-home.log" "${runtime_home}" 1
grep -Fq 'ExecCondition rc=0 ' "${systemctl_condition_log}"

# HK recovery is opt-in because both hosts must be provisioned with the same
# dedicated token. Disabled deployments must not require or advertise it.
rm -- "${hk_recovery_token_file}"
: >"${work}/systemctl.log"
run_installer 0 0 0 0 "" 0 \
  >"${work}/hk-disabled.stdout" 2>"${work}/hk-disabled.stderr" || {
  cat "${work}/hk-disabled.stdout" >&2
  cat "${work}/hk-disabled.stderr" >&2
  exit 1
}
python3 - "${managed_nodes_file}" <<'PY'
import json
import sys

node = json.load(open(sys.argv[1], encoding="utf-8"))["nodes"][0]
assert "hk" not in node["recovery_urls"]
assert "recovery_token_files" not in node
PY

# Exercise the production transition from the historical public
# dbb3-worker home to a dedicated dispatcher profile. This deliberately omits
# HERMES_HOME_DIR so the installer must consume systemd's effective value,
# migrate transactionally, and persist a drop-in.
migration_service_home="${work}/migration-service-home"
migration_profiles="${migration_service_home}/.hermes/profiles"
migration_legacy_home="${migration_profiles}/dbb3-worker"
migration_dispatcher_home="${migration_profiles}/dispatcher"
migration_dropin_dir="${work}/systemd/hermes-agent-test.service.d"
migration_dropin="${migration_dropin_dir}/10-hermes-dispatcher-profile.conf"
migration_env_file="${work}/migration-hermes-agent.env"
migration_reload_marker="${work}/migration-daemon-reloaded"
migration_release_evidence="${work}/migration-release/release-evidence.json"
cp --preserve=mode,ownership -- "${agent_env_file}" "${migration_env_file}"
install -d -m 0755 "${migration_profiles}" "$(dirname "${migration_dropin_dir}")"
# The dispatcher state root must already exist in the guarded ensure-managed
# form (sticky, root:service-group, mode 01770) so the installer's post-stop
# guard adopts it instead of rejecting it.
chmod 01770 "${migration_service_home}/.hermes" "${migration_profiles}"
chown root:"${credential_service_group}" \
  "${migration_service_home}/.hermes" "${migration_profiles}"
install -d -o "${credential_service_user}" -g "${credential_service_group}" \
  -m 0700 "${migration_legacy_home}/collaboration"
install -d -o "${credential_service_user}" -g "${credential_service_group}" \
  -m 0700 "${migration_legacy_home}/skills" \
  "${migration_legacy_home}/skills/common" \
  "${migration_legacy_home}/node/bin" \
  "${migration_legacy_home}/node/lib/node_modules/npm/bin"
printf '%s\n' 'shared-skill-content' \
  >"${migration_legacy_home}/skills/common/SKILL.md"
chown "${credential_service_user}:${credential_service_group}" \
  "${migration_legacy_home}/skills/common/SKILL.md"
ln -s common \
  "${migration_legacy_home}/skills/shared"
printf '%s\n' 'npm-cli' \
  >"${migration_legacy_home}/node/lib/node_modules/npm/bin/npm-cli.js"
ln -s ../lib/node_modules/npm/bin/npm-cli.js \
  "${migration_legacy_home}/node/bin/npm"
printf '%s\n' 'legacy-worker-state' >"${migration_legacy_home}/role.txt"
printf '%s\n' '{"conversations":[{"id":"legacy-dispatcher"}]}' \
  >"${migration_legacy_home}/collaboration/single.json"
printf '%s\n' '{"nodes":[]}' >"${migration_legacy_home}/managed-nodes.json"
python3 - "${migration_legacy_home}/managed-installations.db" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as database:
    database.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    database.execute("INSERT INTO marker VALUES ('legacy-dispatcher-db')")
PY
chown -R "${credential_service_user}:${credential_service_group}" \
  "${migration_legacy_home}"

_run_migration_installer_body() {
  if [[ -f "${migration_dropin}" ]] \
      && grep -Fq "Environment=HERMES_HOME=${migration_dispatcher_home}" \
        "${migration_dropin}"; then
    : >"${migration_reload_marker}"
  else
    rm -f -- "${migration_reload_marker}"
  fi
  env -u HERMES_HOME_DIR -u HERMES_COLLABORATION_STATE_FILE \
    PATH="${fake_bin}:${PATH}" \
    FAKE_STATUS_FAIL=0 \
    FAKE_SIGNAL_ON_START=0 \
    FAKE_HANDSHAKE_FAIL=0 \
    FAKE_NGINX_FAIL=0 \
    FAKE_SERVICE_USER="${credential_service_user}" \
    FAKE_SERVICE_HOME="${migration_service_home}" \
    HERMES_DISPATCHER_STATE_ROOT="${migration_service_home}/.hermes" \
    FAKE_SYSTEMD_ENVIRONMENT="HERMES_HOME=${migration_legacy_home}" \
    FAKE_SYSTEMD_ENVIRONMENT_AFTER_RELOAD="HERMES_HOME=${migration_dispatcher_home}" \
    FAKE_DAEMON_RELOAD_MARKER="${migration_reload_marker}" \
    FAKE_SYSTEMD_PROFILE_DROPIN="${migration_dropin}" \
    FAKE_SYSTEMD_EFFECTIVE_UNIT_FILE="${FAKE_SYSTEMD_EFFECTIVE_UNIT_FILE:-}" \
    FAKE_SYSTEMD_DROPIN_PATHS_AFTER_RELOAD="${5:-${migration_dropin}}" \
    FAKE_SYSTEMD_EXEC_START="${3:-}" \
    FAKE_DISPATCHER_SENTINEL_ON_START="${2:-0}" \
    FAKE_DISPATCHER_HOME="${migration_dispatcher_home}" \
    HERMES_DEPLOY_FAIL_PHASE="${1:-}" \
    HERMES_DEPLOY_HARD_KILL_PHASE="${4:-}" \
    HERMES_HK_ENABLED=0 \
    HERMES_RUNTIME_SOURCE_MIN_FILES=1 \
    IOS_CAPABILITIES_COUNT="${ios_capabilities_count}" \
    HERMES_AGENT_ROOT="${target}" \
    HERMES_BOOTSTRAP_PYTHON="${bootstrap_python}" \
    HERMES_RUNTIME_PYTHON="${runtime_python}" \
    HERMES_AGENT_SERVICE="hermes-agent-test.service" \
    HERMES_AGENT_USER="${credential_service_user}" \
    HERMES_AGENT_GROUP="${credential_service_group}" \
    HERMES_SYSTEMCTL_BINARY="${fake_bin}/systemctl" \
    HERMES_SYSTEMD_RUN_BINARY="${fake_bin}/systemd-run" \
    HERMES_SETPRIV_BINARY="$(command -v setpriv)" \
    HERMES_JOURNALCTL_BINARY="${fake_bin}/journalctl" \
    HERMES_CURL_BINARY="${fake_bin}/curl" \
    HERMES_STAGE_OWNER="root" \
    HERMES_BACKUP_ROOT="${backup}" \
    HERMES_INSTALL_LOCK_FILE="${work}/collaboration-install.lock" \
    HERMES_COLLABORATION_CONNECTOR_TOKEN_FILE="${token_file}" \
    HERMES_MANAGED_NODE_TOKEN_FILE="${status_token_file}" \
    HERMES_MANAGED_INSTALLATION_TOKEN_FILE="${installation_token_file}" \
    HERMES_AGENT_ENV_FILE="${migration_env_file}" \
    HERMES_AGENT_PROFILE_DROPIN="${migration_dropin}" \
    HERMES_NGINX_SECURITY_TARGET="${nginx_security_target}" \
    HERMES_NGINX_SITE_TARGET="${nginx_site_target}" \
    HERMES_NGINX_SERVICE="nginx-test.service" \
    HERMES_NGINX_BINARY="${fake_bin}/nginx" \
    HERMES_RELEASE_EVIDENCE_FILE="${migration_release_evidence}" \
    HERMES_RELEASE_PENDING_MARKER="$(dirname -- "${migration_release_evidence}")/candidate-pending.json" \
    FAKE_SYSTEMCTL_LOG="${work}/migration-systemctl.log" \
    FAKE_SYSTEMCTL_START_HOME_LOG="${work}/migration-start-home.log" \
    FAKE_SYSTEMCTL_STATE_FILE="${systemctl_state_file}" \
    FAKE_SYSTEMCTL_CONDITION_LOG="${systemctl_condition_log}" \
    FAKE_MAIN_SYSTEMD_SERVICE="hermes-agent-test.service" \
    FAKE_SYSTEMD_RUN_LOG="${systemd_run_log}" \
    FAKE_JOURNALCTL_LOG="${work}/journalctl.log" \
    FAKE_NGINX_LOG="${work}/nginx.log" \
    /bin/bash "${installer}" "${version}" "${stage}" \
      0000000000000000000000000000000000000001
}
run_migration_installer() {
  local timing_start="${EPOCHREALTIME}" timing_status=0
  if _run_migration_installer_body "$@"; then
    :
  else
    timing_status=$?
  fi
  harness_report_timing "migration-installer ${1:-}" "${timing_status}" "${timing_start}"
  return "${timing_status}"
}

# An explicit CLI profile overrides HERMES_HOME. Refuse a stale worker-pinned
# ExecStart before stopping or mutating the public service.
: >"${work}/migration-systemctl.log"
set +e
run_migration_installer "" 0 \
  "/opt/hermes/.venv/bin/python -m hermes_cli.main --profile dbb3-worker gateway run" \
  >"${work}/migration-worker-execstart.stdout" \
  2>"${work}/migration-worker-execstart.stderr"
migration_worker_execstart_status=$?
set -e
[[ "${migration_worker_execstart_status}" -ne 0 ]]
grep -Fq "effective Hermes service command selects a non-dispatcher profile" \
  "${work}/migration-worker-execstart.stderr"
[[ ! -s "${work}/migration-systemctl.log" ]]
[[ ! -e "${migration_dispatcher_home}" ]]

# Relative and home-expanded database paths are normalized before containment;
# a legacy dispatcher must never inherit another worker's SQLite database.
install -d -o "${credential_service_user}" -g "${credential_service_group}" \
  -m 0700 "${migration_profiles}/hk-worker"
printf '%s\n' 'hk-private-config' >"${migration_profiles}/hk-worker/.env"
chown "${credential_service_user}:${credential_service_group}" \
  "${migration_profiles}/hk-worker/.env"
printf '%s\n' \
  'ios_intelligence:' \
  '  database_path: ../hk-worker/ios-intelligence.db' \
  >"${migration_legacy_home}/config.yaml"
chown "${credential_service_user}:${credential_service_group}" \
  "${migration_legacy_home}/config.yaml"
: >"${work}/migration-systemctl.log"
set +e
run_migration_installer \
  >"${work}/migration-cross-role-database.stdout" \
  2>"${work}/migration-cross-role-database.stderr"
migration_cross_role_database_status=$?
set -e
[[ "${migration_cross_role_database_status}" -ne 0 ]]
grep -Fq "iOS database path is outside the dispatcher profile" \
  "${work}/migration-cross-role-database.stderr"
[[ ! -e "${migration_dispatcher_home}" ]]
[[ ! -s "${work}/migration-systemctl.log" ]]

install -d -o "${credential_service_user}" -g "${credential_service_group}" \
  -m 0700 "${migration_legacy_home}/data"
printf 'ios_intelligence:\n  database_path: %s/data/custom.sqlite3\n' \
  "${migration_legacy_home}" >"${migration_legacy_home}/config.yaml"
python3 - "${migration_legacy_home}/data/custom.sqlite3" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as database:
    database.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    database.execute("INSERT INTO marker VALUES ('legacy-ios-db')")
PY
chown "${credential_service_user}:${credential_service_group}" \
  "${migration_legacy_home}/config.yaml" \
  "${migration_legacy_home}/data/custom.sqlite3"

# A skill link must not turn the dispatcher migration into a cross-role read.
install -d -o "${credential_service_user}" -g "${credential_service_group}" \
  -m 0700 "${migration_legacy_home}/skills/leak"
ln -s ../../../hk-worker/.env \
  "${migration_legacy_home}/skills/leak/SKILL.md"
: >"${work}/migration-systemctl.log"
set +e
run_migration_installer \
  >"${work}/migration-cross-role-link.stdout" \
  2>"${work}/migration-cross-role-link.stderr"
migration_cross_role_link_status=$?
set -e
[[ "${migration_cross_role_link_status}" -ne 0 ]]
grep -Fq "profile symlink leaves legacy profile" \
  "${work}/migration-cross-role-link.stderr"
[[ ! -e "${migration_dispatcher_home}" ]]
[[ "$(<"${migration_profiles}/hk-worker/.env")" == "hk-private-config" ]]
rm -- "${migration_legacy_home}/skills/leak/SKILL.md"
rmdir -- "${migration_legacy_home}/skills/leak"

: >"${work}/migration-systemctl.log"
: >"${work}/migration-start-home.log"
set +e
run_migration_installer migrate 1 \
  >"${work}/migration-rollback.stdout" \
  2>"${work}/migration-rollback.stderr"
migration_failure_status=$?
set -e
[[ "${migration_failure_status}" -eq 75 ]]
grep -Fq "injected deployment failure at migrate" \
  "${work}/migration-rollback.stderr"
# The service was already started with the forked dispatcher profile. Preserve
# the authoritative candidate and leave it stopped for the marker-aware retry
# without ever restarting the legacy worker profile.
[[ "$(<"${migration_dispatcher_home}/post-start-write.txt")" == \
  "accepted-after-dispatcher-start" ]]
[[ -n "$(find "${migration_dispatcher_home}" -maxdepth 1 \
  -name '.hermes-dispatcher-migration.*' -print -quit)" ]]
[[ "$(<"${migration_legacy_home}/role.txt")" == "legacy-worker-state" ]]
grep -Fq "Environment=HERMES_HOME=${migration_dispatcher_home}" \
  "${migration_dropin}"
assert_dispatcher_ready_guard \
  "${migration_dropin}" "${migration_dispatcher_home}" unpublished bound
assert_release_candidate_marker \
  "$(release_marker_from_dropin "${migration_dropin}")" \
  "${migration_dispatcher_home}"
migration_failed_ready_path="$(dispatcher_ready_from_dropin "${migration_dropin}")"
assert_systemctl_subsequence "${work}/migration-systemctl.log" \
  stop daemon-reload start is-active stop
[[ "$(grep -c '^start$' "${work}/migration-systemctl.log")" == 1 ]]
[[ "$(<"${systemctl_state_file}")" == inactive ]]
assert_started_only_in_home \
  "${work}/migration-start-home.log" "${migration_dispatcher_home}" 1
grep -Fq "runtime candidate preserved; service remains stopped pending retry" \
  "${work}/migration-rollback.stderr"

: >"${work}/migration-systemctl.log"
: >"${work}/migration-start-home.log"
run_migration_installer \
  >"${work}/migration-success.stdout" \
  2>"${work}/migration-success.stderr" || {
  cat "${work}/migration-success.stdout" >&2
  cat "${work}/migration-success.stderr" >&2
  exit 1
}
assert_started_only_in_home \
  "${work}/migration-start-home.log" "${migration_dispatcher_home}" 1
assert_dispatcher_ready_guard \
  "${migration_dropin}" "${migration_dispatcher_home}" published unbound
[[ "$(dispatcher_ready_from_dropin "${migration_dropin}")" != \
  "${migration_failed_ready_path}" ]]
[[ "$(<"${migration_legacy_home}/role.txt")" == "legacy-worker-state" ]]
[[ "$(<"${migration_dispatcher_home}/role.txt")" == "legacy-worker-state" ]]
[[ "$(<"${migration_dispatcher_home}/post-start-write.txt")" == \
  "accepted-after-dispatcher-start" ]]
[[ -L "${migration_dispatcher_home}/skills/shared" ]]
[[ "$(<"${migration_dispatcher_home}/skills/shared/SKILL.md")" == \
  "shared-skill-content" ]]
[[ -L "${migration_dispatcher_home}/node/bin/npm" ]]
[[ "$(<"${migration_dispatcher_home}/node/bin/npm")" == "npm-cli" ]]
[[ "$(<"${migration_dispatcher_home}/collaboration/single.json")" == \
  '{"conversations":[{"id":"legacy-dispatcher"}]}' ]]
[[ "$(python3 - "${migration_dispatcher_home}/config.yaml" <<'PY'
import sys
import yaml

print(yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["ios_intelligence"]["database_path"])
PY
)" == "${migration_dispatcher_home}/data/custom.sqlite3" ]]
[[ "$(python3 - "${migration_dispatcher_home}/data/custom.sqlite3" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as database:
    print(database.execute("SELECT value FROM marker").fetchone()[0])
PY
)" == "legacy-ios-db" ]]
grep -Fq "database_path: ${migration_legacy_home}/data/custom.sqlite3" \
  "${migration_legacy_home}/config.yaml"
[[ "$(python3 - "${migration_dispatcher_home}/managed-installations.db" <<'PY'
import sqlite3
import sys
with sqlite3.connect(sys.argv[1]) as database:
    print(database.execute("SELECT value FROM marker").fetchone()[0])
PY
)" == "legacy-dispatcher-db" ]]
grep -Fq "Environment=HERMES_HOME=${migration_dispatcher_home}" \
  "${migration_dropin}"
[[ -z "$(find "${migration_dispatcher_home}" -maxdepth 1 \
  -name '.hermes-dispatcher-migration.*' -print -quit)" ]]
grep -Fq "daemon-reload" "${work}/migration-systemctl.log"

# Simulate a hard kill after the copy but before commit. A strict root-owned
# marker allows the next transaction to resume the non-empty dispatcher rather
# than refusing it or merging worker state again.
pending_marker="${migration_dispatcher_home}/.hermes-dispatcher-migration.424242"
printf '%s\n' '0000000000000000000000000000000000000001:424242:7' \
  >"${pending_marker}"
chown root:root "${pending_marker}"
chmod 0600 "${pending_marker}"
: >"${work}/migration-systemctl.log"
: >"${work}/migration-start-home.log"
run_migration_installer \
  >"${work}/migration-resume.stdout" \
  2>"${work}/migration-resume.stderr" || {
  cat "${work}/migration-resume.stdout" >&2
  cat "${work}/migration-resume.stderr" >&2
  exit 1
}
assert_started_only_in_home \
  "${work}/migration-start-home.log" "${migration_dispatcher_home}" 1
[[ ! -e "${pending_marker}" ]]
[[ "$(<"${migration_dispatcher_home}/role.txt")" == "legacy-worker-state" ]]

prepare_rebind_hard_kill_fixture() {
  local phase="$1" fixture_root
  fixture_root="${work}/hard-kill-${phase}"
  migration_service_home="${fixture_root}/service-home"
  migration_profiles="${migration_service_home}/.hermes/profiles"
  migration_legacy_home="${migration_profiles}/dbb3-worker"
  migration_dispatcher_home="${migration_profiles}/dispatcher"
  migration_dropin_dir="${fixture_root}/systemd/hermes-agent-test.service.d"
  migration_dropin="${migration_dropin_dir}/10-hermes-dispatcher-profile.conf"
  migration_env_file="${fixture_root}/hermes-agent.env"
  migration_reload_marker="${fixture_root}/daemon-reloaded"
  migration_release_evidence="${fixture_root}/release/release-evidence.json"
  install -d -m 0755 \
    "${fixture_root}" "${migration_profiles}" "$(dirname "${migration_dropin_dir}")"
  # Same guarded ensure-managed state-root form as the primary fixture.
  chmod 01770 "${migration_service_home}/.hermes" "${migration_profiles}"
  chown root:"${credential_service_group}" \
    "${migration_service_home}/.hermes" "${migration_profiles}"
  sed '/^HERMES_HOME=/d' "${agent_env_file}" >"${migration_env_file}"
  chown root:root "${migration_env_file}"
  chmod 0600 "${migration_env_file}"
  install -d -o "${credential_service_user}" -g "${credential_service_group}" \
    -m 0700 "${migration_legacy_home}/collaboration"
  printf '%s\n' "legacy-${phase}" >"${migration_legacy_home}/role.txt"
  printf '%s\n' '{"conversations":[{"id":"legacy-hard-kill"}]}' \
    >"${migration_legacy_home}/collaboration/single.json"
  printf '%s\n' '{"nodes":[]}' >"${migration_legacy_home}/managed-nodes.json"
  python3 - "${migration_legacy_home}/managed-installations.db" "${phase}" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as database:
    database.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    database.execute("INSERT INTO marker VALUES (?)", (f"legacy-{sys.argv[2]}",))
PY
  chown -R "${credential_service_user}:${credential_service_group}" \
    "${migration_legacy_home}"
  : >"${work}/migration-systemctl.log"
  : >"${work}/migration-start-home.log"
}

# Refuse to rewrite the persistent environment until systemd confirms that the
# guarded dispatcher drop-in is part of the loaded unit. This failure is still
# before the role fork, so rollback must remove the drop-in and restart legacy.
prepare_rebind_hard_kill_fixture guard-not-loaded
set +e
run_migration_installer "" 0 "" "" "${migration_dropin}.not-loaded" \
  >"${work}/migration-guard-not-loaded.stdout" \
  2>"${work}/migration-guard-not-loaded.stderr"
guard_not_loaded_status=$?
set -e
[[ "${guard_not_loaded_status}" -ne 0 ]]
grep -Fq "systemd did not load the dispatcher profile guard" \
  "${work}/migration-guard-not-loaded.stderr"
assert_systemctl_subsequence "${work}/migration-systemctl.log" \
  stop daemon-reload stop daemon-reload start
[[ "$(grep -c '^stop$' "${work}/migration-systemctl.log")" == 2 ]]
[[ "$(grep -c '^daemon-reload$' "${work}/migration-systemctl.log")" == 2 ]]
[[ "$(grep -c '^start$' "${work}/migration-systemctl.log")" == 1 ]]
[[ "$(<"${systemctl_state_file}")" == active ]]
assert_started_only_in_home \
  "${work}/migration-start-home.log" "${migration_legacy_home}" 1
[[ ! -e "${migration_dropin}" && ! -L "${migration_dropin}" ]]
[[ ! -e "${migration_dispatcher_home}" && ! -L "${migration_dispatcher_home}" ]]
! grep -q '^HERMES_HOME=' "${migration_env_file}"
[[ "$(<"${migration_legacy_home}/role.txt")" == "legacy-guard-not-loaded" ]]

for hard_kill_phase in rebind-dropin-reloaded rebind-env-rewritten; do
  prepare_rebind_hard_kill_fixture "${hard_kill_phase}"
  set +e
  run_migration_installer "" 0 "" "${hard_kill_phase}" \
    >"${work}/migration-hard-kill-${hard_kill_phase}.stdout" \
    2>"${work}/migration-hard-kill-${hard_kill_phase}.stderr"
  hard_kill_status=$?
  set -e
  [[ "${hard_kill_status}" -eq 137 ]] || {
    printf 'hard-kill phase %s returned %s, expected 137\n' \
      "${hard_kill_phase}" "${hard_kill_status}" >&2
    exit 1
  }
  assert_systemctl_subsequence "${work}/migration-systemctl.log" \
    stop daemon-reload
  [[ "$(grep -c '^stop$' "${work}/migration-systemctl.log")" == 1 ]]
  [[ "$(grep -c '^daemon-reload$' "${work}/migration-systemctl.log")" == 1 ]]
  [[ "$(grep -c '^start$' "${work}/migration-systemctl.log" || true)" == 0 ]]
  [[ "$(tail -n 1 "${work}/migration-systemctl.log")" == daemon-reload ]]
  [[ ! -s "${work}/migration-start-home.log" ]]
  [[ -e "${migration_reload_marker}" ]]
  assert_dispatcher_ready_guard \
    "${migration_dropin}" "${migration_dispatcher_home}" unpublished
  hard_kill_ready_path="$(dispatcher_ready_from_dropin "${migration_dropin}")"
  [[ -d "${migration_dispatcher_home}" \
      && ! -L "${migration_dispatcher_home}" ]]
  [[ -z "$(find "${migration_dispatcher_home}" -mindepth 1 \
    -maxdepth 1 -print -quit)" ]]
  [[ "$(<"${migration_legacy_home}/role.txt")" == \
    "legacy-${hard_kill_phase}" ]]
  if [[ "${hard_kill_phase}" == "rebind-dropin-reloaded" ]]; then
    ! grep -q '^HERMES_HOME=' "${migration_env_file}"
  else
    grep -Fqx "HERMES_HOME=${migration_dispatcher_home}" \
      "${migration_env_file}"
  fi

  : >"${work}/migration-systemctl.log"
  : >"${work}/migration-start-home.log"
  run_migration_installer \
    >"${work}/migration-hard-kill-${hard_kill_phase}-recovery.stdout" \
    2>"${work}/migration-hard-kill-${hard_kill_phase}-recovery.stderr" || {
    cat "${work}/migration-hard-kill-${hard_kill_phase}-recovery.stderr" >&2
    exit 1
  }
  assert_started_only_in_home \
    "${work}/migration-start-home.log" "${migration_dispatcher_home}" 1
  [[ "$(<"${migration_legacy_home}/role.txt")" == \
    "legacy-${hard_kill_phase}" ]]
  [[ "$(<"${migration_dispatcher_home}/role.txt")" == \
    "legacy-${hard_kill_phase}" ]]
  grep -Fqx "HERMES_HOME=${migration_dispatcher_home}" \
    "${migration_env_file}"
  assert_dispatcher_ready_guard \
    "${migration_dropin}" "${migration_dispatcher_home}" published unbound
  [[ "$(dispatcher_ready_from_dropin "${migration_dropin}")" != \
    "${hard_kill_ready_path}" ]]
  [[ ! -e "${hard_kill_ready_path}" && ! -L "${hard_kill_ready_path}" ]]
  [[ -z "$(find "${migration_dispatcher_home}" -maxdepth 1 \
    -name '.hermes-dispatcher-migration.*' -print -quit)" ]]
done

printf '%s\n' "public installer transaction test passed"
