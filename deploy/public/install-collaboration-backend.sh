#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Root-side transactional installer. The caller uploads a stage owned by the
# unprivileged admin account, then invokes this script through sudo. No file is
# replaced until the staged Python/manifest validation and authenticated
# connector-health preflight have passed.

die() { printf 'install-collaboration-backend: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

install_lock="${HERMES_INSTALL_LOCK_FILE:-/run/lock/hermes-agent/collaboration-install.lock}"
install_lock_dir="$(dirname "${install_lock}")"
if [[ ! -d "${install_lock_dir}" ]]; then
  install -d -o root -g root -m 0755 "${install_lock_dir}"
fi
[[ -d "${install_lock_dir}" && ! -L "${install_lock_dir}" ]] || die "unsafe install lock directory"
[[ "$(stat -c '%u' "${install_lock_dir}")" == 0 ]] || die "install lock directory must be root-owned"
lock_dir_mode="$(stat -c '%a' "${install_lock_dir}")"
(( (8#${lock_dir_mode} & 0022) == 0 )) || die "install lock directory must not be group/world-writable"
if [[ -e "${install_lock}" || -L "${install_lock}" ]]; then
  [[ -f "${install_lock}" && ! -L "${install_lock}" ]] || die "unsafe install lock file"
  [[ "$(stat -c '%u' "${install_lock}")" == 0 ]] || die "install lock file must be root-owned"
fi
exec 8>"${install_lock}"
chmod 0600 "${install_lock}"
lock_wait_seconds="${HERMES_INSTALL_LOCK_WAIT_SECONDS:-900}"
[[ "${lock_wait_seconds}" =~ ^[1-9][0-9]*$ ]] \
  || die "HERMES_INSTALL_LOCK_WAIT_SECONDS must be a positive integer"
# A cancelled CI SSH session can leave the remote installer finishing its
# bounded recovery/health transaction. Wait for that owner to release the
# lock instead of turning a safe serialized deployment into a false failure.
flock --wait "${lock_wait_seconds}" 8 \
  || die "another collaboration deployment is still running after ${lock_wait_seconds}s"

version="${1:-}"
stage="${2:-}"
[[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid release version"
[[ -n "${stage}" && -d "${stage}" ]] || die "release stage is missing"
release_commit="${3:-${HERMES_RELEASE_COMMIT:-}}"
[[ "${release_commit}" =~ ^[0-9a-f]{40}$ ]] \
  || die "HERMES_RELEASE_COMMIT must be a full lowercase Git commit"
deploy_fail_phase="${HERMES_DEPLOY_FAIL_PHASE:-}"
case "${deploy_fail_phase}" in
  ""|prepare|migrate|candidate-health|traffic-switch|drain|commit) ;;
  *) die "unknown HERMES_DEPLOY_FAIL_PHASE: ${deploy_fail_phase}" ;;
esac
release_phase() {
  local phase="$1"
  printf 'release_phase=%s\n' "${phase}"
  [[ "${deploy_fail_phase}" != "${phase}" ]] \
    || die "injected deployment failure at ${phase}"
}

stage_owner="${HERMES_STAGE_OWNER:-admin}"
stage_root="$(realpath -e -- "${stage}")"
case "${stage_root}" in
  "/home/${stage_owner}/.cache/hermes-agent-deploy/"*|\
  "/tmp/hermes-agent-deploy/"*|\
  "/dev/shm/hermes-agent-deploy/"*) ;;
  *) die "stage must be below an approved Hermes deployment staging root" ;;
esac
[[ "$(stat -c '%U' "${stage_root}")" == "${stage_owner}" ]] || die "stage is not owned by ${stage_owner}"

required=(
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
  "deploy/public/nginx-00-hermes-security.conf"
  "deploy/public/nginx-daxueshenmai.top.conf"
  "deploy/public/managed-nodes.server.json"
  "deploy/public/runtime-requirements.lock"
)
runtime_service_assets=(
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
  "hermes_constants.py"
  "hermes_logging.py"
  "hermes_secret_compare.py"
  "hermes_state.py"
  "mcp_serve.py"
  "model_tools.py"
  "plugins/context_engine/__init__.py"
  "plugins/account_cleanup_backend.py"
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
)
runtime_source_manifest_relative="deploy/public/runtime-source-files.nul"
runtime_source_manifest="${stage_root}/${runtime_source_manifest_relative}"
[[ -f "${runtime_source_manifest}" && ! -L "${runtime_source_manifest}" ]] \
  || die "runtime source manifest is missing or unsafe"
runtime_source_manifest_sha256="$(sha256sum "${runtime_source_manifest}" | cut -d' ' -f1)"
[[ "${runtime_source_manifest_sha256}" =~ ^[0-9a-f]{64}$ ]] \
  || die "runtime source manifest SHA-256 is invalid"
mapfile -d '' -t staged_runtime_service_assets <"${runtime_source_manifest}"
runtime_source_min_files="${HERMES_RUNTIME_SOURCE_MIN_FILES:-500}"
[[ "${runtime_source_min_files}" =~ ^[1-9][0-9]*$ ]] \
  || die "HERMES_RUNTIME_SOURCE_MIN_FILES must be a positive integer"
(( ${#staged_runtime_service_assets[@]} >= runtime_source_min_files \
   && ${#staged_runtime_service_assets[@]} <= 5000 )) \
  || die "runtime source manifest entry count is outside the production boundary"
runtime_service_assets+=("${staged_runtime_service_assets[@]}")
declare -A runtime_asset_seen=()
deduplicated_runtime_service_assets=()
for relative in "${runtime_service_assets[@]}"; do
  [[ -n "${relative}" && "${relative}" == *.py ]] \
    || die "runtime source manifest contains a non-Python entry"
  [[ "${relative}" =~ ^[A-Za-z0-9_.+/-]+$ ]] \
    || die "runtime source path contains unsupported characters: ${relative}"
  case "/${relative}/" in
    *'//'*|*'/../'*|*'/./'*) die "unsafe runtime source path: ${relative}" ;;
  esac
  case "${relative}" in
    */tests/*|*/test_*.py|*/__pycache__/*)
      die "runtime source manifest contains a test or cache path: ${relative}"
      ;;
  esac
  if [[ "${relative}" == */* ]]; then
    case "${relative}" in
      agent/*|gateway/*|hermes_cli/*|hermes_runtime/*|hermes_services/*|tools/*|\
      tui_gateway/*|providers/*|cron/*|acp_adapter/*|plugins/*) ;;
      *) die "runtime source path is outside approved roots: ${relative}" ;;
    esac
  fi
  [[ -f "${stage_root}/${relative}" && ! -L "${stage_root}/${relative}" ]] \
    || die "runtime source is missing or unsafe: ${relative}"
  if [[ -z "${runtime_asset_seen[${relative}]:-}" ]]; then
    runtime_asset_seen["${relative}"]=1
    deduplicated_runtime_service_assets+=("${relative}")
  fi
done
runtime_service_assets=("${deduplicated_runtime_service_assets[@]}")
for required_runtime_source in \
  hermes_auth_errors.py hermes_cli/web_models.py \
  agent/interrupt_compat.py gateway/streaming_tts_consumer.py; do
  [[ -n "${runtime_asset_seen[${required_runtime_source}]:-}" ]] \
    || die "runtime source manifest omitted ${required_runtime_source}"
done
required+=("${runtime_source_manifest_relative}")
required+=("${runtime_service_assets[@]}")
# The iOS intelligence release is staged alongside the collaboration release.
# Keep this list optional for one-release rollback compatibility: an older
# stage can still be installed, while a stage containing the plugin is copied
# as one transaction with all of its runtime dependencies.
ios_optional=(
  "hermes_cli/account_cleanup.py"
  "hermes_cli/ios_intelligence.py"
  "hermes_cli/ios_intelligence_config.py"
  "hermes_cli/ios_intelligence_scheduler.py"
  "hermes_cli/ios_intelligence_supervisor.py"
  "hermes_cli/ios_mcp_supervisor.py"
  "hermes_cli/ios_mcp_server.py"
  "plugins/ios-intelligence/dashboard/plugin_api.py"
  "plugins/ios-intelligence/dashboard/manifest.json"
  "hermes_cli/dashboard_auth/__init__.py"
  "hermes_cli/dashboard_auth/owner_mobile.py"
  "hermes_cli/dashboard_auth/registry.py"
  "hermes_cli/dashboard_auth/routes.py"
  "hermes_cli/profiles.py"
  "hermes_cli/managed_node_recovery_service.py"
  "plugins/dashboard_auth/basic/__init__.py"
  "tools/mcp_tool.py"
)
for relative in "${required[@]}"; do
  source_file="${stage_root}/${relative}"
  [[ -f "${source_file}" && ! -L "${source_file}" ]] || die "missing or unsafe ${relative}"
done
ios_enabled=0
for relative in "${ios_optional[@]}"; do
  if [[ -f "${stage_root}/${relative}" && ! -L "${stage_root}/${relative}" ]]; then
    ios_enabled=1
  fi
done
if [[ "${ios_enabled}" == 1 ]]; then
  for relative in "${ios_optional[@]}"; do
    source_file="${stage_root}/${relative}"
    [[ -f "${source_file}" && ! -L "${source_file}" ]] || die "missing or unsafe iOS intelligence asset ${relative}"
  done
fi

target_root="${HERMES_AGENT_ROOT:-/opt/hermes-agent}"
runtime_python="${HERMES_RUNTIME_PYTHON:-${target_root}/.venv/bin/python}"
[[ -x "${runtime_python}" ]] || die "Hermes runtime Python is missing: ${runtime_python}"
journalctl_binary="${HERMES_JOURNALCTL_BINARY:-journalctl}"

emit_service_failure_diagnostics() {
  printf '%s\n' "service_diagnostics_begin"
  systemctl show "${service}" --no-pager \
    --property=ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,NRestarts \
    2>&1 || true
  # ``systemctl show`` only reports the supervisor state. Include the exact
  # unit and socket ownership so a process that exits cleanly before binding
  # cannot look healthy merely because systemd keeps restarting it.
  systemctl cat "${service}" --no-pager 2>&1 || true
  if command -v ss >/dev/null 2>&1; then
    ss --listening --numeric --tcp --process 2>&1 || true
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:9119 -sTCP:LISTEN 2>&1 || true
  fi
  if command -v "${journalctl_binary}" >/dev/null 2>&1; then
    # Startup logs are the only useful explanation for a service that exits
    # before binding. Force the central redaction policy because CI logs are
    # an external boundary and runtime redaction may be operator-disabled.
    "${journalctl_binary}" --unit "${service}" --since "${service_start_since}" \
      --no-pager --lines 160 --output=short-iso-precise 2>&1 \
      | env PYTHONPATH="${target_root}${PYTHONPATH:+:${PYTHONPATH}}" \
          "${runtime_python}" -c \
          'import re, sys; from hermes_runtime.redaction import redact_sensitive_text; text = redact_sensitive_text(sys.stdin.read(), force=True, redact_url_credentials=True); text = re.sub(r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|credential|authorization)\s*[=:]\s*)(?:\"[^\"\n]*\"|\x27[^\x27\n]*\x27|[^\s,;]+)", r"\1[REDACTED]", text); print(text, end="")' \
      || true
  else
    printf 'journal diagnostics unavailable: %s not found\n' \
      "${journalctl_binary}" >&2
  fi
  printf '%s\n' "service_diagnostics_end"
}

# Copy through a root-owned snapshot. Reading the admin-owned stage through a
# lower-privileged tar process prevents a symlink swap during privileged copy.
snapshot="$(mktemp -d /run/hermes-agent-collaboration.XXXXXX)"
cleanup_snapshot() { rm -rf -- "${snapshot}"; }
trap cleanup_snapshot EXIT
snapshot_paths=("${required[@]}")
if [[ "${ios_enabled}" == 1 ]]; then
  snapshot_paths+=("${ios_optional[@]}")
fi
if command -v setpriv >/dev/null 2>&1; then
  setpriv --reuid="${stage_owner}" --regid="${stage_owner}" --init-groups -- \
    tar -C "${stage_root}" -cf - -- "${snapshot_paths[@]}" \
    | tar --no-same-owner -C "${snapshot}" -xf -
else
  runuser -u "${stage_owner}" -- tar -C "${stage_root}" -cf - -- "${snapshot_paths[@]}" \
    | tar --no-same-owner -C "${snapshot}" -xf -
fi
for relative in "${required[@]}"; do
  [[ -f "${snapshot}/${relative}" && ! -L "${snapshot}/${relative}" ]] || die "unsafe snapshot ${relative}"
done
[[ "$(sha256sum "${snapshot}/${runtime_source_manifest_relative}" | cut -d' ' -f1)" \
    == "${runtime_source_manifest_sha256}" ]] \
  || die "runtime source manifest changed while the root snapshot was created"
if [[ "${ios_enabled}" == 1 ]]; then
  for relative in "${ios_optional[@]}"; do
    [[ -f "${snapshot}/${relative}" && ! -L "${snapshot}/${relative}" ]] || die "unsafe snapshot ${relative}"
  done
fi

# Validate the immutable root-owned snapshot that will actually be installed.
# Validating the admin-owned stage before this copy would leave a write window
# in which the staged source could diverge from the checked content.
manifest_version="$("${runtime_python}" - "${snapshot}/plugins/collaboration/dashboard/manifest.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("version", ""))
PY
)"
[[ "${manifest_version}" == "${version}" ]] || die "manifest version ${manifest_version@Q} does not match ${version}"
manifest_sha256="$(sha256sum "${snapshot}/plugins/collaboration/dashboard/manifest.json" | cut -d' ' -f1)"
[[ "${manifest_sha256}" =~ ^[0-9a-f]{64}$ ]] || die "manifest SHA-256 is invalid"
"${runtime_python}" - \
  "${snapshot}/plugins/collaboration/dashboard/plugin_api.py" \
  "${snapshot}/hermes_cli/cloud_file_library.py" \
  "${snapshot}/hermes_cli/dashboard_auth/public_paths.py" \
  "${snapshot}/hermes_cli/dashboard_auth/token_auth.py" \
  "${snapshot}/hermes_cli/dashboard_auth/mobile_device_store.py" \
  "${snapshot}/hermes_cli/dashboard_auth/mobile_notifications.py" \
  "${snapshot}/hermes_cli/managed_installations.py" \
  "${snapshot}/hermes_cli/managed_nodes.py" \
  "${snapshot}/hermes_cli/web_server.py" \
  "${snapshot}/tools/managed_installation_tool.py" \
  "${snapshot}/toolsets.py" \
  "${snapshot}/agent/agent_init.py" \
  "${snapshot}/agent/prompt_builder.py" \
  "${snapshot}/agent/system_prompt.py" \
  "${snapshot}/agent/context_diagnostics.py" \
  "${snapshot}/hermes_cli/doctor.py" \
  "${snapshot}/tui_gateway/server.py" <<'PY'
import pathlib, sys
for name in sys.argv[1:]:
    compile(pathlib.Path(name).read_text(encoding="utf-8"), name, "exec")
PY
runtime_compile_assets=()
for relative in "${runtime_service_assets[@]}"; do
  runtime_compile_assets+=("${snapshot}/${relative}")
done
"${runtime_python}" - "${runtime_compile_assets[@]}" <<'PY'
import pathlib, sys
for name in sys.argv[1:]:
    compile(pathlib.Path(name).read_text(encoding="utf-8"), name, "exec")
PY
if [[ "${ios_enabled}" == 1 ]]; then
  "${runtime_python}" - "${snapshot}/hermes_cli/account_cleanup.py" \
    "${snapshot}/hermes_cli/ios_intelligence.py" \
    "${snapshot}/hermes_cli/ios_intelligence_config.py" \
    "${snapshot}/hermes_cli/ios_intelligence_scheduler.py" \
    "${snapshot}/hermes_cli/ios_intelligence_supervisor.py" \
    "${snapshot}/hermes_cli/ios_mcp_supervisor.py" \
    "${snapshot}/hermes_cli/ios_mcp_server.py" \
    "${snapshot}/hermes_cli/dashboard_auth/__init__.py" \
    "${snapshot}/hermes_cli/dashboard_auth/owner_mobile.py" \
    "${snapshot}/hermes_cli/dashboard_auth/registry.py" \
    "${snapshot}/hermes_cli/dashboard_auth/routes.py" \
    "${snapshot}/hermes_cli/profiles.py" \
    "${snapshot}/hermes_cli/managed_nodes.py" \
    "${snapshot}/hermes_cli/managed_node_recovery_service.py" \
    "${snapshot}/plugins/dashboard_auth/basic/__init__.py" \
    "${snapshot}/plugins/ios-intelligence/dashboard/plugin_api.py" \
    "${snapshot}/tools/mcp_tool.py" <<'PY'
import pathlib, sys
for name in sys.argv[1:]:
    compile(pathlib.Path(name).read_text(encoding="utf-8"), name, "exec")
PY
fi

service="${HERMES_AGENT_SERVICE:-hermes-agent.service}"
service_user="${HERMES_AGENT_USER:-hermes-agent}"
service_group="${HERMES_AGENT_GROUP:-hermes-agent}"
plugin_target="${target_root}/plugins/collaboration/dashboard"
core_target="${target_root}/hermes_cli/cloud_file_library.py"
public_paths_target="${target_root}/hermes_cli/dashboard_auth/public_paths.py"
token_auth_target="${target_root}/hermes_cli/dashboard_auth/token_auth.py"
mobile_device_store_target="${target_root}/hermes_cli/dashboard_auth/mobile_device_store.py"
mobile_notifications_target="${target_root}/hermes_cli/dashboard_auth/mobile_notifications.py"
managed_installations_target="${target_root}/hermes_cli/managed_installations.py"
managed_nodes_code_target="${target_root}/hermes_cli/managed_nodes.py"
web_server_target="${target_root}/hermes_cli/web_server.py"
managed_installation_tool_target="${target_root}/tools/managed_installation_tool.py"
toolsets_target="${target_root}/toolsets.py"
agent_init_target="${target_root}/agent/agent_init.py"
prompt_builder_target="${target_root}/agent/prompt_builder.py"
system_prompt_target="${target_root}/agent/system_prompt.py"
context_diagnostics_target="${target_root}/agent/context_diagnostics.py"
doctor_target="${target_root}/hermes_cli/doctor.py"
tui_gateway_target="${target_root}/tui_gateway/server.py"
nginx_security_target="${HERMES_NGINX_SECURITY_TARGET:-/etc/nginx/conf.d/00-hermes-security.conf}"
nginx_site_target="${HERMES_NGINX_SITE_TARGET:-/etc/nginx/conf.d/daxueshenmai.top.conf}"
nginx_service="${HERMES_NGINX_SERVICE:-nginx.service}"
nginx_binary="${HERMES_NGINX_BINARY:-nginx}"
[[ -d "${target_root}" ]] || die "target root does not exist: ${target_root}"
id "${service_user}" >/dev/null 2>&1 || die "service user does not exist: ${service_user}"
command -v "${nginx_binary}" >/dev/null 2>&1 || die "nginx binary is missing: ${nginx_binary}"
for nginx_target in "${nginx_security_target}" "${nginx_site_target}"; do
  nginx_target_dir="$(dirname "${nginx_target}")"
  [[ -d "${nginx_target_dir}" && ! -L "${nginx_target_dir}" ]] \
    || die "unsafe nginx target directory: ${nginx_target_dir}"
  [[ "$(stat -c '%u' "${nginx_target_dir}")" == 0 ]] \
    || die "nginx target directory must be root-owned: ${nginx_target_dir}"
  nginx_target_mode="$(stat -c '%a' "${nginx_target_dir}")"
  (( (8#${nginx_target_mode} & 0022) == 0 )) \
    || die "nginx target directory must not be group/world-writable: ${nginx_target_dir}"
done

# Existing connector installations must pass the deployment gate before any
# file changes. A legacy installation without the route is permitted exactly
# one bootstrap; the same authenticated contract is mandatory after restart.
health_url="${HERMES_CONNECTOR_HEALTH_URL:-http://127.0.0.2:9119/api/plugins/collaboration/connector/health}"
deployment_health_url="${HERMES_DEPLOYMENT_HEALTH_URL:-http://127.0.0.2:9119/api/plugins/collaboration/connector/deployment-health}"
health_curl_proxy_args=()
case "${health_url}" in
  http://127.*|https://127.*|http://localhost/*|https://localhost/*|http://\[::1\]/*|https://\[::1\]/*)
    health_curl_proxy_args=(--noproxy '*')
    ;;
esac
connector_id="${HERMES_CONNECTOR_ID:-dbb3-primary}"
token_file="${HERMES_COLLABORATION_CONNECTOR_TOKEN_FILE:-}"
env_file="${HERMES_AGENT_ENV_FILE:-/etc/hermes-agent/hermes-agent.env}"
if [[ "${ios_enabled}" == 1 ]]; then
  [[ -f "${env_file}" && ! -L "${env_file}" ]] || die "restricted Hermes environment file is missing"
  [[ "$(stat -c '%u' "${env_file}")" == 0 ]] || die "Hermes environment file must be root-owned"
  chmod 0600 "${env_file}"
  env_has_secret() {
    local first="$1" second="${2:-}"
    awk -F= -v first="${first}" -v second="${second}" '
      ($1 == first || (second != "" && $1 == second)) {
        value = substr($0, index($0, "=") + 1)
        gsub(/^[[:space:]"'\'' ]+|[[:space:]"'\'' ]+$/, "", value)
        if (value != "" && value !~ /^\$\{.*\}$/) found = 1
      }
      END { exit(found ? 0 : 1) }
    ' "${env_file}"
  }
  env_has_secret HERMES_QWEATHER_API_KEY QWEATHER_API_KEY \
    || die "QWeather credential is missing from the restricted environment"
  env_has_secret HERMES_AMAP_WEB_API_KEY AMAP_WEB_API_KEY \
    || die "AMap credential is missing from the restricted environment"
  env_has_secret HERMES_IOS_DATA_KEY HERMES_DATA_ENCRYPTION_KEY \
    || die "iOS account data-encryption key is missing from the restricted environment"
fi
if [[ -z "${token_file}" && -r "${env_file}" ]]; then
  token_file="$(sed -n 's/^HERMES_COLLABORATION_CONNECTOR_TOKEN_FILE=//p' "${env_file}" | tail -n 1)"
  token_file="${token_file#\"}"; token_file="${token_file%\"}"
  token_file="${token_file#\'}"; token_file="${token_file%\'}"
fi
[[ -n "${token_file}" && -r "${token_file}" ]] || die "connector token file is not readable; health preflight refused"
env_runtime_home=""
if [[ -r "${env_file}" ]]; then
  env_runtime_home="$(sed -n 's/^HERMES_HOME=//p' "${env_file}" | tail -n 1)"
  env_runtime_home="${env_runtime_home#\"}"; env_runtime_home="${env_runtime_home%\"}"
  env_runtime_home="${env_runtime_home#\'}"; env_runtime_home="${env_runtime_home%\'}"
fi
service_home="$(getent passwd "${service_user}" | cut -d: -f6)"
systemd_environment="$(systemctl show "${service}" --property=Environment --value 2>/dev/null || true)"
systemd_runtime_home="$(
  printf '%s\n' "${systemd_environment}" \
    | tr ' ' '\n' \
    | sed -n 's/^HERMES_HOME=//p' \
    | tail -n 1
)"
systemd_runtime_home="${systemd_runtime_home#\"}"; systemd_runtime_home="${systemd_runtime_home%\"}"
systemd_runtime_home="${systemd_runtime_home#\'}"; systemd_runtime_home="${systemd_runtime_home%\'}"
runtime_home="${HERMES_HOME_DIR:-${systemd_runtime_home:-${env_runtime_home:-${service_home}/.hermes}}}"
[[ -n "${runtime_home}" && "${runtime_home}" == /* ]] \
  || die "Hermes runtime home must be an absolute path"
state_target="${HERMES_COLLABORATION_STATE_FILE:-${runtime_home}/collaboration/single.json}"
config_target="${HERMES_CONFIG_FILE:-${runtime_home}/config.yaml}"
ios_supervisor_target="${runtime_home}/ios-mcp-supervisor.db"
ios_database_target="${runtime_home}/ios-intelligence.db"
mobile_auth_target="${runtime_home}/dashboard/mobile-auth.db"
cloud_files_database_target="${runtime_home}/collaboration/account-files/library.sqlite3"
managed_installations_database_target="${runtime_home}/managed-installations.db"
managed_nodes_target="${runtime_home}/managed-nodes.json"
release_evidence_target="${HERMES_RELEASE_EVIDENCE_FILE:-/var/lib/hermes-agent-release/release-evidence.json}"
[[ "${release_evidence_target}" == /* ]] || die "release evidence path must be absolute"
release_evidence_dir="$(dirname "${release_evidence_target}")"
if [[ ! -d "${release_evidence_dir}" ]]; then
  install -d -o root -g root -m 0755 "${release_evidence_dir}"
fi
[[ -d "${release_evidence_dir}" && ! -L "${release_evidence_dir}" ]] \
  || die "release evidence directory is unsafe"
[[ "$(stat -c '%u' "${release_evidence_dir}")" == 0 ]] \
  || die "release evidence directory must be root-owned"
release_evidence_mode="$(stat -c '%a' "${release_evidence_dir}")"
(( (8#${release_evidence_mode} & 0022) == 0 )) \
  || die "release evidence directory must not be group/world-writable"
managed_node_token_file="${HERMES_MANAGED_NODE_TOKEN_FILE:-/etc/hermes-agent/dbb3-status-token}"
managed_installation_token_file="${HERMES_MANAGED_INSTALLATION_TOKEN_FILE:-/etc/hermes-agent/managed-installation-token}"
[[ "${managed_node_token_file}" != "${managed_installation_token_file}" ]] \
  || die "status and installation credentials must use different files"
validate_managed_token_file() {
  local credential_file="$1" label="$2"
  [[ "${credential_file}" == /* && -f "${credential_file}" && ! -L "${credential_file}" ]] \
    || die "${label} credential path is missing or unsafe"
  [[ "$(stat -c '%U' "${credential_file}")" == root ]] \
    || die "${label} credential must be root-owned"
  [[ "$(stat -c '%G' "${credential_file}")" == "${service_group}" ]] \
    || die "${label} credential group must be ${service_group}"
  [[ "$(stat -c '%a' "${credential_file}")" == 640 ]] \
    || die "${label} credential mode must be 0640"
  local credential_value
  credential_value="$(cat -- "${credential_file}")"
  (( ${#credential_value} >= 32 && ${#credential_value} <= 4096 )) \
    || die "${label} credential length must be 32..4096 characters"
  [[ "${credential_value}" != *$'\n'* && "${credential_value}" != *$'\r'* ]] \
    || die "${label} credential must contain exactly one line"
  printf '%s\n' "${credential_value}" | cmp -s -- - "${credential_file}" \
    || die "${label} credential must have one newline-terminated line"
  unset credential_value
}
for credential_file in "${managed_node_token_file}" "${managed_installation_token_file}"; do
  runuser -u "${service_user}" -- test -r "${credential_file}" \
    || die "managed-node credential is not readable by ${service_user}"
done
validate_managed_token_file "${managed_node_token_file}" status
validate_managed_token_file "${managed_installation_token_file}" installation
for existing_credential in "${managed_node_token_file}" "${token_file}"; do
  if cmp -s -- "${existing_credential}" "${managed_installation_token_file}"; then
    die "managed installation credentials must be dedicated"
  fi
done
if [[ "${ios_enabled}" == 1 ]]; then
  ios_database_target="$("${runtime_python}" - "${config_target}" "${runtime_home}" "${service_home}" <<'PY'
import pathlib
import sys

import yaml

config_path = pathlib.Path(sys.argv[1])
runtime_home = pathlib.Path(sys.argv[2])
service_home = pathlib.Path(sys.argv[3])
data = {}
if config_path.is_file():
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict):
        data = loaded
section = data.get("ios_intelligence")
raw = str(section.get("database_path") or "").strip() if isinstance(section, dict) else ""
if not raw:
    path = runtime_home / "ios-intelligence.db"
elif raw == "~":
    path = service_home
elif raw.startswith("~/"):
    path = service_home / raw[2:]
else:
    path = pathlib.Path(raw)
    if not path.is_absolute():
        path = runtime_home / path
if path.suffix not in {".db", ".sqlite", ".sqlite3"}:
    path = path / "ios-intelligence.db"
print(path.absolute())
PY
)"
  [[ -n "${ios_database_target}" ]] || die "iOS intelligence database path is empty"
fi
token="$(cat -- "${token_file}")"
[[ -n "${token}" ]] || die "connector token file is empty"
curl_cfg="$(mktemp /run/hermes-agent-health.XXXXXX)"
chmod 0600 "${curl_cfg}"
trap 'rm -f -- "${curl_cfg}"; cleanup_snapshot' EXIT
printf 'header = "Authorization: Bearer %s"\nheader = "X-Connector-ID: %s"\nheader = "Accept: application/json"\n' \
  "${token}" "${connector_id}" >"${curl_cfg}"
unset token
validate_connector_health() {
  local output="$1"
  local require_identity="${2:-1}"
  curl --fail --silent --show-error --max-time 8 \
    "${health_curl_proxy_args[@]}" \
    --config "${curl_cfg}" -o "${output}" "${health_url}" \
    && validate_connector_health_payload "${output}" "${require_identity}"
}
validate_connector_health_payload() {
  local output="$1"
  local require_identity="${2:-1}"
  "${runtime_python}" - "${output}" "${connector_id}" "${require_identity}" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data.get("ok") is True
assert int(data.get("contract_version", 0)) == 2
if sys.argv[3] == "1":
    assert data.get("connector_id") == sys.argv[2]
else:
    assert data.get("connector_id") in (None, sys.argv[2])
assert "artifact-upload" in (data.get("capabilities") or [])
assert "attachment-download" in (data.get("capabilities") or [])
PY
}
validate_ios_health() {
  local output="$1"
  "${runtime_python}" - "${output}" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
runtime = data.get("mcp_runtime") or {}
schema_version = int(data.get("code_schema_version") or 0)
assert data.get("ok") is True
assert schema_version > 0
assert data.get("db_user_version") == schema_version
assert data.get("schema_migrated") is True
assert data.get("schema_compatible") is True
assert data.get("scheduler_running") is True
assert data.get("cleanup_worker_running") is True
scheduler = data.get("scheduler") or {}
assert scheduler.get("ok") is True
assert scheduler.get("running") is True
assert scheduler.get("thread_alive") is True
assert int(scheduler.get("cycle_count") or 0) > 0
assert int(scheduler.get("last_cycle_completed_at") or 0) > 0
assert not scheduler.get("last_error")
assert runtime.get("ok") is True
assert runtime.get("running") is True
assert runtime.get("starting") is not True
required_count = int(runtime.get("required_count") or 0)
assert required_count > 0
assert runtime.get("healthy_count") == required_count
services = runtime.get("services") or []
assert len(services) == required_count
assert all(item.get("ok") is True for item in services)
assert all(item.get("contract_ok") is True for item in services)
assert len({item.get("name") for item in services}) == required_count
assert all(item.get("version") for item in services)
assert all(item.get("active_version") == item.get("version") for item in services)
assert all(
    sorted(item.get("tools") or []) == sorted(item.get("expected_tools") or [])
    for item in services
)
assert all(
    set(item.get("granted_scopes") or []).issubset(item.get("declared_scopes") or [])
    for item in services
)
PY
}
validate_deployment_health() {
  local output="$1"
  curl --fail --silent --show-error --max-time 12 \
    "${health_curl_proxy_args[@]}" \
    --config "${curl_cfg}" -o "${output}" "${deployment_health_url}" \
    && "${runtime_python}" - "${output}" "${version}" "${manifest_sha256}" "${connector_id}" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data.get("ok") is True
assert data.get("connector_id") == sys.argv[4]
assert int(data.get("contract_version") or 0) == 2
assert data.get("manifest_version") == sys.argv[2]
assert data.get("manifest_sha256") == sys.argv[3]
assert data.get("managed_catalog_readable") is True
databases = data.get("databases") or {}
assert set(databases) == {"cloud_files", "mobile_auth", "managed_resources"}
for status in databases.values():
    assert status.get("ok") is True
    assert int(status.get("code_schema_version") or 0) > 0
    assert status.get("db_user_version") == status.get("code_schema_version")
    assert status.get("integrity_check") == "ok"
    assert len(str(status.get("schema_sha256") or "")) == 64
managed = databases["managed_resources"]
assert managed.get("catalog_rows") is not None
assert "managed_resource_catalog" in (managed.get("required_tables") or [])
assert "managed_installation_source_lock_immutable" in (
    managed.get("required_triggers") or []
)
PY
}
preflight_health="$(mktemp /run/hermes-agent-connector-preflight.XXXXXX)"
if [[ -f "${plugin_target}/plugin_api.py" ]] \
  && grep -Fq '@router.get("/connector/health")' "${plugin_target}/plugin_api.py"; then
  if ! curl --fail --silent --show-error --max-time 8 \
    "${health_curl_proxy_args[@]}" \
    --config "${curl_cfg}" -o "${preflight_health}" "${health_url}"; then
    printf '%s\n' "connector health endpoint is unreachable; continuing with recovery transaction" >&2
  elif ! validate_connector_health_payload "${preflight_health}" 0; then
    if systemctl is-active --quiet "${service}"; then
      die "connector health preflight failed while ${service} is active; no files were changed"
    fi
    printf '%s\n' "connector health preflight returned an invalid contract while ${service} is inactive; continuing with recovery transaction" >&2
  fi
fi
rm -f -- "${preflight_health}"

stamp="$(date +%Y%m%d-%H%M%S)-$$"
backup_root="${HERMES_BACKUP_ROOT:-/var/backups/hermes-agent}"
install -d -o root -g root -m 0700 "${backup_root}"

# The install lock is already held, so candidate/rollback directories from an
# earlier process are necessarily stale. They are complete virtual-environment
# copies and can otherwise exhaust the host before SQLite records the release.
reclaim_stale_runtime_artifacts() {
  local artifact
  while IFS= read -r -d '' artifact; do
    case "${artifact}" in
      "${target_root}"/.venv.candidate.*|\
      "${target_root}"/.venv.failed.*|\
      "${target_root}"/.venv.rollback-*|\
      "${target_root}"/.collaboration-install.*) ;;
      *) die "refusing to reclaim unexpected runtime artifact: ${artifact}" ;;
    esac
    [[ -d "${artifact}" && ! -L "${artifact}" ]] \
      || die "stale runtime artifact is unsafe: ${artifact}"
    rm -rf -- "${artifact}"
  done < <(
    find "${target_root}" -mindepth 1 -maxdepth 1 -type d \
      \( -name '.venv.candidate.*' -o -name '.venv.failed.*' \
         -o -name '.venv.rollback-*' -o -name '.collaboration-install.*' \) \
      -print0
  )
}

# A full runtime filesystem prevents SQLite from opening WAL databases even
# when the release stage itself fits on tmpfs. Reclaim only bounded, known
# deployment artifacts before taking the rollback snapshot. This is deliberately
# conservative: business databases and runtime objects are never deleted.
reclaim_runtime_disk_pressure() {
  local mount_path="$1"
  local backups_root="$2"
  local minimum_kib="${HERMES_DEPLOY_MIN_FREE_KIB:-65536}"
  local headroom_kib="${HERMES_DEPLOY_VENV_HEADROOM_KIB:-131072}"
  local retention="${HERMES_BACKUP_RETENTION:-3}"
  [[ "${minimum_kib}" =~ ^[0-9]+$ ]] || die "HERMES_DEPLOY_MIN_FREE_KIB must be an integer"
  [[ "${headroom_kib}" =~ ^[0-9]+$ ]] || die "HERMES_DEPLOY_VENV_HEADROOM_KIB must be an integer"
  [[ "${retention}" =~ ^[1-9][0-9]*$ ]] || die "HERMES_BACKUP_RETENTION must be a positive integer"

  local runtime_venv_kib required_kib
  if [[ -d "${target_root}/.venv" && ! -L "${target_root}/.venv" ]]; then
    runtime_venv_kib="$(du -sk -- "${target_root}/.venv" | awk '{print $1}')"
    [[ "${runtime_venv_kib}" =~ ^[0-9]+$ ]] \
      || die "could not determine the runtime virtual-environment size"
    required_kib=$(( runtime_venv_kib + headroom_kib ))
    if (( required_kib > minimum_kib )); then
      minimum_kib="${required_kib}"
    fi
  fi

  local -a old_backups=()
  mapfile -d '' -t old_backups < <(
    find "${backups_root}" -mindepth 1 -maxdepth 1 -type d \
      -name 'collaboration-*' -printf '%T@\t%p\0' \
      | sort -z -n \
      | cut -z -f2-
  )
  local keep_before_current=$(( retention - 1 ))
  local removable_count=$(( ${#old_backups[@]} - keep_before_current ))
  if (( removable_count > 0 )); then
    local index
    for (( index = 0; index < removable_count; index++ )); do
      rm -rf -- "${old_backups[index]}"
    done
  fi

  local available_kib
  available_kib="$(df -Pk -- "${mount_path}" | awk 'NR == 2 {print $4}')"
  [[ "${available_kib}" =~ ^[0-9]+$ ]] || die "could not determine free space for ${mount_path}"
  if (( available_kib >= minimum_kib )); then
    return 0
  fi

  printf '%s\n' "runtime filesystem has ${available_kib} KiB free; reclaiming stale deployment artifacts" >&2
  if command -v journalctl >/dev/null 2>&1; then
    journalctl --vacuum-size="${HERMES_DEPLOY_JOURNAL_VACUUM_SIZE:-256M}" >/dev/null 2>&1 || true
  fi
  available_kib="$(df -Pk -- "${mount_path}" | awk 'NR == 2 {print $4}')"
  [[ "${available_kib}" =~ ^[0-9]+$ ]] || die "could not recheck free space for ${mount_path}"
  printf '%s\n' "runtime filesystem has ${available_kib} KiB free after bounded reclaim" >&2
  (( available_kib >= minimum_kib )) \
    || die "runtime filesystem requires ${minimum_kib} KiB free for the transactional dependency candidate"
}

reclaim_stale_runtime_artifacts
reclaim_runtime_disk_pressure "${runtime_home}" "${backup_root}"
backup="$(mktemp -d "${backup_root}/collaboration-${version}-${stamp}.XXXXXX")"
chown root:root "${backup}"
chmod 0700 "${backup}"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${plugin_target}"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${plugin_target}/dist"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${target_root}/agent"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${target_root}/hermes_cli/dashboard_auth"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${target_root}/hermes_services"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${target_root}/tui_gateway"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${target_root}/tools"
install -d -o "${service_user}" -g "${service_group}" -m 0700 "${runtime_home}"
mkdir -p \
  "${backup}/plugins/collaboration/dashboard/dist" \
  "${backup}/agent" \
  "${backup}/hermes_cli/dashboard_auth" \
  "${backup}/hermes_services" \
  "${backup}/tools" \
  "${backup}/tui_gateway" \
  "${backup}/nginx" \
  "${backup}/release" \
  "${backup}/state"
for relative in "${runtime_service_assets[@]}"; do
  destination_parent="$(dirname "${target_root}/${relative}")"
  backup_parent="$(dirname "${backup}/${relative}")"
  [[ ! -L "${destination_parent}" ]] || die "unsafe runtime destination ${destination_parent}"
  install -d -o "${service_user}" -g "${service_group}" -m 0755 \
    "${destination_parent}"
  mkdir -p "${backup_parent}"
done

backup_one() {
  local source="$1" destination="$2"
  local temporary="${destination}.new.$$"
  rm -f -- "${temporary}"
  if [[ -e "${source}" || -L "${source}" ]]; then
    [[ ! -L "${source}" ]] || die "refusing to back up symlink ${source}"
    cp -a -- "${source}" "${temporary}"
    mv -f -- "${temporary}" "${destination}"
  else
    : >"${destination}.missing"
  fi
}
backup_sqlite() {
  local source="$1" destination="$2"
  local temporary="${destination}.new.$$"
  rm -f -- "${temporary}" "${destination}.missing"
  if [[ -e "${source}" || -L "${source}" ]]; then
    [[ -f "${source}" && ! -L "${source}" ]] || die "refusing to back up unsafe SQLite database ${source}"
    "${runtime_python}" - "${source}" "${temporary}" <<'PY'
import hashlib
import json
import os
import pathlib
import sqlite3
import sys
from urllib.parse import quote

source = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
destination.parent.mkdir(parents=True, exist_ok=True)
source_uri = f"file:{quote(source.as_posix(), safe='/')}?mode=ro"
with sqlite3.connect(source_uri, uri=True, timeout=30) as source_db:
    with sqlite3.connect(destination, timeout=30) as destination_db:
        source_db.backup(destination_db)
os.chmod(destination, 0o600)
with sqlite3.connect(destination, timeout=30) as snapshot_db:
    schema = snapshot_db.execute(
        "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    metadata = {
        "schema": "hermes.sqlite-snapshot.v1",
        "source": str(source),
        "user_version": int(snapshot_db.execute("PRAGMA user_version").fetchone()[0]),
        "application_id": int(snapshot_db.execute("PRAGMA application_id").fetchone()[0]),
        "integrity_check": str(snapshot_db.execute("PRAGMA integrity_check").fetchone()[0]),
        "schema_sha256": hashlib.sha256(
            json.dumps(schema, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "snapshot_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
    }
metadata_path = pathlib.Path(str(destination) + ".metadata.json")
metadata_path.write_text(
    json.dumps(metadata, sort_keys=True, ensure_ascii=True) + "\n",
    encoding="utf-8",
)
os.chmod(metadata_path, 0o600)
PY
    mv -f -- "${temporary}" "${destination}"
    mv -f -- "${temporary}.metadata.json" "${destination}.metadata.json"
  else
    : >"${destination}.missing"
  fi
}
backup_runtime_sqlite_tree() {
  local source_root="$1" destination_root="$2"
  "${runtime_python}" - "${source_root}" "${destination_root}" <<'PY'
import hashlib
import json
import os
import pathlib
import sqlite3
import stat
import sys
from urllib.parse import quote

root = pathlib.Path(sys.argv[1]).resolve(strict=True)
destination = pathlib.Path(sys.argv[2])
database_root = destination / "databases"
database_root.mkdir(parents=True, exist_ok=True)

def metadata(database):
    rows = database.execute(
        "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    return {
        "user_version": int(database.execute("PRAGMA user_version").fetchone()[0]),
        "application_id": int(database.execute("PRAGMA application_id").fetchone()[0]),
        "integrity_check": str(database.execute("PRAGMA integrity_check").fetchone()[0]),
        "schema_sha256": hashlib.sha256(
            json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }

records = []
for directory, child_directories, files in os.walk(root, followlinks=False):
    base = pathlib.Path(directory)
    child_directories[:] = [
        name for name in child_directories if not (base / name).is_symlink()
    ]
    for name in files:
        source = base / name
        if source.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            continue
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"unsafe SQLite path in runtime home: {source}")
        relative = source.relative_to(root)
        snapshot = database_root / relative
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        source_uri = f"file:{quote(source.as_posix(), safe='/')}?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=30) as source_db:
            with sqlite3.connect(snapshot, timeout=30) as snapshot_db:
                source_db.backup(snapshot_db)
        with sqlite3.connect(snapshot, timeout=30) as snapshot_db:
            record = metadata(snapshot_db)
        if record["integrity_check"] != "ok":
            raise RuntimeError(
                f"SQLite integrity check failed for {relative}: {record['integrity_check']}"
            )
        source_stat = source.stat()
        os.chmod(snapshot, 0o600)
        records.append({
            **record,
            "relative_path": relative.as_posix(),
            "snapshot_path": (pathlib.Path("databases") / relative).as_posix(),
            "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            "mode": stat.S_IMODE(source_stat.st_mode),
            "uid": source_stat.st_uid,
            "gid": source_stat.st_gid,
        })

records.sort(key=lambda item: item["relative_path"])
manifest = {
    "schema": "hermes.sqlite-tree-snapshot.v1",
    "root": str(root),
    "database_count": len(records),
    "databases": records,
}
manifest_path = destination / "manifest.json"
manifest_path.write_text(
    json.dumps(manifest, sort_keys=True, ensure_ascii=True, indent=2) + "\n",
    encoding="utf-8",
)
os.chmod(manifest_path, 0o600)
PY
}
restore_runtime_sqlite_tree() {
  local snapshot_root="$1" destination_root="$2"
  "${runtime_python}" - "${snapshot_root}" "${destination_root}" <<'PY'
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import sys

snapshot_root = pathlib.Path(sys.argv[1]).resolve(strict=True)
root = pathlib.Path(sys.argv[2]).resolve(strict=True)
manifest = json.loads((snapshot_root / "manifest.json").read_text(encoding="utf-8"))
if manifest.get("schema") != "hermes.sqlite-tree-snapshot.v1":
    raise RuntimeError("runtime SQLite snapshot manifest is invalid")
records = manifest.get("databases")
if not isinstance(records, list) or manifest.get("database_count") != len(records):
    raise RuntimeError("runtime SQLite snapshot manifest count is invalid")

def bounded(relative):
    candidate = root / pathlib.PurePosixPath(relative)
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise RuntimeError(f"SQLite restore path escapes runtime home: {relative}")
    if candidate.is_symlink():
        raise RuntimeError(f"SQLite restore target is a symlink: {relative}")
    return candidate

expected = {str(item["relative_path"]) for item in records}
for directory, child_directories, files in os.walk(root, followlinks=False):
    base = pathlib.Path(directory)
    child_directories[:] = [
        name for name in child_directories if not (base / name).is_symlink()
    ]
    for name in files:
        current = base / name
        if current.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            continue
        relative = current.relative_to(root).as_posix()
        if relative not in expected:
            if current.is_symlink():
                raise RuntimeError(f"new SQLite target is a symlink: {relative}")
            for suffix in ("", "-wal", "-shm", "-journal"):
                pathlib.Path(str(current) + suffix).unlink(missing_ok=True)

for record in records:
    relative = str(record["relative_path"])
    destination = bounded(relative)
    snapshot = snapshot_root / pathlib.PurePosixPath(str(record["snapshot_path"]))
    resolved_snapshot = snapshot.resolve(strict=True)
    if snapshot_root not in resolved_snapshot.parents:
        raise RuntimeError(f"SQLite snapshot path escapes backup: {relative}")
    if hashlib.sha256(resolved_snapshot.read_bytes()).hexdigest() != record["snapshot_sha256"]:
        raise RuntimeError(f"SQLite snapshot hash mismatch: {relative}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        pathlib.Path(str(destination) + suffix).unlink(missing_ok=True)
    temporary = pathlib.Path(str(destination) + f".rollback.{os.getpid()}")
    shutil.copyfile(resolved_snapshot, temporary)
    os.chmod(temporary, int(record["mode"]))
    os.chown(temporary, int(record["uid"]), int(record["gid"]))
    os.replace(temporary, destination)
    with sqlite3.connect(destination, timeout=30) as database:
        schema = database.execute(
            "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        restored = {
            "user_version": int(database.execute("PRAGMA user_version").fetchone()[0]),
            "application_id": int(database.execute("PRAGMA application_id").fetchone()[0]),
            "integrity_check": str(database.execute("PRAGMA integrity_check").fetchone()[0]),
            "schema_sha256": hashlib.sha256(
                json.dumps(schema, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }
    if any(restored[key] != record[key] for key in restored):
        raise RuntimeError(f"SQLite restore verification failed: {relative}")
PY
}
backup_one "${plugin_target}/plugin_api.py" "${backup}/plugins/collaboration/dashboard/plugin_api.py"
backup_one "${plugin_target}/manifest.json" "${backup}/plugins/collaboration/dashboard/manifest.json"
backup_one "${plugin_target}/dist/index.js" "${backup}/plugins/collaboration/dashboard/dist/index.js"
backup_one "${core_target}" "${backup}/hermes_cli/cloud_file_library.py"
backup_one "${public_paths_target}" "${backup}/hermes_cli/dashboard_auth/public_paths.py"
backup_one "${token_auth_target}" "${backup}/hermes_cli/dashboard_auth/token_auth.py"
backup_one "${mobile_device_store_target}" "${backup}/hermes_cli/dashboard_auth/mobile_device_store.py"
backup_one "${mobile_notifications_target}" "${backup}/hermes_cli/dashboard_auth/mobile_notifications.py"
backup_one "${managed_installations_target}" "${backup}/hermes_cli/managed_installations.py"
backup_one "${managed_nodes_code_target}" "${backup}/hermes_cli/managed_nodes.py"
backup_one "${web_server_target}" "${backup}/hermes_cli/web_server.py"
backup_one "${managed_installation_tool_target}" "${backup}/tools/managed_installation_tool.py"
backup_one "${toolsets_target}" "${backup}/toolsets.py"
backup_one "${agent_init_target}" "${backup}/agent/agent_init.py"
backup_one "${prompt_builder_target}" "${backup}/agent/prompt_builder.py"
backup_one "${system_prompt_target}" "${backup}/agent/system_prompt.py"
backup_one "${context_diagnostics_target}" "${backup}/agent/context_diagnostics.py"
backup_one "${doctor_target}" "${backup}/hermes_cli/doctor.py"
backup_one "${tui_gateway_target}" "${backup}/tui_gateway/server.py"
backup_one "${nginx_security_target}" "${backup}/nginx/00-hermes-security.conf"
backup_one "${nginx_site_target}" "${backup}/nginx/daxueshenmai.top.conf"
backup_one "${release_evidence_target}" "${backup}/release/release-evidence.json"
for relative in "${runtime_service_assets[@]}"; do
  backup_one "${target_root}/${relative}" "${backup}/${relative}"
done
if [[ "${ios_enabled}" == 1 ]]; then
  install -d -o "${service_user}" -g "${service_group}" -m 0755 \
    "${target_root}/plugins/ios-intelligence/dashboard"
  install -d -o "${service_user}" -g "${service_group}" -m 0755 \
    "${target_root}/plugins/dashboard_auth/basic"
  install -d -o "${service_user}" -g "${service_group}" -m 0755 \
    "${target_root}/hermes_cli"
  install -d -o "${service_user}" -g "${service_group}" -m 0755 \
    "${target_root}/tools"
  mkdir -p \
    "${backup}/plugins/ios-intelligence/dashboard" \
    "${backup}/plugins/dashboard_auth/basic" \
    "${backup}/hermes_cli" \
    "${backup}/tools"
  mkdir -p "${backup}/config"
  backup_one "${config_target}" "${backup}/config/config.yaml"
  for relative in "${ios_optional[@]}"; do
    destination="${target_root}/${relative}"
    backup_one "${destination}" "${backup}/${relative}"
  done
fi

transaction="$(mktemp -d "${target_root}/.collaboration-install.XXXXXX")"
installed=0
runtime_venv="${target_root}/.venv"
candidate_venv=""
previous_venv=""
venv_old_moved=0
venv_swapped=0
dependency_update_enabled=0
if [[ "${runtime_python}" == "${runtime_venv}/bin/python" ]]; then
  [[ -d "${runtime_venv}" && ! -L "${runtime_venv}" ]] \
    || die "runtime virtual environment is missing or unsafe"
  dependency_update_enabled=1
fi
# Flipped to 1 immediately before the first in-place install below.  Until
# then a failure (the service stop or a state snapshot) has modified nothing,
# so rollback must not run the restore_* helpers: the state snapshots may not
# have been taken yet (no backup file and no `.missing` marker), which would
# be misreported as a failed restore and leave ${service} stopped.
mutated=0
nginx_reload_attempted=0
fabric_release_published=0
rollback() {
  local exit_code=$?
  local rollback_failed=0
  local service_stopped=0
  trap - EXIT INT TERM HUP
  set +e
  rm -f -- \
    "$(dirname "${nginx_security_target}")/.$(basename "${nginx_security_target}").install.$$" \
    "$(dirname "${nginx_site_target}")/.$(basename "${nginx_site_target}").install.$$" \
    "${release_evidence_target}.new.$$"
  if [[ "${installed}" != 1 && "${venv_old_moved}" == 1 ]]; then
    failed_venv="${target_root}/.venv.failed.$$"
    rm -rf -- "${failed_venv}"
    if [[ -e "${runtime_venv}" || -L "${runtime_venv}" ]]; then
      mv -f -- "${runtime_venv}" "${failed_venv}" || rollback_failed=1
    fi
    if [[ -d "${previous_venv}" && ! -L "${previous_venv}" ]]; then
      mv -f -- "${previous_venv}" "${runtime_venv}" || rollback_failed=1
    else
      rollback_failed=1
    fi
    rm -rf -- "${failed_venv}"
  fi
  if [[ "${installed}" != 1 ]]; then
    if [[ "${mutated}" != 1 ]]; then
      # Failed between `trap rollback EXIT` and the first in-place install
      # (the `systemctl stop` itself, or one of the state snapshots).  No
      # target file has been touched, so there is nothing to restore;
      # leaving service_stopped=0 skips the restore block below.  Just make
      # sure the service is running again — `systemctl start` is a no-op if
      # the stop never went through.
      if ! systemctl start "${service}" >/dev/null 2>&1; then
        printf '%s\n' "rollback failed: no files were changed but ${service} could not be started" >&2
        rollback_failed=1
      fi
    elif systemctl stop "${service}" >/dev/null 2>&1; then
      service_stopped=1
    else
      printf '%s\n' "rollback failed: could not stop ${service}" >&2
      rollback_failed=1
    fi
    if [[ "${service_stopped}" == 1 ]]; then
      rollback_step() {
        local label="$1"
        shift
        if ! "$@"; then
          printf 'rollback failed while restoring %s\n' "${label}" >&2
          rollback_failed=1
        fi
      }
      rollback_step plugin-api restore_one "${backup}/plugins/collaboration/dashboard/plugin_api.py" "${plugin_target}/plugin_api.py"
      rollback_step plugin-manifest restore_one "${backup}/plugins/collaboration/dashboard/manifest.json" "${plugin_target}/manifest.json"
      rollback_step plugin-bundle restore_one "${backup}/plugins/collaboration/dashboard/dist/index.js" "${plugin_target}/dist/index.js"
      rollback_step cloud-files-code restore_one "${backup}/hermes_cli/cloud_file_library.py" "${core_target}"
      rollback_step public-paths restore_one "${backup}/hermes_cli/dashboard_auth/public_paths.py" "${public_paths_target}"
      rollback_step token-auth restore_one "${backup}/hermes_cli/dashboard_auth/token_auth.py" "${token_auth_target}"
      rollback_step mobile-device-store restore_one "${backup}/hermes_cli/dashboard_auth/mobile_device_store.py" "${mobile_device_store_target}"
      rollback_step mobile-notifications restore_one "${backup}/hermes_cli/dashboard_auth/mobile_notifications.py" "${mobile_notifications_target}"
      rollback_step managed-installations-code restore_one "${backup}/hermes_cli/managed_installations.py" "${managed_installations_target}"
      rollback_step managed-nodes-code restore_one "${backup}/hermes_cli/managed_nodes.py" "${managed_nodes_code_target}"
      rollback_step web-server restore_one "${backup}/hermes_cli/web_server.py" "${web_server_target}"
      rollback_step managed-installation-tool restore_one "${backup}/tools/managed_installation_tool.py" "${managed_installation_tool_target}"
      rollback_step toolsets restore_one "${backup}/toolsets.py" "${toolsets_target}"
      rollback_step agent-init restore_one "${backup}/agent/agent_init.py" "${agent_init_target}"
      rollback_step prompt-builder restore_one "${backup}/agent/prompt_builder.py" "${prompt_builder_target}"
      rollback_step system-prompt restore_one "${backup}/agent/system_prompt.py" "${system_prompt_target}"
      rollback_step context-diagnostics restore_one "${backup}/agent/context_diagnostics.py" "${context_diagnostics_target}"
      rollback_step doctor restore_one "${backup}/hermes_cli/doctor.py" "${doctor_target}"
      rollback_step tui-gateway restore_one "${backup}/tui_gateway/server.py" "${tui_gateway_target}"
      rollback_step nginx-security restore_root_file "${backup}/nginx/00-hermes-security.conf" "${nginx_security_target}"
      rollback_step nginx-site restore_root_file "${backup}/nginx/daxueshenmai.top.conf" "${nginx_site_target}"
      if [[ "${fabric_release_published}" == 0 ]]; then
        rollback_step release-evidence restore_root_file "${backup}/release/release-evidence.json" "${release_evidence_target}"
      else
        # Candidate health and the traffic switch already passed. Keep this
        # immutable desired-release identity while restoring public traffic,
        # so pull-based fabric nodes can converge after a transient outage.
        printf 'fabric recovery remains pending for release %s\n' \
          "${release_commit}" >&2
      fi
      for relative in "${runtime_service_assets[@]}"; do
        rollback_step "${relative}" restore_one "${backup}/${relative}" "${target_root}/${relative}"
      done
      rollback_step cloud-files-db restore_sqlite "${backup}/state/cloud-files-library.sqlite3" "${cloud_files_database_target}"
      rollback_step mobile-auth-db restore_sqlite "${backup}/state/mobile-auth.db" "${mobile_auth_target}"
      rollback_step managed-installations-db restore_sqlite "${backup}/state/managed-installations.db" "${managed_installations_database_target}"
      rollback_step managed-nodes-config restore_state "${backup}/state/managed-nodes.json" "${managed_nodes_target}"
      if [[ "${ios_enabled}" == 1 ]]; then
        for relative in "${ios_optional[@]}"; do
          rollback_step "${relative}" restore_one "${backup}/${relative}" "${target_root}/${relative}"
        done
        rollback_step profile-config restore_one "${backup}/config/config.yaml" "${config_target}"
        rollback_step ios-intelligence-db restore_sqlite "${backup}/state/ios-intelligence.db" "${ios_database_target}"
        rollback_step ios-supervisor-db restore_sqlite "${backup}/state/ios-mcp-supervisor.db" "${ios_supervisor_target}"
      fi
      rollback_step conversation-state restore_state "${backup}/state/single.json" "${state_target}"
      rollback_step runtime-sqlite-tree restore_runtime_sqlite_tree \
        "${backup}/state/sqlite-tree" "${runtime_home}"
      if [[ "${nginx_reload_attempted}" == 1 && "${rollback_failed}" == 0 ]]; then
        if ! "${nginx_binary}" -t >/dev/null 2>&1 \
          || ! systemctl reload "${nginx_service}" >/dev/null 2>&1; then
          printf '%s\n' "rollback failed while restoring nginx runtime" >&2
          rollback_failed=1
        fi
      fi
      if [[ "${rollback_failed}" == 0 ]]; then
        if ! systemctl start "${service}" >/dev/null 2>&1; then
          printf '%s\n' "rollback restored files but failed to restart ${service}" >&2
          rollback_failed=1
        fi
      fi
    fi
  fi
  rm -rf -- "${transaction}"
  [[ -z "${candidate_venv}" ]] || rm -rf -- "${candidate_venv}"
  [[ -z "${health_file:-}" ]] || rm -f -- "${health_file}"
  [[ -z "${handshake_file:-}" ]] || rm -f -- "${handshake_file}"
  [[ -z "${ios_health_file:-}" ]] || rm -f -- "${ios_health_file}"
  [[ -z "${connector_health_file:-}" ]] || rm -f -- "${connector_health_file}"
  [[ -z "${deployment_health_file:-}" ]] || rm -f -- "${deployment_health_file}"
  [[ -z "${installation_health_cfg:-}" ]] || rm -f -- "${installation_health_cfg}"
  [[ -z "${node_health:-}" ]] || rm -f -- "${node_health}"
  [[ -z "${installation_probe_body:-}" ]] || rm -f -- "${installation_probe_body}"
  [[ -z "${installation_probe_post:-}" ]] || rm -f -- "${installation_probe_post}"
  [[ -z "${installation_probe_get:-}" ]] || rm -f -- "${installation_probe_get}"
  [[ -z "${release_evidence_temp:-}" ]] || rm -f -- "${release_evidence_temp}"
  rm -f -- "${curl_cfg}"
  cleanup_snapshot
  if [[ "${rollback_failed}" != 0 ]]; then
    if [[ "${mutated}" != 1 ]]; then
      # Nothing was modified, so no restore was attempted — but the service
      # could not be (re)started and needs operator attention.
      printf '%s\n' "no rollback was required but ${service} is not running" >&2
    else
      printf '%s\n' "rollback incomplete; ${service} remains stopped" >&2
    fi
    exit_code=70
  elif [[ "${installed}" != 1 && "${fabric_release_published}" == 1 ]]; then
    # The public service is healthy on its previous version, and the desired
    # release pointer remains available to the pull-based fabric. Tell the
    # outer deployer to retry the transaction after nodes have had time to
    # consume it.
    exit_code=75
  fi
  exit "${exit_code}"
}
restore_one() {
  local source="$1"
  local destination="$2"
  local temporary="${destination}.rollback.$$"
  if [[ -f "${source}" ]]; then
    install -o "${service_user}" -g "${service_group}" -m 0644 "${source}" "${temporary}" \
      || { rm -f -- "${temporary}"; return 1; }
    mv -f -- "${temporary}" "${destination}" \
      || { rm -f -- "${temporary}"; return 1; }
  elif [[ -f "${source}.missing" ]]; then
    rm -f -- "${destination}" || return 1
  else
    return 1
  fi
}
restore_root_file() {
  local source="$1"
  local destination="$2"
  local temporary="${destination}.rollback.$$"
  if [[ -f "${source}" ]]; then
    install -o root -g root -m 0644 "${source}" "${temporary}" \
      || { rm -f -- "${temporary}"; return 1; }
    mv -f -- "${temporary}" "${destination}" \
      || { rm -f -- "${temporary}"; return 1; }
  elif [[ -f "${source}.missing" ]]; then
    rm -f -- "${destination}" || return 1
  else
    return 1
  fi
}
restore_state() {
  local source="$1"
  local destination="$2"
  local temporary="${destination}.rollback.$$"
  install -d -o "${service_user}" -g "${service_group}" -m 0700 "$(dirname "${destination}")" \
    || return 1
  if [[ -f "${source}" ]]; then
    install -o "${service_user}" -g "${service_group}" -m 0600 "${source}" "${temporary}" \
      || { rm -f -- "${temporary}"; return 1; }
    mv -f -- "${temporary}" "${destination}" \
      || { rm -f -- "${temporary}"; return 1; }
    if [[ -f "${source}.metadata.json" ]]; then
      "${runtime_python}" - "${destination}" "${source}.metadata.json" <<'PY' \
        || return 1
import hashlib
import json
import sqlite3
import sys

destination, metadata_path = sys.argv[1:]
metadata = json.load(open(metadata_path, encoding="utf-8"))
with sqlite3.connect(destination, timeout=30) as database:
    schema = database.execute(
        "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    restored = {
        "user_version": int(database.execute("PRAGMA user_version").fetchone()[0]),
        "application_id": int(database.execute("PRAGMA application_id").fetchone()[0]),
        "integrity_check": str(database.execute("PRAGMA integrity_check").fetchone()[0]),
        "schema_sha256": hashlib.sha256(
            json.dumps(schema, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
assert metadata.get("schema") == "hermes.sqlite-snapshot.v1"
assert all(restored[key] == metadata[key] for key in restored)
PY
    fi
  elif [[ -f "${source}.missing" ]]; then
    rm -f -- "${destination}" || return 1
  else
    return 1
  fi
}
restore_sqlite() {
  local source="$1"
  local destination="$2"
  local temporary="${destination}.rollback.$$"
  local destination_dir
  destination_dir="$(dirname "${destination}")"
  if [[ ! -d "${destination_dir}" ]]; then
    install -d -o "${service_user}" -g "${service_group}" -m 0700 "${destination_dir}" \
      || return 1
  fi
  rm -f -- "${temporary}" "${destination}-wal" "${destination}-shm" "${destination}-journal" \
    || return 1
  if [[ -f "${source}" ]]; then
    install -o "${service_user}" -g "${service_group}" -m 0600 "${source}" "${temporary}" \
      || { rm -f -- "${temporary}"; return 1; }
    mv -f -- "${temporary}" "${destination}" \
      || { rm -f -- "${temporary}"; return 1; }
  elif [[ -f "${source}.missing" ]]; then
    rm -f -- "${destination}" || return 1
  else
    return 1
  fi
}
release_phase prepare
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# Build and verify the dependency candidate while the current service and
# interpreter remain untouched. An explicit external HERMES_RUNTIME_PYTHON is
# used by the deployment harness and deliberately skips this production-only
# virtual-environment swap.
if [[ "${dependency_update_enabled}" == 1 ]]; then
  candidate_venv="${target_root}/.venv.candidate.$$"
  previous_venv="${target_root}/.venv.rollback-${version}-${release_commit:0:12}-$$"
  [[ ! -e "${candidate_venv}" && ! -L "${candidate_venv}" ]] \
    || die "runtime dependency candidate already exists"
  [[ ! -e "${previous_venv}" && ! -L "${previous_venv}" ]] \
    || die "runtime dependency rollback path already exists"
  cp -a -- "${runtime_venv}" "${candidate_venv}"
  # The installer keeps umask 077 for release evidence, tokens, and rollback
  # state. Python packages are executable code shared with the unprivileged
  # service account, so install them with searchable/readable permissions.
  # Keeping this override in a subshell restores the restrictive umask for all
  # subsequent deployment artifacts.
  (
    umask 022
    "${candidate_venv}/bin/python" -m pip install \
      --disable-pip-version-check --require-hashes \
      -r "${snapshot}/deploy/public/runtime-requirements.lock"
  )
  "${candidate_venv}/bin/python" - <<'PY'
import mcp  # noqa: F401
from mcp.server.fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool

assert FastMCP and run_in_threadpool
PY
  # Root can import packages installed with mode 0700, while the systemd
  # service cannot. Validate the dashboard's real import surface as the
  # service account before stopping or replacing the live environment.
  sudo -u "${service_user}" -- "${candidate_venv}/bin/python" - <<'PY'
import requests  # noqa: F401
import uvicorn  # noqa: F401
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware  # noqa: F401
from fastapi.responses import (  # noqa: F401
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles  # noqa: F401
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, SecretStr  # noqa: F401
from starlette.concurrency import run_in_threadpool  # noqa: F401

assert FastAPI and FastMCP
PY
fi

# Dependency-backed release checks must use the fully resolved candidate.  A
# live installation may contain an older lock, so checking runtime_python
# before building the candidate would reject the exact transactional upgrade
# that the virtual-environment swap is designed to perform.
dependency_validation_python="${runtime_python}"
if [[ "${dependency_update_enabled}" == 1 ]]; then
  dependency_validation_python="${candidate_venv}/bin/python"
fi
if [[ "${ios_enabled}" == 1 ]]; then
  "${dependency_validation_python}" -c 'from mcp.server.fastmcp import FastMCP; assert FastMCP' \
    || die "Hermes runtime candidate is missing the locked FastMCP SDK required by iOS MCP services"
  "${dependency_validation_python}" -c 'from cryptography.hazmat.primitives.ciphers.aead import AESGCM; assert AESGCM' \
    || die "Hermes runtime candidate is missing AES-GCM support required by encrypted iOS hot and cold storage"
  "${dependency_validation_python}" -c 'from agent.plugin_llm import PluginLlm; assert PluginLlm' \
    || die "Hermes runtime candidate is missing the host LLM facade required by iOS semantic analysis"
fi

# Quiesce the state writer before taking the transactional state snapshot.
# Keep the service stopped until every runtime file has been atomically placed;
# rollback also stops it before restoring this snapshot.
systemctl stop "${service}"
backup_one "${state_target}" "${backup}/state/single.json"
backup_sqlite "${cloud_files_database_target}" "${backup}/state/cloud-files-library.sqlite3"
backup_sqlite "${mobile_auth_target}" "${backup}/state/mobile-auth.db"
backup_sqlite "${managed_installations_database_target}" "${backup}/state/managed-installations.db"
backup_one "${managed_nodes_target}" "${backup}/state/managed-nodes.json"
if [[ "${ios_enabled}" == 1 ]]; then
  backup_sqlite "${ios_database_target}" "${backup}/state/ios-intelligence.db"
  backup_sqlite "${ios_supervisor_target}" "${backup}/state/ios-mcp-supervisor.db"
fi
backup_runtime_sqlite_tree "${runtime_home}" "${backup}/state/sqlite-tree"

install_atomic() {
  local source="$1"
  local destination="$2"
  local mode="${3:-0644}"
  local temporary="${transaction}/.install.$(basename "${destination}").$$"
  install -o "${service_user}" -g "${service_group}" -m "${mode}" "${source}" "${temporary}"
  mv -f -- "${temporary}" "${destination}"
}
install_root_atomic() {
  local source="$1"
  local destination="$2"
  local temporary
  temporary="$(dirname "${destination}")/.$(basename "${destination}").install.$$"
  rm -f -- "${temporary}"
  install -o root -g root -m 0644 "${source}" "${temporary}"
  mv -f -- "${temporary}" "${destination}"
}
# Normalize SQLite ownership and sidecars before the first service-user open.
# A stale root-owned -wal/-shm file makes SQLite report a misleading
# "disk I/O error" while enabling WAL, which previously aborted deployment.
prepare_sqlite_runtime_target() {
  local target="$1"
  local parent
  parent="$(dirname "${target}")"
  install -d -o "${service_user}" -g "${service_group}" -m 0700 "${parent}"
  if [[ -e "${target}" || -L "${target}" ]]; then
    [[ -f "${target}" && ! -L "${target}" ]] \
      || die "SQLite runtime target is not a regular file: ${target}"
    chown "${service_user}:${service_group}" "${target}"
    chmod 0600 "${target}"
  fi
  local suffix sidecar
  for suffix in -wal -shm -journal; do
    sidecar="${target}${suffix}"
    if [[ -e "${sidecar}" || -L "${sidecar}" ]]; then
      [[ -f "${sidecar}" && ! -L "${sidecar}" ]] \
        || die "SQLite sidecar is not a regular file: ${sidecar}"
      chown "${service_user}:${service_group}" "${sidecar}"
      chmod 0600 "${sidecar}"
    fi
  done
}
# Point of no return: everything below replaces live files, so from here on
# rollback must restore the snapshots taken above instead of merely
# restarting the service.
mutated=1
for sqlite_target in \
  "${cloud_files_database_target}" \
  "${mobile_auth_target}" \
  "${managed_installations_database_target}" \
  "${ios_database_target}" \
  "${ios_supervisor_target}"; do
  prepare_sqlite_runtime_target "${sqlite_target}"
done
if [[ "${dependency_update_enabled}" == 1 ]]; then
  mv -f -- "${runtime_venv}" "${previous_venv}"
  venv_old_moved=1
  mv -f -- "${candidate_venv}" "${runtime_venv}"
  candidate_venv=""
  venv_swapped=1
fi
install_atomic "${snapshot}/plugins/collaboration/dashboard/plugin_api.py" "${plugin_target}/plugin_api.py"
install_atomic "${snapshot}/plugins/collaboration/dashboard/manifest.json" "${plugin_target}/manifest.json"
install_atomic "${snapshot}/plugins/collaboration/dashboard/dist/index.js" "${plugin_target}/dist/index.js"
install_atomic "${snapshot}/hermes_cli/cloud_file_library.py" "${core_target}"
install_atomic "${snapshot}/hermes_cli/dashboard_auth/public_paths.py" "${public_paths_target}"
install_atomic "${snapshot}/hermes_cli/dashboard_auth/token_auth.py" "${token_auth_target}"
install_atomic "${snapshot}/hermes_cli/dashboard_auth/mobile_device_store.py" "${mobile_device_store_target}"
install_atomic "${snapshot}/hermes_cli/dashboard_auth/mobile_notifications.py" "${mobile_notifications_target}"
install_atomic "${snapshot}/hermes_cli/managed_installations.py" "${managed_installations_target}"
install_atomic "${snapshot}/hermes_cli/managed_nodes.py" "${managed_nodes_code_target}"
install_atomic "${snapshot}/hermes_cli/web_server.py" "${web_server_target}"
install_atomic "${snapshot}/tools/managed_installation_tool.py" "${managed_installation_tool_target}"
install_atomic "${snapshot}/toolsets.py" "${toolsets_target}"
install_atomic "${snapshot}/agent/agent_init.py" "${agent_init_target}"
install_atomic "${snapshot}/agent/prompt_builder.py" "${prompt_builder_target}"
install_atomic "${snapshot}/agent/system_prompt.py" "${system_prompt_target}"
install_atomic "${snapshot}/agent/context_diagnostics.py" "${context_diagnostics_target}"
install_atomic "${snapshot}/hermes_cli/doctor.py" "${doctor_target}"
install_atomic "${snapshot}/tui_gateway/server.py" "${tui_gateway_target}"
for relative in "${runtime_service_assets[@]}"; do
  install_atomic "${snapshot}/${relative}" "${target_root}/${relative}"
done
managed_nodes_rendered="${transaction}/managed-nodes.json"
"${runtime_python}" - \
  "${snapshot}/deploy/public/managed-nodes.server.json" \
  "${managed_nodes_rendered}" \
  "${managed_node_token_file}" \
  "${managed_installation_token_file}" <<'PY'
import json
import pathlib
import sys

source, destination, status_token_file, installation_token_file = map(
    pathlib.Path, sys.argv[1:]
)
payload = json.loads(source.read_text(encoding="utf-8"))
for node in payload.get("nodes", []):
    if node.get("token_file") != "/etc/hermes-agent/dbb3-status-token":
        raise SystemExit("managed-nodes template has an unexpected status token path")
    if node.get("installation_token_file") != "/etc/hermes-agent/managed-installation-token":
        raise SystemExit("managed-nodes template has an unexpected installation token path")
    node["token_file"] = str(status_token_file)
    node["installation_token_file"] = str(installation_token_file)
destination.write_text(
    json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
    encoding="utf-8",
)
PY
install_atomic "${managed_nodes_rendered}" "${managed_nodes_target}" 0600
install_root_atomic "${snapshot}/deploy/public/nginx-00-hermes-security.conf" "${nginx_security_target}"
install_root_atomic "${snapshot}/deploy/public/nginx-daxueshenmai.top.conf" "${nginx_site_target}"
"${nginx_binary}" -t \
  || { printf '%s\n' "nginx configuration validation failed" >&2; false; }
if [[ "${ios_enabled}" == 1 ]]; then
  for relative in "${ios_optional[@]}"; do
    install_atomic "${snapshot}/${relative}" "${target_root}/${relative}"
  done
  # Persist discovery and supervisor state while the old process is quiesced;
  # the restarted service then boots with the complete MCP tool surface.
  sudo -u "${service_user}" -- env HERMES_HOME="${runtime_home}" \
    PYTHONPATH="${target_root}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${runtime_python}" -m hermes_cli.ios_mcp_server --install \
    --transport streamable-http --host 127.0.0.1 --base-port 8760 \
    || { printf '%s\n' "iOS MCP registration failed" >&2; false; }
  sudo -u "${service_user}" -- env HERMES_HOME="${runtime_home}" \
    PYTHONPATH="${target_root}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${runtime_python}" -m hermes_cli.ios_mcp_supervisor --register \
    --host 127.0.0.1 --base-port 8760 \
    || { printf '%s\n' "iOS MCP supervisor registration failed" >&2; false; }
fi
# Import the installed dashboard entry point with the same user, runtime home,
# source root, and interpreter that systemd will use. This must run after every
# optional iOS asset is installed so mixed dashboard-auth versions cannot reach
# the service restart. The external-runtime path is reserved for the deployment
# harness and does not provide the production dashboard dependencies.
if [[ "${dependency_update_enabled}" == 1 ]]; then
  sudo -u "${service_user}" -- env HERMES_HOME="${runtime_home}" \
    PYTHONPATH="${target_root}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${runtime_python}" -c 'from hermes_cli.web_server import app; assert app' \
    || { printf '%s\n' "installed dashboard import preflight failed" >&2; false; }
  sudo -u "${service_user}" -- env HERMES_HOME="${runtime_home}" \
    PYTHONPATH="${target_root}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${runtime_python}" -c 'from hermes_cli.runtime_provider import resolve_runtime_provider; assert resolve_runtime_provider' \
    || { printf '%s\n' "installed runtime-provider import preflight failed" >&2; false; }
fi
# journalctl accepts the local ``YYYY-MM-DD HH:MM:SS`` form consistently
# across systemd versions. ISO-8601's ``T`` and numeric offset are rejected
# by older journalctl builds, which would hide the only useful crash log.
service_start_since="$(date '+%Y-%m-%d %H:%M:%S')"
systemctl start "${service}"

health_file="$(mktemp /run/hermes-agent-status.XXXXXX)"
healthy=0
for _ in $(seq 1 30); do
  if systemctl is-active --quiet "${service}" \
    && curl --fail --silent --show-error --max-time 3 --noproxy '*' \
      http://127.0.0.2:9119/api/status >"${health_file}"; then
    healthy=1
    break
  fi
  sleep 1
done
[[ "${healthy}" == 1 ]] || {
  printf '%s\n' "${service} did not pass post-restart health check" >&2
  emit_service_failure_diagnostics >&2
  false
}
"${runtime_python}" - "${health_file}" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert isinstance(data, dict)
PY
handshake_file="$(mktemp /run/hermes-agent-mobile-handshake.XXXXXX)"
if ! curl --fail --silent --show-error --max-time 3 --noproxy '*' \
  http://127.0.0.2:9119/api/mobile/v1/handshake >"${handshake_file}"; then
  printf '%s\n' "anonymous mobile handshake did not respond" >&2
  false
fi
"${runtime_python}" - "${handshake_file}" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data.get("api_version") == 1
assert isinstance(data.get("hermes_version"), str) and data["hermes_version"]
assert isinstance(data.get("profiles"), list)
assert isinstance(data.get("capabilities"), list)
assert isinstance(data.get("server_time"), str) and data["server_time"]
PY
deployment_health_file="$(mktemp /run/hermes-agent-deployment-status.XXXXXX)"
deployment_healthy=0
for _ in $(seq 1 30); do
  if systemctl is-active --quiet "${service}" \
    && validate_deployment_health "${deployment_health_file}" 2>/dev/null; then
    deployment_healthy=1
    break
  fi
  sleep 1
done
[[ "${deployment_healthy}" == 1 ]] || {
  printf '%s\n' "deployment database, schema, catalog, version, or manifest gate failed" >&2
  validate_deployment_health "${deployment_health_file}" || true
  false
}
release_phase migrate
ios_health_file=""
if [[ "${ios_enabled}" == 1 ]]; then
  ios_health_file="$(mktemp /run/hermes-agent-ios-status.XXXXXX)"
  ios_health_attempts="${HERMES_IOS_HEALTH_ATTEMPTS:-180}"
  [[ "${ios_health_attempts}" =~ ^[1-9][0-9]*$ ]] \
    || die "HERMES_IOS_HEALTH_ATTEMPTS must be a positive integer"
  ios_healthy=0
  for _ in $(seq 1 "${ios_health_attempts}"); do
    if systemctl is-active --quiet "${service}" \
      && curl --fail --silent --show-error --max-time 3 --noproxy '*' \
        --config "${curl_cfg}" \
        http://127.0.0.2:9119/api/plugins/ios-intelligence/health >"${ios_health_file}" \
      && validate_ios_health "${ios_health_file}" 2>/dev/null; then
      ios_healthy=1
      break
    fi
    sleep 1
  done
  [[ "${ios_healthy}" == 1 ]] || {
    printf '%s\n' "iOS intelligence runtime did not reach all required healthy MCPs and tools" >&2
    validate_ios_health "${ios_health_file}" || true
    false
  }
fi
connector_health_file="$(mktemp /run/hermes-agent-connector-status.XXXXXX)"
validate_connector_health "${connector_health_file}" || {
  printf '%s\n' "connector contract did not pass after restart" >&2
  false
}
release_phase candidate-health
release_phase traffic-switch
nginx_reload_attempted=1
systemctl reload "${nginx_service}" \
  || { printf '%s\n' "nginx reload failed" >&2; false; }
release_phase drain
release_phase commit
# Fabric timers consume committed public evidence. Publish the immutable
# identity before waiting for node routes so stale nodes can begin updating.
release_evidence_temp="$(mktemp "${release_evidence_dir}/.release-evidence.XXXXXX")"
"${runtime_python}" - \
  "${release_evidence_temp}" "${version}" "${release_commit}" \
  "${manifest_sha256}" "${backup}" "${backup}/state/sqlite-tree/manifest.json" \
  "${health_file}" "${handshake_file}" "${deployment_health_file}" \
  "${connector_health_file}" "${ios_health_file}" <<'PY'
from datetime import datetime, timezone
import hashlib
import json
import pathlib
import sys

(
    output,
    version,
    commit,
    manifest_sha256,
    backup,
    sqlite_manifest_path,
    main_health_path,
    handshake_path,
    deployment_health_path,
    connector_health_path,
    ios_health_path,
) = sys.argv[1:]

def load(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8")) if path else None

sqlite_manifest_file = pathlib.Path(sqlite_manifest_path)
sqlite_manifest_bytes = sqlite_manifest_file.read_bytes()
sqlite_manifest = json.loads(sqlite_manifest_bytes.decode("utf-8"))
evidence = {
    "schema": "hermes.release-evidence.v1",
    "phase": "committed",
    "version": version,
    "commit": commit,
    "manifest_sha256": manifest_sha256,
    "deployed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "backup": backup,
    "database_snapshot": {
        "schema": sqlite_manifest.get("schema"),
        "database_count": sqlite_manifest.get("database_count"),
        "manifest_sha256": hashlib.sha256(sqlite_manifest_bytes).hexdigest(),
    },
    "fabric": {"status": "pending", "nodes": {}},
    "probes": {
        "main_api": load(main_health_path),
        "mobile_handshake": load(handshake_path),
        "deployment_health": load(deployment_health_path),
        "connector": load(connector_health_path),
        "ios_runtime": load(ios_health_path),
        "managed_installation_routes": {"dbb3": False, "wsl": False},
        "traffic_switch": {"nginx_reloaded": True},
        "drain": {"previous_service_quiesced": True},
    },
}
pathlib.Path(output).write_text(
    json.dumps(evidence, sort_keys=True, ensure_ascii=True, indent=2) + "\n",
    encoding="utf-8",
)
PY
install -o root -g root -m 0644 \
  "${release_evidence_temp}" "${release_evidence_target}.new.$$"
mv -f -- "${release_evidence_target}.new.$$" "${release_evidence_target}"
fabric_release_published=1
rm -f -- "${release_evidence_temp}"
release_evidence_temp=""
installation_health_cfg="$(mktemp /run/hermes-installation-route-health.XXXXXX)"
chmod 0600 "${installation_health_cfg}"
printf 'header = "X-DBB3-Token: %s"\nheader = "Accept: application/json"\n' \
  "$(cat -- "${managed_installation_token_file}")" >"${installation_health_cfg}"
# Fabric nodes update from a two-minute timer with up to twenty seconds of
# randomized delay. Allow enough time for that poll plus a cold transactional
# node update while still failing within the production workflow timeout.
fabric_health_attempts="${HERMES_FABRIC_HEALTH_ATTEMPTS:-360}"
[[ "${fabric_health_attempts}" =~ ^[1-9][0-9]*$ ]] \
  || die "HERMES_FABRIC_HEALTH_ATTEMPTS must be a positive integer"
for node in dbb3 wsl; do
  node_health="$(mktemp "/run/hermes-installation-${node}.XXXXXX")"
  node_http_status="$(mktemp "/run/hermes-installation-${node}-status.XXXXXX")"
  route_healthy=0
  for _ in $(seq 1 "${fabric_health_attempts}"); do
    : >"${node_health}"
    : >"${node_http_status}"
    if curl --silent --show-error --max-time 5 \
        --noproxy '*' \
        --resolve 'daxueshenmai.top:443:127.0.0.1' \
        --config "${installation_health_cfg}" \
        --write-out '%{http_code}' \
        -o "${node_health}" \
        "https://daxueshenmai.top/_hermes/installations/${node}/health" \
        >"${node_http_status}" \
      && [[ "$(cat -- "${node_http_status}")" == 200 ]] \
      && "${runtime_python}" - "${node_health}" "${node}" \
        "${release_commit}" "${version}" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data.get("ok") is True
assert data.get("node_id") == sys.argv[2]
assert data.get("installations") is True
assert data.get("recovery") is False
release = data.get("release") or {}
assert release.get("commit") == sys.argv[3]
assert release.get("version") == sys.argv[4]
PY
    then
      route_healthy=1
      break
    fi
    sleep 1
  done
  if [[ "${route_healthy}" != 1 ]]; then
    printf 'managed installation route failed: %s\n' "${node}" >&2
    if [[ -s "${node_health}" ]]; then
      # Keep rollout failures actionable without echoing arbitrary response
      # bodies (which could contain credentials or upstream HTML).
      "${runtime_python}" - "${node_health}" <<'PY' >&2 || true
import json
import sys

try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as error:
    print(f"managed installation last response is not JSON: {error}")
else:
    release = payload.get("release")
    print(json.dumps({
        "ok": payload.get("ok"),
        "node_id": payload.get("node_id"),
        "installations": payload.get("installations"),
        "recovery": payload.get("recovery"),
        "release": release if isinstance(release, dict) else None,
    }, sort_keys=True))
PY
    else
      printf '%s\n' 'managed installation last response was empty or unavailable' >&2
    fi
    if [[ -s "${node_http_status}" ]]; then
      printf 'managed installation last HTTP status: %s\n' \
        "$(cat -- "${node_http_status}")" >&2
    fi
  fi
  rm -f -- "${node_health}" "${node_http_status}"
  [[ "${route_healthy}" == 1 ]] || false
  installation_probe_id="mi-$(${runtime_python} -c 'import uuid; print(uuid.uuid4().hex)')"
  installation_probe_body="$(mktemp "/run/hermes-installation-${node}-body.XXXXXX")"
  installation_probe_post="$(mktemp "/run/hermes-installation-${node}-post.XXXXXX")"
  installation_probe_get="$(mktemp "/run/hermes-installation-${node}-get.XXXXXX")"
  printf '{"id":"%s","request_id":"%s","node_id":"%s","kind":"probe","identifier":"managed-installation-route-probe","probe":true}\n' \
    "${installation_probe_id}" "${installation_probe_id}" "${node}" \
    >"${installation_probe_body}"
  curl --fail --silent --show-error --max-time 8 \
    --noproxy '*' \
    --resolve 'daxueshenmai.top:443:127.0.0.1' \
    --config "${installation_health_cfg}" -H 'Content-Type: application/json' \
    --data-binary "@${installation_probe_body}" -o "${installation_probe_post}" \
    "https://daxueshenmai.top/_hermes/installations/${node}"
  curl --fail --silent --show-error --max-time 8 \
    --noproxy '*' \
    --resolve 'daxueshenmai.top:443:127.0.0.1' \
    --config "${installation_health_cfg}" -o "${installation_probe_get}" \
    "https://daxueshenmai.top/_hermes/installations/${node}/${installation_probe_id}"
  "${runtime_python}" - \
    "${installation_probe_post}" "${installation_probe_get}" \
    "${installation_probe_id}" "${node}" <<'PY'
import json, sys
post = json.load(open(sys.argv[1], encoding="utf-8"))
get = json.load(open(sys.argv[2], encoding="utf-8"))
assert post.get("accepted") is True and post.get("id") == sys.argv[3]
assert get.get("id") == sys.argv[3] and get.get("node_id") == sys.argv[4]
assert get.get("state") == "completed"
assert (get.get("detail") or {}).get("probe") is True
assert (get.get("detail") or {}).get("persisted") is True
PY
  rm -f -- \
    "${installation_probe_body}" "${installation_probe_post}" "${installation_probe_get}"
done
rm -f -- "${installation_health_cfg}"
release_evidence_temp="$(mktemp "${release_evidence_dir}/.release-evidence.XXXXXX")"
"${runtime_python}" - "${release_evidence_target}" \
  "${release_evidence_temp}" "${version}" "${release_commit}" <<'PY'
from datetime import datetime, timezone
import json
import pathlib
import sys

source, output, version, commit = sys.argv[1:]
evidence = json.loads(pathlib.Path(source).read_text(encoding="utf-8"))
if evidence.get("version") != version or evidence.get("commit") != commit:
    raise RuntimeError("release evidence identity changed during fabric verification")
evidence["fabric"] = {
    "status": "verified",
    "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "nodes": {
        "dbb3": {"commit": commit, "version": version},
        "wsl": {"commit": commit, "version": version},
    },
}
evidence["probes"]["managed_installation_routes"] = {"dbb3": True, "wsl": True}
pathlib.Path(output).write_text(
    json.dumps(evidence, sort_keys=True, ensure_ascii=True, indent=2) + "\n",
    encoding="utf-8",
)
PY
install -o root -g root -m 0644 \
  "${release_evidence_temp}" "${release_evidence_target}.new.$$"
mv -f -- "${release_evidence_target}.new.$$" "${release_evidence_target}"
rm -f -- "${release_evidence_temp}"
release_evidence_temp=""
installed=1
if [[ "${venv_swapped}" == 1 && -n "${previous_venv}" ]]; then
  rm -rf -- "${previous_venv}" || \
    printf 'warning: committed release could not remove old runtime environment: %s\n' \
      "${previous_venv}" >&2
  previous_venv=""
fi
rm -rf -- "${transaction}" "${health_file}" "${handshake_file}" \
  "${ios_health_file}" "${connector_health_file}" "${deployment_health_file}" \
  "${curl_cfg}"
printf 'service=active\nversion=%s\ncommit=%s\nmanifest_sha256=%s\nbackup=%s\nevidence=%s\n' \
  "${version}" "${release_commit}" "${manifest_sha256}" "${backup}" \
  "${release_evidence_target}"
