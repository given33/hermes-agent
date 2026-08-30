#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() { printf 'configure-main-managed-installation-ssh: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

sshd_config="${HERMES_SSHD_CONFIG:-/etc/ssh/sshd_config}"
sshd_binary="${HERMES_SSHD_BINARY:-/usr/sbin/sshd}"
python_binary="${HERMES_SSHD_PYTHON:-python3}"
sshd_service="${HERMES_SSHD_SERVICE:-}"
lock_file="${HERMES_SSHD_LOCK_FILE:-/run/lock/hermes-agent/managed-installation-ssh.lock}"
backup_root="${HERMES_BACKUP_ROOT:-/var/backups/hermes-agent}"
anchor="${HERMES_SSHD_PERMIT_ANCHOR:-127.0.0.1:19122}"
required="${HERMES_SSHD_PERMIT_REQUIRED:-127.0.0.1:19123 127.0.0.1:19124}"
match_user="${HERMES_SSHD_MATCH_USER:-admin}"
match_address="${HERMES_SSHD_MATCH_ADDRESS:-10.66.0.3}"

[[ -x "${sshd_binary}" ]] || die "sshd binary is missing"
command -v "${python_binary}" >/dev/null 2>&1 || die "Python runtime is missing"
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
  local effective listen
  "${sshd_binary}" -t -f "${sshd_config}"
  effective="$("${sshd_binary}" -T -f "${sshd_config}" \
    -C "user=${match_user},host=localhost,addr=${match_address}")"
  grep -Eq '^allowtcpforwarding (yes|remote)$' <<<"${effective}" \
    || die "remote forwarding is not enabled for ${match_user}"
  for listen in ${required}; do
    [[ "${listen}" =~ ^127\.0\.0\.1:[1-9][0-9]{0,4}$ ]] \
      || die "invalid required PermitListen value: ${listen}"
    grep -Eq "^permitlisten .*(${listen//./\\.})([[:space:]]|$)" <<<"${effective}" \
      || die "effective PermitListen does not contain ${listen}"
  done
}

configured=1
for listen in ${required}; do
  grep -Eiq "^[[:space:]]*PermitListen[[:space:]].*(${listen//./\\.})([[:space:]]|$)" \
    "${sshd_config}" || configured=0
done
if (( configured )); then
  validate_effective
  printf 'state=ready\nchanged=0\n'
  exit 0
fi

install -d -o root -g root -m 0700 "${backup_root}"
stamp="$(date +%Y%m%d-%H%M%S)-$$"

# Rollback state.  changed=1 only while the live config may differ from the
# backup (it is set immediately before the atomic rename below).  backup and
# candidate start empty so on_exit can run safely even when a failure happens
# before the temp files exist.
changed=0
backup=""
candidate=""

restore_original() {
  # Returns 0 when the original config is back on disk and sshd reloaded,
  # 1 when the restore itself failed (the live config may still be the new
  # one), and 2 when the original is back on disk but the reload failed.
  # Callers must not conflate 1 and 2: with 2 the on-disk state is already
  # correct and only a manual reload is outstanding.
  local rollback_candidate
  rollback_candidate="$(mktemp "${config_dir}/.sshd_config.rollback.XXXXXX")" || return 1
  if ! install -o "${config_uid}" -g "${config_gid}" -m "${config_mode}" \
      "${backup}" "${rollback_candidate}" \
    || ! "${sshd_binary}" -t -f "${rollback_candidate}" \
    || ! sync -f "${rollback_candidate}"; then
    rm -f -- "${rollback_candidate}"
    return 1
  fi
  if ! mv -f -- "${rollback_candidate}" "${sshd_config}"; then
    rm -f -- "${rollback_candidate}"
    return 1
  fi
  # The rename is already visible at this point; the directory sync is
  # durability-only and must not turn a successful restore into a failure.
  sync -f "${config_dir}" || true
  reload_sshd || return 2
}
on_exit() {
  local status=$?
  trap - EXIT INT TERM HUP
  if [[ -n "${candidate}" ]]; then
    rm -f -- "${candidate}"
  fi
  if [[ "${status}" != 0 && "${changed}" == 1 ]]; then
    local restore_status=0
    restore_original || restore_status=$?
    if [[ "${restore_status}" == 2 ]]; then
      printf '%s\n' "configure-main-managed-installation-ssh: original sshd configuration restored, but sshd reload failed; reload sshd manually" >&2
      exit 71
    elif [[ "${restore_status}" != 0 ]]; then
      printf '%s\n' "configure-main-managed-installation-ssh: rollback failed; original configuration saved at ${backup}" >&2
      exit 70
    fi
  fi
  exit "${status}"
}
prune_backups() {
  # One backup accumulates per modifying run and nothing ever deleted them,
  # so ${backup_root} grew without bound.  Keep the newest N (including the
  # one just created); pruning is best-effort — a hiccup here must not abort
  # the deployment.
  local keep stale old
  keep="${HERMES_SSHD_BACKUP_KEEP:-5}"
  [[ "${keep}" =~ ^[0-9]+$ ]] || keep=5
  (( keep >= 1 )) || keep=1
  stale="$(ls -1t -- "${backup_root}"/sshd-managed-installation-* 2>/dev/null | tail -n "+$((keep + 1))" || true)"
  [[ -z "${stale}" ]] || while IFS= read -r old; do
    rm -f -- "${old}" || true
  done <<<"${stale}"
}
# Install the cleanup trap before creating any temp file so a failure in the
# snapshot block below cannot leak the backup or candidate temp files.
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

backup="$(mktemp "${backup_root}/sshd-managed-installation-${stamp}.XXXXXX")"
candidate="$(mktemp "${config_dir}/.sshd_config.managed-installation.XXXXXX")"
config_uid="$(stat -c '%u' "${sshd_config}")"
config_gid="$(stat -c '%g' "${sshd_config}")"
config_mode="$(stat -c '%a' "${sshd_config}")"
cp --preserve=mode,ownership,timestamps -- "${sshd_config}" "${backup}"
chmod 0600 "${backup}"
prune_backups

"${python_binary}" - "${sshd_config}" "${candidate}" "${anchor}" ${required} <<'PY'
from pathlib import Path
import re
import sys

source = Path(sys.argv[1])
target = Path(sys.argv[2])
anchor = sys.argv[3]
required = sys.argv[4:]
lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
matches = []
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
values = configuration.split()[1:]
for required_value in required:
    if required_value not in values:
        configuration = configuration.rstrip() + " " + required_value
if marker:
    configuration += " #" + comment
lines[index] = configuration + newline
with open(target, "w", encoding="utf-8", newline="") as handle:
    handle.write("".join(lines))
PY

chown "${config_uid}:${config_gid}" "${candidate}"
chmod "${config_mode}" "${candidate}"
"${sshd_binary}" -t -f "${candidate}" || die "candidate sshd configuration is invalid"
sync -f "${candidate}" || die "candidate sshd configuration could not be synced"
# Only the rename below mutates the live config, so flip changed=1 right
# before it: a candidate-stage failure above must exit without a rollback
# (the config was never touched, and a spurious rollback whose reload failed
# would be misreported as "rollback failed").  It cannot be set after the mv
# either — a reported mv failure is ambiguous (the rename may have landed),
# so rollback must treat the config as modified in that case.
changed=1
mv -f -- "${candidate}" "${sshd_config}" || die "atomic sshd configuration replacement failed"
sync -f "${config_dir}" || die "sshd configuration directory could not be synced"
validate_effective
reload_sshd || die "sshd reload failed"
validate_effective
changed=0
printf 'state=ready\nchanged=1\nbackup=%s\n' "${backup}"
