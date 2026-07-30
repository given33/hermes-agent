#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() { printf 'update-fabric-node: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

role="${HERMES_FABRIC_ROLE:-${1:-}}"
case "${role}" in dbb3|wsl) ;; *) die "role must be dbb3 or wsl" ;; esac
repository_url="${HERMES_FABRIC_REPOSITORY:-https://github.com/given33/hermes-agent.git}"
[[ "${repository_url}" == "https://github.com/given33/hermes-agent.git" ]] \
  || die "repository URL is not approved"
cloud_url="${HERMES_CLOUD_URL:-https://daxueshenmai.top/api/plugins/collaboration}"
case "${cloud_url}" in https://daxueshenmai.top/api/plugins/collaboration) ;; *) die "cloud URL is not approved" ;; esac

state_root="${HERMES_FABRIC_STATE_ROOT:-/var/lib/hermes-agent-fabric-update/${role}}"
mirror="${state_root}/repository.git"
deployed_file="${state_root}/deployed-commit"
lock_file="${state_root}/update.lock"
install -d -o root -g root -m 0700 "${state_root}"
exec 8>"${lock_file}"
chmod 0600 "${lock_file}"
flock -n 8 || exit 0

case "${role}" in
  dbb3)
    token_file="${HERMES_CLOUD_TOKEN_FILE:-/etc/dbb3-team/cloud_connector_token}"
    connector_id="${DBB3_CONNECTOR_ID:-dbb3-primary}"
    ;;
  wsl)
    token_file="${HERMES_CLOUD_TOKEN_FILE:-/etc/pc-team/cloud_connector_token}"
    connector_id="${DBB3_CONNECTOR_ID:-pc-primary}"
    ;;
esac
[[ -f "${token_file}" && ! -L "${token_file}" ]] || die "connector token is missing or unsafe"

evidence_file="$(mktemp /run/hermes-fabric-evidence.XXXXXX)"
curl_config="$(mktemp /run/hermes-fabric-curl.XXXXXX)"
stage="$(mktemp -d "/run/hermes-fabric-${role}.XXXXXX")"
automation_script_temp="/usr/local/lib/hermes-agent/update-fabric-node.sh.new.$$"
automation_service_temp="/etc/systemd/system/hermes-fabric-update.service.new.$$"
automation_timer_temp="/etc/systemd/system/hermes-fabric-update.timer.new.$$"
cleanup() {
  rm -rf -- "${stage}"
  rm -f -- "${evidence_file}" "${curl_config}" \
    "${automation_script_temp}" "${automation_service_temp}" \
    "${automation_timer_temp}"
}
trap cleanup EXIT
connector_token="$(cat -- "${token_file}")"
(( ${#connector_token} >= 32 && ${#connector_token} <= 4096 )) \
  || die "connector token length is invalid"
[[ "${connector_token}" =~ ^[A-Za-z0-9._~+/-]+={0,3}$ ]] \
  || die "connector token contains unsupported characters"
printf 'header = "Authorization: Bearer %s"\nheader = "X-Connector-ID: %s"\nheader = "Accept: application/json"\n' \
  "${connector_token}" "${connector_id}" >"${curl_config}"
chmod 0600 "${curl_config}"
unset connector_token
curl --fail --silent --show-error --max-time 15 --noproxy '*' \
  --config "${curl_config}" \
  -o "${evidence_file}" "${cloud_url}/connector/deployment-health"
release_commit="$(python3 - "${evidence_file}" <<'PY'
import json
import re
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload.get("ok") is True
commit = str((payload.get("release") or {}).get("commit") or "")
assert re.fullmatch(r"[0-9a-f]{40}", commit)
print(commit)
PY
)"
if [[ -f "${deployed_file}" && "$(cat -- "${deployed_file}")" == "${release_commit}" ]]; then
  printf 'role=%s\ncommit=%s\nstate=current\n' "${role}" "${release_commit}"
  exit 0
fi

if [[ ! -d "${mirror}" ]]; then
  temporary_mirror="${state_root}/repository.git.new.$$"
  rm -rf -- "${temporary_mirror}"
  git clone --mirror -- "${repository_url}" "${temporary_mirror}"
  mv -f -- "${temporary_mirror}" "${mirror}"
fi
[[ -d "${mirror}" && ! -L "${mirror}" ]] || die "repository mirror is unsafe"
git --git-dir="${mirror}" remote set-url origin "${repository_url}"
git --git-dir="${mirror}" fetch --force --prune origin \
  "+refs/heads/main:refs/remotes/origin/main"
git --git-dir="${mirror}" cat-file -e "${release_commit}^{commit}"
git --git-dir="${mirror}" merge-base --is-ancestor \
  "${release_commit}" refs/remotes/origin/main \
  || die "committed release is not part of the approved main branch"
archive_paths=(
  "deploy/automation/update-fabric-node.sh"
  "deploy/automation/hermes-fabric-update.service"
  "deploy/automation/hermes-fabric-update.timer"
  "deploy/dbb3/install-dbb3-cloud-connector-user.sh"
  "deploy/dbb3/dbb3_cloud_connector.py"
  "deploy/dbb3/dbb3-cloud-connector.service"
)
if [[ "${role}" == wsl ]]; then
  archive_paths+=(
    "deploy/pc/install-pc-cloud-connector-user.sh"
    "deploy/pc/pc-cloud-connector.service"
  )
fi
git --git-dir="${mirror}" archive --format=tar "${release_commit}" \
  -- "${archive_paths[@]}" | tar -xf - -C "${stage}"
# The connector installer deliberately drops to the service account for its
# preflight. The verified public Git archive contains no node credentials, so
# expose the ephemeral snapshot read-only after ancestry validation while
# keeping every entry root-owned and non-writable by the service account.
chmod -R a+rX "${stage}"

automation_assets=(
  "deploy/automation/update-fabric-node.sh"
  "deploy/automation/hermes-fabric-update.service"
  "deploy/automation/hermes-fabric-update.timer"
)
for relative in "${automation_assets[@]}"; do
  [[ -f "${stage}/${relative}" && ! -L "${stage}/${relative}" ]] \
    || die "missing or unsafe automation asset ${relative}"
done

ensure_user_units_active() {
  local service_user="${HERMES_FABRIC_SERVICE_USER:-hermes}"
  local uid runtime unit
  uid="$(id -u "${service_user}")"
  runtime="/run/user/${uid}"
  systemctl start "user@${uid}.service"
  runuser -u "${service_user}" -- env \
    XDG_RUNTIME_DIR="${runtime}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=${runtime}/bus" \
    systemctl --user enable --now "$@"
  for unit in "$@"; do
    runuser -u "${service_user}" -- env \
      XDG_RUNTIME_DIR="${runtime}" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=${runtime}/bus" \
      systemctl --user is-active --quiet "${unit}" \
      || die "required ${role} user service is not active: ${unit}"
  done
}

case "${role}" in
  dbb3)
    bash "${stage}/deploy/dbb3/install-dbb3-cloud-connector-user.sh" \
      "${stage}/deploy/dbb3/dbb3_cloud_connector.py"
    systemctl enable --now hermes-managed-installation-receiver.service
    systemctl is-active --quiet hermes-managed-installation-receiver.service \
      || die "DBB3 managed installation receiver is not active"
    ;;
  wsl)
    bash "${stage}/deploy/pc/install-pc-cloud-connector-user.sh"
    ensure_user_units_active \
      hermes-wsl-managed-installation-receiver.service \
      hermes-wsl-managed-installation-tunnel.service
    ;;
esac

# A fabric release also advances the updater itself. Install only the three
# root-owned automation assets from the ancestry-verified snapshot; the
# running shell keeps its open script while the next timer invocation uses the
# new implementation.
install -o root -g root -m 0755 \
  "${stage}/deploy/automation/update-fabric-node.sh" \
  "${automation_script_temp}"
install -o root -g root -m 0644 \
  "${stage}/deploy/automation/hermes-fabric-update.service" \
  "${automation_service_temp}"
install -o root -g root -m 0644 \
  "${stage}/deploy/automation/hermes-fabric-update.timer" \
  "${automation_timer_temp}"
mv -f -- "${automation_script_temp}" \
  /usr/local/lib/hermes-agent/update-fabric-node.sh
mv -f -- "${automation_service_temp}" \
  /etc/systemd/system/hermes-fabric-update.service
mv -f -- "${automation_timer_temp}" \
  /etc/systemd/system/hermes-fabric-update.timer
systemctl daemon-reload
systemctl enable --now hermes-fabric-update.timer

printf '%s\n' "${release_commit}" >"${deployed_file}.new.$$"
chmod 0600 "${deployed_file}.new.$$"
mv -f -- "${deployed_file}.new.$$" "${deployed_file}"
printf 'role=%s\ncommit=%s\nstate=updated\n' "${role}" "${release_commit}"
