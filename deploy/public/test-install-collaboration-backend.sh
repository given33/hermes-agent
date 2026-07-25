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
runtime_python="${repo}/venv/bin/python"
[[ -x "${runtime_python}" ]] || runtime_python="$(command -v python3)"
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
  "hermes_cli/dashboard_auth/public_paths.py"
  "hermes_cli/dashboard_auth/token_auth.py"
  "hermes_cli/dashboard_auth/mobile_device_store.py"
  "hermes_cli/dashboard_auth/mobile_notifications.py"
  "hermes_cli/managed_installations.py"
  "hermes_cli/managed_nodes.py"
  "hermes_cli/web_server.py"
  "tools/managed_installation_tool.py"
  "toolsets.py"
  "agent/agent_init.py"
  "agent/prompt_builder.py"
  "agent/system_prompt.py"
  "agent/context_diagnostics.py"
  "hermes_cli/doctor.py"
  "tui_gateway/server.py"
)
nginx_files=(
  "deploy/public/nginx-00-hermes-security.conf"
  "deploy/public/nginx-daxueshenmai.top.conf"
)
managed_nodes_template="deploy/public/managed-nodes.server.json"

target="${work}/target"
backup="${work}/backups"
fake_bin="${work}/bin"
token_file="${work}/connector.token"
status_token_file="${work}/dbb3-status.token"
installation_token_file="${work}/managed-installation.token"
state_file="${work}/state/single.json"
runtime_home="${work}/hermes-home"
managed_installations_db="${runtime_home}/managed-installations.db"
managed_nodes_file="${runtime_home}/managed-nodes.json"
nginx_dir="${work}/nginx"
nginx_security_target="${nginx_dir}/00-hermes-security.conf"
nginx_site_target="${nginx_dir}/daxueshenmai.top.conf"
install -d -m 0700 \
  "${stage}" "${target}" "${backup}" "${fake_bin}" "${nginx_dir}" "${runtime_home}"
install -d -m 0700 "$(dirname "${state_file}")"
printf '%s' "connector-test-token" >"${token_file}"
printf '%s\n' "status-test-token-00000000000000000001" >"${status_token_file}"
printf '%s\n' "installation-test-token-000000000000001" >"${installation_token_file}"
chmod 0640 "${status_token_file}" "${installation_token_file}"
printf '%s\n' '{"conversations":[{"id":"old-state"}]}' >"${state_file}"
printf '%s\n' '{"nodes":[]}' >"${managed_nodes_file}"
assert_old_state() {
  python3 - "$1" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["conversations"][0]["id"] == "old-state"
PY
}
python3 - "${managed_installations_db}" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as database:
    database.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    database.execute("INSERT INTO marker VALUES ('old-managed-installation-state')")
PY
for relative in "${runtime_files[@]}"; do
  install -D -m 0644 "${repo}/${relative}" "${stage}/${relative}"
  install -D -m 0644 /dev/null "${target}/${relative}"
  printf 'old:%s\n' "${relative}" >"${target}/${relative}"
done
for relative in "${nginx_files[@]}"; do
  install -D -m 0644 "${repo}/${relative}" "${stage}/${relative}"
done
install -D -m 0644 \
  "${repo}/${managed_nodes_template}" "${stage}/${managed_nodes_template}"
printf '%s\n' "old:nginx-security" >"${nginx_security_target}"
printf '%s\n' "old:nginx-site" >"${nginx_site_target}"

cat >"${fake_bin}/systemctl" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" != "show" ]]; then
  printf '%s\n' "${1:-}" >>"${FAKE_SYSTEMCTL_LOG}"
fi
if [[ "${1:-}" == "start" && "${FAKE_STATUS_FAIL:-0}" == 1 \
  && ! -e "${HERMES_COLLABORATION_STATE_FILE}.mutated" ]]; then
  printf '%s\n' '{"conversations":[{"id":"new-state"}]}' >"${HERMES_COLLABORATION_STATE_FILE}"
  printf '%s\n' 'not a sqlite database' >"${HERMES_HOME_DIR}/managed-installations.db"
  : >"${HERMES_COLLABORATION_STATE_FILE}.mutated"
fi
if [[ "${1:-}" == "start" && "${FAKE_SIGNAL_ON_START:-0}" == 1 \
  && ! -e "${HERMES_COLLABORATION_STATE_FILE}.signaled" ]]; then
  printf '%s\n' '{"conversations":[{"id":"signal-state"}]}' >"${HERMES_COLLABORATION_STATE_FILE}"
  : >"${HERMES_COLLABORATION_STATE_FILE}.signaled"
  kill -TERM "${PPID}"
fi
case "${1:-}" in
  show)
    printf '%s\n' "${FAKE_SYSTEMD_ENVIRONMENT:-}"
    exit 0
    ;;
  stop|start|is-active|reload) exit 0 ;;
  *) exit 0 ;;
esac
SH
cat >"${fake_bin}/nginx" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FAKE_NGINX_LOG}"
[[ "${FAKE_NGINX_FAIL:-0}" != 1 ]]
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
data_file=""
next_is_data=0
for arg in "$@"; do
  if [[ "${next_is_output}" == 1 ]]; then
    output="${arg}"
    next_is_output=0
  elif [[ "${arg}" == "-o" ]]; then
    next_is_output=1
  elif [[ "${next_is_data}" == 1 ]]; then
    data_file="${arg#@}"
    next_is_data=0
  elif [[ "${arg}" == "--data-binary" ]]; then
    next_is_data=1
  fi
done
url="${!#}"
if [[ "${url}" == */api/status ]]; then
  [[ "${FAKE_STATUS_FAIL:-0}" != 1 ]] || exit 22
  payload='{"status":"ok"}'
elif [[ "${url}" == */api/mobile/v1/handshake ]]; then
  [[ "${FAKE_HANDSHAKE_FAIL:-0}" != 1 ]] || exit 22
  payload='{"api_version":1,"hermes_version":"test","profiles":[],"capabilities":[],"server_time":"2026-07-19T12:00:00Z"}'
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
elif [[ "${url}" == */_hermes/installations/dbb3/health ]]; then
  payload='{"ok":true,"node_id":"dbb3","installations":true,"recovery":false}'
elif [[ "${url}" == */_hermes/installations/wsl/health ]]; then
  payload='{"ok":true,"node_id":"wsl","installations":true,"recovery":false}'
elif [[ "${url}" =~ /_hermes/installations/(dbb3|wsl)$ ]]; then
  node="${BASH_REMATCH[1]}"
  probe_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "${data_file}")"
  payload="{\"accepted\":true,\"id\":\"${probe_id}\",\"state\":\"completed\",\"node_id\":\"${node}\"}"
elif [[ "${url}" =~ /_hermes/installations/(dbb3|wsl)/(mi-[0-9a-f]+)$ ]]; then
  node="${BASH_REMATCH[1]}"
  probe_id="${BASH_REMATCH[2]}"
  payload="{\"id\":\"${probe_id}\",\"node_id\":\"${node}\",\"state\":\"completed\",\"detail\":{\"probe\":true,\"persisted\":true}}"
else
  payload='{"ok":true,"contract_version":2,"connector_id":"dbb3-primary","capabilities":["artifact-upload","attachment-download"]}'
fi
if [[ -n "${output}" ]]; then
  printf '%s\n' "${payload}" >"${output}"
else
  printf '%s\n' "${payload}"
fi
SH
chmod 0755 "${fake_bin}/systemctl" "${fake_bin}/nginx" "${fake_bin}/sleep" "${fake_bin}/curl"

run_installer() {
  env \
    PATH="${fake_bin}:${PATH}" \
    FAKE_STATUS_FAIL="$1" \
    FAKE_SIGNAL_ON_START="${2:-0}" \
    FAKE_HANDSHAKE_FAIL="${3:-0}" \
    FAKE_NGINX_FAIL="${4:-0}" \
    HERMES_AGENT_ROOT="${target}" \
    HERMES_RUNTIME_PYTHON="${runtime_python}" \
    HERMES_AGENT_SERVICE="hermes-agent-test.service" \
    HERMES_AGENT_USER="root" \
    HERMES_AGENT_GROUP="root" \
    HERMES_STAGE_OWNER="root" \
    HERMES_BACKUP_ROOT="${backup}" \
    HERMES_INSTALL_LOCK_FILE="${work}/collaboration-install.lock" \
    HERMES_COLLABORATION_STATE_FILE="${state_file}" \
    HERMES_HOME_DIR="${runtime_home}" \
    HERMES_COLLABORATION_CONNECTOR_TOKEN_FILE="${token_file}" \
    HERMES_MANAGED_NODE_TOKEN_FILE="${status_token_file}" \
    HERMES_MANAGED_INSTALLATION_TOKEN_FILE="${installation_token_file}" \
    HERMES_NGINX_SECURITY_TARGET="${nginx_security_target}" \
    HERMES_NGINX_SITE_TARGET="${nginx_site_target}" \
    HERMES_NGINX_SERVICE="nginx-test.service" \
    HERMES_NGINX_BINARY="${fake_bin}/nginx" \
    FAKE_SYSTEMCTL_LOG="${work}/systemctl.log" \
    FAKE_NGINX_LOG="${work}/nginx.log" \
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
[[ "$(<"${nginx_security_target}")" == "old:nginx-security" ]]
[[ "$(<"${nginx_site_target}")" == "old:nginx-site" ]]
assert_old_state "${state_file}"
grep -Fq '"nodes":[]' "${managed_nodes_file}"
[[ "$(python3 - "${managed_installations_db}" <<'PY'
import sqlite3
import sys
with sqlite3.connect(sys.argv[1]) as database:
    print(database.execute("SELECT value FROM marker").fetchone()[0])
PY
)" == "old-managed-installation-state" ]]
[[ "$(sed -n '1p' "${work}/systemctl.log")" == "stop" ]]
[[ "$(sed -n '2p' "${work}/systemctl.log")" == "start" ]]
[[ "$(sed -n '3p' "${work}/systemctl.log")" == "is-active" ]]
[[ "$(tail -n 2 "${work}/systemctl.log" | sed -n '1p')" == "stop" ]]
[[ "$(tail -n 1 "${work}/systemctl.log")" == "start" ]]

: >"${work}/systemctl.log"
: >"${work}/nginx.log"
set +e
run_installer 0 0 0 1 >"${work}/nginx-failure.stdout" 2>"${work}/nginx-failure.stderr"
nginx_failure_status=$?
set -e
[[ "${nginx_failure_status}" -ne 0 ]] || {
  printf '%s\n' "forced nginx validation failure unexpectedly succeeded" >&2
  exit 1
}
grep -Fq "nginx configuration validation failed" "${work}/nginx-failure.stderr"
[[ "$(<"${nginx_security_target}")" == "old:nginx-security" ]]
[[ "$(<"${nginx_site_target}")" == "old:nginx-site" ]]
for relative in "${runtime_files[@]}"; do
  [[ "$(<"${target}/${relative}")" == "old:${relative}" ]]
done
[[ "$(sed -n '1p' "${work}/systemctl.log")" == "stop" ]]
[[ "$(sed -n '2p' "${work}/systemctl.log")" == "stop" ]]
[[ "$(tail -n 1 "${work}/systemctl.log")" == "start" ]]

: >"${work}/systemctl.log"
set +e
run_installer 0 0 1 >"${work}/handshake.stdout" 2>"${work}/handshake.stderr"
handshake_status=$?
set -e
[[ "${handshake_status}" -ne 0 ]] || {
  printf '%s\n' "forced mobile handshake failure unexpectedly succeeded" >&2
  exit 1
}
grep -Fq "anonymous mobile handshake did not respond" "${work}/handshake.stderr"
for relative in "${runtime_files[@]}"; do
  [[ "$(<"${target}/${relative}")" == "old:${relative}" ]] || {
    printf 'handshake rollback mismatch: %s\n' "${relative}" >&2
    exit 1
  }
done
assert_old_state "${state_file}"
grep -Fq '"nodes":[]' "${managed_nodes_file}"

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
assert_old_state "${state_file}"
grep -Fq '"nodes":[]' "${managed_nodes_file}"
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
"${runtime_python}" - "${target}" "${work}" <<'PY'
import ast
from pathlib import Path
import sys

target = Path(sys.argv[1]).resolve()
scratch = Path(sys.argv[2]).resolve()

expected_symbols = {
    "agent/prompt_builder.py": {"EVIDENCE_FIRST_EXECUTION_GUIDANCE"},
    "agent/system_prompt.py": {"build_system_prompt"},
    "agent/context_diagnostics.py": {"analyze_context_sources"},
    "hermes_cli/doctor.py": {"_check_context_engineering"},
}
for relative, symbols in expected_symbols.items():
    installed = target / relative
    source = installed.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(installed))
    compile(tree, str(installed), "exec")
    declared = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    declared.update(
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        if isinstance(target, ast.Name)
    )
    assert symbols <= declared, (relative, symbols - declared)

prompt_source = (target / "agent/prompt_builder.py").read_text(encoding="utf-8")
assert "# Evidence-first execution" in prompt_source
assert scratch.is_dir()
PY
cmp -- "${stage}/deploy/public/nginx-00-hermes-security.conf" "${nginx_security_target}"
cmp -- "${stage}/deploy/public/nginx-daxueshenmai.top.conf" "${nginx_site_target}"
python3 - \
  "${managed_nodes_file}" "${status_token_file}" "${installation_token_file}" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
node = payload["nodes"][0]
assert node["token_file"] == sys.argv[2]
assert node["installation_token_file"] == sys.argv[3]
assert sorted(node["installation_urls"]) == ["dbb3", "wsl"]
PY
[[ "$(python3 - "${managed_installations_db}" <<'PY'
import sqlite3
import sys
with sqlite3.connect(sys.argv[1]) as database:
    print(database.execute("SELECT value FROM marker").fetchone()[0])
PY
)" == "old-managed-installation-state" ]]
grep -Fq "service=active" "${work}/success.stdout"
[[ "$(sed -n '1p' "${work}/systemctl.log")" == "stop" ]]
[[ "$(sed -n '2p' "${work}/systemctl.log")" == "start" ]]
[[ "$(sed -n '3p' "${work}/systemctl.log")" == "is-active" ]]
[[ "$(tail -n 1 "${work}/systemctl.log")" == "reload" ]]
printf '%s\n' "public installer transaction test passed"
