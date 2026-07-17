#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Root-side transactional installer. The caller uploads a stage owned by the
# unprivileged admin account, then invokes this script through sudo. No file is
# replaced until the staged Python/manifest validation and authenticated
# connector-health preflight have passed.

die() { printf 'install-collaboration-backend: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

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
  "hermes_cli/cloud_file_library.py"
  "hermes_cli/dashboard_auth/token_auth.py"
  "hermes_cli/dashboard_auth/mobile_device_store.py"
  "hermes_cli/dashboard_auth/mobile_notifications.py"
  "hermes_cli/web_server.py"
  "tui_gateway/server.py"
)
for relative in "${required[@]}"; do
  source_file="${stage_root}/${relative}"
  [[ -f "${source_file}" && ! -L "${source_file}" ]] || die "missing or unsafe ${relative}"
done

target_root="${HERMES_AGENT_ROOT:-/opt/hermes-agent}"
runtime_python="${HERMES_RUNTIME_PYTHON:-${target_root}/.venv/bin/python}"
[[ -x "${runtime_python}" ]] || die "Hermes runtime Python is missing: ${runtime_python}"

manifest_version="$("${runtime_python}" - "${stage_root}/plugins/collaboration/dashboard/manifest.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("version", ""))
PY
)"
[[ "${manifest_version}" == "${version}" ]] || die "manifest version ${manifest_version@Q} does not match ${version}"
"${runtime_python}" - \
  "${stage_root}/plugins/collaboration/dashboard/plugin_api.py" \
  "${stage_root}/hermes_cli/cloud_file_library.py" \
  "${stage_root}/hermes_cli/dashboard_auth/token_auth.py" \
  "${stage_root}/hermes_cli/dashboard_auth/mobile_device_store.py" \
  "${stage_root}/hermes_cli/dashboard_auth/mobile_notifications.py" \
  "${stage_root}/hermes_cli/web_server.py" \
  "${stage_root}/tui_gateway/server.py" <<'PY'
import pathlib, sys
for name in sys.argv[1:]:
    compile(pathlib.Path(name).read_text(encoding="utf-8"), name, "exec")
PY

# Copy through a root-owned snapshot. Reading the admin-owned stage through a
# lower-privileged tar process prevents a symlink swap during privileged copy.
snapshot="$(mktemp -d /run/hermes-agent-collaboration.XXXXXX)"
cleanup_snapshot() { rm -rf -- "${snapshot}"; }
trap cleanup_snapshot EXIT
if command -v setpriv >/dev/null 2>&1; then
  setpriv --reuid="${stage_owner}" --regid="${stage_owner}" --init-groups -- \
    tar -C "${stage_root}" -cf - -- "${required[@]}" \
    | tar --no-same-owner -C "${snapshot}" -xf -
else
  runuser -u "${stage_owner}" -- tar -C "${stage_root}" -cf - -- "${required[@]}" \
    | tar --no-same-owner -C "${snapshot}" -xf -
fi
for relative in "${required[@]}"; do
  [[ -f "${snapshot}/${relative}" && ! -L "${snapshot}/${relative}" ]] || die "unsafe snapshot ${relative}"
done

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
if [[ -z "${token_file}" && -r "${env_file}" ]]; then
  token_file="$(sed -n 's/^HERMES_COLLABORATION_CONNECTOR_TOKEN_FILE=//p' "${env_file}" | tail -n 1)"
  token_file="${token_file#\"}"; token_file="${token_file%\"}"
  token_file="${token_file#\'}"; token_file="${token_file%\'}"
fi
[[ -n "${token_file}" && -r "${token_file}" ]] || die "connector token file is not readable; health preflight refused"
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

stamp="$(date +%Y%m%d-%H%M%S)"
backup_root="${HERMES_BACKUP_ROOT:-/var/backups/hermes-agent}"
backup="${backup_root}/collaboration-${version}-${stamp}"
install -d -o root -g root -m 0700 "${backup}"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${plugin_target}"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${plugin_target}/dist"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${target_root}/hermes_cli/dashboard_auth"
install -d -o "${service_user}" -g "${service_group}" -m 0755 "${target_root}/tui_gateway"
mkdir -p \
  "${backup}/plugins/collaboration/dashboard" \
  "${backup}/hermes_cli/dashboard_auth" \
  "${backup}/tui_gateway"

backup_one() {
  local source="$1" destination="$2"
  if [[ -e "${source}" || -L "${source}" ]]; then
    [[ ! -L "${source}" ]] || die "refusing to back up symlink ${source}"
    cp -a -- "${source}" "${destination}"
  else
    : >"${destination}.missing"
  fi
}
backup_one "${plugin_target}/plugin_api.py" "${backup}/plugins/collaboration/dashboard/plugin_api.py"
backup_one "${plugin_target}/manifest.json" "${backup}/plugins/collaboration/dashboard/manifest.json"
backup_one "${core_target}" "${backup}/hermes_cli/cloud_file_library.py"
backup_one "${token_auth_target}" "${backup}/hermes_cli/dashboard_auth/token_auth.py"
backup_one "${mobile_device_store_target}" "${backup}/hermes_cli/dashboard_auth/mobile_device_store.py"
backup_one "${mobile_notifications_target}" "${backup}/hermes_cli/dashboard_auth/mobile_notifications.py"
backup_one "${web_server_target}" "${backup}/hermes_cli/web_server.py"
backup_one "${tui_gateway_target}" "${backup}/tui_gateway/server.py"

transaction="$(mktemp -d "${target_root}/.collaboration-install.XXXXXX")"
installed=0
rollback() {
  local exit_code=$?
  [[ "${installed}" == 1 ]] && exit "${exit_code}"
  restore_one "${backup}/plugins/collaboration/dashboard/plugin_api.py" "${plugin_target}/plugin_api.py"
  restore_one "${backup}/plugins/collaboration/dashboard/manifest.json" "${plugin_target}/manifest.json"
  restore_one "${backup}/hermes_cli/cloud_file_library.py" "${core_target}"
  restore_one "${backup}/hermes_cli/dashboard_auth/token_auth.py" "${token_auth_target}"
  restore_one "${backup}/hermes_cli/dashboard_auth/mobile_device_store.py" "${mobile_device_store_target}"
  restore_one "${backup}/hermes_cli/dashboard_auth/mobile_notifications.py" "${mobile_notifications_target}"
  restore_one "${backup}/hermes_cli/web_server.py" "${web_server_target}"
  restore_one "${backup}/tui_gateway/server.py" "${tui_gateway_target}"
  systemctl restart "${service}" >/dev/null 2>&1 || true
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
trap rollback ERR

install_atomic() {
  local source="$1"
  local destination="$2"
  local temporary="${transaction}/$(basename "${destination}")"
  install -o "${service_user}" -g "${service_group}" -m 0644 "${source}" "${temporary}"
  mv -f -- "${temporary}" "${destination}"
}
install_atomic "${snapshot}/plugins/collaboration/dashboard/plugin_api.py" "${plugin_target}/plugin_api.py"
install_atomic "${snapshot}/plugins/collaboration/dashboard/manifest.json" "${plugin_target}/manifest.json"
install_atomic "${snapshot}/hermes_cli/cloud_file_library.py" "${core_target}"
install_atomic "${snapshot}/hermes_cli/dashboard_auth/token_auth.py" "${token_auth_target}"
install_atomic "${snapshot}/hermes_cli/dashboard_auth/mobile_device_store.py" "${mobile_device_store_target}"
install_atomic "${snapshot}/hermes_cli/dashboard_auth/mobile_notifications.py" "${mobile_notifications_target}"
install_atomic "${snapshot}/hermes_cli/web_server.py" "${web_server_target}"
install_atomic "${snapshot}/tui_gateway/server.py" "${tui_gateway_target}"
systemctl restart "${service}"

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
connector_health_file="$(mktemp /run/hermes-agent-connector-status.XXXXXX)"
validate_connector_health "${connector_health_file}" || {
  printf '%s\n' "connector contract did not pass after restart" >&2
  false
}
installed=1
rm -rf -- "${transaction}" "${health_file}" "${connector_health_file}" "${curl_cfg}"
printf 'service=active\nversion=%s\nbackup=%s\n' "${version}" "${backup}"
