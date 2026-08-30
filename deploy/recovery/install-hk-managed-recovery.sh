#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() { printf 'install-hk-managed-recovery: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

repo="${1:-}"
token_source="${2:-}"
key_source="${3:-}"
known_hosts_source="${4:-}"
control_request="${5:-}"
handle_file=""
if [[ "${control_request}" == --handle-file=* ]]; then
  handle_file="${control_request#--handle-file=}"
  control_request=""
  [[ "${handle_file}" == /* && ! -L "${handle_file}" ]] \
    || die "rollback handle path must be an absolute non-symlink path"
  install -d -o root -g root -m 0700 "$(dirname "${handle_file}")"
fi
[[ -n "${repo}" && -d "${repo}" ]] || die "source repository is missing"
for source in "${token_source}" "${key_source}" "${known_hosts_source}"; do
  [[ -f "${source}" && ! -L "${source}" ]] || die "credential source is missing or unsafe"
done

receiver_user="${HERMES_HK_RECOVERY_USER:-hermes}"
[[ "${receiver_user}" == hermes ]] || die "HK recovery user must be hermes"
agent_root="${HERMES_HK_AGENT_ROOT:-/opt/hk-team/hermes-agent}"
runtime_python="${HERMES_HK_RUNTIME_PYTHON:-${agent_root}/.venv/bin/python}"
config_source="${repo}/deploy/recovery/managed-nodes.hk.json"
recover_source="${repo}/deploy/recovery/recover-hk.sh"
receiver_unit_source="${repo}/deploy/recovery/hermes-hk-managed-node-recovery.service"
tunnel_unit_source="${repo}/deploy/recovery/hermes-hk-managed-node-recovery-tunnel.service"
config_target="${HERMES_HK_RECOVERY_CONFIG:-/etc/hk-team/managed-nodes.json}"
token_target="${HERMES_HK_RECOVERY_TOKEN_TARGET:-/etc/hk-team/recovery_token}"
recover_target="${HERMES_HK_RECOVERY_COMMAND:-/usr/local/sbin/hermes-recover-hk}"
receiver_unit="${HERMES_HK_RECOVERY_UNIT:-/etc/systemd/system/hermes-hk-managed-node-recovery.service}"
tunnel_unit="${HERMES_HK_RECOVERY_TUNNEL_UNIT:-/etc/systemd/system/hermes-hk-managed-node-recovery-tunnel.service}"
receiver_unit_name="$(basename "${receiver_unit}")"
tunnel_unit_name="$(basename "${tunnel_unit}")"
connector_token="${HERMES_HK_CONNECTOR_TOKEN_FILE:-/etc/hk-team/cloud_connector_token}"
state_dir="${HERMES_HK_RECOVERY_STATE_DIR:-/var/lib/hermes-hk-recovery}"
backup_root="${HERMES_RECOVERY_BACKUP_ROOT:-/var/backups/hermes-agent}"

id "${receiver_user}" >/dev/null 2>&1 || die "recovery user is missing"
user_home="$(getent passwd "${receiver_user}" | cut -d: -f6)"
[[ -n "${user_home}" && -d "${user_home}" ]] || die "recovery user home is missing"
ssh_dir="${user_home}/.ssh"
key_target="${HERMES_HK_RECOVERY_KEY_TARGET:-${ssh_dir}/hk_recovery_ed25519}"
known_hosts_target="${HERMES_HK_RECOVERY_KNOWN_HOSTS_TARGET:-${ssh_dir}/hk_recovery_known_hosts}"
[[ -x "${runtime_python}" ]] || die "Hermes HK runtime Python is missing"
command -v ssh-keygen >/dev/null 2>&1 || die "ssh-keygen is missing"
for source in "${config_source}" "${recover_source}" \
  "${receiver_unit_source}" "${tunnel_unit_source}"; do
  [[ -f "${source}" && ! -L "${source}" ]] || die "missing or unsafe ${source}"
done
for module in "${agent_root}/hermes_cli/managed_nodes.py" \
  "${agent_root}/hermes_cli/managed_node_recovery_service.py"; do
  [[ -f "${module}" && ! -L "${module}" ]] \
    || die "HK recovery runtime is not atomically published: ${module}"
done
"${runtime_python}" - "${agent_root}/hermes_cli/managed_nodes.py" \
  "${agent_root}/hermes_cli/managed_node_recovery_service.py" <<'PY'
import pathlib
import sys
for name in sys.argv[1:]:
    compile(pathlib.Path(name).read_text(encoding="utf-8"), name, "exec")
PY

token_value="$(cat -- "${token_source}")"
(( ${#token_value} >= 32 && ${#token_value} <= 4096 )) \
  || die "HK recovery token length must be 32..4096 characters"
[[ "${token_value}" != *$'\n'* && "${token_value}" != *$'\r'* ]] \
  || die "HK recovery token must contain exactly one line"
printf '%s\n' "${token_value}" | cmp -s -- - "${token_source}" \
  || die "HK recovery token must have one newline-terminated line"
[[ -f "${connector_token}" && ! -L "${connector_token}" ]] \
  || die "HK connector token is missing or unsafe"
cmp -s -- "${token_source}" "${connector_token}" \
  && die "HK recovery token must differ from the connector token"
(( $(stat -c '%s' "${key_source}") >= 32 && $(stat -c '%s' "${key_source}") <= 16384 )) \
  || die "HK recovery SSH key size is invalid"
ssh-keygen -y -f "${key_source}" >/dev/null \
  || die "HK recovery SSH key is invalid"
(( $(stat -c '%s' "${known_hosts_source}") >= 32 \
  && $(stat -c '%s' "${known_hosts_source}") <= 65536 )) \
  || die "HK recovery known-hosts file size is invalid"
ssh-keygen -F 10.66.0.1 -f "${known_hosts_source}" >/dev/null \
  || die "HK recovery known-hosts file does not pin 10.66.0.1"

install -d -o root -g root -m 0700 "${backup_root}"
if [[ "${control_request}" == --rollback-backup=* ]]; then
  backup="${control_request#--rollback-backup=}"
elif [[ -n "${control_request}" ]]; then
  die "unsupported recovery control request"
else
  backup="$(mktemp -d "${backup_root}/hk-managed-recovery.XXXXXX")"
fi
backup_one() {
  local current="$1" name="$2"
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
  local current="$1" name="$2" temporary="${current}.rollback.$$"
  rm -f -- "${temporary}"
  if [[ -f "${backup}/${name}.present" ]]; then
    cp -a -- "${backup}/${name}" "${temporary}" && mv -f -- "${temporary}" "${current}"
  elif [[ -f "${backup}/${name}.absent" ]]; then
    rm -f -- "${current}"
  else
    return 1
  fi
}

receiver_was_active=0
receiver_was_enabled=0
tunnel_was_active=0
tunnel_was_enabled=0
systemctl is-active --quiet "${receiver_unit_name}" && receiver_was_active=1
systemctl is-enabled --quiet "${receiver_unit_name}" && receiver_was_enabled=1
systemctl is-active --quiet "${tunnel_unit_name}" && tunnel_was_active=1
systemctl is-enabled --quiet "${tunnel_unit_name}" && tunnel_was_enabled=1

if [[ "${control_request}" == --rollback-backup=* ]]; then
  canonical_backup="$(realpath -e -- "${backup}")" || die "rollback backup does not exist"
  canonical_root="$(realpath -e -- "${backup_root}")"
  [[ "${canonical_backup}" == "${canonical_root}"/hk-managed-recovery.* ]] \
    || die "rollback backup is outside the managed backup root"
  backup="${canonical_backup}"
  [[ -f "${backup}/transaction-state" && ! -L "${backup}/transaction-state" ]] \
    || die "rollback transaction state is missing"
  saved_state="$(cat -- "${backup}/transaction-state")"
  [[ "${saved_state}" =~ ^[01]:[01]:[01]:[01]$ ]] \
    || die "rollback transaction state is invalid"
  IFS=: read -r receiver_was_active receiver_was_enabled \
    tunnel_was_active tunnel_was_enabled <<<"${saved_state}"
  systemctl disable --now "${tunnel_unit_name}" "${receiver_unit_name}" >/dev/null 2>&1 || true
  restore_one "${config_target}" managed-nodes.json
  restore_one "${token_target}" recovery_token
  restore_one "${key_target}" hk_recovery_ed25519
  restore_one "${known_hosts_target}" hk_recovery_known_hosts
  restore_one "${recover_target}" hermes-recover-hk
  restore_one "${receiver_unit}" "${receiver_unit_name}"
  restore_one "${tunnel_unit}" "${tunnel_unit_name}"
  systemctl daemon-reload
  (( receiver_was_enabled == 0 )) || systemctl enable "${receiver_unit_name}"
  (( tunnel_was_enabled == 0 )) || systemctl enable "${tunnel_unit_name}"
  (( receiver_was_active == 0 )) || systemctl start "${receiver_unit_name}"
  (( tunnel_was_active == 0 )) || systemctl start "${tunnel_unit_name}"
  printf 'rolled_back=%s\n' "${backup}"
  exit 0
fi

backup_one "${config_target}" managed-nodes.json
backup_one "${token_target}" recovery_token
backup_one "${key_target}" hk_recovery_ed25519
backup_one "${known_hosts_target}" hk_recovery_known_hosts
backup_one "${recover_target}" hermes-recover-hk
backup_one "${receiver_unit}" "${receiver_unit_name}"
backup_one "${tunnel_unit}" "${tunnel_unit_name}"
printf '%s:%s:%s:%s\n' "${receiver_was_active}" "${receiver_was_enabled}" \
  "${tunnel_was_active}" "${tunnel_was_enabled}" >"${backup}/transaction-state"
chmod 0600 "${backup}/transaction-state"
if [[ -n "${handle_file}" ]]; then
  printf '%s\n' "${backup}" >"${handle_file}.new.$$"
  chmod 0600 "${handle_file}.new.$$"
  mv -f -- "${handle_file}.new.$$" "${handle_file}"
fi

transaction_started=0
transaction_committed=0
rollback_failed=0
rollback() {
  local exit_status=$?
  trap - EXIT INT TERM HUP
  set +e
  rm -f -- "${config_target}.new.$$" "${token_target}.new.$$" \
    "${key_target}.new.$$" "${known_hosts_target}.new.$$" \
    "${recover_target}.new.$$" "${receiver_unit}.new.$$" "${tunnel_unit}.new.$$"
  if (( transaction_started && ! transaction_committed )); then
    systemctl disable --now "${tunnel_unit_name}" "${receiver_unit_name}" >/dev/null 2>&1 || true
    restore_one "${config_target}" managed-nodes.json || rollback_failed=1
    restore_one "${token_target}" recovery_token || rollback_failed=1
    restore_one "${key_target}" hk_recovery_ed25519 || rollback_failed=1
    restore_one "${known_hosts_target}" hk_recovery_known_hosts || rollback_failed=1
    restore_one "${recover_target}" hermes-recover-hk || rollback_failed=1
    restore_one "${receiver_unit}" "${receiver_unit_name}" || rollback_failed=1
    restore_one "${tunnel_unit}" "${tunnel_unit_name}" || rollback_failed=1
    systemctl daemon-reload >/dev/null 2>&1 || rollback_failed=1
    (( receiver_was_enabled == 0 )) || systemctl enable "${receiver_unit_name}" >/dev/null 2>&1 || rollback_failed=1
    (( tunnel_was_enabled == 0 )) || systemctl enable "${tunnel_unit_name}" >/dev/null 2>&1 || rollback_failed=1
    (( receiver_was_active == 0 )) || systemctl start "${receiver_unit_name}" >/dev/null 2>&1 || rollback_failed=1
    (( tunnel_was_active == 0 )) || systemctl start "${tunnel_unit_name}" >/dev/null 2>&1 || rollback_failed=1
  fi
  if (( rollback_failed )); then
    printf 'HK recovery rollback incomplete; backup=%s\n' "${backup}" >&2
    exit 70
  fi
  exit "${exit_status}"
}
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
failpoint() {
  [[ "${HERMES_RECOVERY_FAILPOINT:-}" != "$1" ]] || die "injected failure after $1"
}

install -d -o root -g "${receiver_user}" -m 0750 "$(dirname "${token_target}")"
install -d -o "${receiver_user}" -g "${receiver_user}" -m 0700 "${ssh_dir}"
install -d -o root -g root -m 0700 "${state_dir}"
transaction_started=1
install -o root -g "${receiver_user}" -m 0640 "${token_source}" "${token_target}.new.$$"
mv -f -- "${token_target}.new.$$" "${token_target}"
install -o "${receiver_user}" -g "${receiver_user}" -m 0600 "${key_source}" "${key_target}.new.$$"
mv -f -- "${key_target}.new.$$" "${key_target}"
install -o "${receiver_user}" -g "${receiver_user}" -m 0600 \
  "${known_hosts_source}" "${known_hosts_target}.new.$$"
mv -f -- "${known_hosts_target}.new.$$" "${known_hosts_target}"
install -o root -g root -m 0600 "${config_source}" "${config_target}.new.$$"
mv -f -- "${config_target}.new.$$" "${config_target}"
install -o root -g root -m 0755 "${recover_source}" "${recover_target}.new.$$"
mv -f -- "${recover_target}.new.$$" "${recover_target}"
install -o root -g root -m 0644 "${receiver_unit_source}" "${receiver_unit}.new.$$"
mv -f -- "${receiver_unit}.new.$$" "${receiver_unit}"
install -o root -g root -m 0644 "${tunnel_unit_source}" "${tunnel_unit}.new.$$"
mv -f -- "${tunnel_unit}.new.$$" "${tunnel_unit}"
unset token_value
failpoint files

systemctl daemon-reload
systemctl enable "${receiver_unit_name}" "${tunnel_unit_name}"
systemctl restart "${receiver_unit_name}"
systemctl is-active --quiet "${receiver_unit_name}" \
  || die "HK recovery receiver did not become active"
healthy=0
for _ in $(seq 1 30); do
  if curl --fail --silent --show-error --max-time 2 --noproxy '*' \
      http://127.0.0.1:9121/health \
      | "${runtime_python}" -c \
        'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True; assert d.get("node_id")=="hk"; assert d.get("recovery") is True; assert d.get("installations") is False'; then
    healthy=1
    break
  fi
  sleep 1
done
[[ "${healthy}" == 1 ]] || die "HK recovery receiver capability probe failed"
systemctl restart "${tunnel_unit_name}"
failpoint restart
systemctl is-active --quiet "${tunnel_unit_name}" \
  || die "HK recovery tunnel did not become active"
failpoint health

transaction_committed=1
trap - EXIT INT TERM HUP
printf 'receiver=%s\ntunnel=%s\nrollback_backup=%s\n' \
  "${receiver_unit_name}" "${tunnel_unit_name}" "${backup}"
