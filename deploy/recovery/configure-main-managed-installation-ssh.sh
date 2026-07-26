#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() { printf 'configure-main-managed-installation-ssh: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

sshd_config="${HERMES_SSHD_CONFIG:-/etc/ssh/sshd_config}"
sshd_binary="${HERMES_SSHD_BINARY:-/usr/sbin/sshd}"
sshd_service="${HERMES_SSHD_SERVICE:-}"
lock_file="${HERMES_SSHD_LOCK_FILE:-/run/lock/hermes-agent/managed-installation-ssh.lock}"
backup_root="${HERMES_BACKUP_ROOT:-/var/backups/hermes-agent}"
anchor="${HERMES_SSHD_PERMIT_ANCHOR:-127.0.0.1:19122}"
required="${HERMES_SSHD_PERMIT_REQUIRED:-127.0.0.1:19123}"
match_user="${HERMES_SSHD_MATCH_USER:-admin}"
match_address="${HERMES_SSHD_MATCH_ADDRESS:-10.66.0.3}"

[[ -x "${sshd_binary}" ]] || die "sshd binary is missing"
[[ -f "${sshd_config}" && ! -L "${sshd_config}" ]] || die "unsafe sshd config"
[[ "$(stat -c '%u' "${sshd_config}")" == 0 ]] || die "sshd config must be root-owned"
config_dir="$(dirname "${sshd_config}")"
[[ -d "${config_dir}" && ! -L "${config_dir}" ]] || die "unsafe sshd config directory"
[[ "$(stat -c '%u' "${config_dir}")" == 0 ]] || die "sshd config directory must be root-owned"

lock_dir="$(dirname "${lock_file}")"
if [[ ! -d "${lock_dir}" ]]; then
  install -d -o root -g root -m 0755 "${lock_dir}"
fi
[[ -d "${lock_dir}" && ! -L "${lock_dir}" ]] || die "unsafe lock directory"
[[ "$(stat -c '%u' "${lock_dir}")" == 0 ]] || die "lock directory must be root-owned"
lock_mode="$(stat -c '%a' "${lock_dir}")"
(( (8#${lock_mode} & 0022) == 0 )) || die "lock directory must not be group/world-writable"
exec 8>"${lock_file}"
chmod 0600 "${lock_file}"
flock -n 8 || die "another ssh deployment is running"

reload_sshd() {
  if [[ -n "${sshd_service}" ]]; then
    systemctl reload "${sshd_service}"
    return
  fi
  systemctl reload ssh.service 2>/dev/null || systemctl reload sshd.service
}

validate_effective() {
  local effective
  "${sshd_binary}" -t -f "${sshd_config}"
  effective="$("${sshd_binary}" -T -f "${sshd_config}" \
    -C "user=${match_user},host=localhost,addr=${match_address}")"
  grep -Eq '^allowtcpforwarding (yes|remote)$' <<<"${effective}" \
    || die "remote forwarding is not enabled for ${match_user}"
  grep -Eq "^permitlisten .*(${required//./\\.})([[:space:]]|$)" <<<"${effective}" \
    || die "effective PermitListen does not contain ${required}"
}

if grep -Eiq "^[[:space:]]*PermitListen[[:space:]].*(^|[[:space:]])${required//./\\.}([[:space:]]|$)" \
    "${sshd_config}"; then
  validate_effective
  printf 'state=ready\nchanged=0\n'
  exit 0
fi

install -d -o root -g root -m 0700 "${backup_root}"
stamp="$(date +%Y%m%d-%H%M%S)-$$"
backup="$(mktemp "${backup_root}/sshd-managed-installation-${stamp}.XXXXXX")"
candidate="$(mktemp "${config_dir}/.sshd_config.managed-installation.XXXXXX")"
config_uid="$(stat -c '%u' "${sshd_config}")"
config_gid="$(stat -c '%g' "${sshd_config}")"
config_mode="$(stat -c '%a' "${sshd_config}")"
cp --preserve=mode,ownership,timestamps -- "${sshd_config}" "${backup}"
chmod 0600 "${backup}"

changed=0
on_exit() {
  local status=$?
  trap - EXIT INT TERM HUP
  rm -f -- "${candidate}"
  if [[ "${status}" != 0 && "${changed}" == 1 ]]; then
    install -o "${config_uid}" -g "${config_gid}" -m "${config_mode}" \
      "${backup}" "${sshd_config}"
    if ! "${sshd_binary}" -t -f "${sshd_config}" || ! reload_sshd; then
      printf '%s\n' "configure-main-managed-installation-ssh: rollback failed" >&2
      exit 70
    fi
  fi
  exit "${status}"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

python3 - "${sshd_config}" "${candidate}" "${anchor}" "${required}" <<'PY'
from pathlib import Path
import re
import sys

source = Path(sys.argv[1])
target = Path(sys.argv[2])
anchor = sys.argv[3]
required = sys.argv[4]
lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
matches: list[int] = []
for index, line in enumerate(lines):
    match = re.match(r"^(\s*PermitListen\s+)(.*?)(\r?\n)?$", line, re.IGNORECASE)
    if not match:
        continue
    values = match.group(2).split("#", 1)[0].split()
    if anchor in values:
        matches.append(index)
if len(matches) != 1:
    raise SystemExit(f"expected exactly one PermitListen anchor, found {len(matches)}")
index = matches[0]
line = lines[index]
newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
body = line[: -len(newline)] if newline else line
configuration, marker, comment = body.partition("#")
configuration = configuration.rstrip() + " " + required
if marker:
    configuration += " #" + comment
lines[index] = configuration + newline
target.write_text("".join(lines), encoding="utf-8", newline="")
PY

chown "${config_uid}:${config_gid}" "${candidate}"
chmod "${config_mode}" "${candidate}"
"${sshd_binary}" -t -f "${candidate}" || die "candidate sshd configuration is invalid"
install -o "${config_uid}" -g "${config_gid}" -m "${config_mode}" \
  "${candidate}" "${sshd_config}"
changed=1
validate_effective
reload_sshd || die "sshd reload failed"
validate_effective
changed=0
printf 'state=ready\nchanged=1\nbackup=%s\n' "${backup}"
