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
install -d -m 0700 "${stage}" "${target}" "${backup}" "${fake_bin}"
printf '%s' "connector-test-token" >"${token_file}"
for relative in "${runtime_files[@]}"; do
  install -D -m 0644 "${repo}/${relative}" "${stage}/${relative}"
  install -D -m 0644 /dev/null "${target}/${relative}"
  printf 'old:%s\n' "${relative}" >"${target}/${relative}"
done

cat >"${fake_bin}/systemctl" <<'SH'
#!/usr/bin/env bash
case "${1:-}" in
  restart|is-active) exit 0 ;;
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
    HERMES_AGENT_ROOT="${target}" \
    HERMES_RUNTIME_PYTHON="$(command -v python3)" \
    HERMES_AGENT_SERVICE="hermes-agent-test.service" \
    HERMES_AGENT_USER="root" \
    HERMES_AGENT_GROUP="root" \
    HERMES_STAGE_OWNER="root" \
    HERMES_BACKUP_ROOT="${backup}" \
    HERMES_COLLABORATION_CONNECTOR_TOKEN_FILE="${token_file}" \
    /bin/bash "${installer}" "${version}" "${stage}"
}

set +e
run_installer 1 >"${work}/failure.stdout" 2>"${work}/failure.stderr"
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

run_installer 0 >"${work}/success.stdout" 2>"${work}/success.stderr" || {
  cat "${work}/success.stdout" >&2
  cat "${work}/success.stderr" >&2
  exit 1
}
for relative in "${runtime_files[@]}"; do
  cmp -- "${stage}/${relative}" "${target}/${relative}"
done
grep -Fq "service=active" "${work}/success.stdout"
printf '%s\n' "public installer transaction test passed"
