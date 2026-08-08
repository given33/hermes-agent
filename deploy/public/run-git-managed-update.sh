#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# One-shot update runner for the git-managed Hermes agent install. Pulls
# github.com/given33/hermes-agent main, refreshes the venv, and restarts the
# service only when the remote moved.
#
# Invoked by hermes-git-update.service. Safe to run manually.

die() { printf 'run-git-managed-update: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

agent_root="${1:-/opt/hermes-agent}"
repository_url="${2:-https://github.com/given33/hermes-agent.git}"
service_name="${3:-hermes-agent}"
[[ "${agent_root}" == /opt/hermes-agent ]] || die "agent root override is not allowed"
[[ "${repository_url}" == "https://github.com/given33/hermes-agent.git" ]] \
  || die "repository URL is not approved"
[[ -d "${agent_root}/.git" ]] || die "agent root is not a git checkout"

lock_file="${agent_root}/.git-update.lock"
exec 8>"${lock_file}"
chmod 0600 "${lock_file}"
flock -n 8 || exit 0

before="$(git -C "${agent_root}" rev-parse HEAD 2>/dev/null || true)"
GIT_TERMINAL_PROMPT=0 timeout 120 git -C "${agent_root}" fetch --depth 1 origin main || exit 0
after="$(git -C "${agent_root}" rev-parse origin/main 2>/dev/null || true)"
[[ -n "${before}" && "${before}" == "${after}" ]] && exit 0

git -C "${agent_root}" checkout -q -B main origin/main
git -C "${agent_root}" reset -q --hard origin/main
"${agent_root}/.venv/bin/pip" install -q -e "${agent_root}" || die "editable install failed"
systemctl restart "${service_name}"
systemctl is-active --quiet "${service_name}" || die "service is not active after update"

printf 'run-git-managed-update: %s -> %s\n' "${before}" "${after}"
