#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Convert the server's /opt/hermes-agent install into a git-managed checkout
# of github.com/given33/hermes-agent and install a systemd timer that pulls
# the latest main and restarts the service. Keeps HERMES_HOME data untouched.
#
# Usage: sudo bash install-git-managed-agent.sh
# Idempotent: safe to re-run; a non-git directory is converted in place.

die() { printf 'install-git-managed-agent: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

agent_root="${HERMES_AGENT_ROOT:-/opt/hermes-agent}"
repository_url="${HERMES_AGENT_REPOSITORY:-https://github.com/given33/hermes-agent.git}"
service_name="${HERMES_AGENT_SERVICE:-hermes-agent}"
update_interval_min="${HERMES_AGENT_UPDATE_INTERVAL_MIN:-10}"
[[ "${agent_root}" == /opt/hermes-agent ]] || die "agent root override is not allowed"
[[ "${repository_url}" == "https://github.com/given33/hermes-agent.git" ]] \
  || die "repository URL is not approved"

[[ -d "${agent_root}" && ! -L "${agent_root}" ]] || die "agent root is missing or unsafe"
[[ -d "${agent_root}/hermes_cli" && ! -L "${agent_root}/hermes_cli" ]] \
  || die "agent root has no hermes_cli package"

venv="${agent_root}/.venv"
[[ -x "${venv}/bin/python" ]] || die "agent venv is missing at ${venv}"

# 1. Convert in place to a git checkout of main (keeps .venv and config).
if [[ ! -d "${agent_root}/.git" ]]; then
  git -C "${agent_root}" init -q
  git -C "${agent_root}" remote add origin "${repository_url}"
fi
git -C "${agent_root}" fetch --depth 1 origin main || die "git fetch failed"
git -C "${agent_root}" checkout -q -B main origin/main || die "git checkout failed"
git -C "${agent_root}" reset -q --hard origin/main

# 2. Refresh the virtualenv for the new tree.
"${venv}/bin/python" -m pip install -q --upgrade pip
"${venv}/bin/pip" install -q -e "${agent_root}" || die "editable install failed"

# 3. Restart the service with the new code.
systemctl restart "${service_name}" || die "service restart failed"
systemctl is-active --quiet "${service_name}" || die "service is not active after restart"

# 4. Install the auto-update timer (every N minutes, staggered).
unit_name="hermes-git-update"
unit_file="/etc/systemd/system/${unit_name}.service"
timer_file="/etc/systemd/system/${unit_name}.timer"
cat >"${unit_file}" <<EOF
[Unit]
Description=Pull github.com/given33/hermes-agent main and restart ${service_name}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=${agent_root}/deploy/public/run-git-managed-update.sh ${agent_root} ${repository_url} ${service_name}
EOF
cat >"${timer_file}" <<EOF
[Unit]
Description=Periodic Hermes agent git update

[Timer]
OnBootSec=2min
OnUnitActiveSec=${update_interval_min}min
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now "${unit_name}.timer"

printf 'install-git-managed-agent: ok (root=%s service=%s timer=%s)\n' \
  "${agent_root}" "${service_name}" "${unit_name}.timer"
