#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() { printf 'install-hk-worker: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

repository="${HERMES_HK_REPOSITORY:-https://github.com/given33/hermes-agent.git}"
[[ "${repository}" == "https://github.com/given33/hermes-agent.git" ]] \
  || die "repository URL is not approved"
service_user="${HERMES_FABRIC_SERVICE_USER:-hermes}"
id "${service_user}" >/dev/null 2>&1 || die "service user does not exist: ${service_user}"
token_file="${HERMES_CLOUD_TOKEN_FILE:-/etc/hk-team/cloud_connector_token}"
[[ -f "${token_file}" && ! -L "${token_file}" ]] || die "create the HK connector token first: ${token_file}"
install -d -o root -g root -m 0755 /opt/hk-team /etc/hk-team

agent_root="${HERMES_HK_AGENT_ROOT:-/opt/hk-team/hermes-agent}"
[[ "${agent_root}" == /opt/hk-team/hermes-agent ]] || die "HK agent root override is not allowed"
if [[ ! -d "${agent_root}/.git" ]]; then
  [[ ! -e "${agent_root}" && ! -L "${agent_root}" ]] || die "HK agent root is unsafe"
  git clone --filter=blob:none --no-checkout -- "${repository}" "${agent_root}"
fi
[[ -d "${agent_root}/.git" && ! -L "${agent_root}" ]] || die "HK source checkout is unsafe"
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

if command -v uv >/dev/null 2>&1; then
  uv sync --locked --python 3.11 --extra all --extra dev --directory "${agent_root}"
elif [[ ! -x "${agent_root}/.venv/bin/hermes" ]]; then
  die "uv is required for first HK provisioning (or preinstall ${agent_root}/.venv/bin/hermes)"
fi

install -d -o "${service_user}" -g "${service_user}" -m 0700 \
  "/home/${service_user}/.hermes" "/home/${service_user}/.config" "/home/${service_user}/.local/state"
HERMES_HK_AGENT_ROOT="${agent_root}" \
  HERMES_FABRIC_SERVICE_USER="${service_user}" \
  bash "${agent_root}/deploy/automation/install-fabric-auto-update.sh" hk "${agent_root}"
