#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() { printf 'verify-fabric-release: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root on the public authority"

expected_commit="${1:-}"
expected_version="${2:-}"
[[ "${expected_commit}" =~ ^[0-9a-f]{40}$ ]] || die "expected commit is invalid"
[[ "${expected_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "expected version is invalid"
token_file="${HERMES_MANAGED_INSTALLATION_TOKEN_FILE:-/etc/hermes-agent/managed-installation-token}"
[[ -f "${token_file}" && ! -L "${token_file}" ]] || die "managed installation credential is missing or unsafe"
attempts="${HERMES_FABRIC_VERIFY_ATTEMPTS:-300}"
[[ "${attempts}" =~ ^[1-9][0-9]*$ ]] || die "verification attempts must be a positive integer"

curl_config="$(mktemp /run/hermes-fabric-verify-curl.XXXXXX)"
response="$(mktemp /run/hermes-fabric-verify-response.XXXXXX)"
cleanup() { rm -f -- "${curl_config}" "${response}"; }
trap cleanup EXIT
printf 'header = "X-DBB3-Token: %s"\nheader = "Accept: application/json"\n' \
  "$(cat -- "${token_file}")" >"${curl_config}"
chmod 0600 "${curl_config}"

for node in dbb3 wsl; do
  verified=0
  last_state="unreachable"
  for _ in $(seq 1 "${attempts}"); do
    if curl --fail --silent --show-error --max-time 5 \
        --noproxy '*' \
        --resolve 'daxueshenmai.top:443:127.0.0.1' \
        --config "${curl_config}" \
        "https://daxueshenmai.top/_hermes/installations/${node}/health" \
        >"${response}" 2>/dev/null; then
      if last_state="$(python3 - "${response}" "${node}" \
          "${expected_commit}" "${expected_version}" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    print("invalid-json")
    raise SystemExit(1)
release = data.get("release") if isinstance(data, dict) else None
state = {
    "ok": data.get("ok") if isinstance(data, dict) else None,
    "node_id": data.get("node_id") if isinstance(data, dict) else None,
    "commit": release.get("commit") if isinstance(release, dict) else None,
    "version": release.get("version") if isinstance(release, dict) else None,
}
if state == {
    "ok": True,
    "node_id": sys.argv[2],
    "commit": sys.argv[3],
    "version": sys.argv[4],
}:
    print("verified")
    raise SystemExit(0)
print("identity-mismatch")
raise SystemExit(1)
PY
      )"; then
        verified=1
        break
      fi
    fi
    sleep 5
  done
  [[ "${verified}" == 1 ]] \
    || die "node ${node} did not converge (state=${last_state})"
  printf 'node=%s commit=%s version=%s state=verified\n' \
    "${node}" "${expected_commit}" "${expected_version}"
done
