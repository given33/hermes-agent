#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Install the connector as a user service. Root is only needed for the
# root-owned source path and the existing root:hermes token; the long-running
# process and its systemd manager remain owned by hermes.

die() { printf 'install-dbb3-cloud-connector-user: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

# Check the nearest existing ancestor before any mkdir/install call.  Checking
# only the leaf (or checking after mkdir) would allow a symlinked ancestor to
# redirect root-owned deployment files outside the role boundary.
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

install_lock="${HERMES_CONNECTOR_INSTALL_LOCK_FILE:-/run/lock/hermes-agent/cloud-connector-install.lock}"
install_lock_dir="$(dirname "${install_lock}")"
assert_canonical_path "${install_lock_dir}" "install lock directory"
if [[ ! -d "${install_lock_dir}" ]]; then
  install -d -o root -g root -m 0755 "${install_lock_dir}"
fi
assert_canonical_path "${install_lock_dir}" "install lock directory"
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

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_file="${1:-${here}/dbb3_cloud_connector.py}"
control_request="${2:-}"
handle_file=""
if [[ "${control_request}" == --handle-file=* ]]; then
  handle_file="${control_request#--handle-file=}"
  control_request=""
  [[ "${handle_file}" == /* && ! -L "${handle_file}" ]] \
    || die "rollback handle path must be an absolute non-symlink path"
  assert_canonical_path "$(dirname -- "${handle_file}")" "rollback handle directory"
  install -d -o root -g root -m 0700 "$(dirname "${handle_file}")"
  assert_canonical_path "$(dirname -- "${handle_file}")" "rollback handle directory"
fi
cloud_url="${HERMES_CLOUD_URL:-https://daxueshenmai.top/api/plugins/collaboration}"
connector_user="${DBB3_CONNECTOR_USER:-hermes}"
token_file="${HERMES_CLOUD_TOKEN_FILE:-/etc/dbb3-team/cloud_connector_token}"
connector_id="${DBB3_CONNECTOR_ID:-dbb3-primary}"
target="${DBB3_CONNECTOR_SOURCE_TARGET:-/opt/dbb3-team/dbb3_cloud_connector.py}"
unit_name="${HERMES_CONNECTOR_UNIT_NAME:-dbb3-cloud-connector.service}"
unit_template="${DBB3_CONNECTOR_UNIT_TEMPLATE:-${here}/dbb3-cloud-connector.service}"
runtime_python="${HERMES_CONNECTOR_RUNTIME_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python}"

assert_canonical_path "${source_file}" "connector source"
assert_canonical_path "${unit_template}" "connector unit template"
assert_canonical_path "$(dirname -- "${runtime_python}")" "connector runtime directory"
assert_canonical_path "${token_file}" "connector token"

if [[ "${control_request}" != --rollback-backup=* ]]; then
  [[ -f "${source_file}" && ! -L "${source_file}" ]] || die "connector source is missing or a symlink"
  [[ -f "${unit_template}" && ! -L "${unit_template}" ]] || die "user unit template is missing"
  [[ "${runtime_python}" == /* && -x "${runtime_python}" ]] \
    || die "connector project runtime is missing or not executable: ${runtime_python}"
fi
id "${connector_user}" >/dev/null 2>&1 || die "connector user does not exist"
[[ -r "${token_file}" ]] || die "connector user cannot read token file ${token_file}"
runuser -u "${connector_user}" -- test -r "${token_file}" || die "connector token is not readable by ${connector_user}"

if [[ "${control_request}" != --rollback-backup=* ]]; then
"${runtime_python}" - "${source_file}" <<'PY'
import pathlib, sys
compile(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"), sys.argv[1], "exec")
PY

runuser -u "${connector_user}" -- \
  "${runtime_python}" -c 'from websockets.sync.client import connect' \
  || die "connector project runtime does not provide websockets.sync.client"

# This is the deployment gate. It is read-only and runs before disabling the
# old root unit or replacing any source/config. A missing or changing backend
# connector API therefore leaves the current machine untouched.
runuser -u "${connector_user}" -- env \
  HERMES_CLOUD_URL="${cloud_url}" \
  HERMES_CLOUD_TOKEN_FILE="${token_file}" \
  DBB3_CONNECTOR_ID="${connector_id}" \
  HERMES_CONNECTOR_WORKER_WS=1 \
  "${runtime_python}" "${source_file}" --probe >/dev/null \
  || die "connector health/contract preflight failed; no service changes were made"
fi

user_home="$(getent passwd "${connector_user}" | cut -d: -f6)"
[[ -n "${user_home}" && -d "${user_home}" ]] || die "cannot resolve ${connector_user} home"
assert_canonical_path "${user_home}" "connector user home"
hermes_home="${HERMES_CONNECTOR_HERMES_HOME:-${user_home}/.hermes/profiles/dbb3-worker}"
[[ "${hermes_home}" == /* && ! -L "${hermes_home}" ]] \
  || die "connector Hermes home must be an absolute non-symlink path"
hermes_parent="$(dirname -- "${hermes_home}")"
assert_canonical_path "${hermes_parent}" "connector Hermes home parent"
if [[ ! -e "${hermes_parent}" ]]; then
  install -d -o "${connector_user}" -g "${connector_user}" -m 0700 "${hermes_parent}"
fi
assert_canonical_path "${hermes_parent}" "connector Hermes home parent"
[[ -d "${hermes_parent}" && ! -L "${hermes_parent}" \
    && "$(realpath -e -- "${hermes_parent}")" == "${hermes_parent}" ]] \
  || die "connector Hermes home parent is unsafe"
profile_template_root="${HERMES_CONNECTOR_PROFILE_TEMPLATE_ROOT:-${here}/profile}"
profile_config="${hermes_home}/config.yaml"
profile_soul="${hermes_home}/SOUL.md"
profile_skills="${hermes_home}/skills"
if [[ "${control_request}" != --rollback-backup=* ]]; then
  [[ -f "${profile_template_root}/config.yaml.example" \
      && ! -L "${profile_template_root}/config.yaml.example" ]] \
    || die "worker profile config template is missing or unsafe"
  [[ -f "${profile_template_root}/SOUL.md" \
      && ! -L "${profile_template_root}/SOUL.md" ]] \
    || die "worker profile identity template is missing or unsafe"
fi
config_dir="${HERMES_CONNECTOR_CONFIG_DIR:-${user_home}/.config/dbb3-team}"
state_dir="${HERMES_CONNECTOR_STATE_DIR:-${user_home}/.local/state/dbb3-cloud-connector}"
unit_dir="${user_home}/.config/systemd/user"
env_file="${config_dir}/cloud_connector.env"
unit_file="${unit_dir}/${unit_name}"
backup_root="${DBB3_CONNECTOR_BACKUP_ROOT:-/opt/dbb3-team/backups}"
assert_canonical_path "${profile_template_root}" "worker profile template root"
assert_canonical_path "${config_dir}" "connector config directory"
assert_canonical_path "${state_dir}" "connector state directory"
assert_canonical_path "${unit_dir}" "connector user unit directory"
assert_canonical_path "$(dirname -- "${target}")" "connector source target directory"
assert_canonical_path "${backup_root}" "connector backup root"
stamp="$(date +%Y%m%d-%H%M%S)"
if [[ "${control_request}" == --rollback-backup=* ]]; then
  backup="${control_request#--rollback-backup=}"
elif [[ -n "${control_request}" ]]; then
  die "unsupported connector control request"
else
  backup="${backup_root}/${stamp}-$$"
fi

install -d -o root -g root -m 0755 "$(dirname "${target}")"
assert_canonical_path "$(dirname -- "${target}")" "connector source target directory"
install -d -o "${connector_user}" -g "${connector_user}" -m 0700 \
  "${config_dir}" "${state_dir}" "${unit_dir}"
assert_canonical_path "${config_dir}" "connector config directory"
assert_canonical_path "${state_dir}" "connector state directory"
assert_canonical_path "${unit_dir}" "connector user unit directory"
if [[ "${control_request}" != --rollback-backup=* ]]; then
  assert_canonical_path "${backup}" "connector backup path"
  install -d -o root -g root -m 0700 "${backup}"
  assert_canonical_path "${backup}" "connector backup path"
fi

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

backup_directory_presence() {
  local current="$1"
  local name="$2"
  [[ ! -L "${current}" ]] || die "refusing to use symlink directory ${current}"
  if [[ -d "${current}" ]]; then
    : >"${backup}/${name}.present"
  elif [[ ! -e "${current}" ]]; then
    : >"${backup}/${name}.absent"
  else
    die "refusing to use non-directory ${current}"
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
  elif [[ -f "${backup}/${name}.absent" ]]; then
    rm -f -- "${current}"
  else
    return 1
  fi
}

restore_absent_profile_directory() {
  local current="$1"
  # A connector may persist its own state while it is being stopped.  Never
  # remove that state recursively during rollback; remove only an empty
  # directory that this transaction created, and leave non-empty role state
  # for the next deployment to inspect.
  [[ ! -L "${current}" ]] || return 1
  if [[ -d "${current}" ]]; then
    rmdir -- "${current}" 2>/dev/null || true
  elif [[ -e "${current}" ]]; then
    return 1
  fi
}

restore_profile_directories() {
  if [[ -f "${backup}/profile-skills.absent" ]]; then
    restore_absent_profile_directory "${profile_skills}" || return 1
  elif [[ ! -f "${backup}/profile-skills.present" ]]; then
    return 1
  fi
  if [[ -f "${backup}/profile-home.absent" ]]; then
    restore_absent_profile_directory "${hermes_home}" || return 1
  elif [[ ! -f "${backup}/profile-home.present" ]]; then
    return 1
  fi
}

if [[ "${control_request}" != --rollback-backup=* ]]; then
  backup_one "${target}" "dbb3_cloud_connector.py"
  backup_one "${env_file}" "cloud_connector.env"
  backup_one "${unit_file}" "dbb3-cloud-connector.service"
  backup_one "${profile_config}" "profile-config.yaml"
  backup_one "${profile_soul}" "profile-SOUL.md"
  backup_directory_presence "${hermes_home}" "profile-home"
  backup_directory_presence "${profile_skills}" "profile-skills"
fi

source_tmp="${target}.new.$$"
env_tmp="${env_file}.new.$$"
unit_tmp="${unit_file}.new.$$"
profile_config_tmp="${profile_config}.new.$$"
profile_soul_tmp="${profile_soul}.new.$$"
profile_config_install=0
profile_soul_install=0
rm -f -- "${source_tmp}" "${env_tmp}" "${unit_tmp}" \
  "${profile_config_tmp}" "${profile_soul_tmp}"
if [[ "${control_request}" != --rollback-backup=* ]]; then
install -d -o "${connector_user}" -g "${connector_user}" -m 0700 \
  "${hermes_home}" "${profile_skills}"
assert_canonical_path "${hermes_home}" "connector Hermes home"
assert_canonical_path "${profile_skills}" "connector profile skills directory"
if [[ ! -e "${profile_config}" ]]; then
  install -o "${connector_user}" -g "${connector_user}" -m 0600 \
    "${profile_template_root}/config.yaml.example" "${profile_config_tmp}"
  profile_config_install=1
fi
if [[ ! -e "${profile_soul}" ]]; then
  install -o "${connector_user}" -g "${connector_user}" -m 0600 \
    "${profile_template_root}/SOUL.md" "${profile_soul_tmp}"
  profile_soul_install=1
fi
install -o root -g "${connector_user}" -m 0750 "${source_file}" "${source_tmp}"
cat >"${env_tmp}" <<EOF
HERMES_CLOUD_URL=${cloud_url}
HERMES_CLOUD_TOKEN_FILE=${token_file}
DBB3_CONNECTOR_ID=${connector_id}
# Official low-latency worker channel; REST pull remains the durable fallback.
HERMES_CONNECTOR_WORKER_WS=${HERMES_CONNECTOR_WORKER_WS:-1}
HERMES_HOME=${hermes_home}
DBB3_CONNECTOR_ARTIFACT_ROOTS=${DBB3_CONNECTOR_ARTIFACT_ROOTS:-${hermes_home}:/opt/dbb3-team}
DBB3_CONNECTOR_STATE_FILE=${state_dir}/checkpoint.json
HERMES_CONNECTOR_DRAIN_FILE=${state_dir}/drain_request.json
EOF
chown "${connector_user}:${connector_user}" "${env_tmp}"
chmod 0600 "${env_tmp}"
install -o "${connector_user}" -g "${connector_user}" -m 0644 "${unit_template}" "${unit_tmp}"
fi

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
  rm -f -- "${source_tmp}" "${env_tmp}" "${unit_tmp}" \
    "${profile_config_tmp}" "${profile_soul_tmp}"
  die "cannot inspect linger state for ${connector_user}"
fi
case "${linger_state}" in
  yes) linger_was_enabled=1 ;;
  no) ;;
  *)
    rm -f -- "${source_tmp}" "${env_tmp}" "${unit_tmp}" \
      "${profile_config_tmp}" "${profile_soul_tmp}"
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

if [[ "${control_request}" == --rollback-backup=* ]]; then
  canonical_backup="$(realpath -e -- "${backup}")" \
    || die "connector rollback backup does not exist"
  canonical_root="$(realpath -e -- "${backup_root}")"
  [[ "${canonical_backup}" == "${canonical_root}"/* ]] \
    || die "connector rollback backup is outside the managed backup root"
  backup="${canonical_backup}"
  [[ -f "${backup}/transaction-state" && ! -L "${backup}/transaction-state" ]] \
    || die "connector rollback transaction state is missing"
  saved_state="$(cat -- "${backup}/transaction-state")"
  [[ "${saved_state}" =~ ^[01]:[01]:[01]:[01]:[01]:[01]$ ]] \
    || die "connector rollback transaction state is invalid"
  IFS=: read -r root_was_active root_was_enabled user_was_active \
    user_was_enabled user_unit_was_present linger_was_enabled <<<"${saved_state}"
  rollback_failed=0
  stop_deployed_services
  restore_one "${target}" "dbb3_cloud_connector.py" || rollback_failed=1
  restore_one "${env_file}" "cloud_connector.env" || rollback_failed=1
  restore_one "${unit_file}" "dbb3-cloud-connector.service" || rollback_failed=1
  restore_one "${profile_config}" "profile-config.yaml" || rollback_failed=1
  restore_one "${profile_soul}" "profile-SOUL.md" || rollback_failed=1
  restore_profile_directories || rollback_failed=1
  restore_service_state
  if (( ! linger_was_enabled )); then
    loginctl disable-linger "${connector_user}" >/dev/null 2>&1 || rollback_failed=1
  fi
  (( ! rollback_failed )) || die "connector committed rollback was incomplete"
  printf 'rolled_back=%s\n' "${backup}"
  exit 0
fi

printf '%s:%s:%s:%s:%s:%s\n' \
  "${root_was_active}" "${root_was_enabled}" \
  "${user_was_active}" "${user_was_enabled}" \
  "${user_unit_was_present}" "${linger_was_enabled}" \
  >"${backup}/transaction-state"
chmod 0600 "${backup}/transaction-state"
if [[ -n "${handle_file}" ]]; then
  printf '%s\n' "${backup}" >"${handle_file}.new.$$"
  chmod 0600 "${handle_file}.new.$$"
  mv -f -- "${handle_file}.new.$$" "${handle_file}"
fi

rollback_transaction() {
  local exit_status="$1"
  trap - EXIT
  set +e
  rm -f -- "${source_tmp}" "${env_tmp}" "${unit_tmp}" \
    "${profile_config_tmp}" "${profile_soul_tmp}"
  if (( transaction_committed )); then
    exit "${exit_status}"
  fi
  if (( transaction_started && ! transaction_committed )); then
    printf 'install-dbb3-cloud-connector-user: deployment failed; restoring previous state\n' >&2
    stop_deployed_services
    restore_one "${target}" "dbb3_cloud_connector.py" || rollback_failed=1
    restore_one "${env_file}" "cloud_connector.env" || rollback_failed=1
    restore_one "${unit_file}" "dbb3-cloud-connector.service" || rollback_failed=1
    restore_one "${profile_config}" "profile-config.yaml" || rollback_failed=1
    restore_one "${profile_soul}" "profile-SOUL.md" || rollback_failed=1
    restore_profile_directories || rollback_failed=1
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
(( ! profile_config_install )) \
  || mv -f -- "${profile_config_tmp}" "${profile_config}"
(( ! profile_soul_install )) \
  || mv -f -- "${profile_soul_tmp}" "${profile_soul}"

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
