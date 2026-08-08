#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Convert the server's /opt/hermes-agent install into a GitHub-managed
# checkout of given33/hermes-agent main and install a systemd timer that
# pulls the latest main and restarts the service. Uses the codeload tarball
# transport (see run-git-managed-update.sh). Keeps HERMES_HOME data intact.
#
# Usage: sudo bash install-git-managed-agent.sh
# Idempotent: safe to re-run.

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

# 1. Record the current commit so the first timer run can compare.
if [[ ! -f "${agent_root}/.hermes-product-commit" ]]; then
  git -C "${agent_root}" rev-parse HEAD 2>/dev/null \
    >"${agent_root}/.hermes-product-commit" || true
fi

# 2. Restart with the current tree (already 0.20 from the release sync).
systemctl restart "${service_name}" || die "service restart failed"
systemctl is-active --quiet "${service_name}" || die "service is not active after restart"

# 3. Install the auto-update timer (every N minutes, staggered).
unit_name="hermes-git-update"
unit_file="/etc/systemd/system/${unit_name}.service"
timer_file="/etc/systemd/system/${unit_name}.timer"
cat >"${unit_file}" <<EOF
[Unit]
Description=Pull given33/hermes-agent main and restart ${service_name}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=${agent_root}/deploy/public/run-git-managed-update.sh ${agent_root} given33/hermes-agent ${service_name} main
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
