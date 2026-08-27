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
for required in "${shared_installer}" "${shared_source}" "${unit_template}"; do
  [[ -f "${required}" && ! -L "${required}" ]] || die "required asset is missing: ${required}"
done

connector_user="${HK_CONNECTOR_USER:-hermes}"
user_home="$(getent passwd "${connector_user}" | cut -d: -f6)"
[[ -n "${user_home}" && -d "${user_home}" ]] || die "connector user home is missing"
hermes_home="${HK_CONNECTOR_HERMES_HOME:-${user_home}/.hermes}"
artifact_roots="${HK_CONNECTOR_ARTIFACT_ROOTS:-${hermes_home}:${user_home}/.hermes}"
profile_root="${hermes_home}/profiles/hk-worker"
install -d -o "${connector_user}" -g "${connector_user}" -m 0700 \
  "${hermes_home}" "${hermes_home}/profiles" "${profile_root}" \
  "${profile_root}/skills"
for template in config.yaml.example SOUL.md; do
  source_template="${here}/profile/${template}"
  target_template="${profile_root}/${template}"
  [[ -f "${source_template}" && ! -L "${source_template}" ]] \
    || die "HK profile template is missing: ${source_template}"
  if [[ ! -e "${target_template}" && ! -L "${target_template}" ]]; then
    install -o "${connector_user}" -g "${connector_user}" -m 0600 \
      "${source_template}" "${target_template}"
  fi
done

exec env \
  DBB3_CONNECTOR_USER="${connector_user}" \
  DBB3_CONNECTOR_ID="${HK_CONNECTOR_ID:-hk-primary}" \
  HERMES_CLOUD_URL="${HERMES_CLOUD_URL:-https://daxueshenmai.top/api/plugins/collaboration}" \
  HERMES_CLOUD_TOKEN_FILE="${HERMES_CLOUD_TOKEN_FILE:-/etc/hk-team/cloud_connector_token}" \
  DBB3_CONNECTOR_SOURCE_TARGET="${HK_CONNECTOR_SOURCE_TARGET:-/opt/hk-team/hk_cloud_connector.py}" \
  DBB3_CONNECTOR_UNIT_TEMPLATE="${unit_template}" \
  DBB3_CONNECTOR_BACKUP_ROOT="${HK_CONNECTOR_BACKUP_ROOT:-/opt/hk-team/backups}" \
  DBB3_CONNECTOR_ARTIFACT_ROOTS="${artifact_roots}" \
  HERMES_CONNECTOR_UNIT_NAME="hk-cloud-connector.service" \
  HERMES_CONNECTOR_CONFIG_DIR="${user_home}/.config/hk-team" \
  HERMES_CONNECTOR_STATE_DIR="${user_home}/.local/state/hk-cloud-connector" \
  HERMES_CONNECTOR_HERMES_HOME="${hermes_home}" \
  bash "${shared_installer}" "${shared_source}" "${1:-}"
