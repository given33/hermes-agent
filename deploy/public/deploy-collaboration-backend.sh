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
release_commit="${HERMES_RELEASE_COMMIT:-$(git -C "${repo}" rev-parse HEAD 2>/dev/null || true)}"
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
  "hermes_cli/profiles.py"
  "hermes_cli/managed_nodes.py"
  "hermes_cli/managed_node_recovery_service.py"
  "plugins/dashboard_auth/basic/__init__.py"
)
runtime_service_assets=(
  "agent/agent_runtime_helpers.py"
  "agent/chat_completion_helpers.py"
  "agent/conversation_compression.py"
  "agent/conversation_loop.py"
  "agent/curator_backup.py"
  "agent/lsp/workspace.py"
  "agent/shell_hooks.py"
  "agent/tool_dispatch_helpers.py"
  "agent/tool_executor.py"
  "agent/transports/hermes_tools_mcp_server.py"
  "gateway/hooks.py"
  "gateway/platforms/api_server.py"
  "gateway/run.py"
  "hermes_cli/backup.py"
  "hermes_cli/dashboard_auth/base.py"
  "hermes_cli/main.py"
  "hermes_cli/mcp_config.py"
  "hermes_cli/plugins.py"
  "hermes_cli/profile_distribution.py"
  "hermes_services/__init__.py"
  "hermes_services/application.py"
  "hermes_services/auth.py"
  "hermes_services/behavior_eval.py"
  "hermes_services/contexts.py"
  "hermes_services/contracts.py"
  "hermes_services/cron_fire.py"
  "hermes_services/hosted_event_protocol.py"
  "hermes_services/http_boundary.py"
  "hermes_services/http_policy.py"
  "hermes_services/internal_hooks.py"
  "hermes_services/jsonrpc.py"
  "hermes_services/middleware.py"
  "hermes_services/resource_catalog.py"
  "hermes_services/session_entries.py"
  "hermes_services/session_registry.py"
  "hermes_services/startup.py"
  "hermes_services/tool_contract.py"
  "hermes_services/tool_isolation.py"
  "hermes_services/tool_output_artifacts.py"
  "hermes_cli/account_identity.py"
  "hermes_cli/account_lifecycle.py"
  "hermes_cli/collaboration_plugin_backend.py"
  "hermes_cli/ios_plugin_backend.py"
  "hermes_cli/account_session_facade.py"
  "hermes_cli/account_write_approvals.py"
  "hermes_cli/mobile_console.py"
  "hermes_state.py"
  "mcp_serve.py"
  "model_tools.py"
  "plugins/context_engine/__init__.py"
  "plugins/account_cleanup_backend.py"
  "plugins/cron_providers/__init__.py"
  "plugins/memory/__init__.py"
  "plugins/memory/config_schema.py"
  "providers/__init__.py"
  "run_agent.py"
  "tools/code_execution_tool.py"
  "tools/computer_use/cua_backend.py"
  "tools/credential_files.py"
  "tools/file_operations.py"
  "tools/file_tools.py"
  "tools/lazy_deps.py"
  "tools/mcp_oauth.py"
  "tools/mcp_oauth_manager.py"
  "tools/registry.py"
  "tools/skills_guard.py"
  "tools/skills_hub.py"
  "tools/terminal_tool.py"
  "tools/tool_result_storage.py"
)
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
stage="/home/admin/.cache/hermes-agent-deploy/${version}-${timestamp}"
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
  "${repo}"/hermes_services/*.py \
  "${remote}:${stage}/hermes_services/"
scp "${ssh_args[@]}" \
  "${repo}/hermes_cli/account_identity.py" \
  "${repo}/hermes_cli/account_lifecycle.py" \
  "${repo}/hermes_cli/account_session_facade.py" \
  "${repo}/hermes_cli/account_write_approvals.py" \
  "${repo}/hermes_cli/mobile_console.py" \
  "${remote}:${stage}/hermes_cli/"
# Preserve relative paths for the complete changed runtime surface. The root
# installer validates and snapshots every member before replacing live files.
tar -C "${repo}" -cf - -- "${runtime_service_assets[@]}" \
  | ssh "${ssh_args[@]}" "${remote}" \
      "tar --no-same-owner -C '${stage}' -xf -"
scp "${ssh_args[@]}" \
  "${repo}/deploy/public/nginx-00-hermes-security.conf" \
  "${repo}/deploy/public/nginx-daxueshenmai.top.conf" \
  "${repo}/deploy/public/managed-nodes.server.json" \
  "${repo}/deploy/public/runtime-requirements.lock" \
  "${remote}:${stage}/deploy/public/"
scp "${ssh_args[@]}" \
  "${repo}/deploy/recovery/configure-main-managed-installation-ssh.sh" \
  "${remote}:${stage}/deploy/recovery/"
scp "${ssh_args[@]}" "${installer}" "${remote}:${stage}/install-collaboration-backend.sh"
ssh "${ssh_args[@]}" "${remote}" "chmod 0700 '${stage}/deploy/recovery/configure-main-managed-installation-ssh.sh'; sudo -n /bin/bash '${stage}/deploy/recovery/configure-main-managed-installation-ssh.sh'"
ssh "${ssh_args[@]}" "${remote}" "chmod 0700 '${stage}/install-collaboration-backend.sh'; sudo -n /bin/bash '${stage}/install-collaboration-backend.sh' '${version}' '${stage}' '${release_commit}'"
