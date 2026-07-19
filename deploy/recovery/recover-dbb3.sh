#!/usr/bin/env bash
set -euo pipefail

# This command is intentionally a fixed argv target of the recovery receiver.
systemctl reset-failed hermes-gateway.service hermes-dashboard.service \
  dbb3-team-status.service dbb3-resource-router.service dbb3-ops-watchdog.service \
  dbb3-cloud-connector.service || true
systemctl restart mihomo.service
systemctl restart hermes-gateway.service
systemctl restart hermes-dashboard.service
systemctl restart dbb3-ops-watchdog.service
systemctl restart dbb3-team-status.service
systemctl restart dbb3-resource-router.service

uid="$(id -u hermes)"
runuser -u hermes -- env \
  XDG_RUNTIME_DIR="/run/user/${uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
  systemctl --user restart dbb3-cloud-connector.service dbb3-proxy-relay.service
