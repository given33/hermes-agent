#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Public host defaults to the WireGuard address requested by the deployment
# contract. Override HERMES_PUBLIC_REMOTE when running from a network that can
# only reach the public SSH address (for example admin@8.138.40.16).

die() { printf 'deploy-collaboration-backend: %s\n' "$*" >&2; exit 1; }
repo="${HERMES_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
remote="${HERMES_PUBLIC_REMOTE:-admin@10.66.0.1}"
version="${HERMES_COLLABORATION_VERSION:-}"

resolve_git_head() {
  local gitdir head
  head="$(git -C "${repo}" rev-parse HEAD 2>/dev/null || true)"
  if [[ "${head}" =~ ^[0-9a-f]{40}$ ]]; then
    printf '%s\n' "${head}"
    return 0
  fi

  # Worktrees created by Git for Windows store a Windows gitdir path. Resolve
  # it explicitly when this deployer runs from WSL.
  if [[ -f "${repo}/.git" ]]; then
    gitdir="$(sed -n 's/^gitdir: //p' "${repo}/.git")"
    if [[ "${gitdir}" =~ ^[A-Za-z]:[/\\] ]] && command -v wslpath >/dev/null 2>&1; then
      gitdir="$(wslpath -u "${gitdir}")"
    fi
    head="$(git --git-dir="${gitdir}" --work-tree="${repo}" rev-parse HEAD 2>/dev/null || true)"
  fi
  printf '%s\n' "${head}"
}

release_commit="${HERMES_RELEASE_COMMIT:-$(resolve_git_head)}"
installer="${repo}/deploy/public/install-collaboration-backend.sh"
local_python="${HERMES_LOCAL_PYTHON:-}"
if [[ -z "${local_python}" ]]; then
  local_python="$(command -v python3 || command -v python || true)"
fi

[[ -f "${installer}" ]] || die "installer is missing"
[[ "${release_commit}" =~ ^[0-9a-f]{40}$ ]] || die "release commit must be a full lowercase Git commit"
[[ -n "${local_python}" && -x "${local_python}" ]] || die "local Python runtime is missing"
[[ -f "${repo}/plugins/collaboration/dashboard/plugin_api.py" ]] || die "plugin_api.py is missing"
[[ -f "${repo}/plugins/collaboration/dashboard/manifest.json" ]] || die "manifest.json is missing"
[[ -f "${repo}/plugins/collaboration/dashboard/dist/index.js" ]] || die "dist/index.js is missing"
[[ -f "${repo}/hermes_cli/cloud_file_library.py" ]] || die "cloud_file_library.py is missing"
[[ -f "${repo}/hermes_cli/dashboard_auth/public_paths.py" ]] || die "public_paths.py is missing"
[[ -f "${repo}/hermes_cli/dashboard_auth/token_auth.py" ]] || die "token_auth.py is missing"
[[ -f "${repo}/hermes_cli/dashboard_auth/mobile_device_store.py" ]] || die "mobile_device_store.py is missing"
[[ -f "${repo}/hermes_cli/dashboard_auth/mobile_notifications.py" ]] || die "mobile_notifications.py is missing"
[[ -f "${repo}/hermes_cli/web_server.py" ]] || die "web_server.py is missing"
[[ -f "${repo}/hermes_cli/managed_installations.py" ]] || die "managed_installations.py is missing"
[[ -f "${repo}/tools/managed_installation_tool.py" ]] || die "managed_installation_tool.py is missing"
[[ -f "${repo}/toolsets.py" ]] || die "toolsets.py is missing"
[[ -f "${repo}/agent/agent_init.py" ]] || die "agent_init.py is missing"
[[ -f "${repo}/agent/prompt_builder.py" ]] || die "prompt_builder.py is missing"
[[ -f "${repo}/agent/system_prompt.py" ]] || die "system_prompt.py is missing"
[[ -f "${repo}/agent/context_diagnostics.py" ]] || die "context_diagnostics.py is missing"
[[ -f "${repo}/hermes_cli/doctor.py" ]] || die "doctor.py is missing"
[[ -f "${repo}/tui_gateway/server.py" ]] || die "tui_gateway/server.py is missing"
[[ -f "${repo}/deploy/public/nginx-00-hermes-security.conf" ]] || die "nginx security config is missing"
[[ -f "${repo}/deploy/public/nginx-daxueshenmai.top.conf" ]] || die "nginx site config is missing"
[[ -f "${repo}/deploy/public/managed-nodes.server.json" ]] || die "managed-nodes server config is missing"
[[ -f "${repo}/deploy/public/runtime-requirements.lock" ]] || die "runtime dependency lock is missing"
[[ -f "${repo}/deploy/recovery/configure-main-managed-installation-ssh.sh" ]] \
  || die "managed installation SSH configurator is missing"

ios_hermes_assets=(
  "hermes_cli/account_cleanup.py"
  "hermes_cli/ios_intelligence.py"
  "hermes_cli/ios_intelligence_config.py"
  "hermes_cli/ios_intelligence_scheduler.py"
  "hermes_cli/ios_intelligence_supervisor.py"
  "hermes_cli/ios_mcp_supervisor.py"
  "hermes_cli/ios_mcp_server.py"
)
ios_plugin_assets=(
  "plugins/ios-intelligence/dashboard/plugin_api.py"
  "plugins/ios-intelligence/dashboard/manifest.json"
)
ios_tool_assets=(
  "tools/mcp_tool.py"
)
ios_support_assets=(
  "hermes_cli/dashboard_auth/__init__.py"
  "hermes_cli/dashboard_auth/owner_mobile.py"
  "hermes_cli/dashboard_auth/registry.py"
  "hermes_cli/dashboard_auth/routes.py"
  "hermes_cli/profiles.py"
  "hermes_cli/managed_nodes.py"
  "hermes_cli/managed_node_recovery_service.py"
  "plugins/dashboard_auth/basic/__init__.py"
)
# Build the deployed Python surface from the immutable Git index instead of a
# hand-maintained import subset. Hermes 0.20 split startup/runtime code across
# many new modules; deploying only direct entrypoints leaves the public host on
# a mixed 0.19/0.20 import graph. Only tracked production Python sources below
# approved roots are eligible, so tests, caches, build output, and untracked
# operator files can never enter the release stage.
runtime_source_manifest="$(mktemp)"
cleanup_runtime_source_manifest() {
  rm -f -- "${runtime_source_manifest}"
}
trap cleanup_runtime_source_manifest EXIT
git -C "${repo}" ls-files -z -- \
  agent gateway hermes_cli hermes_runtime hermes_services tools tui_gateway \
  providers cron acp_adapter plugins ':(top,glob)*.py' \
  | while IFS= read -r -d '' relative; do
      case "${relative}" in
        *.py) ;;
        # Known runtime data assets read at start-up (with in-code
        # fallbacks when missing); ship them instead of silently dropping.
        gateway/assets/*.yaml|hermes_cli/data/*.json) ;;
        *) continue ;;
      esac
      case "${relative}" in
        */tests/*|*/test_*.py|*/__pycache__/*) continue ;;
      esac
      [[ "${relative}" =~ ^[A-Za-z0-9_.+/-]+$ ]] \
        || die "runtime source path contains unsupported characters: ${relative}"
      case "/${relative}/" in
        *'//'*|*'/../'*|*'/./'*) die "unsafe runtime source path: ${relative}" ;;
      esac
      printf '%s\0' "${relative}"
    done \
  | sort -zu >"${runtime_source_manifest}"
mapfile -d '' -t runtime_service_assets <"${runtime_source_manifest}"
(( ${#runtime_service_assets[@]} >= 500 )) \
  || die "runtime source manifest is unexpectedly incomplete"
required_runtime_sources=(
  "hermes_auth_errors.py"
  "hermes_cli/web_models.py"
  "agent/interrupt_compat.py"
  "gateway/streaming_tts_consumer.py"
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
  "hermes_constants.py"
  "hermes_logging.py"
  "hermes_secret_compare.py"
  "utils.py"
  "hermes_cli/account_identity.py"
  "hermes_cli/account_lifecycle.py"
  "hermes_cli/collaboration_plugin_backend.py"
  "hermes_cli/ios_plugin_backend.py"
  "hermes_cli/account_session_facade.py"
  "hermes_cli/account_write_approvals.py"
  "hermes_cli/mobile_console.py"
  "plugins/account_cleanup_backend.py"
  "agent/conversation_loop.py"
  "agent/tool_executor.py"
  "agent/transports/hermes_tools_mcp_server.py"
  "gateway/platforms/api_server.py"
  "hermes_cli/dashboard_auth/client_ip.py"
  "hermes_cli/mcp_config.py"
  "plugins/memory/config_schema.py"
  "run_agent.py"
  "tools/file_operations.py"
  "tools/mcp_oauth_manager.py"
  "tools/registry.py"
  "tools/skills_guard.py"
  "tools/terminal_tool.py"
  "hermes_cli/sqlite_util.py"
)
for required_runtime_source in "${required_runtime_sources[@]}"; do
  runtime_source_found=0
  for relative in "${runtime_service_assets[@]}"; do
    if [[ "${relative}" == "${required_runtime_source}" ]]; then
      runtime_source_found=1
      break
    fi
  done
  [[ "${runtime_source_found}" == 1 ]] \
    || die "runtime source manifest omitted ${required_runtime_source}"
done
for relative in "${ios_hermes_assets[@]}" "${ios_plugin_assets[@]}" \
  "${ios_tool_assets[@]}" "${ios_support_assets[@]}" \
  "${runtime_service_assets[@]}"; do
  [[ -f "${repo}/${relative}" && ! -L "${repo}/${relative}" ]] || die "${relative} is missing"
done

if [[ -z "${version}" ]]; then
  version="$("${local_python}" - "${repo}/plugins/collaboration/dashboard/manifest.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("version", ""))
PY
)"
fi
[[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "manifest version is invalid"

timestamp="$(date +%Y%m%d-%H%M%S)-$$"
stage_root="${HERMES_PUBLIC_STAGE_ROOT:-}"
ssh_args=(-o BatchMode=yes -o ConnectTimeout=12)
if [[ -n "${HERMES_SSH_IDENTITY:-}" ]]; then
  ssh_args+=(-i "${HERMES_SSH_IDENTITY}" -o IdentitiesOnly=yes)
fi
if [[ -n "${HERMES_SSH_KNOWN_HOSTS:-}" ]]; then
  [[ "${HERMES_SSH_KNOWN_HOSTS}" == /* ]] \
    || die "HERMES_SSH_KNOWN_HOSTS must be an absolute path"
  [[ -f "${HERMES_SSH_KNOWN_HOSTS}" && ! -L "${HERMES_SSH_KNOWN_HOSTS}" ]] \
    || die "pinned SSH known-hosts file is missing or unsafe"
  ssh_args+=(
    -o "UserKnownHostsFile=${HERMES_SSH_KNOWN_HOSTS}"
    -o StrictHostKeyChecking=yes
  )
elif [[ "${HERMES_REQUIRE_PINNED_SSH_HOST_KEY:-0}" == 1 ]]; then
  die "a pinned SSH known-hosts file is required"
fi

if [[ -n "${stage_root}" ]]; then
  [[ "${stage_root}" == /* ]] || die "HERMES_PUBLIC_STAGE_ROOT must be absolute"
else
  # The runtime home can be full even while a tmpfs still has room for the
  # short-lived release stage. Probe remotely before creating any files so a
  # failed tar stream cannot leave a half-uploaded deployment behind.
  stage_root="$({
    ssh "${ssh_args[@]}" "${remote}" '
      for root in /dev/shm/hermes-agent-deploy /tmp/hermes-agent-deploy /home/admin/.cache/hermes-agent-deploy; do
        if ! mkdir -p -- "$root" 2>/dev/null; then
          continue
        fi
        available="$(df -Pk -- "$root" 2>/dev/null | awk '\''NR == 2 {print $4}'\'')"
        case "$available" in
          ""|*[!0-9]*) continue ;;
        esac
        if [ "$available" -ge 32768 ]; then
          printf "%s\n" "$root"
          exit 0
        fi
      done
      exit 1
    ' 2>/dev/null
  } || true)"
  [[ -n "${stage_root}" ]] || die "remote staging filesystems have insufficient free space"
fi
stage="${stage_root%/}/${version}-${timestamp}"
stage_created=0
cleanup_remote_stage() {
  local status=$?
  trap - EXIT
  if [[ "${stage_created}" == 1 ]]; then
    if ! ssh "${ssh_args[@]}" "${remote}" "rm -rf -- '${stage}'"; then
      printf '%s\n' "deploy-collaboration-backend: remote stage cleanup failed" >&2
      [[ "${status}" != 0 ]] || status=1
    fi
  fi
  cleanup_runtime_source_manifest
  exit "${status}"
}
trap cleanup_remote_stage EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

stage_created=1
ssh "${ssh_args[@]}" "${remote}" "install -d -m 0700 '${stage}' '${stage}/agent' '${stage}/plugins/collaboration/dashboard/dist' '${stage}/hermes_cli' '${stage}/hermes_cli/dashboard_auth' '${stage}/hermes_services' '${stage}/tui_gateway' '${stage}/plugins/ios-intelligence/dashboard' '${stage}/plugins/dashboard_auth/basic' '${stage}/tools' '${stage}/deploy/public' '${stage}/deploy/recovery'"
scp "${ssh_args[@]}" \
  "${repo}/plugins/collaboration/dashboard/plugin_api.py" \
  "${repo}/plugins/collaboration/dashboard/manifest.json" \
  "${remote}:${stage}/plugins/collaboration/dashboard/"
scp "${ssh_args[@]}" \
  "${repo}/plugins/collaboration/dashboard/dist/index.js" \
  "${remote}:${stage}/plugins/collaboration/dashboard/dist/"
scp "${ssh_args[@]}" \
  "${repo}/hermes_cli/cloud_file_library.py" \
  "${repo}/hermes_cli/managed_installations.py" \
  "${repo}/hermes_cli/web_server.py" \
  "${remote}:${stage}/hermes_cli/"
scp "${ssh_args[@]}" \
  "${repo}/tools/managed_installation_tool.py" \
  "${remote}:${stage}/tools/"
scp "${ssh_args[@]}" \
  "${repo}/toolsets.py" \
  "${remote}:${stage}/"
scp "${ssh_args[@]}" \
  "${repo}/agent/agent_init.py" \
  "${repo}/agent/prompt_builder.py" \
  "${repo}/agent/system_prompt.py" \
  "${repo}/agent/context_diagnostics.py" \
  "${remote}:${stage}/agent/"
scp "${ssh_args[@]}" \
  "${repo}/hermes_cli/doctor.py" \
  "${remote}:${stage}/hermes_cli/"
scp "${ssh_args[@]}" \
  "${repo}/hermes_cli/dashboard_auth/public_paths.py" \
  "${repo}/hermes_cli/dashboard_auth/token_auth.py" \
  "${repo}/hermes_cli/dashboard_auth/mobile_device_store.py" \
  "${repo}/hermes_cli/dashboard_auth/mobile_notifications.py" \
  "${remote}:${stage}/hermes_cli/dashboard_auth/"
scp "${ssh_args[@]}" \
  "${repo}/tui_gateway/server.py" \
  "${remote}:${stage}/tui_gateway/"
scp "${ssh_args[@]}" \
  "${repo}/hermes_cli/account_cleanup.py" \
  "${repo}/hermes_cli/ios_intelligence.py" \
  "${repo}/hermes_cli/ios_intelligence_config.py" \
  "${repo}/hermes_cli/ios_intelligence_scheduler.py" \
  "${repo}/hermes_cli/ios_intelligence_supervisor.py" \
  "${repo}/hermes_cli/ios_mcp_supervisor.py" \
  "${repo}/hermes_cli/ios_mcp_server.py" \
  "${remote}:${stage}/hermes_cli/"
scp "${ssh_args[@]}" \
  "${repo}/plugins/ios-intelligence/dashboard/plugin_api.py" \
  "${repo}/plugins/ios-intelligence/dashboard/manifest.json" \
  "${remote}:${stage}/plugins/ios-intelligence/dashboard/"
scp "${ssh_args[@]}" \
  "${repo}/tools/mcp_tool.py" \
  "${remote}:${stage}/tools/"
scp "${ssh_args[@]}" \
  "${repo}/hermes_cli/dashboard_auth/__init__.py" \
  "${repo}/hermes_cli/dashboard_auth/owner_mobile.py" \
  "${repo}/hermes_cli/dashboard_auth/registry.py" \
  "${repo}/hermes_cli/dashboard_auth/routes.py" \
  "${remote}:${stage}/hermes_cli/dashboard_auth/"
scp "${ssh_args[@]}" \
  "${repo}/hermes_cli/profiles.py" \
  "${repo}/hermes_cli/managed_nodes.py" \
  "${repo}/hermes_cli/managed_node_recovery_service.py" \
  "${remote}:${stage}/hermes_cli/"
scp "${ssh_args[@]}" \
  "${repo}/plugins/dashboard_auth/basic/__init__.py" \
  "${remote}:${stage}/plugins/dashboard_auth/basic/"
scp "${ssh_args[@]}" \
  "${repo}/hermes_services/__init__.py" \
  "${repo}/hermes_services/application.py" \
  "${repo}/hermes_services/auth.py" \
  "${repo}/hermes_services/behavior_eval.py" \
  "${repo}/hermes_services/bounded_dict.py" \
  "${repo}/hermes_services/contexts.py" \
  "${repo}/hermes_services/contracts.py" \
  "${repo}/hermes_services/cron_fire.py" \
  "${repo}/hermes_services/hosted_event_protocol.py" \
  "${repo}/hermes_services/hosted_role_migration.py" \
  "${repo}/hermes_services/http_boundary.py" \
  "${repo}/hermes_services/http_policy.py" \
  "${repo}/hermes_services/internal_hooks.py" \
  "${repo}/hermes_services/jsonrpc.py" \
  "${repo}/hermes_services/latency_trace.py" \
  "${repo}/hermes_services/low_latency_protocol.py" \
  "${repo}/hermes_services/middleware.py" \
  "${repo}/hermes_services/resource_catalog.py" \
  "${repo}/hermes_services/session_entries.py" \
  "${repo}/hermes_services/session_registry.py" \
  "${repo}/hermes_services/startup.py" \
  "${repo}/hermes_services/tool_contract.py" \
  "${repo}/hermes_services/tool_isolation.py" \
  "${repo}/hermes_services/tool_output_artifacts.py" \
  "${repo}/hermes_services/worker_channel.py" \
  "${remote}:${stage}/hermes_services/"
scp "${ssh_args[@]}" \
  "${repo}/hermes_cli/account_identity.py" \
  "${repo}/hermes_cli/account_lifecycle.py" \
  "${repo}/hermes_cli/account_session_facade.py" \
  "${repo}/hermes_cli/account_write_approvals.py" \
  "${repo}/hermes_cli/mobile_console.py" \
  "${remote}:${stage}/hermes_cli/"
# Preserve relative paths for the complete tracked runtime surface. The root
# installer validates and snapshots every member before replacing live files.
tar -C "${repo}" --null -T "${runtime_source_manifest}" -cf - \
  | ssh "${ssh_args[@]}" "${remote}" \
      "tar --no-same-owner -C '${stage}' -xf -"
scp "${ssh_args[@]}" \
  "${repo}/deploy/public/nginx-00-hermes-security.conf" \
  "${repo}/deploy/public/nginx-daxueshenmai.top.conf" \
  "${repo}/deploy/public/managed-nodes.server.json" \
  "${repo}/deploy/public/runtime-requirements.lock" \
  "${remote}:${stage}/deploy/public/"
scp "${ssh_args[@]}" \
  "${runtime_source_manifest}" \
  "${remote}:${stage}/deploy/public/runtime-source-files.nul"
scp "${ssh_args[@]}" \
  "${repo}/deploy/recovery/configure-main-managed-installation-ssh.sh" \
  "${remote}:${stage}/deploy/recovery/"
scp "${ssh_args[@]}" "${installer}" "${remote}:${stage}/install-collaboration-backend.sh"
ssh "${ssh_args[@]}" "${remote}" "chmod 0700 '${stage}/deploy/recovery/configure-main-managed-installation-ssh.sh'; sudo -n /bin/bash '${stage}/deploy/recovery/configure-main-managed-installation-ssh.sh'"
recovery_attempts="${HERMES_PUBLIC_FABRIC_RECOVERY_ATTEMPTS:-2}"
[[ "${recovery_attempts}" =~ ^[1-9][0-9]*$ ]] \
  || die "HERMES_PUBLIC_FABRIC_RECOVERY_ATTEMPTS must be a positive integer"
for attempt in $(seq 1 "${recovery_attempts}"); do
  installer_status=0
  if ssh "${ssh_args[@]}" "${remote}" \
      "chmod 0700 '${stage}/install-collaboration-backend.sh'; sudo -n /bin/bash '${stage}/install-collaboration-backend.sh' '${version}' '${stage}' '${release_commit}'"; then
    break
  else
    installer_status=$?
  fi
  if [[ "${installer_status}" != 75 || "${attempt}" == "${recovery_attempts}" ]]; then
    exit "${installer_status}"
  fi
  printf 'fabric convergence is pending; retrying public transaction (%s/%s)\n' \
    "$((attempt + 1))" "${recovery_attempts}" >&2
  sleep 30
done
