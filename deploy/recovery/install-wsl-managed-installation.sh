#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() { printf 'install-wsl-managed-installation: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root inside WSL"

repo="${1:-}"
token_source="${2:-}"
key_source="${3:-}"
rollback_request="${4:-}"
handle_file=""
if [[ "${rollback_request}" == --handle-file=* ]]; then
  handle_file="${rollback_request#--handle-file=}"
  rollback_request=""
  [[ "${handle_file}" == /* && ! -L "${handle_file}" ]] \
    || die "rollback handle path must be an absolute non-symlink path"
  install -d -o root -g root -m 0700 "$(dirname "${handle_file}")"
fi
[[ -n "${repo}" && -d "${repo}" ]] || die "source repository is missing"
[[ -f "${token_source}" && ! -L "${token_source}" ]] || die "token source is missing or unsafe"
[[ -f "${key_source}" && ! -L "${key_source}" ]] || die "SSH key source is missing or unsafe"
token_source_value="$(cat -- "${token_source}")"
(( ${#token_source_value} >= 32 && ${#token_source_value} <= 4096 )) \
  || die "managed installation token length must be 32..4096 characters"
[[ "${token_source_value}" != *$'\n'* && "${token_source_value}" != *$'\r'* ]] \
  || die "managed installation token must contain exactly one line"
printf '%s\n' "${token_source_value}" | cmp -s -- - "${token_source}" \
  || die "managed installation token must have one newline-terminated line"

receiver_user="${HERMES_WSL_RECEIVER_USER:-hermes}"
receiver_home="${HERMES_WSL_HOME:-/mnt/d/Hermes/home/profiles/pc-worker}"
runtime_root="${HERMES_WSL_AGENT_ROOT:-/mnt/d/Hermes/hermes-agent}"
runtime_python="${HERMES_WSL_RUNTIME_PYTHON:-${runtime_root}/venv/bin/python}"
config_source="${repo}/deploy/recovery/managed-installations.wsl.json"
receiver_unit_source="${repo}/deploy/recovery/hermes-wsl-managed-installation-receiver.service"
tunnel_unit_source="${repo}/deploy/recovery/hermes-wsl-managed-installation-tunnel.service"
config_target="${receiver_home}/managed-installations.json"
token_target="/etc/pc-team/managed-installation-token"
connector_token="${HERMES_WSL_CONNECTOR_TOKEN_FILE:-/etc/pc-team/cloud_connector_token}"

id "${receiver_user}" >/dev/null 2>&1 || die "receiver user is missing"
validate_peer_token_file() {
  local path="$1" expected_owner="$2" expected_group="$3"
  [[ -f "${path}" && ! -L "${path}" ]] || die "unsafe peer token file ${path}"
  [[ "$(stat -c '%U' "${path}")" == "${expected_owner}" ]] \
    || die "peer token owner must be ${expected_owner}: ${path}"
  [[ "$(stat -c '%G' "${path}")" == "${expected_group}" ]] \
    || die "peer token group must be ${expected_group}: ${path}"
  [[ "$(stat -c '%a' "${path}")" == 640 ]] \
    || die "peer token mode must be 0640: ${path}"
  local value
  value="$(cat -- "${path}")"
  (( ${#value} >= 32 && ${#value} <= 4096 )) \
    || die "peer token length must be 32..4096 characters: ${path}"
  [[ "${value}" != *$'\n'* && "${value}" != *$'\r'* ]] \
    || die "peer token must contain exactly one line: ${path}"
  printf '%s\n' "${value}" | cmp -s -- - "${path}" \
    || die "peer token must have one newline-terminated line: ${path}"
  unset value
}
validate_peer_token_file "${connector_token}" root "${receiver_user}"
if cmp -s -- "${token_source}" "${connector_token}"; then
  die "managed installation token must differ from the connector token"
fi
user_home="$(getent passwd "${receiver_user}" | cut -d: -f6)"
[[ -n "${user_home}" && -d "${user_home}" ]] || die "receiver home is missing"
unit_dir="${user_home}/.config/systemd/user"
receiver_unit="${unit_dir}/hermes-wsl-managed-installation-receiver.service"
tunnel_unit="${unit_dir}/hermes-wsl-managed-installation-tunnel.service"
ssh_dir="${user_home}/.ssh"
key_target="${ssh_dir}/aliyun_hermes_ed25519"
receiver_unit_name="$(basename "${receiver_unit}")"
tunnel_unit_name="$(basename "${tunnel_unit}")"

[[ -x "${runtime_python}" ]] || die "Hermes runtime Python is missing"
for source in "${config_source}" "${receiver_unit_source}" "${tunnel_unit_source}"; do
  [[ -f "${source}" && ! -L "${source}" ]] || die "missing or unsafe ${source}"
done
runtime_modules=(
  "${runtime_root}/hermes_cli/managed_installations.py"
  "${runtime_root}/hermes_cli/managed_nodes.py"
  "${runtime_root}/hermes_cli/managed_node_recovery_service.py"
)
for module in "${runtime_modules[@]}"; do
  [[ -f "${module}" && ! -L "${module}" ]] \
    || die "managed installation runtime is not atomically published: ${module}"
done
"${runtime_python}" - "${runtime_modules[@]}" <<'PY'
import pathlib
import sys
for name in sys.argv[1:]:
    compile(pathlib.Path(name).read_text(encoding="utf-8"), name, "exec")
PY

uid="$(id -u "${receiver_user}")"
runtime_dir="/run/user/${uid}"
systemctl start "user@${uid}.service"
user_systemctl() {
  runuser -u "${receiver_user}" -- env \
    XDG_RUNTIME_DIR="${runtime_dir}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=${runtime_dir}/bus" \
    systemctl --user "$@"
}
linger_state="$(loginctl show-user "${receiver_user}" -p Linger --value)"
[[ "${linger_state}" == yes || "${linger_state}" == no ]] \
  || die "cannot determine linger state"
receiver_was_active=0
receiver_was_enabled=0
tunnel_was_active=0
tunnel_was_enabled=0
user_systemctl is-active --quiet "${receiver_unit_name}" && receiver_was_active=1
user_systemctl is-enabled --quiet "${receiver_unit_name}" && receiver_was_enabled=1
user_systemctl is-active --quiet "${tunnel_unit_name}" && tunnel_was_active=1
user_systemctl is-enabled --quiet "${tunnel_unit_name}" && tunnel_was_enabled=1

backup_root="${HERMES_INSTALLATION_BACKUP_ROOT:-/var/backups/hermes-agent}"
install -d -o root -g root -m 0700 "${backup_root}"
if [[ -n "${rollback_request}" ]]; then
  backup="${rollback_request#--rollback-backup=}"
else
  backup="$(mktemp -d "${backup_root}/wsl-installation-receiver.XXXXXX")"
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
  local current="$1"
  local name="$2"
  local temporary="${current}.rollback.$$"
  rm -f -- "${temporary}"
  if [[ -f "${backup}/${name}.present" ]]; then
    cp -a -- "${backup}/${name}" "${temporary}" && mv -f -- "${temporary}" "${current}"
  elif [[ -f "${backup}/${name}.absent" ]]; then
    rm -f -- "${current}"
  else
    return 1
  fi
}
rollback_committed_install() {
  local requested="${1#--rollback-backup=}"
  local canonical root_canonical
  canonical="$(realpath -e -- "${requested}")" \
    || die "rollback backup does not exist"
  root_canonical="$(realpath -e -- "${backup_root}")"
  [[ "${canonical}" == "${root_canonical}"/wsl-installation-receiver.* ]] \
    || die "rollback backup is outside the managed backup root"
  backup="${canonical}"
  [[ -f "${backup}/transaction-state" && ! -L "${backup}/transaction-state" ]] \
    || die "rollback backup has no transaction state"
  local values
  values="$(cat -- "${backup}/transaction-state")"
  [[ "${values}" =~ ^[01]:[01]:[01]:[01]:(yes|no)$ ]] \
    || die "rollback transaction state is invalid"
  IFS=: read -r receiver_was_active receiver_was_enabled \
    tunnel_was_active tunnel_was_enabled linger_state <<<"${values}"
  user_systemctl disable --now "${tunnel_unit_name}" "${receiver_unit_name}" \
    >/dev/null 2>&1 || true
  restore_one "${config_target}" managed-installations.json
  restore_one "${token_target}" managed-installation-token
  restore_one "${key_target}" aliyun_hermes_ed25519
  restore_one "${receiver_unit}" "${receiver_unit_name}"
  restore_one "${tunnel_unit}" "${tunnel_unit_name}"
  user_systemctl daemon-reload
  (( receiver_was_enabled == 0 )) || user_systemctl enable "${receiver_unit_name}"
  (( tunnel_was_enabled == 0 )) || user_systemctl enable "${tunnel_unit_name}"
  (( receiver_was_active == 0 )) || user_systemctl start "${receiver_unit_name}"
  (( tunnel_was_active == 0 )) || user_systemctl start "${tunnel_unit_name}"
  [[ "${linger_state}" != no ]] || loginctl disable-linger "${receiver_user}"
  printf 'rolled_back=%s\n' "${backup}"
  exit 0
}
if [[ "${rollback_request}" == --rollback-backup=* ]]; then
  rollback_committed_install "${rollback_request}"
elif [[ -n "${rollback_request}" ]]; then
  die "unsupported fourth argument"
fi
backup_one "${config_target}" managed-installations.json
backup_one "${token_target}" managed-installation-token
backup_one "${key_target}" aliyun_hermes_ed25519
backup_one "${receiver_unit}" "${receiver_unit_name}"
backup_one "${tunnel_unit}" "${tunnel_unit_name}"
printf '%s:%s:%s:%s:%s\n' \
  "${receiver_was_active}" "${receiver_was_enabled}" \
  "${tunnel_was_active}" "${tunnel_was_enabled}" "${linger_state}" \
  >"${backup}/transaction-state"
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
  rm -f -- \
    "${config_target}.new.$$" "${token_target}.new.$$" "${key_target}.new.$$" \
    "${receiver_unit}.new.$$" "${tunnel_unit}.new.$$"
  rm -f -- "${health_cfg:-}" "${probe_body:-}" "${probe_post:-}" "${probe_get:-}"
  if (( transaction_started && ! transaction_committed )); then
    user_systemctl disable --now "${tunnel_unit_name}" "${receiver_unit_name}" \
      >/dev/null 2>&1 || true
    restore_one "${config_target}" managed-installations.json || rollback_failed=1
    restore_one "${token_target}" managed-installation-token || rollback_failed=1
    restore_one "${key_target}" aliyun_hermes_ed25519 || rollback_failed=1
    restore_one "${receiver_unit}" "${receiver_unit_name}" || rollback_failed=1
    restore_one "${tunnel_unit}" "${tunnel_unit_name}" || rollback_failed=1
    user_systemctl daemon-reload >/dev/null 2>&1 || rollback_failed=1
    if (( receiver_was_enabled )); then
      user_systemctl enable "${receiver_unit_name}" >/dev/null 2>&1 \
        || rollback_failed=1
    fi
    if (( tunnel_was_enabled )); then
      user_systemctl enable "${tunnel_unit_name}" >/dev/null 2>&1 \
        || rollback_failed=1
    fi
    if (( receiver_was_active )); then
      user_systemctl start "${receiver_unit_name}" >/dev/null 2>&1 \
        || rollback_failed=1
    fi
    if (( tunnel_was_active )); then
      user_systemctl start "${tunnel_unit_name}" >/dev/null 2>&1 \
        || rollback_failed=1
    fi
    if [[ "${linger_state}" == no ]]; then
      loginctl disable-linger "${receiver_user}" >/dev/null 2>&1 \
        || rollback_failed=1
    fi
  fi
  if (( rollback_failed )); then
    printf 'WSL installation receiver rollback incomplete; backup=%s\n' "${backup}" >&2
    exit 70
  fi
  exit "${exit_status}"
}
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
failpoint() {
  [[ "${HERMES_INSTALLATION_FAILPOINT:-}" != "$1" ]] \
    || die "injected failure after $1"
}

install -d -o root -g "${receiver_user}" -m 0750 "$(dirname "${token_target}")"
install -d -o "${receiver_user}" -g "${receiver_user}" -m 0700 \
  "${receiver_home}" "${receiver_home}/managed-projects" "${unit_dir}" "${ssh_dir}"
transaction_started=1
install -o "${receiver_user}" -g "${receiver_user}" -m 0600 \
  "${token_source}" "${token_target}.new.$$"
mv -f -- "${token_target}.new.$$" "${token_target}"
[[ "$(stat -c '%U:%G:%a' "${token_target}")" == "${receiver_user}:${receiver_user}:600" ]] \
  || die "installed managed installation token metadata is invalid"
unset token_source_value
install -o "${receiver_user}" -g "${receiver_user}" -m 0600 \
  "${key_source}" "${key_target}.new.$$"
mv -f -- "${key_target}.new.$$" "${key_target}"
install -o "${receiver_user}" -g "${receiver_user}" -m 0600 \
  "${config_source}" "${config_target}.new.$$"
mv -f -- "${config_target}.new.$$" "${config_target}"
install -o "${receiver_user}" -g "${receiver_user}" -m 0644 \
  "${receiver_unit_source}" "${receiver_unit}.new.$$"
mv -f -- "${receiver_unit}.new.$$" "${receiver_unit}"
install -o "${receiver_user}" -g "${receiver_user}" -m 0644 \
  "${tunnel_unit_source}" "${tunnel_unit}.new.$$"
mv -f -- "${tunnel_unit}.new.$$" "${tunnel_unit}"
failpoint files

loginctl enable-linger "${receiver_user}"
user_systemctl daemon-reload
failpoint daemon-reload
user_systemctl enable "${receiver_unit_name}" "${tunnel_unit_name}"
user_systemctl restart "${receiver_unit_name}"
user_systemctl is-active --quiet "${receiver_unit_name}" \
  || die "WSL installation receiver did not become active"
user_systemctl restart "${tunnel_unit_name}"
failpoint restart
user_systemctl is-active --quiet "${tunnel_unit_name}" \
  || die "WSL installation tunnel did not become active"
health_cfg="$(mktemp /run/hermes-installation-health.XXXXXX)"
chmod 0600 "${health_cfg}"
printf 'header = "X-DBB3-Token: %s"\nheader = "Accept: application/json"\n' \
  "$(cat "${token_target}")" >"${health_cfg}"
healthy=0
for _ in $(seq 1 30); do
  if curl --fail --silent --show-error --max-time 2 --noproxy '*' \
      --config "${health_cfg}" http://127.0.0.1:9122/health \
      | "${runtime_python}" -c \
        'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True; assert d.get("node_id")=="wsl"; assert d.get("installations") is True; assert d.get("recovery") is False'; then
    healthy=1
    break
  fi
  sleep 1
done
[[ "${healthy}" == 1 ]] || die "WSL installation receiver capability probe failed"
probe_id="mi-$(${runtime_python} -c 'import uuid; print(uuid.uuid4().hex)')"
probe_body="$(mktemp /run/hermes-installation-probe-body.XXXXXX)"
probe_post="$(mktemp /run/hermes-installation-probe-post.XXXXXX)"
probe_get="$(mktemp /run/hermes-installation-probe-get.XXXXXX)"
printf '{"id":"%s","request_id":"%s","node_id":"wsl","kind":"probe","identifier":"managed-installation-route-probe","probe":true}\n' \
  "${probe_id}" "${probe_id}" >"${probe_body}"
curl --fail --silent --show-error --max-time 5 --noproxy '*' \
  --config "${health_cfg}" -H 'Content-Type: application/json' \
  --data-binary "@${probe_body}" -o "${probe_post}" \
  http://127.0.0.1:9122/installations
curl --fail --silent --show-error --max-time 5 --noproxy '*' \
  --config "${health_cfg}" -o "${probe_get}" \
  "http://127.0.0.1:9122/installations/${probe_id}"
"${runtime_python}" - "${probe_post}" "${probe_get}" "${probe_id}" wsl <<'PY'
import json, sys
post = json.load(open(sys.argv[1], encoding="utf-8"))
get = json.load(open(sys.argv[2], encoding="utf-8"))
assert post.get("accepted") is True and post.get("id") == sys.argv[3]
assert get.get("id") == sys.argv[3] and get.get("node_id") == sys.argv[4]
assert get.get("state") == "completed"
assert (get.get("detail") or {}).get("probe") is True
assert (get.get("detail") or {}).get("persisted") is True
PY
rm -f -- "${health_cfg}" "${probe_body}" "${probe_post}" "${probe_get}"
failpoint health

transaction_committed=1
trap - EXIT INT TERM HUP
printf 'receiver=%s\ntunnel=%s\nuser=%s\nrollback_backup=%s\n' \
  "${receiver_unit_name}" "${tunnel_unit_name}" "${receiver_user}" "${backup}"
