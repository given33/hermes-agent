#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ "$(id -u)" == 0 ]] || {
  printf '%s\n' "test-install-collaboration-backend: root is required" >&2
  exit 1
}

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
installer="${here}/install-collaboration-backend.sh"
version="$(python3 - "${repo}/plugins/collaboration/dashboard/manifest.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])
PY
)"
work="$(mktemp -d /tmp/hermes-public-installer-test.XXXXXX)"
stage="/home/root/.cache/hermes-agent-deploy/${version}-test-$$"
cleanup() {
  rm -rf -- "${work}" "${stage}"
}
trap cleanup EXIT

runtime_files=(
  "plugins/collaboration/dashboard/plugin_api.py"
  "plugins/collaboration/dashboard/manifest.json"
  "plugins/collaboration/dashboard/dist/index.js"
  "hermes_cli/cloud_file_library.py"
  "hermes_cli/dashboard_auth/token_auth.py"
  "hermes_cli/dashboard_auth/mobile_device_store.py"
  "hermes_cli/dashboard_auth/mobile_notifications.py"
  "hermes_cli/web_server.py"
  "tui_gateway/server.py"
)

target="${work}/target"
backup="${work}/backups"
fake_bin="${work}/bin"
token_file="${work}/connector.token"
state_file="${work}/state/single.json"
install -d -m 0700 "${stage}" "${target}" "${backup}" "${fake_bin}"
install -d -m 0700 "$(dirname "${state_file}")"
printf '%s' "connector-test-token" >"${token_file}"
printf '%s\n' '{"conversations":[{"id":"old-state"}]}' >"${state_file}"
for relative in "${runtime_files[@]}"; do
  install -D -m 0644 "${repo}/${relative}" "${stage}/${relative}"
  install -D -m 0644 /dev/null "${target}/${relative}"
  printf 'old:%s\n' "${relative}" >"${target}/${relative}"
done

cat >"${fake_bin}/systemctl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "${1:-}" >>"${FAKE_SYSTEMCTL_LOG}"
if [[ "${1:-}" == "start" && "${FAKE_STATUS_FAIL:-0}" == 1 \
  && ! -e "${HERMES_COLLABORATION_STATE_FILE}.mutated" ]]; then
  printf '%s\n' '{"conversations":[{"id":"new-state"}]}' >"${HERMES_COLLABORATION_STATE_FILE}"
  : >"${HERMES_COLLABORATION_STATE_FILE}.mutated"
fi
if [[ "${1:-}" == "start" && "${FAKE_SIGNAL_ON_START:-0}" == 1 \
  && ! -e "${HERMES_COLLABORATION_STATE_FILE}.signaled" ]]; then
  printf '%s\n' '{"conversations":[{"id":"signal-state"}]}' >"${HERMES_COLLABORATION_STATE_FILE}"
  : >"${HERMES_COLLABORATION_STATE_FILE}.signaled"
  kill -TERM "${PPID}"
fi
case "${1:-}" in
  stop|start|is-active) exit 0 ;;
  *) exit 0 ;;
esac
SH
cat >"${fake_bin}/sleep" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat >"${fake_bin}/curl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
output=""
next_is_output=0
for arg in "$@"; do
  if [[ "${next_is_output}" == 1 ]]; then
    output="${arg}"
    next_is_output=0
  elif [[ "${arg}" == "-o" ]]; then
    next_is_output=1
  fi
done
url="${!#}"
if [[ "${url}" == */api/status ]]; then
  [[ "${FAKE_STATUS_FAIL:-0}" != 1 ]] || exit 22
  payload='{"status":"ok"}'
elif [[ "${url}" == */api/plugins/ios-intelligence/health ]]; then
  payload="$(python3 - <<'PY'
import json
services = [
    {"name": f"service-{index}", "ok": True, "tools": ["read", "write"] + (["extra"] if index < 2 else [])}
    for index in range(21)
]
print(json.dumps({
    "ok": True,
    "scheduler_running": True,
    "mcp_runtime": {
        "ok": True,
        "running": True,
        "healthy_count": 21,
        "required_count": 21,
        "services": services,
    },
}))
PY
)"
else
  payload='{"ok":true,"contract_version":1,"connector_id":"dbb3-primary","capabilities":["artifact-upload","attachment-download"]}'
fi
if [[ -n "${output}" ]]; then
  printf '%s\n' "${payload}" >"${output}"
else
  printf '%s\n' "${payload}"
fi
SH
chmod 0755 "${fake_bin}/systemctl" "${fake_bin}/sleep" "${fake_bin}/curl"

run_installer() {
  env \
    PATH="${fake_bin}:${PATH}" \
    FAKE_STATUS_FAIL="$1" \
    FAKE_SIGNAL_ON_START="${2:-0}" \
    HERMES_AGENT_ROOT="${target}" \
    HERMES_RUNTIME_PYTHON="$(command -v python3)" \
    HERMES_AGENT_SERVICE="hermes-agent-test.service" \
    HERMES_AGENT_USER="root" \
    HERMES_AGENT_GROUP="root" \
    HERMES_STAGE_OWNER="root" \
    HERMES_BACKUP_ROOT="${backup}" \
    HERMES_COLLABORATION_STATE_FILE="${state_file}" \
    HERMES_COLLABORATION_CONNECTOR_TOKEN_FILE="${token_file}" \
    FAKE_SYSTEMCTL_LOG="${work}/systemctl.log" \
    /bin/bash "${installer}" "${version}" "${stage}"
}

set +e
run_installer 1 0 >"${work}/failure.stdout" 2>"${work}/failure.stderr"
failure_status=$?
set -e
[[ "${failure_status}" -ne 0 ]] || {
  printf '%s\n' "forced post-start failure unexpectedly succeeded" >&2
  exit 1
}
for relative in "${runtime_files[@]}"; do
  [[ "$(<"${target}/${relative}")" == "old:${relative}" ]] || {
    printf 'rollback mismatch: %s\n' "${relative}" >&2
    exit 1
  }
done
grep -Fq '"id":"old-state"' "${state_file}"
[[ "$(sed -n '1p' "${work}/systemctl.log")" == "stop" ]]
[[ "$(sed -n '2p' "${work}/systemctl.log")" == "start" ]]
[[ "$(sed -n '3p' "${work}/systemctl.log")" == "is-active" ]]
[[ "$(tail -n 2 "${work}/systemctl.log" | sed -n '1p')" == "stop" ]]
[[ "$(tail -n 1 "${work}/systemctl.log")" == "start" ]]

: >"${work}/systemctl.log"
set +e
run_installer 0 1 >"${work}/signal.stdout" 2>"${work}/signal.stderr"
signal_status=$?
set -e
[[ "${signal_status}" -eq 143 ]] || {
  printf 'signal interruption returned %s, expected 143\n' "${signal_status}" >&2
  exit 1
}
for relative in "${runtime_files[@]}"; do
  [[ "$(<"${target}/${relative}")" == "old:${relative}" ]] || {
    printf 'signal rollback mismatch: %s\n' "${relative}" >&2
    exit 1
  }
done
grep -Fq '"id":"old-state"' "${state_file}"
[[ "$(sed -n '1p' "${work}/systemctl.log")" == "stop" ]]
[[ "$(sed -n '2p' "${work}/systemctl.log")" == "start" ]]
[[ "$(sed -n '3p' "${work}/systemctl.log")" == "stop" ]]
[[ "$(sed -n '4p' "${work}/systemctl.log")" == "start" ]]

: >"${work}/systemctl.log"
run_installer 0 0 >"${work}/success.stdout" 2>"${work}/success.stderr" || {
  cat "${work}/success.stdout" >&2
  cat "${work}/success.stderr" >&2
  exit 1
}
for relative in "${runtime_files[@]}"; do
  cmp -- "${stage}/${relative}" "${target}/${relative}"
done
grep -Fq "service=active" "${work}/success.stdout"
[[ "$(sed -n '1p' "${work}/systemctl.log")" == "stop" ]]
[[ "$(sed -n '2p' "${work}/systemctl.log")" == "start" ]]
[[ "$(sed -n '3p' "${work}/systemctl.log")" == "is-active" ]]
printf '%s\n' "public installer transaction test passed"
