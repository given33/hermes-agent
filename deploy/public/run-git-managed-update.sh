#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# One-shot update runner for the git-managed Hermes agent install. Pulls the
# latest main tarball from GitHub (curl transport — the host firewall blocks
# git's smart-HTTP, so the codeload tarball is used), refreshes the venv, and
# restarts the service only when the remote moved.
#
# Invoked by hermes-git-update.service. Safe to run manually.
#
# Requires a GitHub credential in /root/.git-credentials
# (https://user:TOKEN@github.com) for the private repository.

die() { printf 'run-git-managed-update: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

agent_root="${1:-/opt/hermes-agent}"
repository="${2:-given33/hermes-agent}"
service_name="${3:-hermes-agent}"
branch="${4:-main}"
[[ "${agent_root}" == /opt/hermes-agent ]] || die "agent root override is not allowed"
[[ "${repository}" == "given33/hermes-agent" ]] || die "repository is not approved"
[[ -d "${agent_root}/hermes_cli" ]] || die "agent root is missing hermes_cli"

lock_file="${agent_root}/.git-update.lock"
exec 8>"${lock_file}"
chmod 0600 "${lock_file}"
flock -n 8 || exit 0

token="$(sed -n 's#https://[^:]*:\([^@]*\)@github.com#\1#p' /root/.git-credentials 2>/dev/null | head -1)"
[[ -n "${token}" ]] || die "GitHub credential is missing from /root/.git-credentials"

tmp="$(mktemp -d /tmp/hermes-git-update.XXXXXX)"
trap 'rm -rf -- "${tmp}"' EXIT

# Latest commit on the branch.
remote_commit="$(
  curl -fsSL --max-time 60 \
    -H "Authorization: token ${token}" \
    "https://api.github.com/repos/${repository}/commits/${branch}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"][:12])'
)" || die "failed to resolve remote commit"
[[ "${remote_commit}" =~ ^[0-9a-f]{12}$ ]] || die "invalid remote commit"

before="$(cat "${agent_root}/.hermes-product-commit" 2>/dev/null || true)"
[[ "${before}" == "${remote_commit}" ]] && exit 0

curl -fsSL --max-time 600 \
  -H "Authorization: token ${token}" \
  "https://api.github.com/repos/${repository}/tarball/${branch}" \
  -o "${tmp}/agent.tar.gz" || die "tarball download failed"
mkdir -p "${tmp}/stage"
tar -xzf "${tmp}/agent.tar.gz" -C "${tmp}/stage"

# Preserve .venv and replace the code tree in place.
(
  cd "${tmp}/stage"/given33-hermes-agent-*
  tar -cf - --exclude=.git --exclude=.venv . \
    | tar -C "${agent_root}" -xf -
)
printf '%s' "${remote_commit}" >"${agent_root}/.hermes-product-commit"

# pip must run as the service user: running it as root chowns the venv
# files and the service user can no longer import hermes_cli.
chown -R "${service_user:-hermes-agent}:${service_user:-hermes-agent}"   "${agent_root}/.venv" 2>/dev/null || true
service_user="$(stat -c '%U' "${agent_root}/.venv/bin/python" 2>/dev/null || echo hermes-agent)"
# The editable install must not hang on PyPI/network during a managed update.
# The repo ships with a complete venv; --no-deps --no-build-isolation makes the
# refresh local-only and fast, and PIP_DEFAULT_TIMEOUT bounds any residual I/O.
PIP_DEFAULT_TIMEOUT=30 PIP_NO_INDEX=1 runuser -u "${service_user}" -- \
  "${agent_root}/.venv/bin/python" -m pip install -q --no-deps --no-build-isolation -e "${agent_root}" \
  || die "editable install failed"
systemctl restart "${service_name}"
systemctl is-active --quiet "${service_name}" \
  || die "service is not active after update"

printf 'run-git-managed-update: %s -> %s\n' "${before:-none}" "${remote_commit}"
