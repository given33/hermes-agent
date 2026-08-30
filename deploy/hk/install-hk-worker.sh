#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() { printf 'install-hk-worker: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

# Check existing ancestors before bootstrap mkdirs.  A leaf-only symlink
# check can miss a redirect in /opt or /etc and write credentials/profile data
# into an unintended tree.
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

repository="${HERMES_HK_REPOSITORY:-https://github.com/given33/hermes-agent.git}"
[[ "${repository}" == "https://github.com/given33/hermes-agent.git" ]] \
  || die "repository URL is not approved"
service_user="${HERMES_FABRIC_SERVICE_USER:-hermes}"
id "${service_user}" >/dev/null 2>&1 || die "service user does not exist: ${service_user}"
[[ "${service_user}" == hermes ]] || die "HK production service user must be hermes"
token_file="${HERMES_CLOUD_TOKEN_FILE:-/etc/hk-team/cloud_connector_token}"
assert_canonical_path "${token_file}" "HK connector token"
[[ -f "${token_file}" && ! -L "${token_file}" ]] || die "create the HK connector token first: ${token_file}"
for recovery_credential in \
  /etc/hk-team/recovery_token \
  "/home/${service_user}/.ssh/hk_recovery_ed25519" \
  "/home/${service_user}/.ssh/hk_recovery_known_hosts"; do
  assert_canonical_path "${recovery_credential}" "HK recovery credential"
  [[ -f "${recovery_credential}" && ! -L "${recovery_credential}" ]] \
    || die "provision the HK recovery credential first: ${recovery_credential}"
done
assert_canonical_path /opt/hk-team "HK deployment directory"
assert_canonical_path /etc/hk-team "HK configuration directory"
install -d -o root -g root -m 0755 /opt/hk-team /etc/hk-team
assert_canonical_path /opt/hk-team "HK deployment directory"
assert_canonical_path /etc/hk-team "HK configuration directory"

agent_root="${HERMES_HK_AGENT_ROOT:-/opt/hk-team/hermes-agent}"
[[ "${agent_root}" == /opt/hk-team/hermes-agent ]] || die "HK agent root override is not allowed"
assert_canonical_path "${agent_root}" "HK agent root"
if [[ ! -d "${agent_root}/.git" ]]; then
  [[ ! -e "${agent_root}" && ! -L "${agent_root}" ]] || die "HK agent root is unsafe"
  git clone --filter=blob:none --no-checkout -- "${repository}" "${agent_root}"
fi
[[ -d "${agent_root}/.git" && ! -L "${agent_root}" ]] || die "HK source checkout is unsafe"
assert_canonical_path "${agent_root}" "HK agent root"
git -C "${agent_root}" fetch --force --prune origin '+refs/heads/main:refs/remotes/origin/main'
release_commit="${1:-${HERMES_HK_RELEASE_COMMIT:-}}"
if [[ -n "${release_commit}" ]]; then
  [[ "${release_commit}" =~ ^[0-9a-f]{40}$ ]] || die "release commit is invalid"
  git -C "${agent_root}" cat-file -e "${release_commit}^{commit}"
  git -C "${agent_root}" merge-base --is-ancestor "${release_commit}" refs/remotes/origin/main \
    || die "release commit is not part of origin/main"
  git -C "${agent_root}" checkout --force --detach "${release_commit}"
else
  git -C "${agent_root}" checkout --force --detach origin/main
fi

command -v uv >/dev/null 2>&1 \
  || die "uv is required to build the locked HK source generation"

service_home="$(getent passwd "${service_user}" | cut -d: -f6)"
[[ -n "${service_home}" && -d "${service_home}" ]] || die "service user home is missing"
assert_canonical_path "${service_home}" "HK service home"
hermes_home="${service_home}/.hermes/profiles/hk-worker"
profile_root="${hermes_home}"
assert_canonical_path "${hermes_home}" "HK worker profile"
assert_canonical_path "${service_home}/.config" "HK user config directory"
assert_canonical_path "${service_home}/.local/state" "HK user state directory"
install -d -o "${service_user}" -g "${service_user}" -m 0700 \
  "${profile_root}" "${profile_root}/skills" "${service_home}/.config" \
  "${service_home}/.local/state"
assert_canonical_path "${hermes_home}" "HK worker profile"
assert_canonical_path "${profile_root}/skills" "HK profile skills"
for mapping in config.yaml.example:config.yaml SOUL.md:SOUL.md; do
  source_name="${mapping%%:*}"
  target_name="${mapping#*:}"
  source_template="${agent_root}/deploy/hk/profile/${source_name}"
  target_template="${profile_root}/${target_name}"
  assert_canonical_path "${source_template}" "HK profile template"
  assert_canonical_path "${target_template}" "HK profile target"
  [[ -f "${source_template}" && ! -L "${source_template}" ]] \
    || die "HK profile template is missing: ${source_template}"
  [[ ! -L "${target_template}" ]] || die "HK profile target is a symlink: ${target_template}"
  if [[ ! -e "${target_template}" ]]; then
    install -o "${service_user}" -g "${service_user}" -m 0600 \
      "${source_template}" "${target_template}"
  fi
done
HERMES_HK_AGENT_ROOT="${agent_root}" \
  HERMES_HK_HOME="${hermes_home}" \
  HERMES_FABRIC_SERVICE_USER="${service_user}" \
  bash "${agent_root}/deploy/automation/install-fabric-auto-update.sh" hk "${agent_root}"

expected_commit="${release_commit:-$(git -C "${agent_root}" rev-parse HEAD)}"
deployed_file="/var/lib/hermes-agent-fabric-update/hk/deployed-commit"
release_file="/var/lib/hermes-agent-fabric-update/hk/release.json"
current_link="${agent_root}/.fabric-current"
[[ -f "${deployed_file}" && ! -L "${deployed_file}" \
    && "$(cat -- "${deployed_file}")" == "${expected_commit}" ]] \
  || die "HK target commit was not deployed"
[[ -L "${current_link}" ]] || die "HK current source generation is missing"
current_generation="$(readlink -f -- "${current_link}")"
[[ "$(cat -- "${current_generation}/.hermes-source-commit" 2>/dev/null)" == "${expected_commit}" \
    && -f "${current_generation}/uv.lock" \
    && -x "${current_generation}/.venv/bin/hermes" ]] \
  || die "HK current source generation is incomplete"
python3 - "${release_file}" "${expected_commit}" "${current_generation}" <<'PY'
import json
import sys

release = json.load(open(sys.argv[1], encoding="utf-8"))
assert release.get("node_id") == "hk"
assert release.get("commit") == sys.argv[2]
source = release.get("source") or {}
assert source.get("commit") == sys.argv[2]
assert source.get("generation") == sys.argv[3]
assert source.get("lock") == "uv.lock"
PY
systemctl is-active --quiet hermes-fabric-update.timer \
  hermes-hk-managed-node-recovery.service \
  hermes-hk-managed-node-recovery-tunnel.service \
  || die "HK timer or recovery service is not active"
service_uid="$(id -u "${service_user}")"
systemctl start "user@${service_uid}.service"
runuser -u "${service_user}" -- env \
  HOME="${service_home}" \
  XDG_RUNTIME_DIR="/run/user/${service_uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${service_uid}/bus" \
  systemctl --user is-active --quiet hk-cloud-connector.service \
    hermes-gateway-hk-worker.service \
  || die "HK connector or gateway service is not active"
runuser -u "${service_user}" -- env -u HERMES_HOME \
  HOME="${service_home}" \
  PATH="${current_link}/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
  XDG_RUNTIME_DIR="/run/user/${service_uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${service_uid}/bus" \
  "${current_link}/.venv/bin/hermes" -p hk-worker gateway status >/dev/null \
  || die "HK gateway status probe failed"

printf 'commit=%s\ngeneration=%s\nconnector=active\ngateway=active\ntimer=active\nrecovery=active\n' \
  "${expected_commit}" "${current_generation}"
