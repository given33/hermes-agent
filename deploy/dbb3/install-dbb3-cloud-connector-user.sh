#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Install the connector as a user service. Root is only needed for the
# root-owned source path and the existing root:hermes token; the long-running
# process and its systemd manager remain owned by hermes.

die() { printf 'install-dbb3-cloud-connector-user: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

install_lock="${HERMES_CONNECTOR_INSTALL_LOCK_FILE:-/run/lock/hermes-agent/cloud-connector-install.lock}"
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
flock -n 8 || die "another connector deployment is already running"

source_file="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dbb3_cloud_connector.py}"
cloud_url="${HERMES_CLOUD_URL:-https://daxueshenmai.top/api/plugins/collaboration}"
connector_user="${DBB3_CONNECTOR_USER:-hermes}"
token_file="${HERMES_CLOUD_TOKEN_FILE:-/etc/dbb3-team/cloud_connector_token}"
connector_id="${DBB3_CONNECTOR_ID:-dbb3-primary}"
target="${DBB3_CONNECTOR_SOURCE_TARGET:-/opt/dbb3-team/dbb3_cloud_connector.py}"
unit_name="${HERMES_CONNECTOR_UNIT_NAME:-dbb3-cloud-connector.service}"
unit_template="${DBB3_CONNECTOR_UNIT_TEMPLATE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dbb3-cloud-connector.service}"

[[ -f "${source_file}" && ! -L "${source_file}" ]] || die "connector source is missing or a symlink"
[[ -f "${unit_template}" && ! -L "${unit_template}" ]] || die "user unit template is missing"
id "${connector_user}" >/dev/null 2>&1 || die "connector user does not exist"
[[ -r "${token_file}" ]] || die "connector user cannot read token file ${token_file}"
runuser -u "${connector_user}" -- test -r "${token_file}" || die "connector token is not readable by ${connector_user}"

python3 - "${source_file}" <<'PY'
import pathlib, sys
compile(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"), sys.argv[1], "exec")
PY

# This is the deployment gate. It is read-only and runs before disabling the
# old root unit or replacing any source/config. A missing or changing backend
# connector API therefore leaves the current machine untouched.
runuser -u "${connector_user}" -- env \
  HERMES_CLOUD_URL="${cloud_url}" \
  HERMES_CLOUD_TOKEN_FILE="${token_file}" \
  DBB3_CONNECTOR_ID="${connector_id}" \
  python3 "${source_file}" --probe >/dev/null \
  || die "connector health/contract preflight failed; no service changes were made"

user_home="$(getent passwd "${connector_user}" | cut -d: -f6)"
[[ -n "${user_home}" && -d "${user_home}" ]] || die "cannot resolve ${connector_user} home"
config_dir="${HERMES_CONNECTOR_CONFIG_DIR:-${user_home}/.config/dbb3-team}"
state_dir="${HERMES_CONNECTOR_STATE_DIR:-${user_home}/.local/state/dbb3-cloud-connector}"
unit_dir="${user_home}/.config/systemd/user"
env_file="${config_dir}/cloud_connector.env"
unit_file="${unit_dir}/${unit_name}"
backup_root="${DBB3_CONNECTOR_BACKUP_ROOT:-/opt/dbb3-team/backups}"
stamp="$(date +%Y%m%d-%H%M%S)"
backup="${backup_root}/${stamp}-$$"

install -d -o root -g root -m 0755 "$(dirname "${target}")"
install -d -o "${connector_user}" -g "${connector_user}" -m 0700 \
  "${config_dir}" "${state_dir}" "${unit_dir}"
install -d -o root -g root -m 0700 "${backup}"

backup_one() {
  local current="$1"
  local name="$2"
  [[ ! -L "${current}" ]] || die "refusing to replace symlink ${current}"
  if [[ -e "${current}" ]]; then
    [[ -f "${current}" ]] || die "refusing to replace non-file ${current}"
    cp -a -- "${current}" "${backup}/${name}"
    : >"${backup}/${name}.present"
  else
    : >"${backup}/${name}.absent"
  fi
}

restore_one() {
  local current="$1"
  local name="$2"
  local rollback_tmp="${current}.rollback.$$"
  rm -f -- "${rollback_tmp}"
  if [[ -f "${backup}/${name}.present" ]]; then
    cp -a -- "${backup}/${name}" "${rollback_tmp}"
    mv -f -- "${rollback_tmp}" "${current}"
  else
    rm -f -- "${current}"
  fi
}

backup_one "${target}" "dbb3_cloud_connector.py"
backup_one "${env_file}" "cloud_connector.env"
backup_one "${unit_file}" "dbb3-cloud-connector.service"

source_tmp="${target}.new.$$"
env_tmp="${env_file}.new.$$"
unit_tmp="${unit_file}.new.$$"
rm -f -- "${source_tmp}" "${env_tmp}" "${unit_tmp}"
install -o root -g "${connector_user}" -m 0750 "${source_file}" "${source_tmp}"
cat >"${env_tmp}" <<EOF
HERMES_CLOUD_URL=${cloud_url}
HERMES_CLOUD_TOKEN_FILE=${token_file}
DBB3_CONNECTOR_ID=${connector_id}
DBB3_CONNECTOR_ARTIFACT_ROOTS=${DBB3_CONNECTOR_ARTIFACT_ROOTS:-${user_home}/.hermes:/opt/dbb3-team}
DBB3_CONNECTOR_STATE_FILE=${state_dir}/checkpoint.json
EOF
if [[ -n "${HERMES_CONNECTOR_HERMES_HOME:-}" ]]; then
  printf 'HERMES_HOME=%s\n' "${HERMES_CONNECTOR_HERMES_HOME}" >>"${env_tmp}"
fi
chown "${connector_user}:${connector_user}" "${env_tmp}"
chmod 0600 "${env_tmp}"
install -o "${connector_user}" -g "${connector_user}" -m 0644 "${unit_template}" "${unit_tmp}"

uid="$(id -u "${connector_user}")"
runtime="/run/user/${uid}"
user_systemctl() {
  runuser -u "${connector_user}" -- env \
    XDG_RUNTIME_DIR="${runtime}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=${runtime}/bus" \
    systemctl --user "$@"
}

root_was_active=0
root_was_enabled=0
user_was_active=0
user_was_enabled=0
user_unit_was_present=0
linger_was_enabled=0
systemctl is-active --quiet "${unit_name}" && root_was_active=1
systemctl is-enabled --quiet "${unit_name}" && root_was_enabled=1
user_systemctl is-active --quiet "${unit_name}" && user_was_active=1
user_systemctl is-enabled --quiet "${unit_name}" && user_was_enabled=1
[[ -f "${backup}/dbb3-cloud-connector.service.present" ]] && user_unit_was_present=1
if ! linger_state="$(loginctl show-user "${connector_user}" -p Linger --value 2>/dev/null)"; then
  rm -f -- "${source_tmp}" "${env_tmp}" "${unit_tmp}"
  die "cannot inspect linger state for ${connector_user}"
fi
case "${linger_state}" in
  yes) linger_was_enabled=1 ;;
  no) ;;
  *)
    rm -f -- "${source_tmp}" "${env_tmp}" "${unit_tmp}"
    die "unexpected linger state for ${connector_user}: ${linger_state}"
    ;;
esac

transaction_started=0
transaction_committed=0
rollback_failed=0

stop_deployed_services() {
  if user_systemctl is-active --quiet "${unit_name}" >/dev/null 2>&1; then
    user_systemctl stop "${unit_name}" >/dev/null 2>&1 || rollback_failed=1
  fi
  if (( ! user_was_enabled )) \
    && user_systemctl is-enabled --quiet "${unit_name}" >/dev/null 2>&1; then
    user_systemctl disable "${unit_name}" >/dev/null 2>&1 || rollback_failed=1
  fi
  if systemctl is-active --quiet "${unit_name}" >/dev/null 2>&1; then
    systemctl stop "${unit_name}" >/dev/null 2>&1 || rollback_failed=1
  fi
}

restore_service_state() {
  user_systemctl daemon-reload >/dev/null 2>&1 || rollback_failed=1

  if (( user_unit_was_present || user_was_active || user_was_enabled )); then
    if (( user_was_enabled )); then
      user_systemctl enable "${unit_name}" >/dev/null 2>&1 || rollback_failed=1
    else
      user_systemctl disable "${unit_name}" >/dev/null 2>&1 || rollback_failed=1
    fi
  fi
  if (( root_was_active || root_was_enabled )); then
    if (( root_was_enabled )); then
      systemctl enable "${unit_name}" >/dev/null 2>&1 || rollback_failed=1
    else
      systemctl disable "${unit_name}" >/dev/null 2>&1 || rollback_failed=1
    fi
  fi
  if (( user_was_active )); then
    user_systemctl start "${unit_name}" >/dev/null 2>&1 || rollback_failed=1
  fi
  if (( root_was_active )); then
    systemctl start "${unit_name}" >/dev/null 2>&1 || rollback_failed=1
  fi
}

rollback_transaction() {
  local exit_status="$1"
  trap - EXIT
  set +e
  rm -f -- "${source_tmp}" "${env_tmp}" "${unit_tmp}"
  if (( transaction_committed )); then
    exit "${exit_status}"
  fi
  if (( transaction_started && ! transaction_committed )); then
    printf 'install-dbb3-cloud-connector-user: deployment failed; restoring previous state\n' >&2
    stop_deployed_services
    restore_one "${target}" "dbb3_cloud_connector.py" || rollback_failed=1
    restore_one "${env_file}" "cloud_connector.env" || rollback_failed=1
    restore_one "${unit_file}" "dbb3-cloud-connector.service" || rollback_failed=1
    restore_service_state
    if (( ! linger_was_enabled )); then
      loginctl disable-linger "${connector_user}" >/dev/null 2>&1 || rollback_failed=1
    fi
    if (( rollback_failed )); then
      printf 'install-dbb3-cloud-connector-user: rollback was incomplete; inspect %s\n' \
        "${backup}" >&2
    else
      printf 'install-dbb3-cloud-connector-user: rollback complete; backup=%s\n' \
        "${backup}" >&2
    fi
  fi
  (( exit_status != 0 )) || exit_status=1
  exit "${exit_status}"
}
trap 'rollback_transaction $?' EXIT

transaction_started=1
mv -f -- "${source_tmp}" "${target}"
mv -f -- "${env_tmp}" "${env_file}"
mv -f -- "${unit_tmp}" "${unit_file}"

loginctl enable-linger "${connector_user}" >/dev/null

# The root service and this user unit must never consume the same queue. A
# partial root stop is a deployment failure and is restored by the transaction.
if (( root_was_active || root_was_enabled )); then
  systemctl disable --now "${unit_name}" >/dev/null
fi
user_systemctl daemon-reload
user_systemctl enable "${unit_name}"
user_systemctl restart "${unit_name}"
sleep 2
user_systemctl is-active --quiet "${unit_name}" || {
  user_systemctl --no-pager --full status "${unit_name}" | sed -n '1,80p' >&2 || true
  die "user connector did not become active"
}

transaction_committed=1
printf 'unit=%s\nuser=%s\ncloud_url=%s\ntoken_file=%s\nbackup=%s\n' \
  "${unit_name}" "${connector_user}" "${cloud_url}" "${token_file}" "${backup}"
