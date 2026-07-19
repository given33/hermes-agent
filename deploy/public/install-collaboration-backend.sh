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
flock -n 8 || die "another collaboration deployment is already running"

version="${1:-}"
stage="${2:-}"
[[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid release version"
[[ -n "${stage}" && -d "${stage}" ]] || die "release stage is missing"

stage_owner="${HERMES_STAGE_OWNER:-admin}"
stage_root="$(realpath -e -- "${stage}")"
case "${stage_root}" in
  "/home/${stage_owner}/.cache/hermes-agent-deploy/"*) ;;
  *) die "stage must be below /home/${stage_owner}/.cache/hermes-agent-deploy" ;;
esac
[[ "$(stat -c '%U' "${stage_root}")" == "${stage_owner}" ]] || die "stage is not owned by ${stage_owner}"

required=(
  "plugins/collaboration/dashboard/plugin_api.py"
  "plugins/collaboration/dashboard/manifest.json"
  "plugins/collaboration/dashboard/dist/index.js"
  "hermes_cli/cloud_file_library.py"
  "hermes_cli/dashboard_auth/token_auth.py"
  "hermes_cli/dashboard_auth/mobile_device_store.py"
  "hermes_cli/dashboard_auth/mobile_notifications.py"
  "hermes_cli/web_server.py"
  "tui_gateway/server.py"
)
# The iOS intelligence release is staged alongside the collaboration release.
# Keep this list optional for one-release rollback compatibility: an older
# stage can still be installed, while a stage containing the plugin is copied
# as one transaction with all of its runtime dependencies.
ios_optional=(
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
  "hermes_cli/profiles.py"
  "hermes_cli/managed_nodes.py"
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
"${runtime_python}" - \
  "${snapshot}/plugins/collaboration/dashboard/plugin_api.py" \
  "${snapshot}/hermes_cli/cloud_file_library.py" \
  "${snapshot}/hermes_cli/dashboard_auth/token_auth.py" \
  "${snapshot}/hermes_cli/dashboard_auth/mobile_device_store.py" \
  "${snapshot}/hermes_cli/dashboard_auth/mobile_notifications.py" \
  "${snapshot}/hermes_cli/web_server.py" \
  "${snapshot}/tui_gateway/server.py" <<'PY'
import pathlib, sys
for name in sys.argv[1:]:
    compile(pathlib.Path(name).read_text(encoding="utf-8"), name, "exec")
PY
if [[ "${ios_enabled}" == 1 ]]; then
  "${runtime_python}" - "${snapshot}/hermes_cli/ios_intelligence.py" \
    "${snapshot}/hermes_cli/ios_intelligence_config.py" \
    "${snapshot}/hermes_cli/ios_intelligence_scheduler.py" \
    "${snapshot}/hermes_cli/ios_intelligence_supervisor.py" \
    "${snapshot}/hermes_cli/ios_mcp_supervisor.py" \
    "${snapshot}/hermes_cli/ios_mcp_server.py" \
    "${snapshot}/hermes_cli/dashboard_auth/__init__.py" \
    "${snapshot}/hermes_cli/dashboard_auth/owner_mobile.py" \
    "${snapshot}/hermes_cli/dashboard_auth/registry.py" \
    "${snapshot}/hermes_cli/profiles.py" \
    "${snapshot}/plugins/dashboard_auth/basic/__init__.py" \
    "${snapshot}/plugins/ios-intelligence/dashboard/plugin_api.py" \
    "${snapshot}/tools/mcp_tool.py" <<'PY'
import pathlib, sys
for name in sys.argv[1:]:
    compile(pathlib.Path(name).read_text(encoding="utf-8"), name, "exec")
PY
  "${runtime_python}" -c 'from mcp.server.fastmcp import FastMCP; assert FastMCP' \
    || die "Hermes runtime is missing the FastMCP SDK required by iOS MCP services"
  "${runtime_python}" -c 'from cryptography.hazmat.primitives.ciphers.aead import AESGCM; assert AESGCM' \
    || die "Hermes runtime is missing AES-GCM support required by encrypted iOS hot and cold storage"
  "${runtime_python}" -c 'from agent.plugin_llm import PluginLlm; assert PluginLlm' \
    || die "Hermes runtime is missing the host LLM facade required by iOS semantic analysis"
fi

service="${HERMES_AGENT_SERVICE:-hermes-agent.service}"
service_user="${HERMES_AGENT_USER:-hermes-agent}"
service_group="${HERMES_AGENT_GROUP:-hermes-agent}"
plugin_target="${target_root}/plugins/collaboration/dashboard"
core_target="${target_root}/hermes_cli/cloud_file_library.py"
token_auth_target="${target_root}/hermes_cli/dashboard_auth/token_auth.py"
mobile_device_store_target="${target_root}/hermes_cli/dashboard_auth/mobile_device_store.py"
mobile_notifications_target="${target_root}/hermes_cli/dashboard_auth/mobile_notifications.py"
web_server_target="${target_root}/hermes_cli/web_server.py"
tui_gateway_target="${target_root}/tui_gateway/server.py"
[[ -d "${target_root}" ]] || die "target root does not exist: ${target_root}"
id "${service_user}" >/dev/null 2>&1 || die "service user does not exist: ${service_user}"

# Existing connector installations must pass the deployment gate before any
# file changes. A legacy installation without the route is permitted exactly
# one bootstrap; the same authenticated contract is mandatory after restart.
health_url="${HERMES_CONNECTOR_HEALTH_URL:-http://127.0.0.2:9119/api/plugins/collaboration/connector/health}"
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
runtime_home=""
if [[ -r "${env_file}" ]]; then
  runtime_home="$(sed -n 's/^HERMES_HOME=//p' "${env_file}" | tail -n 1)"
  runtime_home="${runtime_home#\"}"; runtime_home="${runtime_home%\"}"
  runtime_home="${runtime_home#\'}"; runtime_home="${runtime_home%\'}"
fi
service_home="$(getent passwd "${service_user}" | cut -d: -f6)"
runtime_home="${HERMES_HOME_DIR:-${runtime_home:-${service_home}/.hermes}}"
state_target="${HERMES_COLLABORATION_STATE_FILE:-${runtime_home}/collaboration/single.json}"
config_target="${HERMES_CONFIG_FILE:-${runtime_home}/config.yaml}"
ios_supervisor_target="${runtime_home}/ios-mcp-supervisor.db"
ios_database_target="${runtime_home}/ios-intelligence.db"
mobile_auth_target="${runtime_home}/dashboard/mobile-auth.db"
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
    --config "${curl_cfg}" -o "${output}" "${health_url}" \
    && "${runtime_python}" - "${output}" "${connector_id}" "${require_identity}" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data.get("ok") is True
assert int(data.get("contract_version", 0)) == 1
if sys.argv[3] == "1":
    assert data.get("connector_id") == sys.argv[2]
else:
    assert data.get("connector_id") in (None, sys.argv[2])
assert "artifact-upload" in (data.get("capabilities") or [])
assert "attachment-download" in (data.get("capabilities") or [])
PY
}
preflight_health="$(mktemp /run/hermes-agent-connector-preflight.XXXXXX)"
if [[ -f "${plugin_target}/plugin_api.py" ]] \
  && grep -Fq '@router.get("/connector/health")' "${plugin_target}/plugin_api.py"; then
  validate_connector_health "${preflight_health}" 0 \
    || die "connector health preflight failed; no files were changed"
fi
rm -f -- "${preflight_health}"

stamp="$(date +%Y%m%d-%H%M%S)-$$"
backup_root="${HERMES_BACKUP_ROOT:-/var/backups/hermes-agent}"
install -d -o root -g root -m 0700 "${backup_root}"
backup="$(mktemp -d "${backup_root}/collaboration-${version}-${stamp}.XXXXXX")"
chown root:root "${backup}"
chmod 0700 "${backup}"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${plugin_target}"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${plugin_target}/dist"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${target_root}/hermes_cli/dashboard_auth"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${target_root}/tui_gateway"
mkdir -p \
  "${backup}/plugins/collaboration/dashboard/dist" \
  "${backup}/hermes_cli/dashboard_auth" \
  "${backup}/tui_gateway" \
  "${backup}/state"

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
PY
    mv -f -- "${temporary}" "${destination}"
  else
    : >"${destination}.missing"
  fi
}
backup_one "${plugin_target}/plugin_api.py" "${backup}/plugins/collaboration/dashboard/plugin_api.py"
backup_one "${plugin_target}/manifest.json" "${backup}/plugins/collaboration/dashboard/manifest.json"
backup_one "${plugin_target}/dist/index.js" "${backup}/plugins/collaboration/dashboard/dist/index.js"
backup_one "${core_target}" "${backup}/hermes_cli/cloud_file_library.py"
backup_one "${token_auth_target}" "${backup}/hermes_cli/dashboard_auth/token_auth.py"
backup_one "${mobile_device_store_target}" "${backup}/hermes_cli/dashboard_auth/mobile_device_store.py"
backup_one "${mobile_notifications_target}" "${backup}/hermes_cli/dashboard_auth/mobile_notifications.py"
backup_one "${web_server_target}" "${backup}/hermes_cli/web_server.py"
backup_one "${tui_gateway_target}" "${backup}/tui_gateway/server.py"
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
rollback() {
  local exit_code=$?
  trap - EXIT INT TERM HUP
  set +e
  if [[ "${installed}" != 1 ]]; then
    systemctl stop "${service}" >/dev/null 2>&1 || true
    restore_one "${backup}/plugins/collaboration/dashboard/plugin_api.py" "${plugin_target}/plugin_api.py"
    restore_one "${backup}/plugins/collaboration/dashboard/manifest.json" "${plugin_target}/manifest.json"
    restore_one "${backup}/plugins/collaboration/dashboard/dist/index.js" "${plugin_target}/dist/index.js"
    restore_one "${backup}/hermes_cli/cloud_file_library.py" "${core_target}"
    restore_one "${backup}/hermes_cli/dashboard_auth/token_auth.py" "${token_auth_target}"
    restore_one "${backup}/hermes_cli/dashboard_auth/mobile_device_store.py" "${mobile_device_store_target}"
    restore_one "${backup}/hermes_cli/dashboard_auth/mobile_notifications.py" "${mobile_notifications_target}"
    restore_one "${backup}/hermes_cli/web_server.py" "${web_server_target}"
    restore_one "${backup}/tui_gateway/server.py" "${tui_gateway_target}"
    if [[ "${ios_enabled}" == 1 ]]; then
      for relative in "${ios_optional[@]}"; do
        restore_one "${backup}/${relative}" "${target_root}/${relative}"
      done
      restore_one "${backup}/config/config.yaml" "${config_target}"
      restore_sqlite "${backup}/state/ios-intelligence.db" "${ios_database_target}"
      restore_sqlite "${backup}/state/ios-mcp-supervisor.db" "${ios_supervisor_target}"
      restore_sqlite "${backup}/state/mobile-auth.db" "${mobile_auth_target}"
    fi
    restore_state "${backup}/state/single.json" "${state_target}"
    systemctl start "${service}" >/dev/null 2>&1 || true
  fi
  rm -rf -- "${transaction}"
  [[ -z "${health_file:-}" ]] || rm -f -- "${health_file}"
  [[ -z "${ios_health_file:-}" ]] || rm -f -- "${ios_health_file}"
  [[ -z "${connector_health_file:-}" ]] || rm -f -- "${connector_health_file}"
  rm -f -- "${curl_cfg}"
  cleanup_snapshot
  exit "${exit_code}"
}
restore_one() {
  local source="$1"
  local destination="$2"
  local temporary="${destination}.rollback.$$"
  if [[ -f "${source}" ]]; then
    install -o "${service_user}" -g "${service_group}" -m 0644 "${source}" "${temporary}"
    mv -f -- "${temporary}" "${destination}"
  elif [[ -f "${source}.missing" ]]; then
    rm -f -- "${destination}"
  fi
}
restore_state() {
  local source="$1"
  local destination="$2"
  local temporary="${destination}.rollback.$$"
  install -d -o "${service_user}" -g "${service_group}" -m 0700 "$(dirname "${destination}")"
  if [[ -f "${source}" ]]; then
    install -o "${service_user}" -g "${service_group}" -m 0600 "${source}" "${temporary}"
    mv -f -- "${temporary}" "${destination}"
  elif [[ -f "${source}.missing" ]]; then
    rm -f -- "${destination}"
  fi
}
restore_sqlite() {
  local source="$1"
  local destination="$2"
  local temporary="${destination}.rollback.$$"
  local destination_dir
  destination_dir="$(dirname "${destination}")"
  if [[ ! -d "${destination_dir}" ]]; then
    install -d -o "${service_user}" -g "${service_group}" -m 0700 "${destination_dir}"
  fi
  rm -f -- "${temporary}" "${destination}-wal" "${destination}-shm" "${destination}-journal"
  if [[ -f "${source}" ]]; then
    install -o "${service_user}" -g "${service_group}" -m 0600 "${source}" "${temporary}"
    mv -f -- "${temporary}" "${destination}"
  elif [[ -f "${source}.missing" ]]; then
    rm -f -- "${destination}"
  fi
}
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# Quiesce the state writer before taking the transactional state snapshot.
# Keep the service stopped until every runtime file has been atomically placed;
# rollback also stops it before restoring this snapshot.
systemctl stop "${service}"
backup_one "${state_target}" "${backup}/state/single.json"
if [[ "${ios_enabled}" == 1 ]]; then
  backup_sqlite "${ios_database_target}" "${backup}/state/ios-intelligence.db"
  backup_sqlite "${ios_supervisor_target}" "${backup}/state/ios-mcp-supervisor.db"
  backup_sqlite "${mobile_auth_target}" "${backup}/state/mobile-auth.db"
fi

install_atomic() {
  local source="$1"
  local destination="$2"
  local temporary="${transaction}/$(basename "${destination}")"
  install -o "${service_user}" -g "${service_group}" -m 0644 "${source}" "${temporary}"
  mv -f -- "${temporary}" "${destination}"
}
install_atomic "${snapshot}/plugins/collaboration/dashboard/plugin_api.py" "${plugin_target}/plugin_api.py"
install_atomic "${snapshot}/plugins/collaboration/dashboard/manifest.json" "${plugin_target}/manifest.json"
install_atomic "${snapshot}/plugins/collaboration/dashboard/dist/index.js" "${plugin_target}/dist/index.js"
install_atomic "${snapshot}/hermes_cli/cloud_file_library.py" "${core_target}"
install_atomic "${snapshot}/hermes_cli/dashboard_auth/token_auth.py" "${token_auth_target}"
install_atomic "${snapshot}/hermes_cli/dashboard_auth/mobile_device_store.py" "${mobile_device_store_target}"
install_atomic "${snapshot}/hermes_cli/dashboard_auth/mobile_notifications.py" "${mobile_notifications_target}"
install_atomic "${snapshot}/hermes_cli/web_server.py" "${web_server_target}"
install_atomic "${snapshot}/tui_gateway/server.py" "${tui_gateway_target}"
if [[ "${ios_enabled}" == 1 ]]; then
  for relative in "${ios_optional[@]}"; do
    install_atomic "${snapshot}/${relative}" "${target_root}/${relative}"
  done
  # Persist discovery and supervisor state while the old process is quiesced;
  # the restarted service then boots with the complete MCP tool surface.
  sudo -u "${service_user}" -- env HERMES_HOME="${runtime_home}" \
    "${runtime_python}" -m hermes_cli.ios_mcp_server --install \
    --transport streamable-http --host 127.0.0.1 --base-port 8760 \
    || { printf '%s\n' "iOS MCP registration failed" >&2; false; }
  sudo -u "${service_user}" -- env HERMES_HOME="${runtime_home}" \
    "${runtime_python}" -m hermes_cli.ios_mcp_supervisor --register \
    --host 127.0.0.1 --base-port 8760 \
    || { printf '%s\n' "iOS MCP supervisor registration failed" >&2; false; }
fi
systemctl start "${service}"

health_file="$(mktemp /run/hermes-agent-status.XXXXXX)"
healthy=0
for _ in $(seq 1 30); do
  if systemctl is-active --quiet "${service}" \
    && curl --fail --silent --show-error --max-time 3 http://127.0.0.2:9119/api/status >"${health_file}"; then
    healthy=1
    break
  fi
  sleep 1
done
[[ "${healthy}" == 1 ]] || {
  printf '%s\n' "${service} did not pass post-restart health check" >&2
  false
}
"${runtime_python}" - "${health_file}" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert isinstance(data, dict)
PY
ios_health_file=""
if [[ "${ios_enabled}" == 1 ]]; then
  ios_health_file="$(mktemp /run/hermes-agent-ios-status.XXXXXX)"
  if ! curl --fail --silent --show-error --max-time 10 \
    --config "${curl_cfg}" \
    http://127.0.0.2:9119/api/plugins/ios-intelligence/health >"${ios_health_file}"; then
    printf '%s\n' "iOS intelligence health endpoint did not respond" >&2
    false
  fi
  "${runtime_python}" - "${ios_health_file}" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
runtime = data.get("mcp_runtime") or {}
assert data.get("scheduler_running") is True
assert runtime.get("ok") is True
assert runtime.get("healthy_count") == 21
assert runtime.get("required_count") == 21
services = runtime.get("services") or []
assert len(services) == 21
assert sum(len(item.get("tools") or []) for item in services) == 44
assert all(item.get("ok") is True for item in services)
PY
fi
connector_health_file="$(mktemp /run/hermes-agent-connector-status.XXXXXX)"
validate_connector_health "${connector_health_file}" || {
  printf '%s\n' "connector contract did not pass after restart" >&2
  false
}
installed=1
rm -rf -- "${transaction}" "${health_file}" "${ios_health_file}" "${connector_health_file}" "${curl_cfg}"
printf 'service=active\nversion=%s\nbackup=%s\n' "${version}" "${backup}"
