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
  runtime_python="$(command -v python3)"
fi
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
      "${work}/failure.stderr" \
      "${work}/nginx-failure.stderr" \
      "${work}/handshake.stderr" \
      "${work}/signal.stderr" \
      "${work}"/phase-*.stderr \
      "${work}/success.stderr"; do
      [[ -s "${diagnostic}" ]] || continue
      printf '%s\n' "--- $(basename "${diagnostic}") (tail) ---" >&2
      tail -n 120 -- "${diagnostic}" >&2 || true
    done
  fi
  rm -rf -- "${work}" "${stage}"
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

target="${work}/target"
backup="${work}/backups"
fake_bin="${work}/bin"
sshd_config="${work}/sshd_config"
sshd_original="${work}/sshd_config.original"
token_file="${work}/connector.token"
status_token_file="${work}/dbb3-status.token"
installation_token_file="${work}/managed-installation.token"
state_file="${work}/state/single.json"
runtime_home="${work}/hermes-home"
managed_installations_db="${runtime_home}/managed-installations.db"
managed_nodes_file="${runtime_home}/managed-nodes.json"
release_evidence_file="${work}/release/release-evidence.json"
nginx_dir="${work}/nginx"
nginx_security_target="${nginx_dir}/00-hermes-security.conf"
nginx_site_target="${nginx_dir}/daxueshenmai.top.conf"
install -d -m 0700 \
  "${stage}" "${target}" "${backup}" "${fake_bin}" "${nginx_dir}" "${runtime_home}"
stale_runtime_artifacts=(
  "${target}/.venv.candidate.stale"
  "${target}/.venv.failed.stale"
  "${target}/.venv.rollback-stale"
  "${target}/.collaboration-install.stale"
)
install -d -m 0700 "${stale_runtime_artifacts[@]}"
install -d -m 0700 "$(dirname "${state_file}")"
printf '%s' "connector-test-token" >"${token_file}"
printf '%s\n' "status-test-token-00000000000000000001" >"${status_token_file}"
printf '%s\n' "installation-test-token-000000000000001" >"${installation_token_file}"
chmod 0640 "${status_token_file}" "${installation_token_file}"
printf '%s\n' '{"conversations":[{"id":"old-state"}]}' >"${state_file}"
printf '%s\n' '{"nodes":[]}' >"${managed_nodes_file}"
assert_old_state() {
  python3 - "$1" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["conversations"][0]["id"] == "old-state"
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
if [[ "${1:-}" != "show" ]]; then
  printf '%s\n' "${1:-}" >>"${FAKE_SYSTEMCTL_LOG}"
fi
if [[ "${1:-}" == "start" && "${FAKE_STATUS_FAIL:-0}" == 1 \
  && ! -e "${HERMES_COLLABORATION_STATE_FILE}.mutated" ]]; then
  printf '%s\n' '{"conversations":[{"id":"new-state"}]}' >"${HERMES_COLLABORATION_STATE_FILE}"
  printf '%s\n' 'not a sqlite database' >"${HERMES_HOME_DIR}/managed-installations.db"
  : >"${HERMES_COLLABORATION_STATE_FILE}.mutated"
fi
if [[ "${1:-}" == "start" && "${FAKE_SIGNAL_ON_START:-0}" == 1 \
  && ! -e "${HERMES_COLLABORATION_STATE_FILE}.signaled" ]]; then
  printf '%s\n' '{"conversations":[{"id":"signal-state"}]}' >"${HERMES_COLLABORATION_STATE_FILE}"
  : >"${HERMES_COLLABORATION_STATE_FILE}.signaled"
  kill -TERM "${PPID}"
fi
case "${1:-}" in
  show)
    if [[ "$*" == *"ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,NRestarts"* ]]; then
      printf '%s\n' \
        'ActiveState=activating' \
        'SubState=auto-restart' \
        'Result=exit-code' \
        'ExecMainCode=1' \
        'ExecMainStatus=1' \
        'NRestarts=3'
    else
      printf '%s\n' "${FAKE_SYSTEMD_ENVIRONMENT:-}"
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
  stop|start|is-active) exit 0 ;;
  *) exit 0 ;;
esac
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
cat >/dev/null
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
  payload='{"api_version":1,"hermes_version":"test","profiles":[],"capabilities":[],"server_time":"2026-07-19T12:00:00Z"}'
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
chmod 0755 "${fake_bin}/systemctl" "${fake_bin}/sshd" "${fake_bin}/mv" \
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
  env \
    PATH="${fake_bin}:${PATH}" \
    FAKE_STATUS_FAIL="$1" \
    FAKE_SIGNAL_ON_START="${2:-0}" \
    FAKE_HANDSHAKE_FAIL="${3:-0}" \
    FAKE_NGINX_FAIL="${4:-0}" \
    HERMES_DEPLOY_FAIL_PHASE="${5:-}" \
    HERMES_RUNTIME_SOURCE_MIN_FILES=1 \
    IOS_CAPABILITIES_COUNT="${ios_capabilities_count}" \
    HERMES_AGENT_ROOT="${target}" \
    HERMES_RUNTIME_PYTHON="${runtime_python}" \
    HERMES_AGENT_SERVICE="hermes-agent-test.service" \
    HERMES_AGENT_USER="root" \
    HERMES_AGENT_GROUP="root" \
    HERMES_STAGE_OWNER="root" \
    HERMES_BACKUP_ROOT="${backup}" \
    HERMES_INSTALL_LOCK_FILE="${work}/collaboration-install.lock" \
    HERMES_COLLABORATION_STATE_FILE="${state_file}" \
    HERMES_HOME_DIR="${runtime_home}" \
    HERMES_COLLABORATION_CONNECTOR_TOKEN_FILE="${token_file}" \
    HERMES_MANAGED_NODE_TOKEN_FILE="${status_token_file}" \
    HERMES_MANAGED_INSTALLATION_TOKEN_FILE="${installation_token_file}" \
    HERMES_NGINX_SECURITY_TARGET="${nginx_security_target}" \
    HERMES_NGINX_SITE_TARGET="${nginx_site_target}" \
    HERMES_NGINX_SERVICE="nginx-test.service" \
    HERMES_NGINX_BINARY="${fake_bin}/nginx" \
    HERMES_RELEASE_EVIDENCE_FILE="${release_evidence_file}" \
    FAKE_SYSTEMCTL_LOG="${work}/systemctl.log" \
    FAKE_JOURNALCTL_LOG="${work}/journalctl.log" \
    FAKE_NGINX_LOG="${work}/nginx.log" \
    /bin/bash "${installer}" "${version}" "${stage}" \
      0000000000000000000000000000000000000001
}

set +e
run_installer 1 0 >"${work}/failure.stdout" 2>"${work}/failure.stderr"
failure_status=$?
set -e
[[ "${failure_status}" -ne 0 ]] || {
  printf '%s\n' "forced post-start failure unexpectedly succeeded" >&2
  exit 1
}
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
for relative in "${runtime_files[@]}"; do
  [[ "$(<"${target}/${relative}")" == "old:${relative}" ]] || {
    printf 'rollback mismatch: %s\n' "${relative}" >&2
    exit 1
  }
done
[[ "$(<"${nginx_security_target}")" == "old:nginx-security" ]]
[[ "$(<"${nginx_site_target}")" == "old:nginx-site" ]]
assert_old_state "${state_file}" || {
  cat "${work}/failure.stderr" >&2
  exit 1
}
grep -Fq '"nodes":[]' "${managed_nodes_file}"
[[ "$(python3 - "${managed_installations_db}" <<'PY'
import sqlite3
import sys
with sqlite3.connect(sys.argv[1]) as database:
    print(database.execute("SELECT value FROM marker").fetchone()[0])
PY
)" == "old-managed-installation-state" ]]
[[ "$(sed -n '1p' "${work}/systemctl.log")" == "stop" ]]
[[ "$(sed -n '2p' "${work}/systemctl.log")" == "start" ]]
[[ "$(sed -n '3p' "${work}/systemctl.log")" == "is-active" ]]
[[ "$(tail -n 2 "${work}/systemctl.log" | sed -n '1p')" == "stop" ]]
[[ "$(tail -n 1 "${work}/systemctl.log")" == "start" ]]

: >"${work}/systemctl.log"
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
[[ "$(sed -n '1p' "${work}/systemctl.log")" == "stop" ]]
[[ "$(sed -n '2p' "${work}/systemctl.log")" == "stop" ]]
[[ "$(tail -n 1 "${work}/systemctl.log")" == "start" ]]

: >"${work}/systemctl.log"
set +e
run_installer 0 0 1 >"${work}/handshake.stdout" 2>"${work}/handshake.stderr"
handshake_status=$?
set -e
[[ "${handshake_status}" -ne 0 ]] || {
  printf '%s\n' "forced mobile handshake failure unexpectedly succeeded" >&2
  exit 1
}
grep -Fq "anonymous mobile handshake did not respond" "${work}/handshake.stderr"
for relative in "${runtime_files[@]}"; do
  [[ "$(<"${target}/${relative}")" == "old:${relative}" ]] || {
    printf 'handshake rollback mismatch: %s\n' "${relative}" >&2
    exit 1
  }
done
assert_old_state "${state_file}"
grep -Fq '"nodes":[]' "${managed_nodes_file}"

: >"${work}/systemctl.log"
set +e
run_installer 0 1 >"${work}/signal.stdout" 2>"${work}/signal.stderr"
signal_status=$?
set -e
[[ "${signal_status}" -eq 143 ]] || {
  printf 'signal interruption returned %s, expected 143\n' "${signal_status}" >&2
  exit 1
}
for relative in "${runtime_files[@]}"; do
  [[ "$(<"${target}/${relative}")" == "old:${relative}" ]] || {
    printf 'signal rollback mismatch: %s\n' "${relative}" >&2
    exit 1
  }
done
assert_old_state "${state_file}"
grep -Fq '"nodes":[]' "${managed_nodes_file}"
[[ "$(sed -n '1p' "${work}/systemctl.log")" == "stop" ]]
[[ "$(sed -n '2p' "${work}/systemctl.log")" == "start" ]]
[[ "$(sed -n '3p' "${work}/systemctl.log")" == "stop" ]]
[[ "$(sed -n '4p' "${work}/systemctl.log")" == "start" ]]

for injected_phase in migrate candidate-health traffic-switch drain commit; do
  : >"${work}/systemctl.log"
  set +e
  run_installer 0 0 0 0 "${injected_phase}" \
    >"${work}/phase-${injected_phase}.stdout" \
    2>"${work}/phase-${injected_phase}.stderr"
  phase_status=$?
  set -e
  [[ "${phase_status}" -ne 0 ]] || {
    printf 'forced %s failure unexpectedly succeeded\n' "${injected_phase}" >&2
    exit 1
  }
  grep -Fq "injected deployment failure at ${injected_phase}" \
    "${work}/phase-${injected_phase}.stderr"
  for relative in "${runtime_files[@]}"; do
    [[ "$(<"${target}/${relative}")" == "old:${relative}" ]]
  done
  assert_old_state "${state_file}"
  [[ "$(python3 - "${managed_installations_db}" <<'PY'
import sqlite3
import sys
with sqlite3.connect(sys.argv[1]) as database:
    print(database.execute("SELECT value FROM marker").fetchone()[0])
PY
)" == "old-managed-installation-state" ]]
  [[ ! -e "${release_evidence_file}" ]]
done

: >"${work}/systemctl.log"
run_installer 0 0 >"${work}/success.stdout" 2>"${work}/success.stderr" || {
  cat "${work}/success.stdout" >&2
  cat "${work}/success.stderr" >&2
  exit 1
}
for relative in "${runtime_files[@]}"; do
  cmp -- "${stage}/${relative}" "${target}/${relative}"
done
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
PY
[[ "$(python3 - "${managed_installations_db}" <<'PY'
import sqlite3
import sys
with sqlite3.connect(sys.argv[1]) as database:
    print(database.execute("SELECT value FROM marker").fetchone()[0])
PY
)" == "old-managed-installation-state" ]]
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
[[ "$(sed -n '1p' "${work}/systemctl.log")" == "stop" ]]
[[ "$(sed -n '2p' "${work}/systemctl.log")" == "start" ]]
[[ "$(sed -n '3p' "${work}/systemctl.log")" == "is-active" ]]
[[ "$(tail -n 1 "${work}/systemctl.log")" == "reload" ]]
printf '%s\n' "public installer transaction test passed"
