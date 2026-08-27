#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() { printf 'install-fabric-auto-update: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"
role="${1:-}"
case "${role}" in dbb3|wsl|hk) ;; *) die "role must be dbb3, wsl, or hk" ;; esac
repo="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
for relative in \
  deploy/automation/update-fabric-node.sh \
  deploy/automation/hermes-fabric-update.service \
  deploy/automation/hermes-fabric-update.timer; do
  [[ -f "${repo}/${relative}" && ! -L "${repo}/${relative}" ]] \
    || die "missing or unsafe ${relative}"
done

install -d -o root -g root -m 0755 /usr/local/lib/hermes-agent /etc/hermes-agent
# ProtectSystem=strict makes /var read-only before ExecStart. The service may
# create its role subdirectory, but its allowlisted parent must already exist.
install -d -o root -g root -m 0755 /var/lib/hermes-agent-fabric-update
install -o root -g root -m 0755 \
  "${repo}/deploy/automation/update-fabric-node.sh" \
  /usr/local/lib/hermes-agent/update-fabric-node.sh
install -o root -g root -m 0644 \
  "${repo}/deploy/automation/hermes-fabric-update.service" \
  /etc/systemd/system/hermes-fabric-update.service
install -o root -g root -m 0644 \
  "${repo}/deploy/automation/hermes-fabric-update.timer" \
  /etc/systemd/system/hermes-fabric-update.timer
printf 'HERMES_FABRIC_ROLE=%s\n' "${role}" >/etc/hermes-agent/fabric-update.env.new.$$
chmod 0600 /etc/hermes-agent/fabric-update.env.new.$$
mv -f -- /etc/hermes-agent/fabric-update.env.new.$$ /etc/hermes-agent/fabric-update.env
systemctl daemon-reload
systemctl enable --now hermes-fabric-update.timer
initial_state=updated
if ! systemctl start hermes-fabric-update.service; then
  # A node may be installed while the public release or another fabric node is
  # temporarily unavailable. The persistent timer owns retries and only marks
  # a commit deployed after the transactional node installer succeeds.
  initial_state=pending
fi
systemctl is-active --quiet hermes-fabric-update.timer
printf 'role=%s\ntimer=active\ninitial_update=%s\n' "${role}" "${initial_state}"
