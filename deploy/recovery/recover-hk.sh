#!/usr/bin/env bash
set -Eeuo pipefail

# This is the recovery receiver's fixed argv target. HK owns only its worker
# connector and the shared fabric updater.
systemctl restart hermes-fabric-update.timer

uid="$(id -u hermes)"
systemctl start "user@${uid}.service"
runuser -u hermes -- env \
  XDG_RUNTIME_DIR="/run/user/${uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
  systemctl --user reset-failed hk-cloud-connector.service || true
runuser -u hermes -- env \
  XDG_RUNTIME_DIR="/run/user/${uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
  systemctl --user restart hk-cloud-connector.service
runuser -u hermes -- env \
  XDG_RUNTIME_DIR="/run/user/${uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
  systemctl --user is-active --quiet hk-cloud-connector.service
