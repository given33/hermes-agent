#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() { printf 'install-fabric-auto-update: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

# Verify the nearest existing ancestor before creating role/profile or
# root-owned updater paths.  This prevents a symlinked ancestor from turning
# an apparently isolated deployment into a write outside its role.
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
role="${1:-}"
case "${role}" in dbb3|wsl|hk) ;; *) die "role must be dbb3, wsl, or hk" ;; esac
repo="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
assert_canonical_path "${repo}" "fabric source repository"
for relative in \
  deploy/automation/update-fabric-node.sh \
  deploy/automation/hermes-fabric-update.service \
  deploy/automation/hermes-fabric-update.timer; do
  [[ -f "${repo}/${relative}" && ! -L "${repo}/${relative}" ]] \
    || die "missing or unsafe ${relative}"
done

service_user="${HERMES_FABRIC_SERVICE_USER:-hermes}"
id "${service_user}" >/dev/null 2>&1 || die "fabric service user is missing"
service_home="$(getent passwd "${service_user}" | cut -d: -f6)"
[[ -n "${service_home}" && -d "${service_home}" ]] || die "fabric service home is missing"
assert_canonical_path "${service_home}" "fabric service home"
case "${role}" in
  dbb3) worker_home="${service_home}/.hermes/profiles/dbb3-worker" ;;
  wsl) worker_home="/mnt/d/Hermes/home/profiles/pc-worker" ;;
  hk) worker_home="${service_home}/.hermes/profiles/hk-worker" ;;
esac
[[ "${worker_home}" == /* && ! -L "${worker_home}" ]] \
  || die "worker profile home is unsafe"
assert_canonical_path "${worker_home}" "worker profile home"
install -d -o "${service_user}" -g "${service_user}" -m 0700 \
  "${worker_home}" "${worker_home}/skills"
assert_canonical_path "${worker_home}" "worker profile home"
assert_canonical_path "${worker_home}/skills" "worker profile skills"

assert_canonical_path /usr/local/lib/hermes-agent "fabric runtime directory"
assert_canonical_path /etc/hermes-agent "fabric configuration directory"
install -d -o root -g root -m 0755 /usr/local/lib/hermes-agent /etc/hermes-agent
# ProtectSystem=strict makes /var read-only before ExecStart. The service may
# create its role subdirectory, but its allowlisted parent must already exist.
state_root="/var/lib/hermes-agent-fabric-update/${role}"
assert_canonical_path /var/lib/hermes-agent-fabric-update "fabric state parent"
assert_canonical_path "${state_root}" "fabric state root"
install -d -o root -g root -m 0755 /var/lib/hermes-agent-fabric-update
install -d -o root -g root -m 0755 "${state_root}"
assert_canonical_path "${state_root}" "fabric state root"
[[ -d "${state_root}" && ! -L "${state_root}" \
    && "$(realpath -e -- "${state_root}")" == "${state_root}" ]] \
  || die "fabric updater state root is unsafe"
lock_file="${state_root}/update.lock"
exec 8>"${lock_file}"
chmod 0600 "${lock_file}"
flock -w 900 8 || die "timed out waiting for the fabric updater lock"

script_target=/usr/local/lib/hermes-agent/update-fabric-node.sh
service_target=/etc/systemd/system/hermes-fabric-update.service
timer_target=/etc/systemd/system/hermes-fabric-update.timer
script_temp="${script_target}.new.$$"
service_temp="${service_target}.new.$$"
timer_temp="${timer_target}.new.$$"
env_temp="/etc/hermes-agent/fabric-update.env.new.$$"
assert_canonical_path "$(dirname -- "${script_target}")" "fabric script target directory"
assert_canonical_path "$(dirname -- "${service_target}")" "fabric service target directory"
assert_canonical_path "$(dirname -- "${timer_target}")" "fabric timer target directory"
assert_canonical_path "$(dirname -- /etc/hermes-agent/fabric-update.env)" "fabric env target directory"
cleanup() {
  rm -f -- "${script_temp}" "${service_temp}" "${timer_temp}" "${env_temp}"
}
trap cleanup EXIT
install -o root -g root -m 0755 \
  "${repo}/deploy/automation/update-fabric-node.sh" \
  "${script_temp}"
install -o root -g root -m 0644 \
  "${repo}/deploy/automation/hermes-fabric-update.service" \
  "${service_temp}"
install -o root -g root -m 0644 \
  "${repo}/deploy/automation/hermes-fabric-update.timer" \
  "${timer_temp}"
printf 'HERMES_FABRIC_ROLE=%s\n' "${role}" >"${env_temp}"
chmod 0600 "${env_temp}"
mv -f -- "${script_temp}" "${script_target}"
mv -f -- "${service_temp}" "${service_target}"
mv -f -- "${timer_temp}" "${timer_target}"
mv -f -- "${env_temp}" /etc/hermes-agent/fabric-update.env
systemctl daemon-reload
systemctl enable --now hermes-fabric-update.timer
flock -u 8
initial_state=updated
if ! systemctl start hermes-fabric-update.service; then
  # A node may be installed while the public release or another fabric node is
  # temporarily unavailable. The persistent timer owns retries and only marks
  # a commit deployed after the transactional node installer succeeds.
  initial_state=pending
fi
systemctl is-active --quiet hermes-fabric-update.timer
trap - EXIT
cleanup
printf 'role=%s\ntimer=active\ninitial_update=%s\n' "${role}" "${initial_state}"
