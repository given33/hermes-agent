#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Install the shared connector into WSL as an isolated pc-primary user service.
# The delegated installer owns preflight, atomic replacement, service startup,
# post-start verification, and restoration of every prior file/service state.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
shared_installer="${here}/../dbb3/install-dbb3-cloud-connector-user.sh"
shared_source="${here}/../dbb3/dbb3_cloud_connector.py"
unit_template="${here}/pc-cloud-connector.service"

die() { printf 'install-pc-cloud-connector-user: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root inside WSL"
[[ -f "${shared_installer}" && ! -L "${shared_installer}" ]] \
  || die "shared transactional installer is missing"
[[ -f "${shared_source}" && ! -L "${shared_source}" ]] \
  || die "shared connector source is missing"
[[ -f "${unit_template}" && ! -L "${unit_template}" ]] \
  || die "PC user unit template is missing"

connector_user="${PC_CONNECTOR_USER:-hermes}"
pc_home="${PC_CONNECTOR_HERMES_HOME:-/mnt/d/Hermes/home}"
user_home="$(getent passwd "${connector_user}" | cut -d: -f6)"
[[ -n "${user_home}" && -d "${user_home}" ]] || die "connector user home is missing"
[[ -d "${pc_home}" ]] || die "Hermes PC home is missing: ${pc_home}"
artifact_roots="${PC_CONNECTOR_ARTIFACT_ROOTS:-${pc_home}:${user_home}/.hermes}"

exec env \
  DBB3_CONNECTOR_USER="${connector_user}" \
  DBB3_CONNECTOR_ID="${PC_CONNECTOR_ID:-pc-primary}" \
  HERMES_CLOUD_URL="${HERMES_CLOUD_URL:-https://daxueshenmai.top/api/plugins/collaboration}" \
  HERMES_CLOUD_TOKEN_FILE="${HERMES_CLOUD_TOKEN_FILE:-/etc/pc-team/cloud_connector_token}" \
  DBB3_CONNECTOR_SOURCE_TARGET="${PC_CONNECTOR_SOURCE_TARGET:-/opt/pc-team/pc_cloud_connector.py}" \
  DBB3_CONNECTOR_UNIT_TEMPLATE="${unit_template}" \
  DBB3_CONNECTOR_BACKUP_ROOT="${PC_CONNECTOR_BACKUP_ROOT:-/opt/pc-team/backups}" \
  DBB3_CONNECTOR_ARTIFACT_ROOTS="${artifact_roots}" \
  HERMES_CONNECTOR_UNIT_NAME="pc-cloud-connector.service" \
  HERMES_CONNECTOR_CONFIG_DIR="${user_home}/.config/pc-team" \
  HERMES_CONNECTOR_STATE_DIR="${user_home}/.local/state/pc-cloud-connector" \
  HERMES_CONNECTOR_HERMES_HOME="${pc_home}" \
  bash "${shared_installer}" "${shared_source}"
