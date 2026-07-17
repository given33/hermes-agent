#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() { printf 'configure-connector-credential: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

connector_id="${1:-}"
token_source="${2:-}"
env_file="${HERMES_AGENT_ENV_FILE:-/etc/hermes-agent/hermes-agent.env}"
map_file="${HERMES_CONNECTOR_TOKENS_FILE:-/etc/hermes-agent/collaboration-connector-tokens.json}"
service="${HERMES_AGENT_SERVICE:-hermes-agent.service}"
service_group="${HERMES_AGENT_GROUP:-hermes-agent}"
health_url="${HERMES_CONNECTOR_HEALTH_URL:-http://127.0.0.2:9119/api/plugins/collaboration/connector/health}"

[[ "${connector_id}" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]] || die "invalid connector id"
[[ -f "${token_source}" && ! -L "${token_source}" ]] || die "token source is missing or unsafe"
[[ -f "${env_file}" && ! -L "${env_file}" ]] || die "service environment file is missing or unsafe"
getent group "${service_group}" >/dev/null || die "service group does not exist"

legacy_file="$(sed -n 's/^HERMES_COLLABORATION_CONNECTOR_TOKEN_FILE=//p' "${env_file}" | tail -n 1)"
legacy_file="${legacy_file#\"}"; legacy_file="${legacy_file%\"}"
legacy_file="${legacy_file#\'}"; legacy_file="${legacy_file%\'}"
[[ -f "${legacy_file}" && ! -L "${legacy_file}" ]] || die "legacy DBB3 credential is missing or unsafe"
[[ ! -L "${map_file}" ]] || die "credential map must not be a symlink"

stamp="$(date +%Y%m%d-%H%M%S)"
backup="/var/backups/hermes-agent/connector-credentials-${stamp}-$$"
install -d -o root -g root -m 0700 "${backup}"
cp -a -- "${env_file}" "${backup}/hermes-agent.env"
if [[ -f "${map_file}" ]]; then
  cp -a -- "${map_file}" "${backup}/connector-tokens.json"
else
  : >"${backup}/connector-tokens.json.missing"
fi

map_tmp="${map_file}.new.$$"
env_tmp="${env_file}.new.$$"
committed=0
rollback() {
  local result=$?
  trap - EXIT
  rm -f -- "${map_tmp}" "${env_tmp}"
  if (( ! committed )); then
    install -o root -g "${service_group}" -m 0640 \
      "${backup}/hermes-agent.env" "${env_file}"
    if [[ -f "${backup}/connector-tokens.json" ]]; then
      install -o root -g "${service_group}" -m 0640 \
        "${backup}/connector-tokens.json" "${map_file}"
    else
      rm -f -- "${map_file}"
    fi
    systemctl restart "${service}" >/dev/null 2>&1 || true
  fi
  exit "${result}"
}
trap rollback EXIT

python3 - "${legacy_file}" "${token_source}" "${map_file}" "${connector_id}" "${map_tmp}" <<'PY'
import json
import os
from pathlib import Path
import re
import sys

legacy_path = Path(sys.argv[1])
token_path = Path(sys.argv[2])
current_path = Path(sys.argv[3])
connector_id = sys.argv[4]
output_path = Path(sys.argv[5])

def read_token(path: Path) -> str:
    token = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{32,512}", token):
        raise SystemExit(f"invalid connector credential in {path}")
    return token

tokens = {}
if current_path.exists():
    parsed = json.loads(current_path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise SystemExit("connector credential map must be a JSON object")
    tokens.update({str(key): str(value) for key, value in parsed.items()})
tokens["dbb3-primary"] = read_token(legacy_path)
tokens[connector_id] = read_token(token_path)
if len(set(tokens.values())) != len(tokens):
    raise SystemExit("each connector must have a distinct credential")

target = output_path
target.parent.mkdir(parents=True, exist_ok=True)
with target.open("x", encoding="utf-8", newline="\n") as handle:
    os.chmod(target, 0o600)
    json.dump(tokens, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
PY
chown "root:${service_group}" "${map_tmp}"
chmod 0640 "${map_tmp}"

sed '/^HERMES_COLLABORATION_CONNECTOR_TOKENS_FILE=/d' "${env_file}" >"${env_tmp}"
printf 'HERMES_COLLABORATION_CONNECTOR_TOKENS_FILE=%s\n' "${map_file}" >>"${env_tmp}"
chown "root:${service_group}" "${env_tmp}"
chmod 0640 "${env_tmp}"

mv -f -- "${map_tmp}" "${map_file}"
mv -f -- "${env_tmp}" "${env_file}"
systemctl restart "${service}"

healthy=0
for _ in $(seq 1 30); do
  if systemctl is-active --quiet "${service}" \
    && python3 - "${map_file}" "${health_url}" 2>/dev/null <<'PY'
import json
from pathlib import Path
import sys
import urllib.request

tokens = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for connector_id, token in tokens.items():
    request = urllib.request.Request(
        sys.argv[2],
        headers={
            "Authorization": f"Bearer {token}",
            "X-Connector-ID": connector_id,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        data = json.load(response)
    assert data.get("ok") is True
    assert data.get("connector_id") == connector_id
PY
  then
    healthy=1
    break
  fi
  sleep 1
done
[[ "${healthy}" == 1 ]] || die "service did not accept every bound connector credential"

committed=1
printf 'connector=%s\ncredential_map=configured\nbackup=%s\n' \
  "${connector_id}" "${backup}"
