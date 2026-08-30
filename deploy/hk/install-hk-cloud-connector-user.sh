#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# HK uses the same transactional connector implementation as DBB3, but all
# files, credentials, state, and the worker profile live under HK-specific
# paths.  This keeps the source contract shared without sharing mutable data.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
shared_installer="${here}/../dbb3/install-dbb3-cloud-connector-user.sh"
shared_source="${here}/../dbb3/dbb3_cloud_connector.py"
unit_template="${here}/hk-cloud-connector.service"

die() { printf 'install-hk-cloud-connector-user: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

assert_canonical_path() {
  local path="$1" label="${2:-path}" probe parent resolved
  [[ "${path}" == /* ]] || die "${label} must be absolute"
  probe="${path}"
  while [[ ! -e "${probe}" && ! -L "${probe}" ]]; do
    parent="$(dirname -- "${probe}")"
    [[ "${parent}" != "${probe}" ]] || die "${label} has no existing ancestor"
    probe="${parent}"
  done
  [[ ! -L "${probe}" ]] || die "${label} has a symlink ancestor"
  resolved="$(realpath -e -- "${probe}")" \
    || die "${label} ancestor cannot be resolved"
  [[ "${resolved}" == "${probe}" ]] \
    || die "${label} ancestor resolves outside its lexical path"
}

for required in "${shared_installer}" "${shared_source}" "${unit_template}"; do
  assert_canonical_path "${required}" "HK deployment asset"
  [[ -f "${required}" && ! -L "${required}" ]] || die "required asset is missing: ${required}"
done

connector_user="${HK_CONNECTOR_USER:-hermes}"
user_home="$(getent passwd "${connector_user}" | cut -d: -f6)"
[[ -n "${user_home}" && -d "${user_home}" ]] || die "connector user home is missing"
hermes_home="${HK_CONNECTOR_HERMES_HOME:-${user_home}/.hermes/profiles/hk-worker}"
artifact_roots="${HK_CONNECTOR_ARTIFACT_ROOTS:-${hermes_home}}"
[[ "${hermes_home}" == /* && ! -L "${hermes_home}" ]] \
  || die "HK worker Hermes home must be an absolute non-symlink path"
assert_canonical_path "${hermes_home}" "HK worker Hermes home"
assert_canonical_path "$(dirname -- "${artifact_roots%%:*}")" "HK artifact root parent"

exec env \
  DBB3_CONNECTOR_USER="${connector_user}" \
  DBB3_CONNECTOR_ID="${HK_CONNECTOR_ID:-hk-primary}" \
  HERMES_CLOUD_URL="${HERMES_CLOUD_URL:-https://daxueshenmai.top/api/plugins/collaboration}" \
  HERMES_CLOUD_TOKEN_FILE="${HERMES_CLOUD_TOKEN_FILE:-/etc/hk-team/cloud_connector_token}" \
  DBB3_CONNECTOR_SOURCE_TARGET="${HK_CONNECTOR_SOURCE_TARGET:-/opt/hk-team/hk_cloud_connector.py}" \
  DBB3_CONNECTOR_UNIT_TEMPLATE="${unit_template}" \
  DBB3_CONNECTOR_BACKUP_ROOT="${HK_CONNECTOR_BACKUP_ROOT:-/opt/hk-team/backups}" \
  DBB3_CONNECTOR_ARTIFACT_ROOTS="${artifact_roots}" \
  HERMES_CONNECTOR_RUNTIME_PYTHON="${HERMES_HK_RUNTIME_PYTHON:-/opt/hk-team/hermes-agent/.fabric-current/.venv/bin/python}" \
  HERMES_CONNECTOR_UNIT_NAME="hk-cloud-connector.service" \
  HERMES_CONNECTOR_CONFIG_DIR="${user_home}/.config/hk-team" \
  HERMES_CONNECTOR_STATE_DIR="${user_home}/.local/state/hk-cloud-connector" \
  HERMES_CONNECTOR_HERMES_HOME="${hermes_home}" \
  HERMES_CONNECTOR_PROFILE_TEMPLATE_ROOT="${here}/profile" \
  bash "${shared_installer}" "${shared_source}" "${1:-}"
