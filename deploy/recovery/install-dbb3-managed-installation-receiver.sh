#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() { printf 'install-dbb3-managed-installation-receiver: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

repo="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
control_request="${2:-}"
handle_file=""
if [[ "${control_request}" == --handle-file=* ]]; then
  handle_file="${control_request#--handle-file=}"
  control_request=""
  [[ "${handle_file}" == /* && ! -L "${handle_file}" ]] \
    || die "rollback handle path must be an absolute non-symlink path"
  install -d -o root -g root -m 0700 "$(dirname "${handle_file}")"
fi
runtime_root="${HERMES_DBB3_AGENT_ROOT:-/usr/local/lib/hermes-agent}"
runtime_python="${HERMES_DBB3_RUNTIME_PYTHON:-${runtime_root}/venv/bin/python}"
receiver_user="${HERMES_DBB3_RECEIVER_USER:-hermes}"
receiver_home="${HERMES_DBB3_HOME:-/home/hermes/.hermes/profiles/dbb3-worker}"
config_source="${repo}/deploy/recovery/managed-installations.dbb3.json"
unit_source="${repo}/deploy/recovery/hermes-managed-installation-receiver.service"
config_target="${receiver_home}/managed-installations.json"
unit_target="${HERMES_DBB3_INSTALLATION_UNIT:-/etc/systemd/system/hermes-managed-installation-receiver.service}"
unit_name="$(basename "${unit_target}")"
installation_token="${HERMES_DBB3_INSTALLATION_TOKEN_FILE:-/etc/dbb3-team/managed-installation-token}"
status_token="${HERMES_DBB3_STATUS_TOKEN_FILE:-/etc/dbb3-team/status_token}"

[[ -x "${runtime_python}" ]] || die "Hermes runtime Python is missing"
id "${receiver_user}" >/dev/null 2>&1 || die "receiver user is missing"
for source in "${config_source}" "${unit_source}"; do
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
runuser -u "${receiver_user}" -- test -r "${installation_token}" \
  || die "managed-node token is not readable by ${receiver_user}"
validate_token_file() {
  local path="$1" expected_owner="$2" expected_group="$3"
  [[ -f "${path}" && ! -L "${path}" ]] || die "unsafe token file ${path}"
  [[ "$(stat -c '%U' "${path}")" == "${expected_owner}" ]] \
    || die "token owner must be ${expected_owner}: ${path}"
  [[ "$(stat -c '%G' "${path}")" == "${expected_group}" ]] \
    || die "token group must be ${expected_group}: ${path}"
  [[ "$(stat -c '%a' "${path}")" == 640 ]] \
    || die "token mode must be 0640: ${path}"
  local value
  value="$(cat -- "${path}")"
  (( ${#value} >= 32 && ${#value} <= 4096 )) \
    || die "token length must be 32..4096 characters: ${path}"
  [[ "${value}" != *$'\n'* && "${value}" != *$'\r'* ]] \
    || die "token must contain exactly one line: ${path}"
  printf '%s\n' "${value}" | cmp -s -- - "${path}" \
    || die "token must have one newline-terminated line: ${path}"
  unset value
}
validate_token_file \
  "${installation_token}" root "${receiver_user}"
validate_token_file "${status_token}" root "${receiver_user}"
if cmp -s -- "${status_token}" "${installation_token}"; then
  die "managed installation token must differ from the status token"
fi

backup_root="${HERMES_INSTALLATION_BACKUP_ROOT:-/var/backups/hermes-agent}"
install -d -o root -g root -m 0700 "${backup_root}"
if [[ "${control_request}" == --rollback-backup=* ]]; then
  backup="${control_request#--rollback-backup=}"
elif [[ -n "${control_request}" ]]; then
  die "unsupported receiver control request"
else
  backup="$(mktemp -d "${backup_root}/dbb3-installation-receiver.XXXXXX")"
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
unit_was_active=0
unit_was_enabled=0
systemctl is-active --quiet "${unit_name}" && unit_was_active=1
systemctl is-enabled --quiet "${unit_name}" && unit_was_enabled=1
if [[ "${control_request}" == --rollback-backup=* ]]; then
  canonical_backup="$(realpath -e -- "${backup}")" \
    || die "receiver rollback backup does not exist"
  canonical_root="$(realpath -e -- "${backup_root}")"
  [[ "${canonical_backup}" == "${canonical_root}"/dbb3-installation-receiver.* ]] \
    || die "receiver rollback backup is outside the managed backup root"
  backup="${canonical_backup}"
  [[ -f "${backup}/transaction-state" && ! -L "${backup}/transaction-state" ]] \
    || die "receiver rollback transaction state is missing"
  saved_state="$(cat -- "${backup}/transaction-state")"
  [[ "${saved_state}" =~ ^[01]:[01]$ ]] \
    || die "receiver rollback transaction state is invalid"
  IFS=: read -r unit_was_active unit_was_enabled <<<"${saved_state}"
  systemctl disable --now "${unit_name}" >/dev/null 2>&1 || true
  restore_one "${config_target}" managed-installations.json
  restore_one "${unit_target}" hermes-managed-installation-receiver.service
  systemctl daemon-reload
  (( unit_was_enabled == 0 )) || systemctl enable "${unit_name}"
  (( unit_was_active == 0 )) || systemctl start "${unit_name}"
  printf 'rolled_back=%s\n' "${backup}"
  exit 0
fi
backup_one "${config_target}" managed-installations.json
backup_one "${unit_target}" hermes-managed-installation-receiver.service
printf '%s:%s\n' "${unit_was_active}" "${unit_was_enabled}" \
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
  rm -f -- "${config_target}.new.$$" "${unit_target}.new.$$"
  rm -f -- "${health_cfg:-}" "${probe_body:-}" "${probe_post:-}" "${probe_get:-}"
  if (( transaction_started && ! transaction_committed )); then
    systemctl disable --now "${unit_name}" >/dev/null 2>&1 || true
    restore_one "${config_target}" managed-installations.json || rollback_failed=1
    restore_one "${unit_target}" hermes-managed-installation-receiver.service \
      || rollback_failed=1
    systemctl daemon-reload >/dev/null 2>&1 || rollback_failed=1
    if (( unit_was_enabled )); then
      systemctl enable "${unit_name}" >/dev/null 2>&1 || rollback_failed=1
    fi
    if (( unit_was_active )); then
      systemctl start "${unit_name}" >/dev/null 2>&1 || rollback_failed=1
    fi
  fi
  if (( rollback_failed )); then
    printf 'DBB3 installation receiver rollback incomplete; backup=%s\n' "${backup}" >&2
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

install -d -o "${receiver_user}" -g "${receiver_user}" -m 0700 "${receiver_home}"
transaction_started=1
install -o "${receiver_user}" -g "${receiver_user}" -m 0600 \
  "${config_source}" "${config_target}.new.$$"
mv -f -- "${config_target}.new.$$" "${config_target}"
install -o root -g root -m 0644 "${unit_source}" "${unit_target}.new.$$"
mv -f -- "${unit_target}.new.$$" "${unit_target}"
failpoint files
systemctl daemon-reload
failpoint daemon-reload
systemctl enable "${unit_name}"
systemctl restart "${unit_name}"
failpoint restart
systemctl is-active --quiet "${unit_name}" \
  || die "installation receiver did not become active"
health_cfg="$(mktemp /run/hermes-installation-health.XXXXXX)"
chmod 0600 "${health_cfg}"
printf 'header = "X-DBB3-Token: %s"\nheader = "Accept: application/json"\n' \
  "$(cat /etc/dbb3-team/managed-installation-token)" >"${health_cfg}"
healthy=0
for _ in $(seq 1 30); do
  if curl --fail --silent --show-error --max-time 2 --noproxy '*' \
      --config "${health_cfg}" http://10.66.0.2:9122/health \
      | "${runtime_python}" -c \
        'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True; assert d.get("node_id")=="dbb3"; assert d.get("installations") is True; assert d.get("recovery") is False'; then
    healthy=1
    break
  fi
  sleep 1
done
[[ "${healthy}" == 1 ]] || die "installation receiver capability probe failed"
probe_id="mi-$(${runtime_python} -c 'import uuid; print(uuid.uuid4().hex)')"
probe_body="$(mktemp /run/hermes-installation-probe-body.XXXXXX)"
probe_post="$(mktemp /run/hermes-installation-probe-post.XXXXXX)"
probe_get="$(mktemp /run/hermes-installation-probe-get.XXXXXX)"
printf '{"id":"%s","request_id":"%s","node_id":"dbb3","kind":"probe","identifier":"managed-installation-route-probe","probe":true}\n' \
  "${probe_id}" "${probe_id}" >"${probe_body}"
curl --fail --silent --show-error --max-time 5 --noproxy '*' \
  --config "${health_cfg}" -H 'Content-Type: application/json' \
  --data-binary "@${probe_body}" -o "${probe_post}" \
  http://10.66.0.2:9122/installations
curl --fail --silent --show-error --max-time 5 --noproxy '*' \
  --config "${health_cfg}" -o "${probe_get}" \
  "http://10.66.0.2:9122/installations/${probe_id}"
"${runtime_python}" - "${probe_post}" "${probe_get}" "${probe_id}" dbb3 <<'PY'
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
printf 'unit=%s\nuser=%s\nbackup=%s\n' "${unit_name}" "${receiver_user}" "${backup}"
