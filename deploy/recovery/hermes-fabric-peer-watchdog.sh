#!/usr/bin/env bash
set -Eeuo pipefail

role="${HERMES_FABRIC_WATCHDOG_ROLE:?HERMES_FABRIC_WATCHDOG_ROLE is required}"
interval="${HERMES_FABRIC_WATCHDOG_INTERVAL:-30}"
failure_threshold="${HERMES_FABRIC_WATCHDOG_FAILURE_THRESHOLD:-2}"
state_dir="${HERMES_FABRIC_WATCHDOG_STATE_DIR:-/var/lib/hermes-fabric-peer-watchdog}"
ssh_identity="${HERMES_FABRIC_WATCHDOG_IDENTITY:-${HOME}/.ssh/id_ed25519}"
known_hosts="${HERMES_FABRIC_WATCHDOG_KNOWN_HOSTS:-${HOME}/.ssh/known_hosts.hermes-fabric}"

case "${role}" in
  aliyun|wsl) targets=(
    'dbb3|10.67.0.2|root|22|systemctl|hermes-gateway.service'
    'hk|10.67.0.4|root|22|systemctl|hermes-gateway-hk-worker.service'
  ) ;;
  dbb3|hk) targets=(
    'aliyun|10.67.0.1|admin|22|sudo -n systemctl|hermes-gateway.service'
    'wsl|10.67.0.3|hermes|2222|systemctl --user|hermes-wsl-gateway.service'
  ) ;;
  *) printf 'unknown watchdog role: %s\n' "${role}" >&2; exit 2 ;;
esac

mkdir -p "${state_dir}"
chmod 0700 "${state_dir}"
ssh_args=(
  -F /dev/null -i "${ssh_identity}" -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile="${known_hosts}"
  -o ConnectTimeout=8 -o ConnectionAttempts=1
  -o ControlMaster=no -o ControlPath=none
  -o ServerAliveInterval=5 -o ServerAliveCountMax=1
)

while :; do
  for row in "${targets[@]}"; do
    IFS='|' read -r id host user port ctl unit <<<"${row}"
    marker="${state_dir}/${id}.failures"
    # Failed SSH probes say nothing about service health. systemd owns startup
    # deadlines, crash restarts and watchdog timeouts on the destination host.
    if ! state=$(ssh "${ssh_args[@]}" -p "${port}" "${user}@${host}" \
        "if test -f /run/hermes-runtime-code-updating && systemctl is-active --quiet hermes-runtime-sync.service; then printf updating; else ${ctl} show '${unit}' --property=ActiveState --value; fi" 2>/dev/null); then
      rm -f -- "${marker}"
      logger -t hermes-fabric-peer-watchdog "${id} unreachable; leaving its service unchanged"
      continue
    fi
    case "${state}" in
      active|activating|reloading|deactivating|updating) rm -f -- "${marker}"; continue ;;
      inactive|failed) ;;
      *) rm -f -- "${marker}"; continue ;;
    esac
    failures=0
    if [[ -r "${marker}" ]]; then read -r failures <"${marker}" || failures=0; fi
    [[ "${failures}" =~ ^[0-9]+$ ]] || failures=0
    failures=$((failures + 1))
    printf '%s\n' "${failures}" >"${marker}"
    if (( failures < failure_threshold )); then continue; fi
    # Recheck remotely immediately before start; never interrupt a healthy or
    # already-starting gateway when another supervisor recovered it meanwhile.
    if ssh "${ssh_args[@]}" -p "${port}" "${user}@${host}" \
        "if test -f /run/hermes-runtime-code-updating && systemctl is-active --quiet hermes-runtime-sync.service; then exit 0; fi; state=\$(${ctl} show '${unit}' --property=ActiveState --value) || exit; case \"\$state\" in inactive|failed) ${ctl} start --no-block '${unit}';; esac" >/dev/null 2>&1; then
      logger -t hermes-fabric-peer-watchdog "${id} recovery check completed"
      rm -f -- "${marker}"
    fi
  done
  sleep "${interval}"
done
