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
installer="${repo}/deploy/public/install-collaboration-backend.sh"

[[ -f "${installer}" ]] || die "installer is missing"
[[ -f "${repo}/plugins/collaboration/dashboard/plugin_api.py" ]] || die "plugin_api.py is missing"
[[ -f "${repo}/plugins/collaboration/dashboard/manifest.json" ]] || die "manifest.json is missing"
[[ -f "${repo}/plugins/collaboration/dashboard/dist/index.js" ]] || die "dist/index.js is missing"
[[ -f "${repo}/hermes_cli/cloud_file_library.py" ]] || die "cloud_file_library.py is missing"
[[ -f "${repo}/hermes_cli/dashboard_auth/token_auth.py" ]] || die "token_auth.py is missing"
[[ -f "${repo}/hermes_cli/dashboard_auth/mobile_device_store.py" ]] || die "mobile_device_store.py is missing"
[[ -f "${repo}/hermes_cli/dashboard_auth/mobile_notifications.py" ]] || die "mobile_notifications.py is missing"
[[ -f "${repo}/hermes_cli/web_server.py" ]] || die "web_server.py is missing"
[[ -f "${repo}/tui_gateway/server.py" ]] || die "tui_gateway/server.py is missing"

ios_hermes_assets=(
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
for relative in "${ios_hermes_assets[@]}" "${ios_plugin_assets[@]}" "${ios_tool_assets[@]}"; do
  [[ -f "${repo}/${relative}" && ! -L "${repo}/${relative}" ]] || die "${relative} is missing"
done

if [[ -z "${version}" ]]; then
  version="$(python3 - "${repo}/plugins/collaboration/dashboard/manifest.json" <<'PY'
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

ssh "${ssh_args[@]}" "${remote}" "install -d -m 0700 '${stage}' '${stage}/plugins/collaboration/dashboard/dist' '${stage}/hermes_cli' '${stage}/hermes_cli/dashboard_auth' '${stage}/tui_gateway' '${stage}/plugins/ios-intelligence/dashboard' '${stage}/tools'"
scp "${ssh_args[@]}" \
  "${repo}/plugins/collaboration/dashboard/plugin_api.py" \
  "${repo}/plugins/collaboration/dashboard/manifest.json" \
  "${remote}:${stage}/plugins/collaboration/dashboard/"
scp "${ssh_args[@]}" \
  "${repo}/plugins/collaboration/dashboard/dist/index.js" \
  "${remote}:${stage}/plugins/collaboration/dashboard/dist/"
scp "${ssh_args[@]}" \
  "${repo}/hermes_cli/cloud_file_library.py" \
  "${repo}/hermes_cli/web_server.py" \
  "${remote}:${stage}/hermes_cli/"
scp "${ssh_args[@]}" \
  "${repo}/hermes_cli/dashboard_auth/token_auth.py" \
  "${repo}/hermes_cli/dashboard_auth/mobile_device_store.py" \
  "${repo}/hermes_cli/dashboard_auth/mobile_notifications.py" \
  "${remote}:${stage}/hermes_cli/dashboard_auth/"
scp "${ssh_args[@]}" \
  "${repo}/tui_gateway/server.py" \
  "${remote}:${stage}/tui_gateway/"
scp "${ssh_args[@]}" \
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
scp "${ssh_args[@]}" "${installer}" "${remote}:${stage}/install-collaboration-backend.sh"
ssh "${ssh_args[@]}" "${remote}" "chmod 0700 '${stage}/install-collaboration-backend.sh'; sudo -n /bin/bash '${stage}/install-collaboration-backend.sh' '${version}' '${stage}'"
ssh "${ssh_args[@]}" "${remote}" "rm -rf -- '${stage}'"
