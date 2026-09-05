#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
# The installer performs privileged filesystem and service operations.  Do not
# inherit a caller-controlled search path (for example, from an SSH/sudo
# wrapper) or shell startup hooks that could make a user-owned executable run
# as root.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset PYTHONHOME PYTHONPATH CDPATH BASH_ENV ENV
installer_script="$(realpath -e -- "${BASH_SOURCE[0]}")" \
  || { printf '%s\n' 'installer script cannot be resolved' >&2; exit 1; }
cd /

# Root-side transactional installer. The caller uploads a stage owned by the
# unprivileged admin account, then invokes this script through sudo. No file is
# replaced until the staged Python/manifest validation and authenticated
# connector-health preflight have passed.

die() { printf 'install-collaboration-backend: %s\n' "$*" >&2; exit 1; }
validate_root_controlled_parent() {
  local path="$1" label="$2"
  [[ "${path}" == /* ]] || die "${label} path must be absolute"
  [[ "${path}" != "/" ]] || return 0
  case "/${path#/}/" in
    *'//'*|*'/../'*|*'/./'*) die "${label} path is not lexically normalized" ;;
  esac
  local current="/" component resolved_current ancestor_mode previous_mode="755"
  local -a components=()
  IFS='/' read -r -a components <<<"${path#/}"
  for component in "${components[@]}"; do
    [[ -n "${component}" ]] || continue
    current="${current%/}/${component}"
    if [[ ! -e "${current}" && ! -L "${current}" ]]; then
      # Never create a trusted path directly below a writable sticky parent:
      # another user could win the validation-to-mkdir race with a new entry.
      (( (8#${previous_mode} & 0022) == 0 )) \
        || die "${label} missing path is below a writable ancestor: ${current}"
      break
    fi
    [[ -d "${current}" && ! -L "${current}" ]] \
      || die "${label} path has an unsafe ancestor: ${current}"
    resolved_current="$(realpath -e -- "${current}")" \
      || die "${label} path ancestor cannot be resolved: ${current}"
    [[ "${resolved_current}" == "${current}" ]] \
      || die "${label} path ancestor resolves outside its lexical path: ${current}"
    [[ "$(stat -c '%u' "${current}")" == 0 ]] \
      || die "${label} path ancestor must be root-owned: ${current}"
    ancestor_mode="$(stat -c '%a' "${current}")"
    if (( (8#${ancestor_mode} & 0022) != 0 \
        && (8#${ancestor_mode} & 01000) == 0 )); then
      die "${label} path ancestor must not be group/world-writable: ${current}"
    fi
    previous_mode="${ancestor_mode}"
  done
}
[[ "$(id -u)" == 0 ]] || die "must run as root"

install_lock="${HERMES_INSTALL_LOCK_FILE:-/run/lock/hermes-agent/collaboration-install.lock}"
install_lock_dir="$(dirname "${install_lock}")"
install_lock_parent="$(dirname -- "${install_lock_dir}")"
validate_root_controlled_parent "${install_lock_parent}" "install lock parent"
if [[ ! -d "${install_lock_dir}" && ! -L "${install_lock_dir}" ]]; then
  install -d -o root -g root -m 0755 "${install_lock_dir}"
fi
[[ -d "${install_lock_dir}" && ! -L "${install_lock_dir}" ]] || die "unsafe install lock directory"
[[ "$(stat -c '%u' "${install_lock_dir}")" == 0 ]] || die "install lock directory must be root-owned"
lock_dir_mode="$(stat -c '%a' "${install_lock_dir}")"
(( (8#${lock_dir_mode} & 0022) == 0 )) \
  || die "install lock directory must not be group/world-writable"
if [[ -e "${install_lock}" || -L "${install_lock}" ]]; then
  [[ -f "${install_lock}" && ! -L "${install_lock}" ]] \
    || die "install lock file must be a regular file"
  [[ "$(stat -c '%u' "${install_lock}")" == 0 ]] \
    || die "install lock file must be root-owned"
fi
exec 8>"${install_lock}"
chmod 0600 "${install_lock}"
lock_fd_identity="$(stat -Lc '%d:%i' "/proc/$$/fd/8")"
lock_path_identity="$(stat -c '%d:%i' "${install_lock}")"
[[ "${lock_fd_identity}" == "${lock_path_identity}" \
    && "$(stat -c '%u:%a' "${install_lock}")" == "0:600" ]] \
  || die "install lock identity changed while opening"
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
deploy_hard_kill_phase="${HERMES_DEPLOY_HARD_KILL_PHASE:-}"
case "${deploy_hard_kill_phase}" in
  ""|rebind-dropin-reloaded|rebind-env-rewritten|venv-prepared|venv-old-moved|venv-candidate-live|candidate-marker-written|venv-candidate-journal|candidate-authoritative|candidate-running|candidate-marker-committed|watchdog-detached|venv-committed-cleanup) ;;
  *) die "unknown HERMES_DEPLOY_HARD_KILL_PHASE: ${deploy_hard_kill_phase}" ;;
esac
deploy_hard_kill() {
  local phase="$1"
  if [[ "${deploy_hard_kill_phase}" == "${phase}" ]]; then
    kill -KILL "$$"
  fi
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
  "deploy/public/candidate-start-guard.py"
  "deploy/public/runtime-home-guard.py"
  "deploy/public/profile-runtime-io.py"
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
  [[ -n "${relative}" ]] || die "runtime source manifest contains an empty entry"
  case "${relative}" in
    *.py|gateway/assets/*.yaml|hermes_cli/data/*.json) ;;
    *) die "runtime source manifest contains an unsupported entry: ${relative}" ;;
  esac
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
hk_enabled="${4:-${HERMES_HK_ENABLED:-0}}"
[[ "${hk_enabled}" == 0 || "${hk_enabled}" == 1 ]] \
  || die "HERMES_HK_ENABLED must be 0 or 1"

target_root="${HERMES_AGENT_ROOT:-/opt/hermes-agent}"
service="${HERMES_AGENT_SERVICE:-hermes-agent.service}"
service_user="${HERMES_AGENT_USER:-hermes-agent}"
service_group="${HERMES_AGENT_GROUP:-hermes-agent}"
[[ "${service}" =~ ^[A-Za-z0-9_.@:-]+\.service$ ]] \
  || die "Hermes service name is unsafe"
id "${service_user}" >/dev/null 2>&1 || die "service user does not exist: ${service_user}"
getent group "${service_group}" >/dev/null 2>&1 \
  || die "service group does not exist: ${service_group}"
service_uid="$(id -u "${service_user}")"
service_gid="$(getent group "${service_group}" | cut -d: -f3)"
[[ "${service_uid}" =~ ^[0-9]+$ && "${service_gid}" =~ ^[0-9]+$ ]] \
  || die "service account numeric identity is invalid"
[[ -d "${target_root}" && ! -L "${target_root}" ]] \
  || die "target root is missing or unsafe: ${target_root}"
resolved_target_root="$(realpath -e -- "${target_root}")" \
  || die "target root cannot be resolved: ${target_root}"
[[ "${resolved_target_root}" == "${target_root}" ]] \
  || die "target root resolves outside its lexical path: ${target_root}"
validate_root_controlled_parent "${target_root}" "Hermes target root"
target_root_uid="$(stat -c '%u' "${target_root}")"
[[ "${target_root_uid}" == 0 ]] \
  || die "target root must be root-owned"
target_root_mode="$(stat -c '%a' "${target_root}")"
(( (8#${target_root_mode} & 0022) == 0 )) \
  || die "target root must not be group/world-writable"
target_root_identity="$(stat -c '%d:%i' "${target_root}")"

runtime_venv="${target_root}/.venv"
runtime_python="${HERMES_RUNTIME_PYTHON:-${runtime_venv}/bin/python}"
bootstrap_python="${HERMES_BOOTSTRAP_PYTHON:-}"
if [[ -z "${bootstrap_python}" ]]; then
  bootstrap_python="$(command -v python3 || true)"
fi
[[ "${bootstrap_python}" == /* && -x "${bootstrap_python}" ]] \
  || die "trusted bootstrap Python is missing: ${bootstrap_python}"
bootstrap_python_resolved="$(realpath -e -- "${bootstrap_python}")" \
  || die "trusted bootstrap Python cannot be resolved"
validate_root_controlled_parent "$(dirname -- "${bootstrap_python_resolved}")" \
  "trusted bootstrap Python"
[[ "$(stat -c '%u' "${bootstrap_python_resolved}")" == 0 ]] \
  || die "trusted bootstrap Python must be root-owned"
bootstrap_python_mode="$(stat -c '%a' "${bootstrap_python_resolved}")"
(( (8#${bootstrap_python_mode} & 0022) == 0 )) \
  || die "trusted bootstrap Python must not be group/world-writable"
bootstrap_python_version="$(
  "${bootstrap_python_resolved}" -I -c \
    'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
)" || die "trusted bootstrap Python version could not be read"
case "${bootstrap_python_version}" in
  3.11|3.12|3.13) ;;
  *) die "trusted bootstrap Python must be >=3.11,<3.14" ;;
esac
assert_no_process_references() {
  # Stopping systemd is asynchronous.  Before a descriptor-relative copy or
  # directory switch, wait until no other process still has the runtime (or a
  # legacy source) as cwd/root/open fd.  Without this gate SQLite/WAL and a
  # long-lived worker can continue mutating a tree while it is being cloned.
  local -a paths=("$@")
  local report=""
  for _ in $(seq 1 40); do
    if report="$(${bootstrap_python_resolved} -I - "${paths[@]}" <<'PY'
import os
import pathlib
import sys

roots = [os.path.normpath(value) for value in sys.argv[1:] if value]
pid_self = os.getpid()

def relevant(target: str) -> bool:
    if target.endswith(" (deleted)"):
        target = target[:-10]
    target = os.path.normpath(target)
    return any(target == root or target.startswith(root + os.sep) for root in roots)

hits = []
for proc in pathlib.Path("/proc").glob("[0-9]*"):
    try:
        pid = int(proc.name)
    except ValueError:
        continue
    if pid == pid_self:
        continue
    for name in ("cwd", "root"):
        link = proc / name
        try:
            target = os.readlink(link)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if relevant(target):
            hits.append(f"pid={pid} {name}={target}")
    fd_dir = proc / "fd"
    try:
        entries = list(fd_dir.iterdir())
    except (FileNotFoundError, PermissionError, OSError):
        continue
    for entry in entries:
        try:
            target = os.readlink(entry)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if relevant(target):
            hits.append(f"pid={pid} fd={entry.name}={target}")
if hits:
    print("\n".join(sorted(set(hits))))
    raise SystemExit(1)
PY
    )"; then
      return 0
    fi
    sleep 0.25
  done
  printf '%s\n' "processes still reference a quiescing Hermes tree:" >&2
  printf '%s\n' "${report}" >&2
  return 1
}
systemctl_command="${HERMES_SYSTEMCTL_BINARY:-$(command -v systemctl || true)}"
systemd_run_command="${HERMES_SYSTEMD_RUN_BINARY:-}"
if [[ -z "${systemd_run_command}" ]]; then
  systemd_run_command="$(command -v systemd-run || true)"
fi
setpriv_command="${HERMES_SETPRIV_BINARY:-$(command -v setpriv || true)}"
for trusted_systemd_command in \
    "${systemctl_command}" "${systemd_run_command}" "${setpriv_command}"; do
  [[ "${trusted_systemd_command}" == /* && -x "${trusted_systemd_command}" ]] \
    || die "trusted systemd command is missing: ${trusted_systemd_command:-<unset>}"
  trusted_systemd_resolved="$(realpath -e -- "${trusted_systemd_command}")" \
    || die "trusted systemd command cannot be resolved"
  validate_root_controlled_parent "$(dirname -- "${trusted_systemd_resolved}")" \
    "trusted systemd command"
  [[ "$(stat -c '%u' "${trusted_systemd_resolved}")" == 0 ]] \
    || die "trusted systemd command must be root-owned"
  trusted_systemd_mode="$(stat -c '%a' "${trusted_systemd_resolved}")"
  (( (8#${trusted_systemd_mode} & 0022) == 0 )) \
    || die "trusted systemd command must not be group/world-writable"
done
systemctl_resolved="$(realpath -e -- "${systemctl_command}")"
systemd_run_resolved="$(realpath -e -- "${systemd_run_command}")"
setpriv_resolved="$(realpath -e -- "${setpriv_command}")"
[[ "$(basename -- "${systemctl_resolved}")" == systemctl \
    && "$(basename -- "${systemd_run_resolved}")" == systemd-run \
    && "$(basename -- "${setpriv_resolved}")" == setpriv ]] \
  || die "trusted systemd command names are invalid"
systemctl() {
  "${systemctl_resolved}" "$@"
}
systemd_run() {
  "${systemd_run_resolved}" "$@"
}
curl_command="${HERMES_CURL_BINARY:-$(command -v curl || true)}"
[[ "${curl_command}" == /* && -x "${curl_command}" ]] \
  || die "trusted curl command is missing: ${curl_command:-<unset>}"
curl_resolved="$(realpath -e -- "${curl_command}")" \
  || die "trusted curl command cannot be resolved"
validate_root_controlled_parent "$(dirname -- "${curl_resolved}")" \
  "trusted curl command"
[[ "$(stat -c '%u' "${curl_resolved}")" == 0 ]] \
  || die "trusted curl command must be root-owned"
curl_mode="$(stat -c '%a' "${curl_resolved}")"
(( (8#${curl_mode} & 0022) == 0 )) \
  || die "trusted curl command must not be group/world-writable"
curl() {
  "${curl_resolved}" "$@"
}
dependency_runtime_managed=0
venv_swap_journal=""
venv_swap_journal_dir=""
venv_recovery_disposition=""
venv_recovery_had_journal=0
early_recovery_service_stopped=0
early_recovery_restart_allowed=1
release_retry_stopped=0
release_candidate_pending=0

release_evidence_target="${HERMES_RELEASE_EVIDENCE_FILE:-/var/lib/hermes-agent-release/release-evidence.json}"
[[ "${release_evidence_target}" == /* ]] || die "release evidence path must be absolute"
release_evidence_dir="$(dirname -- "${release_evidence_target}")"
validate_root_controlled_parent "${release_evidence_dir}" "release evidence"
if [[ ! -e "${release_evidence_dir}" && ! -L "${release_evidence_dir}" ]]; then
  install -d -o root -g root -m 0755 "${release_evidence_dir}"
fi
validate_root_controlled_parent "${release_evidence_dir}" "release evidence"
[[ -d "${release_evidence_dir}" && ! -L "${release_evidence_dir}" ]] \
  || die "release evidence directory is unsafe"
[[ "$(stat -c '%u' "${release_evidence_dir}")" == 0 ]] \
  || die "release evidence directory must be root-owned"
release_evidence_mode="$(stat -c '%a' "${release_evidence_dir}")"
(( (8#${release_evidence_mode} & 0022) == 0 )) \
  || die "release evidence directory must not be group/world-writable"
release_evidence_dir_identity="$(stat -c '%d:%i' "${release_evidence_dir}")"
[[ "${release_evidence_dir_identity}" =~ ^[0-9]+:[0-9]+$ ]] \
  || die "release evidence directory identity is invalid"

publish_release_evidence() {
  local source="$1"
  "${bootstrap_python_resolved}" -I - \
    "${source}" "${release_evidence_target}" \
    "${release_evidence_dir_identity}" "${version}" "${release_commit}" <<'PY'
import json
import os
import pathlib
import stat
import sys

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
expected_parent_identity = sys.argv[3]
expected_version = sys.argv[4]
expected_commit = sys.argv[5]
if source.parent != target.parent or source.name == target.name:
    raise RuntimeError("release evidence paths are not a same-directory transaction")

parent_fd = os.open(
    target.parent,
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
)
source_fd = None
try:
    parent = os.fstat(parent_fd)
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != 0
        or stat.S_IMODE(parent.st_mode) & 0o022
        or f"{parent.st_dev}:{parent.st_ino}" != expected_parent_identity
    ):
        raise RuntimeError("release evidence directory changed")
    source_fd = os.open(
        source.name,
        os.O_RDWR | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    metadata = os.fstat(source_fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_size <= 0
        or metadata.st_size > 4 * 1024 * 1024
    ):
        raise RuntimeError("release evidence temporary is unsafe")
    chunks = []
    while True:
        chunk = os.read(source_fd, 64 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    content = b"".join(chunks)
    payload = json.loads(content.decode("utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "hermes.release-evidence.v1"
        or payload.get("phase") != "committed"
        or payload.get("version") != expected_version
        or payload.get("commit") != expected_commit
    ):
        raise RuntimeError("release evidence identity is invalid")
    os.fchown(source_fd, 0, 0)
    os.fchmod(source_fd, 0o644)
    os.fsync(source_fd)
    current = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
        raise RuntimeError("release evidence temporary changed before publish")
    os.replace(
        source.name,
        target.name,
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
    )
    os.fsync(parent_fd)
    published = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        (published.st_dev, published.st_ino) != (metadata.st_dev, metadata.st_ino)
        or published.st_uid != 0
        or stat.S_IMODE(published.st_mode) != 0o644
    ):
        raise RuntimeError("release evidence changed after publish")
finally:
    if source_fd is not None:
        os.close(source_fd)
    os.close(parent_fd)
PY
}

# This marker outlives both the checkout and the installer process. It is the
# durable point-of-no-return for a candidate that may have accepted writes.
release_candidate_marker="${HERMES_RELEASE_PENDING_MARKER:-${release_evidence_dir}/candidate-pending.json}"
[[ "${release_candidate_marker}" == /* \
    && "$(basename -- "${release_candidate_marker}")" != "." \
    && "$(basename -- "${release_candidate_marker}")" != ".." ]] \
  || die "release candidate marker path is unsafe"
[[ "${release_candidate_marker}" != "${release_evidence_target}" ]] \
  || die "release candidate marker must be distinct from release evidence"
case "${release_candidate_marker}" in
  "${target_root}"|"${target_root}"/*)
    die "release candidate marker must live outside the service checkout" ;;
esac
release_candidate_marker_dir="$(dirname -- "${release_candidate_marker}")"
validate_root_controlled_parent "${release_candidate_marker_dir}" \
  "release candidate marker"
if [[ ! -e "${release_candidate_marker_dir}" \
    && ! -L "${release_candidate_marker_dir}" ]]; then
  install -d -o root -g root -m 0755 "${release_candidate_marker_dir}"
fi
validate_root_controlled_parent "${release_candidate_marker_dir}" \
  "release candidate marker"
[[ -d "${release_candidate_marker_dir}" \
    && ! -L "${release_candidate_marker_dir}" \
    && "$(stat -c '%u' "${release_candidate_marker_dir}")" == 0 ]] \
  || die "release candidate marker directory is unsafe"
release_candidate_marker_dir_mode="$(stat -c '%a' "${release_candidate_marker_dir}")"
(( (8#${release_candidate_marker_dir_mode} & 0022) == 0 )) \
  || die "release candidate marker directory must not be group/world-writable"
release_candidate_marker_dir_identity="$(
  stat -c '%d:%i' "${release_candidate_marker_dir}"
)"
[[ "${release_candidate_marker_dir_identity}" =~ ^[0-9]+:[0-9]+$ ]] \
  || die "release candidate marker directory identity is invalid"
release_start_guard_sha256="$(
  sha256sum "${stage_root}/deploy/public/candidate-start-guard.py" | cut -d' ' -f1
)"
[[ "${release_start_guard_sha256}" =~ ^[0-9a-f]{64}$ ]] \
  || die "candidate start guard SHA-256 is invalid"
release_start_guard="${release_candidate_marker_dir}/candidate-start-guard.${release_start_guard_sha256}.py"
release_start_lease="${release_candidate_marker_dir}/candidate-start-lease.json"
for safe_release_path in \
    "${target_root}" "${bootstrap_python_resolved}" \
    "${release_candidate_marker}" "${release_start_guard}" \
    "${release_start_lease}"; do
  [[ "${safe_release_path}" =~ ^/[A-Za-z0-9._/@+:-]+$ ]] \
    || die "release transaction path contains unsupported systemd characters: ${safe_release_path}"
done
[[ "${release_start_guard}" != "${release_candidate_marker}" \
    && "${release_start_lease}" != "${release_candidate_marker}" \
    && "${release_start_guard}" != "${release_start_lease}" ]] \
  || die "release marker, guard, and lease paths must be distinct"

write_release_start_lease() {
  "${bootstrap_python_resolved}" -I "${release_start_guard}" write-lease \
    "${release_candidate_marker}" "${release_start_lease}" \
    "${target_root}" "${target_root_identity}" "${service}" \
    "${release_candidate_marker_dir_identity}" \
    "${runtime_home}" "${release_candidate_txid}" \
    "${version}" "${release_commit}" "${BASHPID}"
}

remove_release_start_lease() {
  "${bootstrap_python_resolved}" -I "${release_start_guard}" remove-lease \
    "${release_candidate_marker}" "${release_start_lease}" \
    "${target_root}" "${target_root_identity}" "${service}" \
    "${release_candidate_marker_dir_identity}" \
    "${runtime_home}" "${release_candidate_txid}" \
    "${version}" "${release_commit}"
}

release_candidate_marker_action() {
  local action="$1"
  local expected_runtime_home="${2:-}"
  local marker_txid="${3:-}"
  local marker_version="${4:-}"
  local marker_commit="${5:-}"
  "${bootstrap_python_resolved}" -I - \
    "${release_candidate_marker}" "${action}" \
    "${target_root}" "${target_root_identity}" "${service}" \
    "${expected_runtime_home}" "${marker_txid}" \
    "${marker_version}" "${marker_commit}" <<'PY'
import json
import os
import pathlib
import re
import stat
import sys
import uuid

(marker_arg, action, target_root, target_identity, service,
 expected_runtime_home, expected_txid, expected_version,
 expected_commit) = sys.argv[1:]
marker = pathlib.Path(marker_arg)
parent = marker.parent
expected_keys = {
    "schema", "phase", "txid", "target_root", "target_identity",
    "runtime_home", "runtime_identity", "service", "version", "commit",
}

def fail(message: str) -> None:
    raise RuntimeError(f"unsafe release candidate marker: {message}")

def fsync_directory(path: pathlib.Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def directory_identity(path: pathlib.Path) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        fail(f"directory is missing: {path}")
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fail(f"path is not a real directory: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        fail(f"directory cannot be resolved: {path}: {error}")
    if str(resolved) != str(path):
        fail(f"directory resolves outside its lexical path: {path}")
    return f"{metadata.st_dev}:{metadata.st_ino}"

def load_marker():
    try:
        metadata = marker.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 16384):
        fail("ownership, mode, type, or size is invalid")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except Exception as error:
        fail(f"cannot be parsed: {error}")
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        fail("schema fields are invalid")
    if payload["schema"] != "hermes.release-candidate.v1" or payload["phase"] != "candidate":
        fail("schema or phase is invalid")
    for key in expected_keys:
        if not isinstance(payload[key], str):
            fail(f"{key} must be a string")
    if (re.fullmatch(r"[0-9a-f]{32}", payload["txid"]) is None
            or re.fullmatch(r"[0-9]+:[0-9]+", payload["target_identity"]) is None
            or re.fullmatch(r"[0-9]+:[0-9]+", payload["runtime_identity"]) is None
            or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", payload["version"]) is None
            or re.fullmatch(r"[0-9a-f]{40}", payload["commit"]) is None):
        fail("transaction or release identity is invalid")
    if (payload["target_root"] != target_root
            or payload["target_identity"] != target_identity
            or payload["service"] != service):
        fail("target root or service identity changed")
    runtime_path = pathlib.Path(payload["runtime_home"])
    if not runtime_path.is_absolute() or str(runtime_path) != payload["runtime_home"]:
        fail("runtime home path is invalid")
    if directory_identity(runtime_path) != payload["runtime_identity"]:
        fail("runtime home identity changed")
    return payload

temporary_prefix = f".{marker.name}.new-"
temporary_markers = [
    entry for entry in parent.iterdir() if entry.name.startswith(temporary_prefix)
]
for temporary_marker in temporary_markers:
    metadata = temporary_marker.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600):
        fail("temporary marker ownership, mode, or type is invalid")

payload = load_marker()
if action == "inspect":
    if payload is not None:
        print("present")
    elif temporary_markers:
        # A kill can leave a partial file before the atomic replace. The file
        # itself is root-authenticated evidence that an in-place candidate was
        # at the point of no return; keep it durable until a retry atomically
        # publishes a complete marker.
        print("interrupted")
    else:
        print("absent")
    raise SystemExit(0)

if action == "runtime":
    if payload is None:
        fail("marker is missing")
    print(payload["runtime_home"])
    raise SystemExit(0)

if action not in {"validate", "write", "remove"}:
    fail("action is invalid")
if action != "write" and payload is None:
    fail("marker is missing")
if action in {"validate", "remove"}:
    assert payload is not None
    if expected_runtime_home and payload["runtime_home"] != expected_runtime_home:
        fail("runtime home path changed")
    if expected_txid and payload["txid"] != expected_txid:
        fail("transaction id changed")
    if expected_version and payload["version"] != expected_version:
        fail("release version changed")
    if expected_commit and payload["commit"] != expected_commit:
        fail("release commit changed")
    if action == "validate":
        print("present")
        raise SystemExit(0)
    for temporary_marker in temporary_markers:
        temporary_marker.unlink()
    if temporary_markers:
        fsync_directory(parent)
    marker.unlink()
    fsync_directory(parent)
    print("absent")
    raise SystemExit(0)

if (not expected_runtime_home
        or re.fullmatch(r"[0-9a-f]{32}", expected_txid) is None
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", expected_version) is None
        or re.fullmatch(r"[0-9a-f]{40}", expected_commit) is None):
    fail("candidate release arguments are invalid")
runtime_path = pathlib.Path(expected_runtime_home)
runtime_identity = directory_identity(runtime_path)
new_payload = {
    "schema": "hermes.release-candidate.v1",
    "phase": "candidate",
    "txid": expected_txid,
    "target_root": target_root,
    "target_identity": target_identity,
    "runtime_home": expected_runtime_home,
    "runtime_identity": runtime_identity,
    "service": service,
    "version": expected_version,
    "commit": expected_commit,
}
temporary = marker.with_name(f".{marker.name}.new-{uuid.uuid4().hex}")
descriptor = os.open(
    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
)
try:
    os.fchown(descriptor, 0, 0)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
        json.dump(new_payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    # Remove only previously authenticated temporary files before publishing
    # this transaction. Once os.replace succeeds, no best-effort cleanup is
    # allowed to turn a durable candidate into a reported write failure.
    for stale_temporary in temporary_markers:
        stale_temporary.unlink()
    if temporary_markers:
        fsync_directory(parent)
    os.replace(temporary, marker)
    fsync_directory(parent)
except BaseException:
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    raise
print("present")
PY
}

# Marker recovery is checked before the live runtime interpreter is touched.
# A leftover temporary also proves an interrupted point-of-no-return attempt,
# even when atomic replacement had not completed yet.
release_marker_recovery_required=0
if [[ -e "${release_candidate_marker}" || -L "${release_candidate_marker}" ]] \
    || find "${release_candidate_marker_dir}" -mindepth 1 -maxdepth 1 \
      -name ".$(basename -- "${release_candidate_marker}").new-*" \
      -print -quit | grep -q .; then
  release_marker_recovery_required=1
  systemctl stop "${service}" \
    || die "could not quiesce Hermes before release candidate recovery"
  assert_no_process_references "${target_root}" \
    || die "a process still references the release target during candidate recovery"
  early_recovery_service_stopped=1
  early_recovery_restart_allowed=0
  release_retry_stopped=1
fi
release_candidate_marker_state="$(release_candidate_marker_action inspect)" \
  || die "release candidate marker validation failed; service remains stopped"
case "${release_candidate_marker_state}" in
  present|interrupted)
    release_candidate_pending=1
    early_recovery_restart_allowed=0
    release_retry_stopped=1
    ;;
  absent) ;;
  *) die "release candidate marker returned an invalid state" ;;
esac

# Recover an interrupted dependency swap before the live interpreter is used.
# The journal is stored outside the service-owned checkout and identifies both
# directory trees by inode, so recovery never selects a rollback by age/name.
recover_venv_swap() {
  [[ "${dependency_runtime_managed}" == 1 ]] || return 0
  local recovery_output
  if ! recovery_output="$(
    "${bootstrap_python_resolved}" -I - "${target_root}" "${venv_swap_journal}" <<'PY'
import json
import os
import pathlib
import re
import shutil
import signal
import stat
import sys

root = pathlib.Path(sys.argv[1])
journal = pathlib.Path(sys.argv[2])
expected_keys = {
    "schema", "phase", "txid", "target_root", "target_identity",
    "live", "old_identity", "candidate", "new_identity", "rollback",
}

def fail(message: str) -> None:
    raise RuntimeError(f"unsafe virtual-environment swap state: {message}")

def identity(path: pathlib.Path):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fail(f"{path} is not a real directory")
    return f"{metadata.st_dev}:{metadata.st_ino}"

def fsync_directory(path: pathlib.Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def remove_tree(path: pathlib.Path, expected_identity: str) -> None:
    if identity(path) != expected_identity:
        fail(f"identity changed before removing {path.name}")
    shutil.rmtree(path)
    fsync_directory(root)

def recovery_kill(phase: str) -> None:
    if os.environ.get("HERMES_VENV_RECOVERY_KILL_PHASE") == phase:
        os.kill(os.getpid(), signal.SIGKILL)

root_stat = root.stat(follow_symlinks=False)
if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
    fail("target root is not a real directory")
root_identity = f"{root_stat.st_dev}:{root_stat.st_ino}"

temporary_prefix = f".{journal.name}.new-"
temporary_journals = [
    entry for entry in journal.parent.iterdir() if entry.name.startswith(temporary_prefix)
]
for temporary_journal in temporary_journals:
    temporary_stat = temporary_journal.lstat()
    if (not stat.S_ISREG(temporary_stat.st_mode)
            or stat.S_ISLNK(temporary_stat.st_mode)
            or temporary_stat.st_uid != 0
            or stat.S_IMODE(temporary_stat.st_mode) != 0o600):
        fail("temporary journal ownership or mode is invalid")
    temporary_journal.unlink()
if temporary_journals:
    fsync_directory(journal.parent)

swap_names = {
    entry.name
    for entry in root.iterdir()
    if entry.name.startswith((".venv.candidate.", ".venv.rollback-", ".venv.failed."))
}
live = root / ".venv"

if not journal.exists() and not journal.is_symlink():
    rollback_names = {name for name in swap_names if name.startswith((".venv.rollback-", ".venv.failed."))}
    if rollback_names:
        fail("rollback artifacts exist without a journal")
    live_identity = identity(live)
    if live_identity is None:
        fail("live environment is missing without a journal")
    for name in sorted(swap_names):
        candidate = root / name
        candidate_identity = identity(candidate)
        if candidate_identity is None:
            fail(f"candidate disappeared during recovery: {name}")
        remove_tree(candidate, candidate_identity)
    print("old")
    raise SystemExit(0)

journal_stat = journal.lstat()
if (not stat.S_ISREG(journal_stat.st_mode) or stat.S_ISLNK(journal_stat.st_mode)
        or journal_stat.st_uid != 0 or stat.S_IMODE(journal_stat.st_mode) != 0o600):
    fail("journal ownership or mode is invalid")
try:
    payload = json.loads(journal.read_text(encoding="utf-8"))
except Exception as error:
    fail(f"journal cannot be parsed: {error}")
if not isinstance(payload, dict) or set(payload) != expected_keys:
    fail("journal schema fields are invalid")
if payload["schema"] != "hermes.venv-swap.v1":
    fail("journal schema is unsupported")
if payload["phase"] not in {"prepared", "candidate", "committed"}:
    fail("journal phase is invalid")
txid = payload["txid"]
if not isinstance(txid, str) or re.fullmatch(r"[0-9a-f]{32}", txid) is None:
    fail("journal transaction id is invalid")
if payload["target_root"] != str(root) or payload["target_identity"] != root_identity:
    fail("journal target root identity changed")
if payload["live"] != ".venv":
    fail("journal live name is invalid")
if payload["candidate"] != f".venv.candidate.{txid}":
    fail("journal candidate name is invalid")
if payload["rollback"] != f".venv.rollback-{txid}":
    fail("journal rollback name is invalid")
for key in ("old_identity", "new_identity"):
    if not isinstance(payload[key], str) or re.fullmatch(r"[0-9]+:[0-9]+", payload[key]) is None:
        fail(f"journal {key} is invalid")
if payload["old_identity"] == payload["new_identity"]:
    fail("journal environment identities must differ")

candidate = root / payload["candidate"]
rollback = root / payload["rollback"]
allowed_swap_names = {candidate.name, rollback.name}
extra_names = swap_names - allowed_swap_names
if extra_names:
    fail(f"unrecorded swap artifacts exist: {sorted(extra_names)!r}")

old_identity = payload["old_identity"]
new_identity = payload["new_identity"]
live_identity = identity(live)
candidate_identity = identity(candidate)
rollback_identity = identity(rollback)
for actual, allowed, label in (
    (live_identity, {None, old_identity, new_identity}, "live"),
    (candidate_identity, {None, new_identity}, "candidate"),
    (rollback_identity, {None, old_identity}, "rollback"),
):
    if actual not in allowed:
        fail(f"{label} environment has an unexpected identity")

disposition = payload["phase"]
if disposition == "prepared":
    if live_identity == old_identity and candidate_identity == new_identity and rollback_identity is None:
        remove_tree(candidate, new_identity)
        recovery_kill("after-candidate-delete")
    elif live_identity is None and candidate_identity == new_identity and rollback_identity == old_identity:
        os.replace(rollback, live)
        fsync_directory(root)
        recovery_kill("after-old-restore")
        remove_tree(candidate, new_identity)
        recovery_kill("after-candidate-delete")
    elif live_identity == new_identity and candidate_identity is None and rollback_identity == old_identity:
        os.replace(live, candidate)
        fsync_directory(root)
        recovery_kill("after-live-quarantine")
        os.replace(rollback, live)
        fsync_directory(root)
        recovery_kill("after-old-restore")
        remove_tree(candidate, new_identity)
        recovery_kill("after-candidate-delete")
    elif not (live_identity == old_identity and candidate_identity is None and rollback_identity is None):
        fail("prepared journal does not match a recoverable rename state")
    disposition = "old"
else:
    if not (live_identity == new_identity and candidate_identity is None
            and rollback_identity in {None, old_identity}):
        fail("candidate journal does not match the candidate environment")
    if rollback_identity == old_identity:
        remove_tree(rollback, old_identity)
        recovery_kill("after-rollback-delete")

journal.unlink()
fsync_directory(journal.parent)
print(disposition)
PY
  )"; then
    return 1
  fi
  case "${recovery_output}" in
    old|candidate|committed) venv_recovery_disposition="${recovery_output}" ;;
    *) return 1 ;;
  esac
}

write_venv_swap_journal() {
  local phase="$1"
  [[ "${dependency_runtime_managed}" == 1 ]] || return 0
  "${bootstrap_python_resolved}" -I - \
    "${venv_swap_journal}" "${phase}" "${venv_swap_txid}" \
    "${target_root}" "${target_root_identity}" \
    "$(basename -- "${runtime_venv}")" "${venv_old_identity}" \
    "$(basename -- "${candidate_venv}")" "${venv_new_identity}" \
    "$(basename -- "${previous_venv}")" <<'PY'
import json
import os
import pathlib
import re
import stat
import sys
import uuid

(journal_arg, phase, txid, target_root, target_identity, live,
 old_identity, candidate, new_identity, rollback) = sys.argv[1:]
journal = pathlib.Path(journal_arg)
root = pathlib.Path(target_root)
if (phase not in {"prepared", "candidate", "committed"}
        or re.fullmatch(r"[0-9a-f]{32}", txid) is None
        or live != ".venv"
        or candidate != f".venv.candidate.{txid}"
        or rollback != f".venv.rollback-{txid}"
        or old_identity == new_identity):
    raise RuntimeError("invalid virtual-environment journal transition")

def identity(path: pathlib.Path):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"invalid virtual-environment path: {path}")
    return f"{metadata.st_dev}:{metadata.st_ino}"

root_metadata = root.stat(follow_symlinks=False)
if f"{root_metadata.st_dev}:{root_metadata.st_ino}" != target_identity:
    raise RuntimeError("target root identity changed before journal transition")
positions = (identity(root / live), identity(root / candidate), identity(root / rollback))
expected_positions = {
    "prepared": (old_identity, new_identity, None),
    "candidate": (new_identity, None, old_identity),
    "committed": (new_identity, None, old_identity),
}
if positions != expected_positions[phase]:
    raise RuntimeError("virtual-environment positions do not match journal transition")
if phase in {"candidate", "committed"}:
    metadata = journal.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600):
        raise RuntimeError("prepared virtual-environment journal is missing or unsafe")
payload = {
    "schema": "hermes.venv-swap.v1",
    "phase": phase,
    "txid": txid,
    "target_root": target_root,
    "target_identity": target_identity,
    "live": live,
    "old_identity": old_identity,
    "candidate": candidate,
    "new_identity": new_identity,
    "rollback": rollback,
}
temporary = journal.with_name(f".{journal.name}.new-{uuid.uuid4().hex}")
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chown(temporary, 0, 0)
    os.chmod(temporary, 0o600)
    os.replace(temporary, journal)
    directory_fd = os.open(journal.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
except BaseException:
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    raise
PY
}

fsync_target_root() {
  "${bootstrap_python_resolved}" -I - "${target_root}" <<'PY'
import os
import sys

fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
PY
}

if [[ "${runtime_python}" == "${runtime_venv}/bin/python" ]]; then
  dependency_runtime_managed=1
  venv_swap_journal="${HERMES_VENV_SWAP_JOURNAL:-/var/lib/hermes-agent-release/venv-swap.json}"
  [[ "${venv_swap_journal}" == /* \
      && "$(basename -- "${venv_swap_journal}")" == "venv-swap.json" ]] \
    || die "virtual-environment swap journal path is unsafe"
  venv_swap_journal_dir="$(dirname -- "${venv_swap_journal}")"
  validate_root_controlled_parent "${venv_swap_journal_dir}" \
    "virtual-environment swap journal"
  if [[ ! -e "${venv_swap_journal_dir}" && ! -L "${venv_swap_journal_dir}" ]]; then
    install -d -o root -g root -m 0755 "${venv_swap_journal_dir}"
  fi
  validate_root_controlled_parent "${venv_swap_journal_dir}" \
    "virtual-environment swap journal"
  [[ "$(stat -c '%u' "${venv_swap_journal_dir}")" == 0 ]] \
    || die "virtual-environment swap journal directory must be root-owned"
  venv_swap_journal_dir_mode="$(stat -c '%a' "${venv_swap_journal_dir}")"
  (( (8#${venv_swap_journal_dir_mode} & 0022) == 0 )) \
    || die "virtual-environment swap journal directory must not be group/world-writable"
  venv_recovery_requires_quiesce=0
  if [[ -e "${venv_swap_journal}" || -L "${venv_swap_journal}" \
      || ! -d "${runtime_venv}" || -L "${runtime_venv}" ]] \
      || find "${target_root}" -mindepth 1 -maxdepth 1 \
        \( -name '.venv.rollback-*' -o -name '.venv.failed.*' \
          -o -name '.venv.candidate.*' \) \
        -print -quit | grep -q .; then
    venv_recovery_requires_quiesce=1
  fi
  if find "${venv_swap_journal_dir}" -mindepth 1 -maxdepth 1 \
      -name ".$(basename -- "${venv_swap_journal}").new-*" \
      -print -quit | grep -q .; then
    venv_recovery_requires_quiesce=1
  fi
  if [[ "${venv_recovery_requires_quiesce}" == 1 ]]; then
    systemctl stop "${service}" \
      || die "could not quiesce Hermes before runtime environment recovery"
    assert_no_process_references "${target_root}" "${runtime_venv}" \
      || die "a process still references the runtime environment during recovery"
    early_recovery_service_stopped=1
  fi
  if [[ -e "${venv_swap_journal}" || -L "${venv_swap_journal}" ]]; then
    venv_recovery_had_journal=1
  fi
  recover_venv_swap \
    || die "virtual-environment swap recovery failed; service remains stopped"
  if [[ "${venv_recovery_requires_quiesce}" == 1 \
      && ( "${venv_recovery_disposition}" != committed \
        || "${release_candidate_pending}" == 1 ) ]]; then
    # A prepared/candidate recovery can coexist with an already rebound
    # systemd unit whose ready barrier is absent. Keep the service stopped and
    # require the serialized deployment retry to finish; `systemctl start`
    # would otherwise report success even when ConditionPathExists skipped it.
    # Only an explicitly committed journal with no pending marker is a
    # last-known-good release that an early failure may restart.
    early_recovery_restart_allowed=0
    release_retry_stopped=1
  fi
fi
[[ -x "${runtime_python}" ]] || die "Hermes runtime Python is missing: ${runtime_python}"
journalctl_command="${HERMES_JOURNALCTL_BINARY:-$(command -v journalctl || true)}"
[[ "${journalctl_command}" == /* && -x "${journalctl_command}" ]] \
  || die "trusted journalctl command is missing: ${journalctl_command:-<unset>}"
journalctl_resolved="$(realpath -e -- "${journalctl_command}")" \
  || die "trusted journalctl command cannot be resolved"
validate_root_controlled_parent "$(dirname -- "${journalctl_resolved}")" \
  "trusted journalctl command"
[[ "$(stat -c '%u' "${journalctl_resolved}")" == 0 ]] \
  || die "trusted journalctl command must be root-owned"
journalctl_mode="$(stat -c '%a' "${journalctl_resolved}")"
(( (8#${journalctl_mode} & 0022) == 0 )) \
  || die "trusted journalctl command must not be group/world-writable"
journalctl_binary="${journalctl_resolved}"

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
  if [[ -x "${journalctl_binary}" ]]; then
    # Startup logs are the only useful explanation for a service that exits
    # before binding. Force the central redaction policy because CI logs are
    # an external boundary and runtime redaction may be operator-disabled.
    "${journalctl_binary}" --unit "${service}" --since "${service_start_since}" \
      --no-pager --lines 160 --output=short-iso-precise 2>&1 \
      | runuser -u "${service_user}" -g "${service_group}" -- \
          env PYTHONPATH="${target_root}${PYTHONPATH:+:${PYTHONPATH}}" \
          "${runtime_python}" -c \
          'import re, sys; from hermes_runtime.redaction import redact_sensitive_text; text = redact_sensitive_text(sys.stdin.read(), force=True, redact_url_credentials=True); text = re.sub(r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|credential|authorization)\s*[=:]\s*)(?:\"[^\"\n]*\"|\x27[^\x27\n]*\x27|[^\s,;]+)", r"\1[REDACTED]", text); print(text, end="")' \
      || true
  else
    printf 'journal diagnostics unavailable: %s not found\n' \
      "${journalctl_binary}" >&2
  fi
  printf '%s\n' "service_diagnostics_end"
}
verify_service_active() {
  # ExecCondition/ConditionPathExists skips can make a start job itself look
  # successful. Require the unit to become genuinely active before rollback,
  # recovery, or the install itself may report success.
  for _ in $(seq 1 20); do
    systemctl is-active --quiet "${service}" && return 0
    sleep 0.25
  done
  return 1
}
start_and_verify_active() {
  systemctl reset-failed "${service}" >/dev/null 2>&1 || true
  systemctl start "${service}" >/dev/null 2>&1 || return 1
  verify_service_active
}
start_release_watchdog() {
  [[ "${release_watchdog_unit}" =~ ^hermes-release-watchdog-[0-9a-f]{32}\.service$ ]] \
    || return 1
  systemd_run --quiet --collect \
    --unit="${release_watchdog_unit}" \
    --service-type=exec \
    --property=User=root \
    --property=UMask=0077 \
    --property=NoNewPrivileges=yes \
    --property=Restart=on-failure \
    --property=RestartSec=250ms \
    --property=StartLimitIntervalSec=0 \
    "${bootstrap_python_resolved}" -I "${release_start_guard}" watch \
    "${release_candidate_marker}" "${release_start_lease}" \
    "${target_root}" "${target_root_identity}" "${service}" \
    "${release_candidate_marker_dir_identity}" \
    "${runtime_home}" "${release_candidate_txid}" \
    "${version}" "${release_commit}" "${BASHPID}" \
    "${systemctl_resolved}" \
    || return 1
  for _ in $(seq 1 20); do
    if systemctl is-active --quiet "${release_watchdog_unit}"; then
      release_watchdog_started=1
      return 0
    fi
    sleep 0.1
  done
  return 1
}
stop_release_watchdog() {
  [[ -n "${release_watchdog_unit}" ]] || return 0
  if systemctl is-active --quiet "${release_watchdog_unit}"; then
    systemctl stop "${release_watchdog_unit}" >/dev/null 2>&1 || return 1
  fi
  for _ in $(seq 1 20); do
    if ! systemctl is-active --quiet "${release_watchdog_unit}"; then
      systemctl reset-failed "${release_watchdog_unit}" >/dev/null 2>&1 || true
      release_watchdog_started=0
      return 0
    fi
    sleep 0.1
  done
  return 1
}

# Copy through a root-owned snapshot. Reading the admin-owned stage through a
# lower-privileged tar process prevents a symlink swap during privileged copy.
# Source snapshots can outlive SIGKILL; keeping them on /run exhausts tmpfs
# across interrupted deployments and consumes memory needed by the service.
validate_root_controlled_parent /var/tmp "source snapshot parent"
snapshot="$(mktemp -d /var/tmp/hermes-agent-collaboration.XXXXXX)"
cleanup_snapshot() {
  rm -rf -- "${snapshot}"
  if [[ "${early_recovery_service_stopped}" == 1 ]]; then
    if [[ "${early_recovery_restart_allowed}" == 1 \
        && "${release_retry_stopped}" != 1 ]]; then
      if ! start_and_verify_active; then
        printf '%s\n' \
          "runtime environment recovered but ${service} could not be restarted" >&2
      fi
    else
      printf '%s\n' \
        "release candidate remains stopped pending a marker-aware retry" >&2
    fi
    early_recovery_service_stopped=0
  fi
}
trap cleanup_snapshot EXIT
chown "root:${service_group}" "${snapshot}"
chmod 0750 "${snapshot}"
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
# The staged tree is intentionally private to the uploading admin.  The
# service account nevertheless has to execute the unprivileged runtime-I/O
# helper from the root-owned snapshot.  Normalize only this fixed, validated
# helper path (and its two parent directories) instead of recursively making
# the complete source snapshot readable.
for helper_path in \
    "${snapshot}/deploy" "${snapshot}/deploy/public" \
    "${snapshot}/deploy/public/profile-runtime-io.py"; do
  [[ -e "${helper_path}" && ! -L "${helper_path}" ]] \
    || die "unsafe snapshot helper path: ${helper_path}"
done
chown "root:${service_group}" \
  "${snapshot}/deploy" "${snapshot}/deploy/public" \
  "${snapshot}/deploy/public/profile-runtime-io.py"
chmod 0750 "${snapshot}/deploy" "${snapshot}/deploy/public" \
  "${snapshot}/deploy/public/profile-runtime-io.py"
for relative in "${required[@]}"; do
  [[ -f "${snapshot}/${relative}" && ! -L "${snapshot}/${relative}" ]] || die "unsafe snapshot ${relative}"
done
[[ "$(sha256sum "${snapshot}/${runtime_source_manifest_relative}" | cut -d' ' -f1)" \
    == "${runtime_source_manifest_sha256}" ]] \
  || die "runtime source manifest changed while the root snapshot was created"
[[ "$(sha256sum "${snapshot}/deploy/public/candidate-start-guard.py" | cut -d' ' -f1)" \
    == "${release_start_guard_sha256}" ]] \
  || die "candidate start guard changed while the root snapshot was created"
if [[ "${ios_enabled}" == 1 ]]; then
  for relative in "${ios_optional[@]}"; do
    [[ -f "${snapshot}/${relative}" && ! -L "${snapshot}/${relative}" ]] || die "unsafe snapshot ${relative}"
  done
fi

# Validate the immutable root-owned snapshot that will actually be installed.
# Validating the admin-owned stage before this copy would leave a write window
# in which the staged source could diverge from the checked content.
manifest_version="$("${bootstrap_python_resolved}" -I - "${snapshot}/plugins/collaboration/dashboard/manifest.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("version", ""))
PY
)"
[[ "${manifest_version}" == "${version}" ]] || die "manifest version ${manifest_version@Q} does not match ${version}"
manifest_sha256="$(sha256sum "${snapshot}/plugins/collaboration/dashboard/manifest.json" | cut -d' ' -f1)"
[[ "${manifest_sha256}" =~ ^[0-9a-f]{64}$ ]] || die "manifest SHA-256 is invalid"
"${bootstrap_python_resolved}" -I - \
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
  "${snapshot}/tui_gateway/server.py" \
  "${snapshot}/deploy/public/candidate-start-guard.py" \
  "${snapshot}/deploy/public/runtime-home-guard.py" \
  "${snapshot}/deploy/public/profile-runtime-io.py" <<'PY'
import pathlib, sys
for name in sys.argv[1:]:
    compile(pathlib.Path(name).read_text(encoding="utf-8"), name, "exec")
PY
runtime_compile_assets=()
for relative in "${runtime_service_assets[@]}"; do
  if [[ "${relative}" == *.py ]]; then
    runtime_compile_assets+=("${snapshot}/${relative}")
  fi
done
"${bootstrap_python_resolved}" -I - "${runtime_compile_assets[@]}" <<'PY'
import pathlib, sys
for name in sys.argv[1:]:
    compile(pathlib.Path(name).read_text(encoding="utf-8"), name, "exec")
PY
if [[ "${ios_enabled}" == 1 ]]; then
  "${bootstrap_python_resolved}" -I - "${snapshot}/hermes_cli/account_cleanup.py" \
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
env_mode_normalization_required=0
env_mode_before=""
if [[ "${ios_enabled}" == 1 ]]; then
  [[ -f "${env_file}" && ! -L "${env_file}" ]] || die "restricted Hermes environment file is missing"
  [[ "$(stat -c '%u' "${env_file}")" == 0 ]] || die "Hermes environment file must be root-owned"
  env_mode_before="$(stat -c '%a' "${env_file}")"
  (( 8#${env_mode_before} == 0600 )) || env_mode_normalization_required=1
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
[[ -n "${service_home}" && "${service_home}" == /* ]] \
  || die "Hermes service home is missing or not absolute"
dispatcher_home_default="${service_home}/.hermes/profiles/dispatcher"
run_as_service() {
  "${setpriv_resolved}" \
    --reuid="${service_uid}" --regid="${service_gid}" \
    --clear-groups --no-new-privs -- \
    env -i HOME="${service_home}" USER="${service_user}" \
      LOGNAME="${service_user}" PATH=/usr/sbin:/usr/bin:/sbin:/bin \
      LANG=C.UTF-8 LC_ALL=C.UTF-8 "$@"
}
run_profile_io() {
  run_as_service "${bootstrap_python_resolved}" -I \
    "${snapshot}/deploy/public/profile-runtime-io.py" "$@"
}
run_profile_io_root() {
  # Compatibility overrides can point at root-only files.  This entry point
  # is used only for post-stop snapshots/rollback artifacts; the helper still
  # enforces O_NOFOLLOW, regular-file and identity checks for every path.
  "${bootstrap_python_resolved}" -I \
    "${snapshot}/deploy/public/profile-runtime-io.py" "$@"
}
read_effective_systemd_runtime_home() {
  local effective_home=""
  effective_home="$(
    { systemctl show "${service}" --property=Environment --value 2>/dev/null \
        || true; } \
      | tr ' ' '\n' \
      | sed -n 's/^HERMES_HOME=//p' \
      | tail -n 1
  )"
  printf '%s\n' "${effective_home}"
}
validate_effective_service_profile() {
  local effective_exec_start
  if ! effective_exec_start="$(
      systemctl show "${service}" --property=ExecStart --value 2>/dev/null
    )"; then
    return 1
  fi
  [[ -n "${effective_exec_start}" ]] || return 0
  "${bootstrap_python_resolved}" -I - "${effective_exec_start}" <<'PY'
import re
import sys

command = sys.argv[1]
if re.search(r"(?:^|[\s{;])(?:--profile(?:=|\s)|-p(?:\s|$))", command):
    raise SystemExit("effective ExecStart contains an explicit profile override")
PY
}
systemd_runtime_home="$(read_effective_systemd_runtime_home)"
validate_effective_service_profile \
  || die "effective Hermes service command selects a non-dispatcher profile"
dispatcher_state_root="${HERMES_DISPATCHER_STATE_ROOT:-/var/lib/hermes-dispatcher}"
[[ "${dispatcher_state_root}" == /* && "${dispatcher_state_root}" != "/" \
    && "${dispatcher_state_root}" =~ ^/[A-Za-z0-9._/@+:-]+$ ]] \
  || die "dispatcher state root path is unsafe"
dispatcher_state_root="${dispatcher_state_root%/}"
validate_root_controlled_parent "$(dirname -- "${dispatcher_state_root}")" \
  "dispatcher state root parent"
if [[ -e "${dispatcher_state_root}" || -L "${dispatcher_state_root}" ]]; then
  [[ -d "${dispatcher_state_root}" && ! -L "${dispatcher_state_root}" ]] \
    || die "dispatcher state root is not a directory"
  [[ "$(stat -c '%u' "${dispatcher_state_root}")" == 0 ]] \
    || die "dispatcher state root must be root-owned"
  dispatcher_state_root_mode="$(stat -c '%a' "${dispatcher_state_root}")"
  if (( (8#${dispatcher_state_root_mode} & 8#1000) != 0 )); then
    # A sticky state root is the guarded ensure-managed form (root:service
    # group 01770); only world-writability remains fatal there.
    (( (8#${dispatcher_state_root_mode} & 0002) == 0 )) \
      || die "dispatcher state root must not be world-writable"
  else
    (( (8#${dispatcher_state_root_mode} & 0022) == 0 )) \
      || die "dispatcher state root must not be group/world-writable"
  fi
fi
dispatcher_profiles_root="${dispatcher_state_root}/profiles"
dispatcher_home_default="${dispatcher_profiles_root}/dispatcher"
# Authentication is split into the same root/profile topology as the runtime:
# the profile-local auth store is used for normal provider credentials, while
# the root-level stores are the official Hermes fallback/shared Nous stores.
# Keep the legacy service-home paths only as one-time migration sources.
legacy_global_auth_file="${service_home}/.hermes/auth.json"
legacy_shared_nous_auth_file="${service_home}/.hermes/shared/nous_auth.json"
dispatcher_global_auth_file="${dispatcher_state_root}/auth.json"
dispatcher_shared_auth_dir="${dispatcher_state_root}/shared"
dispatcher_shared_nous_auth_file="${dispatcher_shared_auth_dir}/nous_auth.json"
profile_migration_journal="${release_candidate_marker_dir}/dispatcher-migration.json"
[[ "${profile_migration_journal}" =~ ^/[A-Za-z0-9._/@+:-]+$ \
    && "${profile_migration_journal}" != "${release_candidate_marker}" \
    && "${profile_migration_journal}" != "${release_start_lease}" ]] \
  || die "dispatcher migration journal path is unsafe"
profile_migration_journal_state="$(
  "${bootstrap_python_resolved}" -I \
    "${stage_root}/deploy/public/runtime-home-guard.py" journal-inspect \
    "${profile_migration_journal}" "${release_candidate_marker_dir_identity}"
)" || die "dispatcher migration journal validation failed"
case "${profile_migration_journal_state}" in
  absent|prepared|copied) ;;
  *) die "dispatcher migration journal returned an invalid state" ;;
esac
runtime_seal_journal="${release_candidate_marker_dir}/runtime-home-seal.json"
[[ "${runtime_seal_journal}" != "${release_candidate_marker}" \
    && "${runtime_seal_journal}" != "${release_start_lease}" \
    && "${runtime_seal_journal}" != "${profile_migration_journal}" ]] \
  || die "runtime seal journal path is unsafe"
runtime_seal_journal_state="absent"
if [[ -e "${runtime_seal_journal}" || -L "${runtime_seal_journal}" ]]; then
  runtime_seal_journal_state="present"
fi
configured_runtime_home="${HERMES_HOME_DIR:-${systemd_runtime_home:-${env_runtime_home:-${dispatcher_home_default}}}}"
runtime_home="${configured_runtime_home}"
if [[ "${release_candidate_marker_state}" == present ]]; then
  # A pending candidate is already authoritative for its recorded profile.
  # Finish that exact transaction first; a subsequent marker-free run moves
  # legacy service-home state into the root-anchored dispatcher profile.
  runtime_home="$(release_candidate_marker_action runtime)" \
    || die "pending release candidate runtime profile is invalid"
elif [[ "${release_candidate_marker_state}" == interrupted ]]; then
  # The temporary root-owned marker proves that an in-place candidate crossed
  # its point of no return, but it does not contain a complete payload from
  # which another profile can safely be selected. Continue only on the exact
  # home currently loaded by systemd; a marker-free transaction may migrate it
  # to the managed dispatcher root later.
  [[ -n "${systemd_runtime_home}" ]] \
    || die "interrupted release candidate has no effective systemd runtime home"
  runtime_home="${systemd_runtime_home}"
fi
profile_migration_journal_source=""
profile_migration_journal_destination=""
profile_migration_journal_txid=""
if [[ "${profile_migration_journal_state}" != absent ]]; then
  profile_migration_journal_source="$(
    "${bootstrap_python_resolved}" -I \
      "${stage_root}/deploy/public/runtime-home-guard.py" journal-field \
      "${profile_migration_journal}" "${release_candidate_marker_dir_identity}" source
  )" || die "dispatcher migration source is invalid"
  profile_migration_journal_destination="$(
    "${bootstrap_python_resolved}" -I \
      "${stage_root}/deploy/public/runtime-home-guard.py" journal-field \
      "${profile_migration_journal}" "${release_candidate_marker_dir_identity}" destination
  )" || die "dispatcher migration destination is invalid"
  profile_migration_journal_txid="$(
    "${bootstrap_python_resolved}" -I \
      "${stage_root}/deploy/public/runtime-home-guard.py" journal-field \
      "${profile_migration_journal}" "${release_candidate_marker_dir_identity}" txid
  )" || die "dispatcher migration transaction identity is invalid"
  if [[ "${release_candidate_marker_state}" == absent ]]; then
    runtime_home="${profile_migration_journal_destination}"
  elif [[ "${runtime_home}" != "${profile_migration_journal_destination}" ]]; then
    die "pending release candidate and dispatcher migration journal disagree"
  fi
fi
[[ -n "${runtime_home}" && "${runtime_home}" == /* ]] \
  || die "Hermes runtime home must be an absolute path"
runtime_home="${runtime_home%/}"
[[ "${runtime_home}" != "/" ]] \
  || die "Hermes dispatcher profile path must not be the filesystem root"
[[ "${runtime_home}" =~ ^/[A-Za-z0-9._/@+:-]+$ ]] \
  || die "Hermes dispatcher profile path contains unsupported systemd characters"
path_overlaps() {
  local left="${1%/}" right="${2%/}"
  [[ "${left}" == "${right}" || "${left}" == "${right}/"* \
      || "${right}" == "${left}/"* ]]
}
validate_pending_migration_marker() {
  # A strict root-owned marker is the only proof that a non-empty dispatcher
  # leaf is the already-published fork of the legacy worker home for this
  # exact release commit. Anything looser would let a foreign profile be
  # resumed instead of merged.
  local marker="$1"
  [[ -f "${marker}" && ! -L "${marker}" ]] || return 1
  [[ "$(stat -c '%u:%g:%a' "${marker}")" == "0:0:600" ]] || return 1
  "${bootstrap_python_resolved}" -I - "${marker}" "${release_commit}" <<'PY'
import pathlib
import re
import sys

marker = pathlib.Path(sys.argv[1])
commit = sys.argv[2]
content = marker.read_text(encoding="utf-8")
match = re.fullmatch(r"([0-9a-f]{40}):([0-9a-f]+):([0-9]+)\n", content)
if match is None:
    raise SystemExit("pending dispatcher migration marker is malformed")
if match.group(1) != commit:
    raise SystemExit(
        "pending dispatcher migration marker names a different release"
    )
PY
}
legacy_dispatcher_home="${service_home}/.hermes/profiles/dbb3-worker"
worker_homes=(
  "${legacy_dispatcher_home}"
  "${service_home}/.hermes/profiles/hk-worker"
  "/mnt/d/Hermes/home/profiles/pc-worker"
)
explicit_runtime_home="${HERMES_HOME_DIR:-}"
runtime_home_source="configured"
if [[ -z "${explicit_runtime_home}" ]]; then
  if [[ -n "${systemd_runtime_home}" ]]; then
    runtime_home_source="systemd"
  elif [[ -n "${env_runtime_home}" ]]; then
    runtime_home_source="environment-file"
  else
    runtime_home_source="default"
  fi
fi
if [[ "${profile_migration_journal_state}" != absent ]]; then
  runtime_home_source="migration-journal"
fi
legacy_runtime_home="${profile_migration_journal_source}"
if [[ "${profile_migration_journal_state}" == absent \
    && "${release_candidate_marker_state}" == absent \
    && -z "${explicit_runtime_home}" \
    && "${runtime_home}" != "${dispatcher_home_default}" ]]; then
  # Every new production transaction converges on a dispatcher leaf whose
  # entire ancestor chain is controlled by root. Existing service-home or
  # custom systemd paths are copied once as legacy input; they are never used
  # as a privileged write target again.
  legacy_runtime_home="${runtime_home}"
  runtime_home="${dispatcher_home_default}"
  runtime_home_source="legacy-profile"
fi
for worker_home in "${worker_homes[@]}"; do
  candidate_profile_path="${legacy_runtime_home:-${runtime_home}}"
  ! path_overlaps "${candidate_profile_path}" "${worker_home}" \
    || {
      if [[ -n "${explicit_runtime_home}" ]]; then
        die "dispatcher runtime home cannot overlap worker profile: ${worker_home}"
      fi
      # Before the four-role layout the public service was sometimes pointed
      # at the local dbb3-worker profile. Only that one exact legacy path may
      # be copied into the dispatcher profile. HK and every other worker path
      # remain hard failures so a real worker can never be co-opted.
      if [[ "${runtime_home}" == "${legacy_dispatcher_home}" \
          || "${candidate_profile_path}" == "${legacy_dispatcher_home}" ]] \
          && [[ "${worker_home}" == "${legacy_dispatcher_home}" ]]; then
        legacy_runtime_home="${candidate_profile_path}"
        runtime_home="${dispatcher_home_default}"
        runtime_home_source="legacy-profile"
        break
      fi
      die "dispatcher runtime home cannot overlap worker profile: ${worker_home}"
    }
done
if [[ "${profile_migration_journal_state}" == absent \
    && "${release_candidate_marker_state}" == absent \
    && -z "${explicit_runtime_home}" \
    && -z "${legacy_runtime_home}" \
    && "${runtime_home}" == "${dispatcher_home_default}" \
    && -d "${legacy_dispatcher_home}" && ! -L "${legacy_dispatcher_home}" ]]; then
  # A previous transaction may have republished the systemd binding on the
  # dispatcher leaf and then died before the copy ran. The canonical legacy
  # worker profile is still the only allowed migration source, so while it
  # exists the migration is pending: an empty leaf is completed by a fresh
  # copy, and a leaf carrying this release's strict pending marker is
  # completed by the resume path. Without either signal the dispatcher leaf
  # is simply managed in place and the legacy profile is left alone.
  pending_legacy_leaf_entry=""
  if [[ -d "${runtime_home}" && ! -L "${runtime_home}" ]]; then
    pending_legacy_leaf_entry="$(find "${runtime_home}" -mindepth 1 -maxdepth 1 \
      -print -quit 2>/dev/null)" || pending_legacy_leaf_entry="inspect-failed"
  fi
  if [[ "${pending_legacy_leaf_entry}" == "inspect-failed" ]]; then
    die "dispatcher profile target could not be inspected: ${runtime_home}"
  fi
  if [[ -z "${pending_legacy_leaf_entry}" ]]; then
    legacy_runtime_home="${legacy_dispatcher_home}"
  else
    pending_legacy_marker="$(find "${runtime_home}" -maxdepth 1 \
      -name '.hermes-dispatcher-migration.*' -print -quit 2>/dev/null)" \
      || die "dispatcher profile target could not be inspected: ${runtime_home}"
    if [[ -n "${pending_legacy_marker}" ]] \
        && validate_pending_migration_marker "${pending_legacy_marker}"; then
      legacy_runtime_home="${legacy_dispatcher_home}"
    fi
  fi
fi
[[ -n "${runtime_home}" && "${runtime_home}" == /* ]] \
  || die "Hermes dispatcher profile path must be absolute"
runtime_home="${runtime_home%/}"
if [[ "${release_candidate_marker_state}" == present ]]; then
  release_candidate_marker_action validate "${runtime_home}" \
    || die "pending release candidate belongs to a different dispatcher profile"
fi
runtime_topology_mode="custom-managed"
if [[ "${release_candidate_marker_state}" != absent ]]; then
  runtime_topology_mode="pending-existing"
elif [[ "${runtime_home}" == "${dispatcher_home_default}" ]]; then
  runtime_topology_mode="managed-dispatcher"
fi
profile_parent="$(dirname -- "${dispatcher_home_default}")"
runtime_home_parent="$(dirname -- "${runtime_home}")"
for profile_path in "${profile_parent}" "${runtime_home_parent}" "${runtime_home}"; do
  [[ "${profile_path}" == /* ]] || die "Hermes profile path must be absolute"
  probe="${profile_path}"
  while [[ ! -e "${probe}" && ! -L "${probe}" ]]; do
    next_probe="$(dirname -- "${probe}")"
    [[ "${next_probe}" != "${probe}" ]] || die "Hermes profile path has no existing ancestor"
    probe="${next_probe}"
  done
  [[ ! -L "${probe}" ]] || die "Hermes profile path has a symlink ancestor: ${probe}"
  resolved_probe="$(realpath -e -- "${probe}")" \
    || die "Hermes profile path ancestor cannot be resolved: ${probe}"
  [[ "${resolved_probe}" == "${probe}" ]] \
    || die "Hermes profile path ancestor resolves outside its lexical path: ${probe}"
done
case "${runtime_topology_mode}" in
  managed-dispatcher)
    validate_root_controlled_parent "$(dirname -- "${dispatcher_state_root}")" \
      "dispatcher state root parent"
    ;;
  custom-managed)
    [[ -d "${runtime_home_parent}" && ! -L "${runtime_home_parent}" ]] \
      || die "explicit dispatcher profile parent must already exist"
    validate_root_controlled_parent "${runtime_home_parent}" \
      "explicit dispatcher profile parent"
    ;;
  pending-existing)
    [[ -d "${runtime_home}" && ! -L "${runtime_home}" ]] \
      || die "pending candidate dispatcher profile is missing or unsafe"
    ;;
  *) die "unknown dispatcher topology mode" ;;
esac
profile_migration_required=0
profile_migration_resumed=0
if [[ -n "${legacy_runtime_home}" && ( -e "${legacy_runtime_home}" || -L "${legacy_runtime_home}" ) ]]; then
  [[ -d "${legacy_runtime_home}" && ! -L "${legacy_runtime_home}" ]] \
    || die "legacy dispatcher profile is missing or unsafe: ${legacy_runtime_home}"
  case "${profile_migration_journal_state}" in
    absent|prepared) profile_migration_required=1 ;;
    copied)
      profile_migration_resumed=1
      profile_migration_required=0
      ;;
  esac
fi
if [[ -e "${runtime_home}" || -L "${runtime_home}" ]]; then
  [[ -d "${runtime_home}" && ! -L "${runtime_home}" ]] \
    || die "dispatcher profile target is not a directory: ${runtime_home}"
  existing_dispatcher_entry="$(find "${runtime_home}" -mindepth 1 -maxdepth 1 -print -quit)" \
    || die "dispatcher profile target could not be inspected: ${runtime_home}"
  if [[ -n "${existing_dispatcher_entry}" ]]; then
    pending_dispatcher_marker="$(find "${runtime_home}" -maxdepth 1 \
      -name '.hermes-dispatcher-migration.*' -print -quit)" \
      || die "dispatcher profile target could not be inspected: ${runtime_home}"
    if [[ "${profile_migration_required}" == 1 \
        && "${profile_migration_journal_state}" != prepared ]]; then
      # A non-empty target only resumes when the external journal already
      # recorded the copy or a strict root-owned pending-migration marker
      # proves this leaf is the published fork for this release; anything else
      # would be an implicit merge of foreign worker state.
      if [[ -z "${pending_dispatcher_marker}" ]] \
          || ! validate_pending_migration_marker "${pending_dispatcher_marker}"; then
        die "dispatcher profile target is non-empty; refusing an implicit merge: ${runtime_home}"
      fi
    elif [[ -n "${pending_dispatcher_marker}" ]] \
        && validate_pending_migration_marker "${pending_dispatcher_marker}"; then
      # The binding already points at this leaf and no legacy redirect is
      # pending, so a strict pending-migration marker only marks a fork whose
      # publish already completed; resume it in place by consuming the token.
      rm -f -- "${pending_dispatcher_marker}" \
        || die "pending dispatcher migration marker could not be consumed: ${pending_dispatcher_marker}"
    fi
  fi
fi
runtime_home_preexisting=0
if [[ -e "${runtime_home}" || -L "${runtime_home}" ]]; then
  runtime_home_preexisting=1
fi
legacy_runtime_identity=""
if [[ -d "${legacy_runtime_home}" && ! -L "${legacy_runtime_home}" ]]; then
  legacy_runtime_identity="$(stat -c '%d:%i' "${legacy_runtime_home}")"
fi
profile_parent_identity=""
if [[ -d "${profile_parent}" && ! -L "${profile_parent}" ]]; then
  profile_parent_identity="$(stat -c '%d:%i' "${profile_parent}")"
fi
runtime_home_identity=""
if [[ "${runtime_home_preexisting}" == 1 \
    && -d "${runtime_home}" && ! -L "${runtime_home}" ]]; then
  runtime_home_identity="$(stat -c '%d:%i' "${runtime_home}")"
fi
# Rotate an absent startup condition for every deployment. The current service
# keeps running until it is quiesced, while any crash or external restart after
# daemon-reload remains blocked until the complete candidate is authoritative.
dispatcher_ready_token="${release_commit}:${BASHPID}:${RANDOM}"
dispatcher_ready_name=".hermes-dispatcher-ready.${release_commit:0:12}.${BASHPID}.${RANDOM}"
dispatcher_ready_path="${release_candidate_marker_dir}/${dispatcher_ready_name}"
[[ "${dispatcher_ready_path}" =~ ^/[A-Za-z0-9._/@+:-]+$ \
    && "${dispatcher_ready_path}" != "${release_candidate_marker}" \
    && "${dispatcher_ready_path}" != "${release_start_guard}" \
    && "${dispatcher_ready_path}" != "${release_start_lease}" ]] \
  || die "dispatcher startup barrier path is unsafe"
profile_rebind_required=0
if [[ "${systemd_runtime_home}" != "${runtime_home}" \
    || "${env_runtime_home}" != "${runtime_home}" \
    || "${profile_migration_required}" == 1 ]]; then
  profile_rebind_required=1
fi
# Even an unchanged binding needs a fresh absent startup barrier while live
# code and state are replaced in place.
profile_rebind_required=1
[[ "${service}" =~ ^[A-Za-z0-9_.@:-]+\.service$ ]] \
  || die "Hermes service name is unsafe"
profile_dropin_target="${HERMES_AGENT_PROFILE_DROPIN:-/etc/systemd/system/${service}.d/10-hermes-dispatcher-profile.conf}"
[[ "${profile_dropin_target}" == /* ]] \
  || die "Hermes profile drop-in path must be absolute"
[[ "$(basename -- "${profile_dropin_target}")" == "10-hermes-dispatcher-profile.conf" \
    && "$(basename -- "$(dirname -- "${profile_dropin_target}")")" == "${service}.d" ]] \
  || die "Hermes profile drop-in path must be the service dispatcher drop-in"
profile_dropin_dir="$(dirname -- "${profile_dropin_target}")"
if [[ "${profile_rebind_required}" == 1 \
    || "${env_mode_normalization_required}" == 1 ]]; then
  validate_root_controlled_parent "$(dirname -- "${env_file}")" \
    "Hermes environment file"
fi
if [[ "${profile_rebind_required}" == 1 ]]; then
  validate_root_controlled_parent "${profile_dropin_dir}" \
    "Hermes profile drop-in"
  if [[ -e "${env_file}" || -L "${env_file}" ]]; then
    [[ -f "${env_file}" && ! -L "${env_file}" ]] \
      || die "Hermes environment file is missing or unsafe"
    [[ "$(stat -c '%u' "${env_file}")" == 0 ]] \
      || die "Hermes environment file must be root-owned"
    env_file_mode="$(stat -c '%a' "${env_file}")"
    (( (8#${env_file_mode} & 0022) == 0 )) \
      || die "Hermes environment file must not be group/world-writable"
  fi
  if [[ -e "${profile_dropin_dir}" || -L "${profile_dropin_dir}" ]]; then
    [[ -d "${profile_dropin_dir}" && ! -L "${profile_dropin_dir}" ]] \
      || die "Hermes profile drop-in directory is unsafe"
    [[ "$(stat -c '%u' "${profile_dropin_dir}")" == 0 ]] \
      || die "Hermes profile drop-in directory must be root-owned"
  fi
  if [[ -e "${profile_dropin_target}" || -L "${profile_dropin_target}" ]]; then
    [[ -f "${profile_dropin_target}" && ! -L "${profile_dropin_target}" ]] \
      || die "Hermes profile drop-in file is missing or unsafe"
    [[ "$(stat -c '%u' "${profile_dropin_target}")" == 0 ]] \
      || die "Hermes profile drop-in file must be root-owned"
    profile_dropin_mode="$(stat -c '%a' "${profile_dropin_target}")"
    (( (8#${profile_dropin_mode} & 0022) == 0 )) \
      || die "Hermes profile drop-in file must not be group/world-writable"
  fi
fi
state_target="${HERMES_COLLABORATION_STATE_FILE:-${runtime_home}/collaboration/single.json}"
config_target="${HERMES_CONFIG_FILE:-${runtime_home}/config.yaml}"
config_resolution_target="${config_target}"
migrated_config_rewrite_required=0
if [[ "${profile_migration_required}" == 1 && -z "${HERMES_CONFIG_FILE:-}" \
    && ! -e "${config_target}" && -f "${legacy_runtime_home}/config.yaml" ]]; then
  # Resolve legacy relative iOS database paths against the new dispatcher
  # home while still reading the configuration that is being migrated.
  config_resolution_target="${legacy_runtime_home}/config.yaml"
  migrated_config_rewrite_required=1
fi
ios_supervisor_target="${runtime_home}/ios-mcp-supervisor.db"
ios_database_target="${runtime_home}/ios-intelligence.db"
mobile_auth_target="${runtime_home}/dashboard/mobile-auth.db"
cloud_files_database_target="${runtime_home}/collaboration/account-files/library.sqlite3"
managed_installations_database_target="${runtime_home}/managed-installations.db"
managed_nodes_target="${runtime_home}/managed-nodes.json"
  managed_node_token_file="${HERMES_MANAGED_NODE_TOKEN_FILE:-/etc/hermes-agent/dbb3-status-token}"
managed_installation_token_file="${HERMES_MANAGED_INSTALLATION_TOKEN_FILE:-/etc/hermes-agent/managed-installation-token}"
hk_recovery_token_file="${HERMES_HK_RECOVERY_TOKEN_FILE:-/etc/hermes-agent/hk-recovery-token}"
[[ "${managed_node_token_file}" != "${managed_installation_token_file}" ]] \
  || die "status and installation credentials must use different files"
managed_credential_args=(
  status "${managed_node_token_file}"
  installation "${managed_installation_token_file}"
)
if [[ "${hk_enabled}" == 1 ]]; then
  managed_credential_args+=("HK recovery" "${hk_recovery_token_file}")
fi
# Bind validation and metadata changes to already-open inodes. This prevents a
# custom credential path or a replaced path component from redirecting root's
# permission changes to an unrelated file.
"${bootstrap_python_resolved}" -I - "${service_group}" "${token_file}" \
  "${managed_credential_args[@]}" <<'PY' \
  || die "managed credential validation failed"
import errno
import grp
import os
import stat
import sys

service_group = sys.argv[1]
connector_path = sys.argv[2]
raw_credentials = sys.argv[3:]
if len(raw_credentials) not in {4, 6}:
    raise SystemExit("exactly two or three managed credentials are required")
try:
    service_gid = grp.getgrnam(service_group).gr_gid
except KeyError:
    raise SystemExit(f"service group does not exist: {service_group}") from None

directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
path_flags = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
reopen_flags = os.O_RDONLY | os.O_CLOEXEC
opened = []
handles = []


def fail(label, message):
    raise SystemExit(f"{label} credential {message}")


def open_parent(path, label):
    if not os.path.isabs(path):
        fail(label, "path must be absolute")
    components = [part for part in path.split("/") if part]
    if not components or any(part in {".", ".."} for part in components):
        fail(label, "path is unsafe")
    directory_fd = os.open("/", directory_flags)
    try:
        for component in components[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
            metadata = os.fstat(directory_fd)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0:
                fail(label, "parent directories must be root-owned directories")
            permissions = stat.S_IMODE(metadata.st_mode)
            if permissions & 0o022 and not permissions & stat.S_ISVTX:
                fail(label, "parent directories must not be writable by other users")
        return directory_fd, components[-1]
    except BaseException:
        os.close(directory_fd)
        raise


def open_regular(parent_fd, filename, label):
    path_fd = os.open(filename, path_flags, dir_fd=parent_fd)
    try:
        path_metadata = os.fstat(path_fd)
        if not stat.S_ISREG(path_metadata.st_mode):
            fail(label, "must be a regular file")
        credential_fd = os.open(f"/proc/self/fd/{path_fd}", reopen_flags)
        metadata = os.fstat(credential_fd)
        if (metadata.st_dev, metadata.st_ino) != (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ):
            os.close(credential_fd)
            fail(label, "inode changed while opening")
        return credential_fd, metadata
    finally:
        os.close(path_fd)


def reject_access_acl(credential_fd, label):
    try:
        names = os.listxattr(credential_fd)
    except OSError as exc:
        unsupported = {errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
        if exc.errno in unsupported:
            return
        raise
    if "system.posix_acl_access" in names:
        fail(label, "must not carry a POSIX access ACL")


try:
    seen_inodes = set()
    seen_payloads = set()
    for label, path in zip(raw_credentials[::2], raw_credentials[1::2]):
        parent_fd, filename = open_parent(path, label)
        try:
            credential_fd, metadata = open_regular(parent_fd, filename, label)
        except BaseException:
            os.close(parent_fd)
            raise
        handles.append((parent_fd, credential_fd))
        reject_access_acl(credential_fd, label)
        if metadata.st_uid != 0:
            fail(label, "must be root-owned")
        if metadata.st_nlink != 1:
            fail(label, "must have exactly one hard link")
        permissions = stat.S_IMODE(metadata.st_mode)
        if permissions not in {0o400, 0o440, 0o600, 0o640}:
            fail(label, "mode must already be restricted to owner/group read")
        if permissions & 0o040 and metadata.st_gid not in {0, service_gid}:
            fail(label, "must not be readable by an unrelated group")
        with os.fdopen(os.dup(credential_fd), "rb") as stream:
            payload = stream.read(4098)
        if not payload.endswith(b"\n") or b"\n" in payload[:-1] or b"\r" in payload:
            fail(label, "must have one newline-terminated line")
        if not 32 <= len(payload) - 1 <= 4096:
            fail(label, "length must be 32..4096 characters")
        inode = (metadata.st_dev, metadata.st_ino)
        if inode in seen_inodes:
            fail(label, "must use a dedicated file")
        if payload in seen_payloads:
            fail(label, "must use a dedicated value")
        seen_inodes.add(inode)
        seen_payloads.add(payload)
        opened.append((label, parent_fd, filename, credential_fd, inode, payload))

    connector_parent_fd, connector_filename = open_parent(connector_path, "connector")
    try:
        connector_fd, connector_metadata = open_regular(
            connector_parent_fd, connector_filename, "connector"
        )
    except BaseException:
        os.close(connector_parent_fd)
        raise
    handles.append((connector_parent_fd, connector_fd))
    with os.fdopen(os.dup(connector_fd), "rb") as stream:
        connector_payload = stream.read(4098)
    connector_inode = (connector_metadata.st_dev, connector_metadata.st_ino)
    if connector_inode in seen_inodes:
        fail("connector", "must use a dedicated file")
    if connector_payload.rstrip(b"\n") + b"\n" in seen_payloads:
        fail("connector", "must use a dedicated value")

    for label, parent_fd, filename, credential_fd, inode, _ in opened:
        os.fchown(credential_fd, 0, service_gid)
        os.fchmod(credential_fd, 0o640)
        reject_access_acl(credential_fd, label)
        metadata = os.fstat(credential_fd)
        path_metadata = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        if (metadata.st_dev, metadata.st_ino) != inode:
            fail(label, "inode changed during normalization")
        if (path_metadata.st_dev, path_metadata.st_ino) != inode:
            fail(label, "path changed during normalization")
        if (
            not stat.S_ISREG(path_metadata.st_mode)
            or path_metadata.st_uid != 0
            or path_metadata.st_gid != service_gid
            or stat.S_IMODE(path_metadata.st_mode) != 0o640
        ):
            fail(label, "metadata normalization failed")
finally:
    for parent_fd, credential_fd in handles:
        os.close(credential_fd)
        os.close(parent_fd)
PY
managed_credential_files=(
  "${managed_node_token_file}"
  "${managed_installation_token_file}"
)
if [[ "${hk_enabled}" == 1 ]]; then
  managed_credential_files+=("${hk_recovery_token_file}")
fi
for credential_file in "${managed_credential_files[@]}"; do
  runuser -u "${service_user}" -g "${service_group}" -- test -r "${credential_file}" \
    || die "managed-node credential is not readable by ${service_user}"
done
ios_database_target="$(runuser -u "${service_user}" -g "${service_group}" -- \
  "${runtime_python}" - "${config_resolution_target}" "${runtime_home}" \
  "${service_home}" "${legacy_runtime_home}" "${profile_migration_required}" <<'PY'
import pathlib
import sys

import yaml

config_path = pathlib.Path(sys.argv[1])
runtime_home = pathlib.Path(sys.argv[2]).resolve(strict=False)
service_home = pathlib.Path(sys.argv[3]).resolve(strict=False)
legacy_runtime_home = (
    pathlib.Path(sys.argv[4]).resolve(strict=False) if sys.argv[4] else None
)
profile_migration = sys.argv[5] == "1"
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
path = path.resolve(strict=False)
if profile_migration and legacy_runtime_home is not None:
    try:
        relative = path.relative_to(legacy_runtime_home)
    except ValueError:
        pass
    else:
        path = (runtime_home / relative).resolve(strict=False)
try:
    path.relative_to(runtime_home)
except ValueError:
    raise SystemExit("iOS database path is outside the dispatcher profile")
if path.suffix not in {".db", ".sqlite", ".sqlite3"}:
    path = path / "ios-intelligence.db"
print(path)
PY
)"
[[ -n "${ios_database_target}" ]] || die "iOS intelligence database path is empty"
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
  "${bootstrap_python_resolved}" -I - "${output}" "${connector_id}" "${require_identity}" <<'PY'
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
  "${bootstrap_python_resolved}" -I - "${output}" <<'PY'
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
    && "${bootstrap_python_resolved}" -I - "${output}" "${version}" "${manifest_sha256}" "${connector_id}" <<'PY'
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
[[ "${backup_root}" == /* && "${backup_root}" != "/" ]] \
  || die "Hermes backup root must be an absolute non-root path"
if [[ -e "${backup_root}" || -L "${backup_root}" ]]; then
  [[ -d "${backup_root}" && ! -L "${backup_root}" ]] \
    || die "Hermes backup root is missing or unsafe"
fi
validate_root_controlled_parent "$(dirname -- "${backup_root}")" \
  "Hermes backup root"
if [[ ! -e "${backup_root}" && ! -L "${backup_root}" ]]; then
  # Runtime snapshots are written by the service-user helper.  The root of
  # the bounded backup namespace therefore needs search permission for the
  # service group; the sticky bit prevents that group from deleting another
  # transaction while still allowing the helper to create its own snapshot
  # files. Sensitive root-only material remains below the 0700
  # ``service``/``release`` subdirectories created for each transaction.
  install -d -o root -g "${service_group}" -m 1770 "${backup_root}"
fi
validate_root_controlled_parent "${backup_root}" "Hermes backup root"
[[ "$(stat -c '%u' "${backup_root}")" == 0 ]] \
  || die "Hermes backup root must be root-owned"
chown "root:${service_group}" "${backup_root}"
chmod 1770 "${backup_root}"
backup_root_identity="$(stat -c '%d:%i' "${backup_root}")"

# Venv candidates and rollbacks are reclaimed only by recover_venv_swap, which
# verifies the persistent journal and recorded inodes. This cleanup is limited
# to installer staging directories whose contents are never live runtime state.
reclaim_stale_runtime_artifacts() {
  local artifact
  while IFS= read -r -d '' artifact; do
    case "${artifact}" in
      "${target_root}"/.collaboration-install.*) ;;
      *) die "refusing to reclaim unexpected runtime artifact: ${artifact}" ;;
    esac
    [[ -d "${artifact}" && ! -L "${artifact}" ]] \
      || die "stale runtime artifact is unsafe: ${artifact}"
    rm -rf -- "${artifact}"
  done < <(
    find "${target_root}" -mindepth 1 -maxdepth 1 -type d \
      -name '.collaboration-install.*' \
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
runtime_disk_path="${runtime_home}"
while [[ ! -d "${runtime_disk_path}" ]]; do
  runtime_disk_parent="$(dirname -- "${runtime_disk_path}")"
  [[ "${runtime_disk_parent}" != "${runtime_disk_path}" ]] \
    || die "Hermes runtime home has no existing filesystem ancestor"
  runtime_disk_path="${runtime_disk_parent}"
done
reclaim_runtime_disk_pressure "${runtime_disk_path}" "${backup_root}"
backup="$(mktemp -d "${backup_root}/collaboration-${version}-${stamp}.XXXXXX")"
chown "root:${service_group}" "${backup}"
chmod 1770 "${backup}"
runtime_home_created=0
runtime_home_created_identity=""
install -d -o root -g root -m 0700 "${backup}/service"
install -d -o "${service_user}" -g "${service_group}" -m 0700 \
  "${backup}/state" "${backup}/config"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${plugin_target}"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${plugin_target}/dist"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${target_root}/agent"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${target_root}/hermes_cli/dashboard_auth"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${target_root}/hermes_services"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${target_root}/tui_gateway"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${target_root}/tools"
mkdir -p \
  "${backup}/plugins/collaboration/dashboard/dist" \
  "${backup}/agent" \
  "${backup}/hermes_cli/dashboard_auth" \
  "${backup}/hermes_services" \
  "${backup}/tools" \
  "${backup}/tui_gateway" \
  "${backup}/nginx" \
  "${backup}/release"
for relative in "${runtime_service_assets[@]}"; do
  destination_parent="$(dirname "${target_root}/${relative}")"
  backup_parent="$(dirname "${backup}/${relative}")"
  [[ ! -L "${destination_parent}" ]] || die "unsafe runtime destination ${destination_parent}"
  if [[ "${destination_parent}" == "${target_root}" ]]; then
    [[ -d "${destination_parent}" \
        && "$(stat -c '%u:%d:%i' "${destination_parent}")" == \
          "0:${target_root_identity}" ]] \
      || die "runtime source preparation changed the root-owned target"
  else
    install -d -o "${service_user}" -g "${service_group}" -m 0755 \
      "${destination_parent}"
  fi
  mkdir -p "${backup_parent}"
done
# Runtime snapshots are written by the service-user helper.  Make every
# destination directory in this bounded, freshly-created backup tree
# service-writable without recursively touching any pre-existing backup.
for backup_runtime_dir in \
    "${backup}/state" "${backup}/config" \
    "${backup}/plugins" "${backup}/plugins/collaboration" \
    "${backup}/plugins/collaboration/dashboard" \
    "${backup}/plugins/collaboration/dashboard/dist" \
    "${backup}/agent" "${backup}/hermes_cli" \
    "${backup}/hermes_cli/dashboard_auth" "${backup}/hermes_services" \
    "${backup}/tools" "${backup}/tui_gateway"; do
  [[ -d "${backup_runtime_dir}" && ! -L "${backup_runtime_dir}" ]] \
    || die "runtime backup directory is unsafe: ${backup_runtime_dir}"
  chown "${service_user}:${service_group}" "${backup_runtime_dir}"
  chmod 0700 "${backup_runtime_dir}"
done
while IFS= read -r -d '' backup_runtime_dir; do
  [[ ! -L "${backup_runtime_dir}" ]] \
    || die "runtime backup directory is a symlink: ${backup_runtime_dir}"
  chown "${service_user}:${service_group}" "${backup_runtime_dir}"
  chmod 0700 "${backup_runtime_dir}"
done < <(
  find "${backup}" -mindepth 1 -type d \
    ! -path "${backup}/service" ! -path "${backup}/service/*" \
    ! -path "${backup}/nginx" ! -path "${backup}/nginx/*" \
    ! -path "${backup}/release" ! -path "${backup}/release/*" \
    -print0
)

backup_one() {
  local source="$1" destination="$2" mode="${3:-0600}"
  # Most runtime files are service-owned and should never be opened by root
  # during a backup.  An operator may, however, point a compatibility state
  # override at a root-only path (the deployment harness does this too).  In
  # that case the service helper cannot traverse the source namespace; use
  # the root-only backup primitive after the service has been quiesced.
  local source_uid=""
  if [[ -f "${source}" && ! -L "${source}" ]]; then
    source_uid="$(stat -c '%u' "${source}" 2>/dev/null || true)"
  fi
  if [[ "${source}" == "${runtime_home}/"* \
      || "${source}" == "${target_root}/"* \
      || ( -n "${legacy_runtime_home}" \
        && "${source}" == "${legacy_runtime_home}/"* ) ]] \
      && [[ "${source_uid}" == "${service_uid}" ]]; then
    run_profile_io backup-file "${source}" "${destination}" "${mode}"
    return
  fi
  local temporary="${destination}.new.$$"
  rm -f -- "${temporary}"
  if [[ -e "${source}" || -L "${source}" ]]; then
    [[ ! -L "${source}" ]] || die "refusing to back up symlink ${source}"
    cp -a -- "${source}" "${temporary}"
    mv -f -- "${temporary}" "${destination}"
    if [[ -f "${source}" && ! -L "${source}" ]]; then
      chmod "${mode}" "${destination}" 2>/dev/null || true
    fi
  else
    : >"${destination}.missing"
  fi
}
if [[ "${profile_rebind_required}" == 1 \
    || "${env_mode_normalization_required}" == 1 ]]; then
  backup_one "${env_file}" "${backup}/service/hermes-agent.env"
fi
if [[ "${profile_rebind_required}" == 1 ]]; then
  backup_one "${profile_dropin_target}" "${backup}/service/profile-dropin.conf"
fi
backup_sqlite() {
  local source="$1" destination="$2"
  local source_uid=""
  if [[ -f "${source}" && ! -L "${source}" ]]; then
    source_uid="$(stat -c '%u' "${source}" 2>/dev/null || true)"
  fi
  if [[ "${source_uid}" == "${service_uid}" ]]; then
    run_profile_io snapshot-sqlite "${source}" "${destination}"
  else
    run_profile_io_root snapshot-sqlite "${source}" "${destination}"
  fi
}
backup_runtime_sqlite_tree() {
  local source_root="$1" destination_root="$2"
  local source_uid=""
  if [[ -d "${source_root}" && ! -L "${source_root}" ]]; then
    source_uid="$(stat -c '%u' "${source_root}" 2>/dev/null || true)"
  fi
  if [[ "${source_uid}" == "${service_uid}" ]]; then
    run_profile_io snapshot-tree "${source_root}" "${destination_root}"
  else
    run_profile_io_root snapshot-tree "${source_root}" "${destination_root}"
  fi
}
restore_runtime_sqlite_tree() {
  local snapshot_root="$1" destination_root="$2"
  run_profile_io_root restore-tree "${snapshot_root}" "${destination_root}"
}
legacy_profile_backup="${backup}/state/legacy-profile"
legacy_profile_backup_ready=0
validate_migration_paths_after_stop() {
  [[ "${profile_migration_required}" == 1 ]] || return 0
  [[ -d "${legacy_runtime_home}" && ! -L "${legacy_runtime_home}" ]] || return 1
  [[ -n "${legacy_runtime_identity}" \
      && "$(stat -c '%d:%i' "${legacy_runtime_home}")" == \
      "${legacy_runtime_identity}" ]] || return 1
  [[ -d "${profile_parent}" && ! -L "${profile_parent}" ]] || return 1
  [[ -n "${profile_parent_identity}" \
      && "$(stat -c '%d:%i' "${profile_parent}")" == \
      "${profile_parent_identity}" ]] || return 1
  if [[ "${runtime_home_preexisting}" == 1 ]]; then
    [[ -d "${runtime_home}" && ! -L "${runtime_home}" \
        && -n "${runtime_home_identity}" \
        && "$(stat -c '%d:%i' "${runtime_home}")" == \
        "${runtime_home_identity}" ]] || return 1
  else
    [[ -d "${runtime_home}" && ! -L "${runtime_home}" \
        && -n "${runtime_home_created_identity}" \
        && "$(stat -c '%d:%i' "${runtime_home}")" == \
        "${runtime_home_created_identity}" ]] || return 1
  fi
}
validate_target_root_after_stop() {
  [[ -d "${target_root}" && ! -L "${target_root}" \
      && "$(stat -c '%d:%i' "${target_root}")" == \
      "${target_root_identity}" ]]
}
backup_legacy_runtime_home() {
  [[ "${profile_migration_required}" == 1 ]] || return 0
  validate_migration_paths_after_stop || return 1
  [[ ! -e "${legacy_profile_backup}" && ! -L "${legacy_profile_backup}" ]] \
    || return 1
  # Reject cross-role symlinks before any service-side copy of the legacy
  # profile: the copy helper would otherwise surface its own generic escape
  # error without naming the migration boundary this transaction refuses to
  # cross, and a dispatcher leaf must never inherit a link that leaves it.
  if ! "${bootstrap_python_resolved}" -I - "${legacy_runtime_home}" <<'PY'
import os
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
root_resolved = root.resolve()
for path in sorted(root.rglob("*")):
    if not path.is_symlink():
        continue
    link_target = os.readlink(path)
    if os.path.isabs(link_target):
        raise SystemExit(f"profile symlink must be relative: {path}")
    resolved = (path.parent / link_target).resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise SystemExit(f"profile symlink leaves legacy profile: {path}")
PY
  then
    return 1
  fi
  run_profile_io copy-profile "${legacy_runtime_home}" "${legacy_profile_backup}" \
    || return 1
  legacy_profile_backup_ready=1
}
backup_one "${plugin_target}/plugin_api.py" "${backup}/plugins/collaboration/dashboard/plugin_api.py" 0644
backup_one "${plugin_target}/manifest.json" "${backup}/plugins/collaboration/dashboard/manifest.json" 0644
backup_one "${plugin_target}/dist/index.js" "${backup}/plugins/collaboration/dashboard/dist/index.js" 0644
backup_one "${core_target}" "${backup}/hermes_cli/cloud_file_library.py" 0644
backup_one "${public_paths_target}" "${backup}/hermes_cli/dashboard_auth/public_paths.py" 0644
backup_one "${token_auth_target}" "${backup}/hermes_cli/dashboard_auth/token_auth.py" 0644
backup_one "${mobile_device_store_target}" "${backup}/hermes_cli/dashboard_auth/mobile_device_store.py" 0644
backup_one "${mobile_notifications_target}" "${backup}/hermes_cli/dashboard_auth/mobile_notifications.py" 0644
backup_one "${managed_installations_target}" "${backup}/hermes_cli/managed_installations.py" 0644
backup_one "${managed_nodes_code_target}" "${backup}/hermes_cli/managed_nodes.py" 0644
backup_one "${web_server_target}" "${backup}/hermes_cli/web_server.py" 0644
backup_one "${managed_installation_tool_target}" "${backup}/tools/managed_installation_tool.py" 0644
backup_one "${toolsets_target}" "${backup}/toolsets.py" 0644
backup_one "${agent_init_target}" "${backup}/agent/agent_init.py" 0644
backup_one "${prompt_builder_target}" "${backup}/agent/prompt_builder.py" 0644
backup_one "${system_prompt_target}" "${backup}/agent/system_prompt.py" 0644
backup_one "${context_diagnostics_target}" "${backup}/agent/context_diagnostics.py" 0644
backup_one "${doctor_target}" "${backup}/hermes_cli/doctor.py" 0644
backup_one "${tui_gateway_target}" "${backup}/tui_gateway/server.py" 0644
backup_one "${nginx_security_target}" "${backup}/nginx/00-hermes-security.conf"
backup_one "${nginx_site_target}" "${backup}/nginx/daxueshenmai.top.conf"
backup_one "${release_evidence_target}" "${backup}/release/release-evidence.json"
for relative in "${runtime_service_assets[@]}"; do
  backup_one "${target_root}/${relative}" "${backup}/${relative}" 0644
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
  for relative in "${ios_optional[@]}"; do
    destination="${target_root}/${relative}"
    backup_one "${destination}" "${backup}/${relative}" 0644
  done
fi

transaction="$(mktemp -d "${target_root}/.collaboration-install.XXXXXX")"
chown "root:${service_group}" "${transaction}"
chmod 0750 "${transaction}"
release_candidate_txid="$("${bootstrap_python_resolved}" -I -c 'import uuid; print(uuid.uuid4().hex)')"
[[ "${release_candidate_txid}" =~ ^[0-9a-f]{32}$ ]] \
  || die "could not generate the release candidate transaction id"
installed=0
candidate_venv=""
previous_venv=""
venv_swap_txid=""
venv_old_identity=""
venv_new_identity=""
venv_swap_prepared=0
venv_swap_committed=0
runtime_candidate_started=0
release_start_lease_written=0
release_watchdog_unit=""
release_watchdog_started=0
release_watchdog_binding=1
runtime_home_migrated=0
runtime_home_migration_started=0
dispatcher_profile_preserved=0
migration_staging=""
migration_staging_identity=""
profile_migration_txid="${profile_migration_journal_txid:-${release_candidate_txid}}"
if [[ "${profile_migration_resumed}" == 1 ]]; then
  runtime_home_migrated=1
  runtime_home_migrated_identity="${runtime_home_identity}"
  # A root-owned external journal proves that the service-user copy completed.
  # Preserve it across any pre-candidate failure and resume under the absent
  # systemd ready barrier instead of touching its contents as root.
  dispatcher_profile_preserved=1
fi
runtime_home_migrated_identity="${runtime_home_migrated_identity:-}"
profile_env_rebound=0
profile_env_rebind_started=0
profile_dropin_rebound=0
profile_dropin_rebind_started=0
profile_dropin_dir_created=0
env_mode_normalization_started=0
env_mode_normalized=0
venv_old_moved=0
venv_swapped=0
dependency_update_enabled=0
if [[ "${dependency_runtime_managed}" == 1 ]]; then
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
  if [[ "${installed}" != 1 \
      && "${runtime_candidate_started}" != 1 \
      && "${dependency_runtime_managed}" == 1 ]]; then
    # If the old environment was moved aside but the swap journal itself is
    # gone, the shell restores the last-known-good environment directly; the
    # guarded journal recovery below covers every journaled state.
    if [[ "${venv_old_moved}" == 1 && ! -e "${venv_swap_journal}" ]] \
        && [[ ! -e "${runtime_venv}" && ! -L "${runtime_venv}" ]] \
        && [[ -d "${previous_venv}" && ! -L "${previous_venv}" ]]; then
      mv -f -- "${previous_venv}" "${runtime_venv}"
      previous_venv=""
      venv_old_moved=0
    fi
    if ! recover_venv_swap; then
      printf '%s\n' "rollback failed while recovering the runtime environment" >&2
      rollback_failed=1
    else
      venv_old_moved=0
      venv_swapped=0
      venv_swap_prepared=0
    fi
  fi
  if [[ "${installed}" != 1 ]]; then
    if [[ "${runtime_candidate_started}" == 1 ]]; then
      # The durable marker makes this code/configuration/state candidate one
      # authoritative unit before it may start. Revoke its live-installer
      # lease first so an external restart cannot race the stop or ready-file
      # cleanup, then keep the candidate stopped for a marker-aware retry.
      dispatcher_profile_preserved=1
      if [[ "${release_start_lease_written}" == 1 ]]; then
        if remove_release_start_lease; then
          release_start_lease_written=0
        else
          printf '%s\n' \
            "rollback failed: could not revoke the candidate start lease" >&2
          rollback_failed=1
        fi
      fi
      if systemctl stop "${service}" >/dev/null 2>&1; then
        service_stopped=1
      else
        printf '%s\n' "rollback failed: could not stop authoritative runtime candidate" >&2
        rollback_failed=1
      fi
      if ! revoke_dispatcher_ready; then
        printf '%s\n' \
          "rollback failed: could not revoke the dispatcher startup barrier" >&2
        rollback_failed=1
      elif [[ "${service_stopped}" == 1 ]]; then
        printf '%s\n' \
          "runtime candidate preserved; service remains stopped pending retry" >&2
      fi
    elif [[ "${mutated}" != 1 ]]; then
      # Failed between `trap rollback EXIT` and the first in-place install
      # (the `systemctl stop` itself, or one of the state snapshots).  No
      # target file has been touched, so there is nothing to restore;
      # leaving service_stopped=0 skips the restore block below.  Just make
      # sure the service is running again — `systemctl start` is a no-op if
      # the stop never went through.
      if [[ "${runtime_home_migration_started}" == 1 \
          && "${runtime_home_migrated}" != 1 ]]; then
        if ! cleanup_partial_profile_migration; then
          printf '%s\n' "rollback failed while cleaning partial profile migration" >&2
          rollback_failed=1
        fi
      fi
      if [[ "${runtime_home_created}" == 1 \
          && "${runtime_home_migrated}" != 1 ]]; then
        if ! remove_created_dispatcher_profile; then
          printf '%s\n' "rollback failed while removing created dispatcher profile" >&2
          rollback_failed=1
        fi
      fi
      if [[ "${dispatcher_profile_preserved}" == 1 \
          || "${release_retry_stopped}" == 1 ]]; then
        printf '%s\n' \
          "dispatcher profile preserved; service remains stopped pending retry" >&2
      elif ! start_and_verify_active; then
        printf '%s\n' "rollback failed: no files were changed but ${service} could not be started" >&2
        rollback_failed=1
      fi
    elif systemctl stop "${service}" >/dev/null 2>&1; then
      service_stopped=1
    else
      printf '%s\n' "rollback failed: could not stop ${service}" >&2
      rollback_failed=1
    fi
    if [[ "${service_stopped}" == 1 && "${runtime_seal_journal_state}" == present ]]; then
      if ! unseal_runtime_home; then
        printf '%s\n' "rollback failed: could not reopen sealed dispatcher profile" >&2
        rollback_failed=1
      fi
    fi
    if [[ "${service_stopped}" == 1 \
        && "${runtime_candidate_started}" != 1 ]]; then
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
      if [[ "${ios_enabled}" == 1 ]]; then
        for relative in "${ios_optional[@]}"; do
          rollback_step "${relative}" restore_one "${backup}/${relative}" "${target_root}/${relative}"
        done
      fi
      if [[ "${runtime_home_migration_started}" == 1 \
          && "${runtime_home_migrated}" != 1 ]]; then
        rollback_step partial-profile-migration cleanup_partial_profile_migration
      fi
      if [[ "${profile_migration_required}" != 1 \
          && "${dispatcher_profile_preserved}" != 1 ]]; then
        rollback_step cloud-files-db restore_sqlite "${backup}/state/cloud-files-library.sqlite3" "${cloud_files_database_target}"
        rollback_step mobile-auth-db restore_sqlite "${backup}/state/mobile-auth.db" "${mobile_auth_target}"
        rollback_step managed-installations-db restore_sqlite "${backup}/state/managed-installations.db" "${managed_installations_database_target}"
        rollback_step managed-nodes-config restore_state "${backup}/state/managed-nodes.json" "${managed_nodes_target}"
        if [[ "${ios_enabled}" == 1 ]]; then
          rollback_step profile-config restore_profile_file \
            "${backup}/config/config.yaml" "${config_target}" 0600
          rollback_step ios-intelligence-db restore_sqlite "${backup}/state/ios-intelligence.db" "${ios_database_target}"
          rollback_step ios-supervisor-db restore_sqlite "${backup}/state/ios-mcp-supervisor.db" "${ios_supervisor_target}"
        fi
        rollback_step conversation-state restore_state "${backup}/state/single.json" "${state_target}"
        rollback_step runtime-sqlite-tree restore_runtime_sqlite_tree \
          "${backup}/state/sqlite-tree" "${runtime_home}"
      fi
      if [[ "${dispatcher_profile_preserved}" != 1 \
          && ( "${profile_env_rebound}" == 1 \
          || "${profile_env_rebind_started}" == 1 \
          || "${env_mode_normalization_started}" == 1 ) ]]; then
        rollback_step profile-environment restore_preserved_file \
          "${backup}/service/hermes-agent.env" "${env_file}"
      fi
      if [[ "${dispatcher_profile_preserved}" != 1 \
          && ( "${profile_dropin_rebound}" == 1 \
            || "${profile_dropin_rebind_started}" == 1 ) ]]; then
        rollback_step profile-dropin restore_preserved_file \
          "${backup}/service/profile-dropin.conf" "${profile_dropin_target}"
      fi
      if [[ "${dispatcher_profile_preserved}" != 1 \
          && "${profile_dropin_dir_created}" == 1 ]]; then
        rollback_step profile-dropin-directory cleanup_profile_dropin_directory
      fi
      if [[ "${dispatcher_profile_preserved}" != 1 \
          && ( "${profile_env_rebound}" == 1 \
          || "${profile_env_rebind_started}" == 1 \
          || "${env_mode_normalization_started}" == 1 \
          || "${profile_dropin_rebound}" == 1 \
          || "${profile_dropin_rebind_started}" == 1 \
          || "${profile_dropin_dir_created}" == 1 ) ]]; then
        rollback_step systemd-daemon-reload systemctl daemon-reload
      fi
      if [[ "${runtime_home_migrated}" == 1 ]]; then
        if [[ "${dispatcher_profile_preserved}" == 1 \
            || "${release_retry_stopped}" == 1 ]]; then
          printf '%s\n' \
            "preserving forked dispatcher profile for migration resume" >&2
        else
          rollback_step legacy-profile-migration restore_legacy_runtime_home
        fi
      elif [[ "${runtime_home_created}" == 1 ]]; then
        rollback_step empty-dispatcher-profile remove_created_dispatcher_profile
      fi
      if [[ "${nginx_reload_attempted}" == 1 && "${rollback_failed}" == 0 ]]; then
        if ! "${nginx_binary}" -t >/dev/null 2>&1 \
          || ! systemctl reload "${nginx_service}" >/dev/null 2>&1; then
          printf '%s\n' "rollback failed while restoring nginx runtime" >&2
          rollback_failed=1
        fi
      fi
      if [[ "${rollback_failed}" == 0 ]]; then
        if [[ "${dispatcher_profile_preserved}" == 1 \
            || "${release_retry_stopped}" == 1 ]]; then
          printf '%s\n' \
            "dispatcher profile preserved; service remains stopped pending retry" >&2
        else
          systemctl reset-failed "${service}" >/dev/null 2>&1 || true
          if ! systemctl start "${service}"; then
            printf '%s\n' \
              "rollback restored files but failed to restart ${service}" >&2
            rollback_failed=1
          elif ! verify_service_active; then
            printf '%s\n' \
              "rollback restarted ${service} but it is not active" >&2
            rollback_failed=1
          fi
        fi
      fi
    fi
  fi
  rm -rf -- "${transaction}"
  if [[ "${dependency_runtime_managed}" != 1 \
      && -n "${candidate_venv}" ]]; then
    rm -rf -- "${candidate_venv}"
  fi
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
  elif [[ "${installed}" != 1 \
      && ( "${runtime_candidate_started}" == 1 \
        || "${dispatcher_profile_preserved}" == 1 \
        || "${release_retry_stopped}" == 1 ) ]]; then
    # No legacy service was restarted, so all accepted writes remain on the
    # single dispatcher branch. Ask the outer deployer for an immediate
    # marker-aware retry instead of leaving the service unavailable.
    exit_code=75
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
  # Runtime/code destinations are service-owned.  Restore through the same
  # unprivileged descriptor-relative helper used for backup/publish so root
  # never follows a service-controlled destination pathname during rollback.
  run_profile_io_root restore-file "${source}" "${destination}" 0644
}
restore_root_file() {
  local source="$1"
  local destination="$2"
  local temporary="${destination}.rollback.$$"
  if [[ -f "${source}" && ! -L "${source}" ]]; then
    install -o root -g root -m 0644 "${source}" "${temporary}" \
      || { rm -f -- "${temporary}"; return 1; }
    mv -f -- "${temporary}" "${destination}" \
      || { rm -f -- "${temporary}"; return 1; }
  elif [[ -f "${source}.missing" && ! -L "${source}.missing" ]]; then
    rm -f -- "${destination}" || return 1
  else
    return 1
  fi
}
restore_state() {
  local source="$1"
  local destination="$2"
  run_profile_io_root restore-file "${source}" "${destination}" 0600
}
restore_sqlite() {
  local source="$1"
  local destination="$2"
  run_profile_io_root restore-sqlite "${source}" "${destination}"
}
restore_profile_file() {
  local source="$1"
  local destination="$2"
  local mode="${3:-0600}"
  run_profile_io_root restore-file "${source}" "${destination}" "${mode}"
}
restore_preserved_file() {
  local source="$1"
  local destination="$2"
  local temporary="${destination}.rollback.$$"
  local destination_parent
  destination_parent="$(dirname -- "${destination}")"
  rm -f -- "${temporary}" || return 1
  if [[ -f "${source}" && ! -L "${source}" ]]; then
    [[ ! -L "${destination}" ]] || return 1
    if [[ ! -e "${destination_parent}" && ! -L "${destination_parent}" ]]; then
      install -d -o root -g root -m 0755 "${destination_parent}" || return 1
    fi
    [[ -d "${destination_parent}" && ! -L "${destination_parent}" ]] || return 1
    cp -a -- "${source}" "${temporary}" || { rm -f -- "${temporary}"; return 1; }
    mv -f -- "${temporary}" "${destination}" || { rm -f -- "${temporary}"; return 1; }
  elif [[ -f "${source}.missing" && ! -L "${source}.missing" ]]; then
    [[ ! -L "${destination}" ]] || return 1
    rm -f -- "${destination}" || return 1
  else
    return 1
  fi
}
cleanup_profile_dropin_directory() {
  [[ "${profile_dropin_dir_created}" == 1 ]] || return 0
  [[ -d "${profile_dropin_dir}" && ! -L "${profile_dropin_dir}" ]] || return 1
  [[ ! -e "${profile_dropin_target}" && ! -L "${profile_dropin_target}" ]] || return 0
  # A concurrently added file must not make rollback fail or be removed.
  rmdir -- "${profile_dropin_dir}" 2>/dev/null || true
}
remove_created_dispatcher_profile() {
  [[ "${runtime_home_created}" == 1 \
      && "${runtime_home_migrated}" != 1 ]] || return 0
  [[ -d "${runtime_home}" && ! -L "${runtime_home}" ]] || return 1
  [[ -n "${runtime_home_created_identity}" \
      && "$(stat -c '%d:%i' "${runtime_home}")" == \
      "${runtime_home_created_identity}" ]] || return 1
  seal_runtime_home || return 1
  "${bootstrap_python_resolved}" -I \
    "${stage_root}/deploy/public/runtime-home-guard.py" remove-empty \
    "${runtime_home}" "${runtime_home_created_identity}" \
    "${runtime_seal_journal}" "${release_candidate_marker_dir_identity}" \
    >/dev/null || return 1
  runtime_seal_journal_state="absent"
  runtime_home_created=0
}
cleanup_partial_profile_migration() {
  if [[ -n "${migration_staging}" ]]; then
    [[ ! -L "${migration_staging}" ]] || return 1
    [[ -n "${migration_staging_identity}" \
        && "$(stat -c '%d:%i' "${migration_staging}")" == \
        "${migration_staging_identity}" ]] || return 1
    # The migrated staging legitimately contains self-contained relative
    # symlinks, so the profile-I/O helper's link-refusing remove-tree can
    # never take it down. rm -rf unlinks entries without following them and
    # only ever touches the identity-verified staging inode itself.
    rm -rf -- "${migration_staging}" || return 1
    migration_staging=""
    migration_staging_identity=""
  fi
  if [[ "${runtime_home_migration_started}" == 1 \
      && "${runtime_home_migrated}" != 1 \
      && ( ! -e "${runtime_home}" || -L "${runtime_home}" ) ]]; then
    # A missing leaf is recreated only through the descriptor-anchored guard;
    # never let root follow a service-controlled pathname during rollback.
    local parent_identity
    parent_identity="$(stat -c '%d:%i' "${runtime_home_parent}")" || return 1
    runtime_home_created_identity="$(
      "${bootstrap_python_resolved}" -I \
        "${stage_root}/deploy/public/runtime-home-guard.py" ensure-leaf \
        "${runtime_home_parent}" "${runtime_home}" "${service_uid}" \
        "${service_gid}" "${parent_identity}"
    )" || return 1
  fi
  runtime_home_migration_started=0
}
restore_legacy_runtime_home() {
  if [[ "${runtime_home_migrated}" != 1 ]]; then
    cleanup_partial_profile_migration
    return 0
  fi
  # Once the marker-bearing copy exists it is an independent role. It may
  # contain writes accepted by a briefly started dispatcher, so rollback may
  # restore routing to the legacy service but must never delete this profile.
  [[ "${dispatcher_profile_preserved}" != 1 ]] || return 0
  [[ -d "${runtime_home}" && ! -L "${runtime_home}" ]] || return 1
  # The legacy worker directory was copied, never moved. Keep that worker's
  # original state intact and remove only the dispatcher copy created by this
  # transaction. If an external cleanup removed the source unexpectedly, use
  # the root-owned full snapshot as a last-resort restoration.
  if [[ ! -e "${legacy_runtime_home}" && ! -L "${legacy_runtime_home}" ]]; then
    [[ "${legacy_profile_backup_ready}" == 1 \
        && -d "${legacy_profile_backup}" \
        && ! -L "${legacy_profile_backup}" ]] || return 1
    run_profile_io copy-profile "${legacy_profile_backup}" "${legacy_runtime_home}" \
      || return 1
  else
    [[ -d "${legacy_runtime_home}" && ! -L "${legacy_runtime_home}" ]] \
      || return 1
  fi
  [[ "$(stat -c '%d:%i' "${runtime_home}")" == \
      "${runtime_home_migrated_identity}" ]] || return 1
  # The new dispatcher leaf is root-owned and may have accepted writes. Keep
  # it intact on rollback; a subsequent marker/journal-aware retry decides
  # whether it can be removed. This avoids root recursive deletion of a
  # service-controlled tree.
  return 0
  runtime_home_migrated=0
  runtime_home_migration_started=0
  runtime_home_migrated_identity=""
}
rewrite_profile_environment() {
  [[ -f "${env_file}" && ! -L "${env_file}" ]] || return 0
  profile_env_rebind_started=1
  validate_root_controlled_parent "$(dirname -- "${env_file}")" \
    "Hermes environment file" || return 1
  local env_parent temporary
  env_parent="$(dirname -- "${env_file}")"
  temporary="$(mktemp "${env_parent}/.hermes-agent.env.XXXXXX")" || return 1
  [[ "${temporary}" =~ ^/[A-Za-z0-9._/@+:-]+$ ]] || { rm -f -- "${temporary}"; return 1; }
  "${bootstrap_python_resolved}" -I - "${env_file}" "${runtime_home}" "${temporary}" <<'PY'
import os
import pathlib
import stat
import sys

source = pathlib.Path(sys.argv[1])
home = sys.argv[2]
temporary = pathlib.Path(sys.argv[3])
parent_fd = os.open(
    source.parent,
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
)
try:
    source_fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        source_stat = os.fstat(source_fd)
        published = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(source_stat.st_mode)
            or (source_stat.st_dev, source_stat.st_ino)
            != (published.st_dev, published.st_ino)
        ):
            raise RuntimeError("Hermes environment file changed while opening")
        chunks = []
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        lines = b"".join(chunks).decode("utf-8").splitlines(keepends=True)
    finally:
        os.close(source_fd)
    updated = []
    found = False
    for line in lines:
        if line.startswith("HERMES_HOME="):
            if not found:
                updated.append(f"HERMES_HOME={home}\n")
                found = True
            continue
        updated.append(line)
    if not found:
        if updated and not updated[-1].endswith(("\n", "\r")):
            updated.append("\n")
        updated.append(f"HERMES_HOME={home}\n")
    temporary_fd = os.open(
        temporary.name,
        os.O_WRONLY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    with os.fdopen(temporary_fd, "w", encoding="utf-8", newline="") as stream:
        stream.write("".join(updated))
        stream.flush()
        os.fsync(stream.fileno())
    current = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (source_stat.st_dev, source_stat.st_ino):
        raise RuntimeError("Hermes environment file changed before publish")
    os.replace(temporary, source)
    directory_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    os.close(parent_fd)
PY
  local status=$?
  rm -f -- "${temporary}"
  return "${status}"
}
write_profile_dropin() {
  profile_dropin_rebind_started=1
  if [[ ! -e "${profile_dropin_dir}" && ! -L "${profile_dropin_dir}" ]]; then
    install -d -o root -g root -m 0755 "${profile_dropin_dir}" || return 1
    profile_dropin_dir_created=1
  fi
  validate_root_controlled_parent "${profile_dropin_dir}" \
    "Hermes profile drop-in" || return 1
  [[ ! -L "${profile_dropin_target}" ]] || return 1
  local dropin_content start_condition unit_content
  start_condition="+${bootstrap_python_resolved} -I ${release_start_guard} check ${release_candidate_marker} ${release_start_lease} ${target_root} ${target_root_identity} ${service} ${release_candidate_marker_dir_identity}"
  dropin_content="[Service]
Environment=HERMES_HOME=${runtime_home}
ExecCondition=${start_condition}"
  if [[ "${profile_rebind_required}" == 1 ]]; then
    unit_content="[Unit]
ConditionPathExists=${dispatcher_ready_path}"
    if [[ "${release_watchdog_binding}" == 1 ]]; then
      unit_content="[Unit]
BindsTo=${release_watchdog_unit}
After=${release_watchdog_unit}
ConditionPathExists=${dispatcher_ready_path}"
    fi
    dropin_content="${unit_content}
${dropin_content}"
  fi
  "${bootstrap_python_resolved}" -I - "${profile_dropin_target}" "${dropin_content}" <<'PY' \
    || return 1
import os
import pathlib
import sys
import stat

target = pathlib.Path(sys.argv[1])
content = sys.argv[2] + "\n"
parent_fd = os.open(
    target.parent,
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
)
try:
    parent_metadata = os.fstat(parent_fd)
    published_parent = os.stat(target.parent, follow_symlinks=False)
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != 0
        or stat.S_IMODE(parent_metadata.st_mode) & 0o022
        or (parent_metadata.st_dev, parent_metadata.st_ino)
        != (published_parent.st_dev, published_parent.st_ino)
    ):
        raise RuntimeError("profile drop-in parent changed before publish")
    temporary = target.with_name(f".{target.name}.new-{os.getpid()}")
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            os.fchown(stream.fileno(), 0, 0)
            os.fchmod(stream.fileno(), 0o644)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        current_parent = os.fstat(parent_fd)
        if (current_parent.st_dev, current_parent.st_ino) != (
            parent_metadata.st_dev,
            parent_metadata.st_ino,
        ):
            raise RuntimeError("profile drop-in parent changed during publish")
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
finally:
    os.close(parent_fd)
PY
}
validate_loaded_profile_guard() {
  [[ "${profile_rebind_required}" == 1 ]] || return 0
  local expected_ready_state="${1:-absent}"
  case "${expected_ready_state}" in
    absent)
      [[ ! -e "${dispatcher_ready_path}" && ! -L "${dispatcher_ready_path}" ]] \
        || return 1
      ;;
    present)
      [[ -f "${dispatcher_ready_path}" && ! -L "${dispatcher_ready_path}" \
          && "$(stat -c '%u:%a' "${dispatcher_ready_path}" 2>/dev/null || true)" == "0:600" \
          && "$(<"${dispatcher_ready_path}")" == \
            "hermes-dispatcher-ready:${dispatcher_ready_token}" ]] \
        || return 1
      ;;
    *) return 1 ;;
  esac
  [[ -f "${release_start_guard}" && ! -L "${release_start_guard}" \
      && "$(stat -c '%u:%a' "${release_start_guard}" 2>/dev/null || true)" == "0:755" \
      && "$(stat -c '%d:%i' "${release_candidate_marker_dir}" 2>/dev/null || true)" == "${release_candidate_marker_dir_identity}" \
      && "$(sha256sum "${release_start_guard}" 2>/dev/null | cut -d' ' -f1)" == \
        "$(sha256sum "${snapshot}/deploy/public/candidate-start-guard.py" | cut -d' ' -f1)" ]] \
    || return 1
  [[ -f "${profile_dropin_target}" && ! -L "${profile_dropin_target}" \
      && "$(stat -c '%u:%a' "${profile_dropin_target}" 2>/dev/null || true)" == "0:644" ]] \
    || return 1
  local loaded_dropins loaded_unit systemd_version
  loaded_dropins="$(
    systemctl show "${service}" --property=DropInPaths --value 2>/dev/null
  )" || return 1
  loaded_unit="$(systemctl cat "${service}" --no-pager 2>/dev/null)" \
    || return 1
  systemd_version="$(systemctl --version 2>/dev/null | sed -n '1s/^systemd \([0-9][0-9]*\).*/\1/p')"
  # systemd 243-245 can loop Restart=always units whose ExecCondition skips,
  # exhausting StartLimit before a marker-aware retry. Version 246 includes
  # the corrected restart semantics required by this fail-closed gate.
  [[ "${systemd_version}" =~ ^[0-9]+$ && "${systemd_version}" -ge 246 ]] \
    || return 1
  "${bootstrap_python_resolved}" -I - \
    "${profile_dropin_target}" "${dispatcher_ready_path}" "${runtime_home}" \
    "+${bootstrap_python_resolved} -I ${release_start_guard} check ${release_candidate_marker} ${release_start_lease} ${target_root} ${target_root_identity} ${service} ${release_candidate_marker_dir_identity}" \
    "${loaded_dropins}" "${loaded_unit}" \
    "${release_watchdog_unit}" "${release_watchdog_binding}" <<'PY'
import pathlib
import shlex
import sys

dropin = pathlib.Path(sys.argv[1])
ready_path = sys.argv[2]
runtime_home = sys.argv[3]
start_condition = sys.argv[4]
loaded = sys.argv[5]
loaded_unit = sys.argv[6]
watchdog_unit = sys.argv[7]
watchdog_binding = sys.argv[8] == "1"
unit_lines = ["[Unit]"]
if watchdog_binding:
    unit_lines.extend([f"BindsTo={watchdog_unit}", f"After={watchdog_unit}"])
unit_lines.append(f"ConditionPathExists={ready_path}")
expected = "\n".join(unit_lines) + (
    "\n[Service]\n"
    f"Environment=HERMES_HOME={runtime_home}\n"
    f"ExecCondition={start_condition}\n"
)
if dropin.read_text(encoding="utf-8") != expected:
    raise SystemExit("dispatcher profile guard content changed before reload")
try:
    loaded_paths = shlex.split(loaded)
except ValueError as error:
    raise SystemExit(f"systemd DropInPaths could not be parsed: {error}")
if str(dropin) not in loaded_paths:
    raise SystemExit("systemd did not load the dispatcher profile guard")

section = ""
conditions = []
exec_conditions = []
binds_to = []
after = []
for raw_line in loaded_unit.splitlines():
    line = raw_line.strip()
    if not line or line.startswith(("#", ";")):
        continue
    if line.startswith("[") and line.endswith("]"):
        section = line[1:-1]
        continue
    if "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if section == "Unit" and key.startswith("Condition"):
        if not value:
            conditions.clear()
        else:
            conditions.append((key, value))
    elif section == "Unit" and key in {"BindsTo", "After"}:
        destination = binds_to if key == "BindsTo" else after
        if not value:
            destination.clear()
        else:
            try:
                destination.extend(shlex.split(value))
            except ValueError as error:
                raise SystemExit(f"systemd dependency could not be parsed: {error}")
    elif section == "Service" and key == "ExecCondition":
        if not value:
            exec_conditions.clear()
        else:
            exec_conditions.append(value)
if ("ConditionPathExists", ready_path) not in conditions:
    raise SystemExit("effective systemd unit cleared the dispatcher ready guard")
if start_condition not in exec_conditions:
    raise SystemExit("effective systemd unit cleared the candidate start guard")
if watchdog_binding:
    if watchdog_unit not in binds_to or watchdog_unit not in after:
        raise SystemExit("effective systemd unit cleared the release watchdog binding")
elif watchdog_unit in binds_to or watchdog_unit in after:
    raise SystemExit("effective systemd unit retained the committed watchdog binding")
PY
}
migrate_legacy_runtime_home() {
  [[ "${profile_migration_required}" == 1 ]] || return 0
  [[ "${legacy_profile_backup_ready}" == 1 ]] || return 1
  validate_migration_paths_after_stop || return 1
  [[ -d "${runtime_home}" && ! -L "${runtime_home}" ]] || return 1
  local existing_entry
  if ! existing_entry="$(find "${runtime_home}" -mindepth 1 -maxdepth 1 -print -quit)"; then
    return 1
  fi
  if [[ -n "${existing_entry}" ]]; then
    # The leaf is already occupied: resume it as the authoritative fork only
    # when the external journal proves the copy happened, or when a strict
    # root-owned pending-migration marker proves this is the published fork
    # for this release. Anything else is an implicit merge and must fail.
    local pending_marker=""
    if [[ "${profile_migration_journal_state}" != copied ]]; then
      pending_marker="$(find "${runtime_home}" -maxdepth 1 \
        -name '.hermes-dispatcher-migration.*' -print -quit)" || return 1
      [[ -n "${pending_marker}" ]] \
        || return 1
      validate_pending_migration_marker "${pending_marker}" || return 1
      rm -f -- "${pending_marker}"
    fi
    runtime_home_migrated_identity="$(stat -c '%d:%i' "${runtime_home}")" || return 1
    runtime_home_migrated=1
    dispatcher_profile_preserved=1
    return 0
  fi
  runtime_home_migration_started=1
  profile_migration_txid="${profile_migration_txid:-${release_candidate_txid}}"
  [[ "${profile_migration_txid}" =~ ^[0-9a-f]{32}$ ]] || return 1
  if ! migration_staging="$(
    mktemp -d "${profile_parent}/.dispatcher-profile-migration.XXXXXX"
  )"; then
    return 1
  fi
  if ! chown "${service_user}:${service_group}" "${migration_staging}" \
      || ! chmod 0770 "${migration_staging}"; then
    rmdir -- "${migration_staging}" 2>/dev/null || true
    migration_staging=""
    runtime_home_migration_started=0
    return 1
  fi
  migration_staging_identity="$(stat -c '%d:%i' "${migration_staging}")" || return 1
  local source_identity destination_identity
  source_identity="${legacy_runtime_identity}"
  destination_identity="$(stat -c '%d:%i' "${runtime_home}")" || return 1
  if ! "${bootstrap_python_resolved}" -I \
      "${stage_root}/deploy/public/runtime-home-guard.py" journal-write \
      "${profile_migration_journal}" "${release_candidate_marker_dir_identity}" \
      "${profile_migration_txid}" "${legacy_runtime_home}" "${source_identity}" \
      "${runtime_home}" "${destination_identity}" "${version}" "${release_commit}" >/dev/null; then
    rmdir -- "${migration_staging}" 2>/dev/null || true
    migration_staging=""
    runtime_home_migration_started=0
    return 1
  fi
  # Root performs the profile copy after the service has been quiesced: the
  # root-owned full snapshot is the authoritative source and cp -a preserves
  # every service-owned inode and mode exactly.
  if ! cp -a -- "${legacy_profile_backup}/." "${migration_staging}/"; then
    return 1
  fi
  # cp -a applies the snapshot directory's own attributes to the staging
  # root; re-assert the exact service-owned mode-0770 form that the
  # descriptor-anchored adoption requires before the publish.
  if ! chown "${service_user}:${service_group}" "${migration_staging}" \
      || ! chmod 0770 "${migration_staging}"; then
    cleanup_partial_profile_migration || true
    return 1
  fi
  # A migrated profile must be self-contained: preserve a copied symlink only
  # when it is relative and cannot escape the dispatcher profile, otherwise the
  # new home would silently re-couple to the legacy worker tree.
  if ! "${bootstrap_python_resolved}" -I - "${migration_staging}" <<'PY'
import os
import pathlib
import sys

staging = pathlib.Path(sys.argv[1])
staging_root = staging.resolve()
for path in sorted(staging.rglob("*")):
    if not path.is_symlink():
        continue
    link_target = os.readlink(path)
    if os.path.isabs(link_target):
        raise SystemExit(f"profile symlink must be relative: {path}")
    resolved = (path.parent / link_target).resolve()
    if resolved != staging_root and staging_root not in resolved.parents:
        raise SystemExit(f"profile symlink leaves legacy profile: {path}")
PY
  then
    cleanup_partial_profile_migration || true
    return 1
  fi
  if ! restore_runtime_sqlite_tree \
      "${backup}/state/sqlite-tree" "${migration_staging}"; then
    return 1
  fi
  if [[ "${migrated_config_rewrite_required}" == 1 ]]; then
    local migrated_config="${migration_staging}/config.yaml"
    [[ -f "${migrated_config}" && ! -L "${migrated_config}" ]] || return 1
    if ! run_as_service \
      "${runtime_python}" - "${migrated_config}" "${ios_database_target}" <<'PY'
import os
import pathlib
import stat
import sys

import yaml

config_path = pathlib.Path(sys.argv[1])
database_path = sys.argv[2]
if not config_path.is_absolute() or config_path.name in {"", ".", ".."}:
    raise RuntimeError("legacy dispatcher config path is invalid")
parent_fd = os.open(
    config_path.parent,
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
)
source_fd = os.open(
    config_path.name,
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    dir_fd=parent_fd,
)
try:
    metadata = os.fstat(source_fd)
    published = os.stat(config_path.name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != (published.st_dev, published.st_ino)
    ):
        raise RuntimeError("legacy dispatcher config changed while opening")
    chunks = []
    while True:
        chunk = os.read(source_fd, 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    loaded = yaml.safe_load(b"".join(chunks).decode("utf-8"))
finally:
    os.close(source_fd)
if loaded is None:
    loaded = {}
if not isinstance(loaded, dict):
    raise RuntimeError("legacy dispatcher config root must be a mapping")
section = loaded.get("ios_intelligence")
if section is None:
    section = {}
if not isinstance(section, dict):
    raise RuntimeError("legacy ios_intelligence config must be a mapping")
section["database_path"] = database_path
loaded["ios_intelligence"] = section
temporary_name = f".{config_path.name}.migration-{os.getpid()}"
temporary_fd = os.open(
    temporary_name,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    stat.S_IMODE(metadata.st_mode),
    dir_fd=parent_fd,
)
try:
    rendered = yaml.safe_dump(loaded, sort_keys=False, allow_unicode=True).encode("utf-8")
    view = memoryview(rendered)
    while view:
        written = os.write(temporary_fd, view)
        if written <= 0:
            raise RuntimeError("could not write migrated dispatcher config")
        view = view[written:]
    os.fchmod(temporary_fd, stat.S_IMODE(metadata.st_mode))
    os.fsync(temporary_fd)
finally:
    os.close(temporary_fd)
current = os.stat(config_path.name, dir_fd=parent_fd, follow_symlinks=False)
if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
    try:
        os.unlink(temporary_name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    raise RuntimeError("legacy dispatcher config changed before publish")
os.replace(
    temporary_name,
    config_path.name,
    src_dir_fd=parent_fd,
    dst_dir_fd=parent_fd,
)
os.fsync(parent_fd)
os.close(parent_fd)
PY
    then
      cleanup_partial_profile_migration || true
      return 1
    fi
  fi
  if ! "${bootstrap_python_resolved}" -I \
      "${stage_root}/deploy/public/runtime-home-guard.py" adopt-staging \
      "${profile_parent}" "${migration_staging}" "${service_uid}" "${service_gid}" \
      "${profile_parent_identity}" "${migration_staging_identity}" >/dev/null; then
    return 1
  fi
  if ! "${bootstrap_python_resolved}" -I \
      "${stage_root}/deploy/public/runtime-home-guard.py" journal-advance \
      "${profile_migration_journal}" "${release_candidate_marker_dir_identity}" \
      "${profile_migration_txid}" >/dev/null; then
    return 1
  fi
  # The runtime home leaf is a freshly ensured empty directory at this point.
  # Re-check its identity, remove exactly that leaf, then publish the adopted
  # staging tree with a rename inside the same parent directory. The service
  # is stopped and no live process references either tree, so no writer can
  # re-create the leaf name in the rmdir/mv gap.
  [[ "$(stat -c '%d:%i' "${runtime_home}")" == "${destination_identity}" ]] \
    || return 1
  if ! rmdir -- "${runtime_home}"; then
    return 1
  fi
  if ! mv -- "${migration_staging}" "${runtime_home}"; then
    return 1
  fi
  migration_staging=""
  migration_staging_identity=""
  # The published fork is independent of the legacy worker home from here on.
  # Publish a strict root-owned pending marker inside it so a briefly started
  # dispatcher (and any later transaction) can recognize the fork, while the
  # finalization step removes it once this release has fully committed.
  local pending_marker="${runtime_home}/.hermes-dispatcher-migration.${profile_migration_txid}"
  if ! printf '%s\n' "${release_commit}:${profile_migration_txid}:1" \
      >"${pending_marker}" \
    || ! chown root:root "${pending_marker}" \
    || ! chmod 0600 "${pending_marker}"; then
    return 1
  fi
  if ! runtime_home_migrated_identity="$(stat -c '%d:%i' "${runtime_home}")"; then
    return 1
  fi
  runtime_home_migrated=1
  dispatcher_profile_preserved=1
}
migrate_dispatcher_auth_state() {
  # Explicit custom HERMES_HOME deployments (including the isolated worker
  # connectors) own their credential namespace and must not be redirected to
  # the public dispatcher's /var/lib tree.
  [[ "${runtime_topology_mode}" == managed-dispatcher \
      || "${profile_migration_required}" == 1 ]] || return 0
  # ``get_default_hermes_root()`` resolves the parent of a named profile, so a
  # dispatcher rooted at /var/lib/hermes-dispatcher would otherwise lose the
  # credentials that lived under the old service home.  Copy only when the
  # new store is absent; an operator's newer login always wins.  All reads and
  # writes run as the service account through descriptor-relative helpers.
  run_profile_io ensure-dir "${dispatcher_shared_auth_dir}" 0770 || return 1
  run_profile_io copy-if-absent \
    "${legacy_global_auth_file}" "${dispatcher_global_auth_file}" 0600 \
    || return 1
  run_profile_io copy-if-absent \
    "${legacy_shared_nous_auth_file}" "${dispatcher_shared_nous_auth_file}" 0600 \
    || return 1
  # A profile-local auth file from the cloned legacy profile is authoritative.
  # If it did not exist, seed it from the migrated root fallback so ordinary
  # provider writes/refreshes remain profile-local after the move.
  if [[ "${runtime_home}/auth.json" != "${dispatcher_global_auth_file}" ]]; then
    run_profile_io copy-if-absent \
      "${dispatcher_global_auth_file}" "${runtime_home}/auth.json" 0600 \
      || return 1
  fi
}
publish_dispatcher_ready() {
  [[ "${profile_rebind_required}" == 1 ]] || return 0
  [[ -d "${runtime_home}" && ! -L "${runtime_home}" ]] || return 1
  [[ "$(stat -c '%d:%i' "${runtime_home}")" == \
      "${runtime_home_migrated_identity:-${runtime_home_identity:-${runtime_home_created_identity}}}" ]] \
    || return 1
  "${bootstrap_python_resolved}" -I - \
    "${dispatcher_ready_path}" "${dispatcher_ready_token}" \
    "${release_candidate_marker_dir_identity}" <<'PY'
import os
import pathlib
import stat
import sys

target = pathlib.Path(sys.argv[1])
content = f"hermes-dispatcher-ready:{sys.argv[2]}\n"
expected_parent_identity = sys.argv[3]
parent_fd = os.open(
    target.parent,
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
)
created_identity = None
try:
    parent = os.fstat(parent_fd)
    if (
        f"{parent.st_dev}:{parent.st_ino}" != expected_parent_identity
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != 0
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise RuntimeError("dispatcher startup barrier directory changed")
    fd = os.open(
        target.name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="") as stream:
            metadata = os.fstat(stream.fileno())
            created_identity = (metadata.st_dev, metadata.st_ino)
            os.fchown(stream.fileno(), 0, 0)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    published_fd = os.open(
        target.name,
        os.O_RDONLY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        published = os.fstat(published_fd)
        current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        with os.fdopen(published_fd, "r", encoding="ascii", closefd=False) as stream:
            published_content = stream.read()
        if (
            (published.st_dev, published.st_ino) != created_identity
            or (current.st_dev, current.st_ino) != created_identity
            or not stat.S_ISREG(published.st_mode)
            or published.st_uid != 0
            or stat.S_IMODE(published.st_mode) != 0o600
            or published_content != content
        ):
            raise RuntimeError("dispatcher startup barrier changed while publishing")
    finally:
        os.close(published_fd)
    os.fsync(parent_fd)
except BaseException:
    try:
        current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if created_identity == (current.st_dev, current.st_ino):
            os.unlink(target.name, dir_fd=parent_fd)
    raise
finally:
    os.close(parent_fd)
PY
}
revoke_dispatcher_ready() {
  [[ "${profile_rebind_required}" == 1 ]] || return 0
  "${bootstrap_python_resolved}" -I - \
    "${dispatcher_ready_path}" "${dispatcher_ready_token}" \
    "${release_candidate_marker_dir_identity}" <<'PY'
import os
import pathlib
import stat
import sys

target = pathlib.Path(sys.argv[1])
expected = f"hermes-dispatcher-ready:{sys.argv[2]}\n"
expected_parent_identity = sys.argv[3]
parent_fd = os.open(
    target.parent,
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
)
try:
    parent = os.fstat(parent_fd)
    if (
        f"{parent.st_dev}:{parent.st_ino}" != expected_parent_identity
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != 0
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise RuntimeError("dispatcher startup barrier directory changed")
    try:
        descriptor = os.open(
            target.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        raise SystemExit(0)
    try:
        metadata = os.fstat(descriptor)
        with os.fdopen(descriptor, "r", encoding="ascii", closefd=False) as stream:
            content = stream.read()
        current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise RuntimeError("dispatcher startup barrier has unsafe ownership or mode")
        if content != expected:
            raise RuntimeError("dispatcher startup barrier belongs to another release")
        os.unlink(target.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(descriptor)
finally:
    os.close(parent_fd)
PY
}
record_pending_profile_migration_journal() {
  # The migration journal is the only durable record of which legacy profile
  # feeds the dispatcher leaf. Once the guarded systemd binding names the
  # dispatcher leaf, a later transaction can no longer derive the legacy path
  # from the effective home, so the journal must be written before the first
  # daemon-reload of a pending migration. Re-running the exact same write from
  # the copy step is idempotent: the guard accepts an identical prepared
  # journal for the same transaction.
  [[ "${profile_migration_required}" == 1 ]] || return 0
  [[ "${profile_migration_journal_state}" == absent ]] || return 0
  [[ -n "${legacy_runtime_home}" && -n "${legacy_runtime_identity}" ]] || return 0
  [[ -d "${runtime_home}" && ! -L "${runtime_home}" ]] || return 0
  local destination_identity
  destination_identity="$(stat -c '%d:%i' "${runtime_home}")" || return 1
  if ! "${bootstrap_python_resolved}" -I \
      "${stage_root}/deploy/public/runtime-home-guard.py" journal-write \
      "${profile_migration_journal}" "${release_candidate_marker_dir_identity}" \
      "${profile_migration_txid}" "${legacy_runtime_home}" "${legacy_runtime_identity}" \
      "${runtime_home}" "${destination_identity}" "${version}" "${release_commit}" >/dev/null; then
    return 1
  fi
  profile_migration_journal_state="prepared"
}
rebind_dispatcher_profile() {
  [[ "${profile_rebind_required}" == 1 ]] || return 0
  record_pending_profile_migration_journal || return 1
  # Persist the guarded systemd binding before publishing the profile copy.
  # A reboot between these steps sees ConditionPathExists=false and cannot
  # restart legacy; the atomic clone publishes the ready file and marker.
  write_profile_dropin || return 1
  profile_dropin_rebound=1
  systemctl daemon-reload || return 1
  validate_loaded_profile_guard || return 1
  deploy_hard_kill rebind-dropin-reloaded
  local effective_runtime_home
  effective_runtime_home="$(read_effective_systemd_runtime_home)"
  if [[ "${effective_runtime_home}" != "${runtime_home}" ]]; then
    printf 'effective systemd HERMES_HOME mismatch: expected %s, got %s\n' \
      "${runtime_home}" "${effective_runtime_home:-<unset>}" >&2
    return 1
  fi
  validate_effective_service_profile || return 1
  if [[ -f "${env_file}" ]]; then
    rewrite_profile_environment || return 1
    profile_env_rebound=1
  fi
  deploy_hard_kill rebind-env-rewritten
  effective_runtime_home="$(read_effective_systemd_runtime_home)"
  [[ "${effective_runtime_home}" == "${runtime_home}" ]] || return 1
  validate_effective_service_profile || return 1
  migrate_legacy_runtime_home || return 1
}
finalize_profile_migration_marker() {
  [[ "${profile_migration_journal_state}" != absent ]] || return 0
  [[ -n "${profile_migration_txid}" ]] || return 1
  "${bootstrap_python_resolved}" -I \
    "${stage_root}/deploy/public/runtime-home-guard.py" journal-remove \
    "${profile_migration_journal}" "${release_candidate_marker_dir_identity}" \
    "${profile_migration_txid}" >/dev/null || return 1
  profile_migration_journal_state="absent"
  # The fork is now the committed profile for this release; drop the pending
  # marker so later starts authorize purely on the committed state.
  rm -f -- "${runtime_home}/.hermes-dispatcher-migration.${profile_migration_txid}"
}
normalize_profile_environment_mode() {
  [[ "${env_mode_normalization_required}" == 1 ]] || return 0
  [[ -f "${env_file}" && ! -L "${env_file}" ]] || return 1
  env_mode_normalization_started=1
  validate_root_controlled_parent "$(dirname -- "${env_file}")" \
    "Hermes environment file" || return 1
  chmod 0600 "${env_file}" || return 1
  env_mode_normalized=1
}
ensure_runtime_home_after_stop() {
  local ensured_identity=""
  if [[ -d "${runtime_home}" && ! -L "${runtime_home}" \
      && "$(stat -c '%a' "${runtime_home}")" == 700 ]]; then
    # Mode 0700 is also the normal legacy service-owned profile mode.  Treat
    # it as sealed only when the external journal proves that this installer
    # created the seal; otherwise ensure-leaf below adopts the legacy inode.
    if [[ "${runtime_seal_journal_state}" == present ]]; then
      local sealed_identity sealed_parent_identity
      sealed_identity="$(stat -c '%d:%i' "${runtime_home}")" || return 1
      sealed_parent_identity="$(stat -c '%d:%i' "${release_candidate_marker_dir}")" || return 1
      "${bootstrap_python_resolved}" -I \
        "${stage_root}/deploy/public/runtime-home-guard.py" unseal \
        "${runtime_home}" "${sealed_identity}" "${runtime_seal_journal}" \
        "${sealed_parent_identity}" >/dev/null || return 1
      runtime_seal_journal_state="absent"
    fi
  fi
  case "${runtime_topology_mode}" in
    managed-dispatcher)
      ensured_identity="$(
        "${bootstrap_python_resolved}" -I \
          "${snapshot}/deploy/public/runtime-home-guard.py" ensure-managed \
          "${dispatcher_state_root}" "${dispatcher_profiles_root}" \
          "${runtime_home}" "${service_uid}" "${service_gid}"
      )" || {
        [[ "${HERMES_DEBUG_INSTALLER:-0}" == 1 ]] \
          && printf 'ensure-managed failed\n' >&2
        return 1
      }
      ;;
    custom-managed)
      local expected_parent_identity
      expected_parent_identity="$(stat -c '%d:%i' "${runtime_home_parent}")" \
        || return 1
      ensured_identity="$(
        "${bootstrap_python_resolved}" -I \
          "${snapshot}/deploy/public/runtime-home-guard.py" ensure-leaf \
          "${runtime_home_parent}" "${runtime_home}" \
          "${service_uid}" "${service_gid}" "${expected_parent_identity}"
      )" || {
        [[ "${HERMES_DEBUG_INSTALLER:-0}" == 1 ]] \
          && printf 'ensure-leaf failed (parent=%s leaf=%s uid=%s gid=%s expected=%s)\n' \
            "${runtime_home_parent}" "${runtime_home}" "${service_uid}" \
            "${service_gid}" "${expected_parent_identity}" >&2
        return 1
      }
      ;;
    pending-existing)
      ensured_identity="$(stat -c '%d:%i' "${runtime_home}")" || return 1
      [[ -n "${runtime_home_identity}" \
          && "${ensured_identity}" == "${runtime_home_identity}" ]] || return 1
      ;;
    *) return 1 ;;
  esac
  [[ "${ensured_identity}" =~ ^[0-9]+:[0-9]+$ ]] || return 1
  if [[ "${runtime_home_preexisting}" == 0 ]]; then
    runtime_home_created=1
    runtime_home_created_identity="${ensured_identity}"
  fi
  runtime_home_identity="${ensured_identity}"
  if [[ -d "${profile_parent}" && ! -L "${profile_parent}" ]]; then
    profile_parent_identity="$(stat -c '%d:%i' "${profile_parent}")" \
      || return 1
  fi
}
ensure_runtime_service_directories() {
  # Legacy profiles can contain root-only 0700 directories (for example
  # collaboration/account-files). Normalize each ancestor from the leaf down
  # while the service is stopped so the unprivileged SQLite/file helpers can
  # operate without granting access to the rest of the host.
  local target_path="$1" current relative component
  [[ "${target_path}" == "${runtime_home}/"* ]] || return 1
  relative="${target_path#${runtime_home}/}"
  current="${runtime_home}"
  local -a components=()
  IFS='/' read -r -a components <<<"${relative}"
  for component in "${components[@]}"; do
    [[ -n "${component}" && "${component}" != . && "${component}" != .. ]] || return 1
    current="${current}/${component}"
    run_profile_io_root ensure-owned-dir \
      "${current}" "${service_uid}" "${service_gid}" 0770 \
      || return 1
  done
}
seal_runtime_home() {
  [[ -d "${runtime_home}" && ! -L "${runtime_home}" ]] || return 1
  local identity parent_identity
  identity="$(stat -c '%d:%i' "${runtime_home}")" || return 1
  parent_identity="$(stat -c '%d:%i' "${release_candidate_marker_dir}")" || return 1
  "${bootstrap_python_resolved}" -I \
    "${stage_root}/deploy/public/runtime-home-guard.py" seal \
    "${runtime_home}" "${identity}" "${runtime_seal_journal}" \
    "${parent_identity}" >/dev/null || return 1
  runtime_seal_journal_state="present"
  runtime_home_identity="${identity}"
}
unseal_runtime_home() {
  [[ -d "${runtime_home}" && ! -L "${runtime_home}" ]] || return 1
  [[ "${runtime_seal_journal_state}" == present ]] || return 0
  local identity parent_identity
  identity="$(stat -c '%d:%i' "${runtime_home}")" || return 1
  parent_identity="$(stat -c '%d:%i' "${release_candidate_marker_dir}")" || return 1
  "${bootstrap_python_resolved}" -I \
    "${stage_root}/deploy/public/runtime-home-guard.py" unseal \
    "${runtime_home}" "${identity}" "${runtime_seal_journal}" \
    "${parent_identity}" >/dev/null || return 1
  runtime_seal_journal_state="absent"
}
release_phase prepare
early_recovery_service_stopped=0
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# The dispatcher profile leaf must exist (service-owned, mode 0700) before the
# service is quiesced: every guarded topology transition after the stop adopts
# this exact inode instead of racing a creation against a running writer. The
# managed-dispatcher topology instead ensures its whole root/parent/leaf chain
# through the descriptor-anchored guard after the stop.
if [[ "${runtime_topology_mode}" != "managed-dispatcher" \
    && ( ! -d "${runtime_home}" || -L "${runtime_home}" ) ]]; then
  install -d -o "${service_user}" -g "${service_group}" -m 0700 "${runtime_home}" \
    || die "could not create the dispatcher profile directory: ${runtime_home}"
fi

# Build and verify the dependency candidate while the current service and
# interpreter remain untouched. An explicit external HERMES_RUNTIME_PYTHON is
# used by the deployment harness and deliberately skips this production-only
# virtual-environment swap.
if [[ "${dependency_update_enabled}" == 1 ]]; then
  venv_swap_txid="$("${bootstrap_python_resolved}" -I -c 'import uuid; print(uuid.uuid4().hex)')"
  [[ "${venv_swap_txid}" =~ ^[0-9a-f]{32}$ ]] \
    || die "could not generate the runtime dependency transaction id"
  release_candidate_txid="${venv_swap_txid}"
  candidate_venv="${target_root}/.venv.candidate.${venv_swap_txid}"
  previous_venv="${target_root}/.venv.rollback-${venv_swap_txid}"
  [[ ! -e "${candidate_venv}" && ! -L "${candidate_venv}" ]] \
    || die "runtime dependency candidate already exists"
  [[ ! -e "${previous_venv}" && ! -L "${previous_venv}" ]] \
    || die "runtime dependency rollback path already exists"
  # Start from the known-good live environment so the candidate keeps the
  # exact interpreter the running service already validated; the locked
  # requirements are then upgraded into that copy in place.
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
  chown -R root:root "${candidate_venv}"
  chmod -R go-w "${candidate_venv}"
  sudo -u "${service_user}" -- "${candidate_venv}/bin/python" - <<'PY'
import mcp  # noqa: F401
from mcp.server import MCPServer
from starlette.concurrency import run_in_threadpool

assert MCPServer and run_in_threadpool
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
from mcp.server import MCPServer
from pydantic import BaseModel, SecretStr  # noqa: F401
from starlette.concurrency import run_in_threadpool  # noqa: F401

assert FastAPI and MCPServer
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
  sudo -u "${service_user}" -- "${dependency_validation_python}" -c 'from mcp.server import MCPServer; assert MCPServer' \
    || die "Hermes runtime candidate is missing the locked MCP SDK required by iOS MCP services"
  sudo -u "${service_user}" -- "${dependency_validation_python}" \
    -c 'from cryptography.hazmat.primitives.ciphers.aead import AESGCM; assert AESGCM' \
    || die "Hermes runtime candidate is missing AES-GCM support required by encrypted iOS hot and cold storage"
  sudo -u "${service_user}" -- env \
    PYTHONPATH="${target_root}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${dependency_validation_python}" \
    -c 'from agent.plugin_llm import PluginLlm; assert PluginLlm' \
    || die "Hermes runtime candidate is missing the host LLM facade required by iOS semantic analysis"
fi
release_watchdog_unit="hermes-release-watchdog-${release_candidate_txid}.service"
[[ "${release_watchdog_unit}" =~ ^hermes-release-watchdog-[0-9a-f]{32}\.service$ ]] \
  || die "release watchdog unit identity is invalid"

# Quiesce the state writer before taking the transactional state snapshot.
# Keep the service stopped until every runtime file has been atomically placed;
# rollback also stops it before restoring this snapshot.
systemctl stop "${service}"
assert_no_process_references "${target_root}" "${runtime_home}" \
  "${legacy_runtime_home}" \
  || die "a process still references a Hermes runtime tree after service stop"
validate_target_root_after_stop \
  || die "target root changed while preparing the deployment transaction"
ensure_runtime_home_after_stop \
  || die "could not establish the root-anchored dispatcher profile"
runtime_directory_targets=(
  "$(dirname -- "${state_target}")"
  "$(dirname -- "${config_target}")"
  "$(dirname -- "${cloud_files_database_target}")"
  "$(dirname -- "${mobile_auth_target}")"
  "$(dirname -- "${managed_installations_database_target}")"
  "$(dirname -- "${managed_nodes_target}")"
)
if [[ "${profile_migration_required}" != 1 ]]; then
  # A pending profile migration publishes the whole legacy layout through the
  # atomic clone, so the fresh dispatcher leaf must stay empty until then:
  # pre-created service directories would break the rmdir+mv publish and the
  # remove-empty rollback alike.
  for runtime_directory_target in "${runtime_directory_targets[@]}"; do
    [[ "${runtime_directory_target}" == "${runtime_home}"/* ]] || continue
    ensure_runtime_service_directories "${runtime_directory_target}" \
      || die "could not prepare dispatcher runtime directory: ${runtime_directory_target}"
  done
fi
backup_legacy_runtime_home \
  || die "could not snapshot the legacy dispatcher profile before migration"
backup_one "${state_target}" "${backup}/state/single.json" 0600
backup_sqlite "${cloud_files_database_target}" "${backup}/state/cloud-files-library.sqlite3"
backup_sqlite "${mobile_auth_target}" "${backup}/state/mobile-auth.db"
backup_sqlite "${managed_installations_database_target}" "${backup}/state/managed-installations.db"
backup_one "${managed_nodes_target}" "${backup}/state/managed-nodes.json" 0600
if [[ "${ios_enabled}" == 1 ]]; then
  backup_one "${config_target}" "${backup}/config/config.yaml" 0600
  backup_sqlite "${ios_database_target}" "${backup}/state/ios-intelligence.db"
  backup_sqlite "${ios_supervisor_target}" "${backup}/state/ios-mcp-supervisor.db"
fi
runtime_sqlite_snapshot_source="${runtime_home}"
if [[ "${profile_migration_required}" == 1 ]]; then
  runtime_sqlite_snapshot_source="${legacy_runtime_home}"
fi
backup_runtime_sqlite_tree "${runtime_sqlite_snapshot_source}" "${backup}/state/sqlite-tree"

install_atomic() {
  local source="$1"
  local destination="$2"
  local mode="${3:-0644}"
  [[ -f "${source}" && ! -L "${source}" ]] \
    || return 1
  # Profile/state files are deliberately service-owned and are published via
  # the descriptor-relative unprivileged helper.  The checked-out Python
  # surface under target_root is root-controlled code; publishing it through
  # that helper would fail on a 0755 root-owned checkout (and would grant the
  # service account write access to executable code).  Root-controlled code
  # goes through the hardened root atomic path below.
  if [[ "${destination}" == "${runtime_home}/"* ]]; then
    run_profile_io publish-stdin "${destination}" "${mode}" <"${source}"
  else
    install_root_atomic "${source}" "${destination}" "${mode}"
  fi
}
install_root_atomic() {
  local source="$1"
  local destination="$2"
  local mode="${3:-0644}"
  local temporary
  temporary="$(dirname "${destination}")/.$(basename "${destination}").install.$$"
  rm -f -- "${temporary}"
  install -o root -g root -m "${mode}" "${source}" "${temporary}"
  mv -f -- "${temporary}" "${destination}"
}
install_release_start_guard() {
  "${bootstrap_python_resolved}" -I - \
    "${snapshot}/deploy/public/candidate-start-guard.py" \
    "${release_start_guard}" \
    "${release_candidate_marker_dir_identity}" <<'PY'
import os
import pathlib
import stat
import sys
import uuid

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
expected_parent_identity = sys.argv[3]

source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
try:
    source_metadata = os.fstat(source_fd)
    if not stat.S_ISREG(source_metadata.st_mode) or source_metadata.st_size > 1024 * 1024:
        raise RuntimeError("candidate start guard source is unsafe")
    chunks = []
    while True:
        chunk = os.read(source_fd, 64 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    content = b"".join(chunks)
finally:
    os.close(source_fd)

parent_fd = os.open(
    target.parent,
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
)
temporary = f".{target.name}.install-{uuid.uuid4().hex}"
try:
    parent_metadata = os.fstat(parent_fd)
    parent_identity = f"{parent_metadata.st_dev}:{parent_metadata.st_ino}"
    parent_mode = stat.S_IMODE(parent_metadata.st_mode)
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != 0
        or parent_mode & 0o022
        or parent_identity != expected_parent_identity
    ):
        raise RuntimeError("candidate start guard directory changed")
    try:
        existing_fd = os.open(
            target.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        pass
    else:
        try:
            existing_metadata = os.fstat(existing_fd)
            existing_chunks = []
            while True:
                chunk = os.read(existing_fd, 64 * 1024)
                if not chunk:
                    break
                existing_chunks.append(chunk)
            current = os.stat(
                target.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(existing_metadata.st_mode)
                or existing_metadata.st_uid != 0
                or stat.S_IMODE(existing_metadata.st_mode) != 0o755
                or (current.st_dev, current.st_ino)
                != (existing_metadata.st_dev, existing_metadata.st_ino)
                or b"".join(existing_chunks) != content
            ):
                raise RuntimeError("immutable candidate start guard path changed")
            os.fsync(existing_fd)
            os.fsync(parent_fd)
        finally:
            os.close(existing_fd)
        raise SystemExit(0)
    destination_fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o755,
        dir_fd=parent_fd,
    )
    try:
        os.fchown(destination_fd, 0, 0)
        os.fchmod(destination_fd, 0o755)
        with os.fdopen(destination_fd, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(destination_fd)
    os.replace(
        temporary,
        target.name,
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
    )
    os.fsync(parent_fd)
    installed_fd = os.open(
        target.name,
        os.O_RDONLY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        installed_metadata = os.fstat(installed_fd)
        if (
            not stat.S_ISREG(installed_metadata.st_mode)
            or installed_metadata.st_uid != 0
            or stat.S_IMODE(installed_metadata.st_mode) != 0o755
            or os.read(installed_fd, len(content) + 1) != content
        ):
            raise RuntimeError("installed candidate start guard changed")
    finally:
        os.close(installed_fd)
except BaseException:
    try:
        os.unlink(temporary, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    raise
finally:
    os.close(parent_fd)
PY
}
# Normalize SQLite ownership and sidecars before the first service-user open.
# A stale root-owned -wal/-shm file makes SQLite report a misleading
# "disk I/O error" while enabling WAL, which previously aborted deployment.
prepare_sqlite_runtime_target() {
  local target="$1"
  [[ "${target}" == "${runtime_home}/"* ]] \
    || die "SQLite runtime target is outside the dispatcher profile: ${target}"
  local relative="${target#"${runtime_home}/"}"
  local -a relative_files=("${relative}")
  local suffix
  for suffix in -wal -shm -journal; do
    relative_files+=("${relative}${suffix}")
  done
  # A freshly migrated profile only contains the files the legacy worker
  # already had: new-layout databases and parents do not exist yet and are
  # created service-owned by the profile-I/O helper afterwards. Normalize
  # only the entries that actually exist so the descriptor-anchored guard
  # never has to open a not-yet-created parent component.
  local -a existing_files=()
  local candidate_relative
  for candidate_relative in "${relative_files[@]}"; do
    [[ -e "${runtime_home}/${candidate_relative}" \
        || -L "${runtime_home}/${candidate_relative}" ]] \
      || continue
    existing_files+=("${candidate_relative}")
  done
  if [[ "${#existing_files[@]}" -gt 0 ]]; then
    "${bootstrap_python_resolved}" -I \
      "${snapshot}/deploy/public/runtime-home-guard.py" normalize-files \
      "${runtime_home}" "${runtime_home_identity}" \
      "${service_uid}" "${service_gid}" "${existing_files[@]}" \
      || die "could not safely normalize legacy SQLite ownership: ${target}"
  fi
  # Re-assert normalized sidecar ownership directly: the -wal/-shm/-journal
  # siblings are transient SQLite files the service account must own
  # exclusively, even when an earlier crash left one behind root-owned.
  local sidecar
  for sidecar in "${runtime_home}/${relative}-wal" \
      "${runtime_home}/${relative}-shm" \
      "${runtime_home}/${relative}-journal"; do
    [[ -e "${sidecar}" || -L "${sidecar}" ]] || continue
    [[ -f "${sidecar}" && ! -L "${sidecar}" ]] \
      || die "SQLite sidecar is not a regular file: ${sidecar}"
    chown "${service_user}:${service_group}" "${sidecar}"
    chmod 0600 "${sidecar}"
  done
}
prepare_sqlite_service_target() {
  local target="$1"
  run_profile_io prepare-sqlite "${target}" \
    || die "could not prepare SQLite runtime target as the service account: ${target}"
}
# Point of no return: everything below replaces live files, so from here on
# rollback must restore the snapshots taken above instead of merely
# restarting the service.
install_release_start_guard \
  || die "could not install the durable candidate start guard"
mutated=1
rebind_dispatcher_profile \
  || die "could not migrate the public service to its isolated dispatcher profile"
migrate_dispatcher_auth_state \
  || die "could not migrate dispatcher authentication and shared Nous state"
seal_runtime_home \
  || die "could not seal the dispatcher profile before privileged topology changes"
normalize_profile_environment_mode \
  || die "could not restrict the Hermes environment file permissions"
for sqlite_target in \
  "${cloud_files_database_target}" \
  "${mobile_auth_target}" \
  "${managed_installations_database_target}" \
  "${ios_database_target}" \
  "${ios_supervisor_target}"; do
  prepare_sqlite_runtime_target "${sqlite_target}"
done
unseal_runtime_home \
  || die "could not reopen the dispatcher profile for service-owned preparation"
for sqlite_target in \
  "${cloud_files_database_target}" \
  "${mobile_auth_target}" \
  "${managed_installations_database_target}" \
  "${ios_database_target}" \
  "${ios_supervisor_target}"; do
  prepare_sqlite_service_target "${sqlite_target}"
done
if [[ "${dependency_update_enabled}" == 1 ]]; then
  [[ "$(stat -c '%d:%i' "${target_root}")" == "${target_root_identity}" ]] \
    || die "target root changed before the runtime dependency swap"
  venv_old_identity="$(stat -c '%d:%i' "${runtime_venv}")"
  venv_new_identity="$(stat -c '%d:%i' "${candidate_venv}")"
  [[ "${venv_old_identity}" =~ ^[0-9]+:[0-9]+$ \
      && "${venv_new_identity}" =~ ^[0-9]+:[0-9]+$ \
      && "${venv_old_identity}" != "${venv_new_identity}" ]] \
    || die "runtime dependency identities are invalid"
  write_venv_swap_journal prepared
  venv_swap_prepared=1
  deploy_hard_kill venv-prepared
  mv -f -- "${runtime_venv}" "${previous_venv}"
  fsync_target_root
  venv_old_moved=1
  deploy_hard_kill venv-old-moved
  mv -f -- "${candidate_venv}" "${runtime_venv}"
  fsync_target_root
  venv_swapped=1
  deploy_hard_kill venv-candidate-live
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
"${bootstrap_python_resolved}" -I - \
  "${snapshot}/deploy/public/managed-nodes.server.json" \
  "${managed_nodes_rendered}" \
  "${managed_node_token_file}" \
  "${managed_installation_token_file}" \
  "${hk_recovery_token_file}" \
  "${hk_enabled}" <<'PY'
import json
import pathlib
import sys

source, destination, status_token_file, installation_token_file, hk_recovery_token_file = map(
    pathlib.Path, sys.argv[1:6]
)
hk_enabled = sys.argv[6] == "1"
payload = json.loads(source.read_text(encoding="utf-8"))
for node in payload.get("nodes", []):
    if node.get("token_file") != "/etc/hermes-agent/dbb3-status-token":
        raise SystemExit("managed-nodes template has an unexpected status token path")
    if node.get("installation_token_file") != "/etc/hermes-agent/managed-installation-token":
        raise SystemExit("managed-nodes template has an unexpected installation token path")
    recovery_token_files = node.get("recovery_token_files") or {}
    if recovery_token_files.get("hk") != "/etc/hermes-agent/hk-recovery-token":
        raise SystemExit("managed-nodes template has an unexpected HK recovery token path")
    node["token_file"] = str(status_token_file)
    node["installation_token_file"] = str(installation_token_file)
    recovery_urls = node.get("recovery_urls") or {}
    if hk_enabled:
        recovery_token_files["hk"] = str(hk_recovery_token_file)
    else:
        recovery_urls.pop("hk", None)
        recovery_token_files.pop("hk", None)
    node["recovery_urls"] = recovery_urls
    if recovery_token_files:
        node["recovery_token_files"] = recovery_token_files
    else:
        node.pop("recovery_token_files", None)
destination.write_text(
    json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
    encoding="utf-8",
)
PY
chown "root:${service_group}" "${managed_nodes_rendered}"
chmod 0640 "${managed_nodes_rendered}"
run_profile_io publish-file "${managed_nodes_rendered}" "${managed_nodes_target}" 0600 \
  || die "could not publish the managed-node catalog as the service account"
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
trap '' INT TERM HUP
runtime_candidate_started=1
if ! release_candidate_marker_action write \
    "${runtime_home}" "${release_candidate_txid}" \
    "${version}" "${release_commit}"; then
  # The helper can report an error after os.replace (for example, while
  # cleaning an old temporary or fsyncing the parent). Re-read the exact
  # marker before deciding that publication failed: once it names this
  # candidate, the durable state wins and rollback must never restore old
  # code/state over it.
    if ! release_candidate_marker_action validate \
      "${runtime_home}" "${release_candidate_txid}" \
      "${version}" "${release_commit}"; then
    runtime_candidate_started=0
    false
  fi
  # The marker was published, but its parent fsync or a post-publish operation
  # failed. Treat the candidate as authoritative and leave it stopped; never
  # continue to publish a startup barrier whose durability is uncertain.
  release_candidate_pending=1
  release_retry_stopped=1
  false
fi
# The systemd ExecCondition accepts the pending candidate only while this
# exact installer PID/start-time/boot lease remains live. A hard kill after
# this point leaves a dead lease, so reboot, Restart=always, and manual starts
# all fail closed even if the ready barrier was already published.
if ! write_release_start_lease; then
  printf '%s\n' "could not publish the candidate start lease" >&2
  false
fi
release_start_lease_written=1
if ! start_release_watchdog; then
  printf '%s\n' "could not start the candidate release watchdog" >&2
  false
fi
deploy_hard_kill candidate-marker-written
if [[ "${venv_swapped}" == 1 ]]; then
  if ! write_venv_swap_journal candidate; then
    # The release marker has already been published. Keep the candidate
    # authoritative and stopped; the next run will recover the prepared
    # journal (or fail closed) rather than mixing it with restored state.
    false
  fi
fi
deploy_hard_kill venv-candidate-journal
publish_dispatcher_ready \
  || { printf '%s\n' "could not publish the dispatcher startup barrier" >&2; false; }
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
deploy_hard_kill candidate-authoritative
service_start_since="$(date '+%Y-%m-%d %H:%M:%S')"
systemctl reset-failed "${service}" >/dev/null 2>&1 || true
systemctl start "${service}"
deploy_hard_kill candidate-running

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
"${bootstrap_python_resolved}" -I - "${health_file}" <<'PY'
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
"${bootstrap_python_resolved}" -I - "${handshake_file}" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data.get("api_version") == 1
assert isinstance(data.get("hermes_version"), str) and data["hermes_version"]
assert isinstance(data.get("profiles"), list)
assert isinstance(data.get("capabilities"), list)
assert isinstance(data.get("server_time"), int) and data["server_time"] > 0
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
"${bootstrap_python_resolved}" -I - \
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
publish_release_evidence "${release_evidence_temp}"
fabric_release_published=1
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
      && "${bootstrap_python_resolved}" -I - "${node_health}" "${node}" \
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
      "${bootstrap_python_resolved}" -I - "${node_health}" <<'PY' >&2 || true
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
  installation_probe_id="mi-$(${bootstrap_python_resolved} -I -c 'import uuid; print(uuid.uuid4().hex)')"
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
  "${bootstrap_python_resolved}" -I - \
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
"${bootstrap_python_resolved}" -I - "${release_evidence_target}" \
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
publish_release_evidence "${release_evidence_temp}"
release_evidence_temp=""
trap '' INT TERM HUP
if [[ "${venv_swapped}" == 1 ]]; then
  # From the durable committed transition through the shell commit flag, TERM
  # must not enter rollback and combine the new dependency tree with old code.
  write_venv_swap_journal committed
  venv_swap_committed=1
fi
if ! release_candidate_marker_action remove \
    "${runtime_home}" "${release_candidate_txid}" \
    "${version}" "${release_commit}"; then
  printf '%s\n' \
    "could not clear the release candidate marker after commit" >&2
  false
fi
release_candidate_pending=0
deploy_hard_kill candidate-marker-committed
# Detach the committed service only after marker absence is durable. Until
# daemon-reload confirms this exact BindsTo removal, installer death makes the
# transient watchdog disappear and systemd stops the still-bound service.
release_watchdog_binding=0
write_profile_dropin \
  || { printf '%s\n' "could not detach the committed release watchdog" >&2; false; }
systemctl daemon-reload \
  || { printf '%s\n' "could not reload the detached release watchdog" >&2; false; }
validate_loaded_profile_guard present \
  || { printf '%s\n' "committed release watchdog remained effectively bound" >&2; false; }
deploy_hard_kill watchdog-detached
installed=1
release_retry_stopped=0
early_recovery_service_stopped=0
if ! stop_release_watchdog; then
  printf '%s\n' "warning: detached committed release watchdog did not exit" >&2
fi
# Marker absence is the durable commit authority: from here every systemd
# start is allowed even if this process is interrupted. The now-irrelevant
# lease is cleaned afterward so there is no lease-removed/marker-present
# window in which a service crash could turn a good commit into a skipped
# restart.
if remove_release_start_lease; then
  release_start_lease_written=0
else
  printf '%s\n' \
    "warning: committed release retained an ignored candidate start lease" >&2
fi
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
deploy_hard_kill venv-committed-cleanup
if ! finalize_profile_migration_marker; then
  printf '%s\n' \
    "warning: committed release retained its dispatcher migration marker for safe retry" >&2
fi
if [[ "${venv_swap_committed}" == 1 ]]; then
  if ! recover_venv_swap; then
    printf '%s\n' \
      "warning: committed runtime environment retained its recovery journal" >&2
  else
    # The replaced environment is a bounded deployment artifact; the committed
    # release no longer needs it and its only purpose was rollback recovery.
    if [[ -n "${previous_venv}" && -d "${previous_venv}" \
        && ! -L "${previous_venv}" ]]; then
      rm -rf -- "${previous_venv}"
    fi
    previous_venv=""
    candidate_venv=""
  fi
fi
# A condition skip can make `systemctl start` return success while leaving a
# unit inactive. Re-probe the committed release after all marker/lease and
# venv cleanup, explicitly restarting it now that marker absence authorizes
# every start, and never print `service=active` without a fresh HTTP response.
if ! systemctl is-active --quiet "${service}"; then
  start_and_verify_active \
    || { printf '%s\n' "committed service could not be restarted" >&2; false; }
fi
committed_healthy=0
for _ in $(seq 1 30); do
  if systemctl is-active --quiet "${service}" \
    && curl --fail --silent --show-error --max-time 3 --noproxy '*' \
      http://127.0.0.2:9119/api/status >"${health_file}"; then
    committed_healthy=1
    break
  fi
  sleep 1
done
[[ "${committed_healthy}" == 1 ]] || {
  printf '%s\n' "committed service did not remain healthy" >&2
  emit_service_failure_diagnostics >&2
  false
}
rm -rf -- "${transaction}" "${health_file}" "${handshake_file}" \
  "${ios_health_file}" "${connector_health_file}" "${deployment_health_file}" \
  "${curl_cfg}"
printf 'service=active\nversion=%s\ncommit=%s\nmanifest_sha256=%s\nbackup=%s\nevidence=%s\n' \
  "${version}" "${release_commit}" "${manifest_sha256}" "${backup}" \
  "${release_evidence_target}"
