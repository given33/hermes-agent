#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() { printf 'update-fabric-node: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

# Resolve the nearest existing ancestor before creating anything below a
# caller-controlled path.  A leaf-only check is insufficient because a
# symlinked ancestor can redirect root-owned deployment files elsewhere.
assert_canonical_path() {
  local path="$1" label="${2:-path}" probe parent resolved
  [[ "${path}" == /* ]] || die "${label} must be absolute"
  probe="${path}"
  while [[ ! -e "${probe}" && ! -L "${probe}" ]]; do
    parent="$(dirname -- "${probe}")"
    [[ "${parent}" != "${probe}" ]] || die "${label} has no existing ancestor"
    probe="${parent}"
  done
  [[ ! -L "${probe}" ]] || die "${label} has a symlink ancestor"
  resolved="$(realpath -e -- "${probe}")" \
    || die "${label} ancestor cannot be resolved"
  [[ "${resolved}" == "${probe}" ]] \
    || die "${label} ancestor resolves outside its lexical path"
}

role="${HERMES_FABRIC_ROLE:-${1:-}}"
case "${role}" in dbb3|wsl|hk) ;; *) die "role must be dbb3, wsl, or hk" ;; esac
repository_url="${HERMES_FABRIC_REPOSITORY:-https://github.com/given33/hermes-agent.git}"
[[ "${repository_url}" == "https://github.com/given33/hermes-agent.git" ]] \
  || die "repository URL is not approved"
cloud_url="${HERMES_CLOUD_URL:-https://daxueshenmai.top/api/plugins/collaboration}"
[[ "${cloud_url}" == "https://daxueshenmai.top/api/plugins/collaboration" ]] \
  || die "cloud URL is not approved"
git_network_timeout="${HERMES_FABRIC_GIT_TIMEOUT_SECONDS:-90}"
[[ "${git_network_timeout}" =~ ^[1-9][0-9]*$ ]] \
  || die "Git timeout must be a positive integer"
command -v timeout >/dev/null 2>&1 || die "timeout command is missing"
run_network_git() {
  GIT_TERMINAL_PROMPT=0 timeout --signal=TERM --kill-after=10s \
    "${git_network_timeout}s" git "$@"
}

state_root="${HERMES_FABRIC_STATE_ROOT:-/var/lib/hermes-agent-fabric-update/${role}}"
allow_test_paths="${HERMES_FABRIC_ALLOW_TEST_PATHS:-0}"
if [[ "${allow_test_paths}" != 1 ]]; then
  [[ "${state_root}" == "/var/lib/hermes-agent-fabric-update/${role}" ]] \
    || die "fabric state root override is not allowed"
fi
mirror="${state_root}/repository.git"
deployed_file="${state_root}/deployed-commit"
release_evidence_file="${state_root}/release.json"
lock_file="${state_root}/update.lock"
assert_canonical_path "$(dirname -- "${state_root}")" "fabric state parent"
assert_canonical_path "${state_root}" "fabric state root"
install -d -o root -g root -m 0755 "$(dirname "${state_root}")" "${state_root}"
assert_canonical_path "${state_root}" "fabric state root"
[[ -d "${state_root}" && ! -L "${state_root}" \
    && "$(realpath -e -- "${state_root}")" == "${state_root}" ]] \
  || die "fabric state root is unsafe"
exec 8>"${lock_file}"
chmod 0600 "${lock_file}"
flock -n 8 || exit 0

service_user="${HERMES_FABRIC_SERVICE_USER:-hermes}"
id "${service_user}" >/dev/null 2>&1 || die "fabric service user is missing"
service_uid="$(id -u "${service_user}")"
service_home="$(getent passwd "${service_user}" | cut -d: -f6)"
[[ -n "${service_home}" && -d "${service_home}" ]] \
  || die "fabric service user home is missing"
assert_canonical_path "${service_home}" "fabric service user home"
user_runtime="/run/user/${service_uid}"
user_systemctl() {
  runuser -u "${service_user}" -- env \
    HOME="${service_home}" \
    XDG_RUNTIME_DIR="${user_runtime}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=${user_runtime}/bus" \
    systemctl --user "$@"
}

case "${role}" in
  dbb3)
    token_file="${HERMES_CLOUD_TOKEN_FILE:-/etc/dbb3-team/cloud_connector_token}"
    connector_id="${DBB3_CONNECTOR_ID:-dbb3-primary}"
    worker_node_id="dbb3-worker"
    runtime_root="${HERMES_DBB3_AGENT_ROOT:-/usr/local/lib/hermes-agent}"
    external_hermes_home="${HERMES_DBB3_HOME:-${service_home}/.hermes/profiles/dbb3-worker}"
    worker_profile="dbb3-worker"
    connector_unit="dbb3-cloud-connector.service"
    ;;
  wsl)
    token_file="${HERMES_CLOUD_TOKEN_FILE:-/etc/pc-team/cloud_connector_token}"
    connector_id="${PC_CONNECTOR_ID:-pc-primary}"
    worker_node_id="pc-worker"
    runtime_root="${HERMES_WSL_AGENT_ROOT:-/mnt/d/Hermes/hermes-agent}"
    external_hermes_home="${HERMES_WSL_HOME:-/mnt/d/Hermes/home/profiles/pc-worker}"
    worker_profile="pc-worker"
    connector_unit="pc-cloud-connector.service"
    ;;
  hk)
    token_file="${HERMES_CLOUD_TOKEN_FILE:-/etc/hk-team/cloud_connector_token}"
    connector_id="${HK_CONNECTOR_ID:-hk-primary}"
    worker_node_id="hk-worker"
    runtime_root="${HERMES_HK_AGENT_ROOT:-/opt/hk-team/hermes-agent}"
    external_hermes_home="${HERMES_HK_HOME:-${service_home}/.hermes/profiles/hk-worker}"
    worker_profile="hk-worker"
    connector_unit="hk-cloud-connector.service"
    ;;
esac
if [[ "${allow_test_paths}" != 1 ]]; then
  case "${role}" in
    dbb3)
      [[ "${external_hermes_home}" == "${service_home}/.hermes/profiles/dbb3-worker" ]] \
        || die "DBB3 worker Hermes home must be the isolated dbb3-worker profile"
      ;;
    wsl)
      [[ "${external_hermes_home}" == /mnt/d/Hermes/home/profiles/pc-worker ]] \
        || die "PC worker Hermes home must be the isolated pc-worker profile"
      ;;
    hk)
      [[ "${external_hermes_home}" == "${service_home}/.hermes/profiles/hk-worker" ]] \
        || die "HK worker Hermes home must be the isolated hk-worker profile"
      ;;
  esac
fi
# A worker home is a mutable boundary. Reject a profile path that would
# resolve through a symlink or alias into another role's home before any
# connector/recovery installer can create state beneath it.
[[ "${external_hermes_home}" == /* && ! -L "${external_hermes_home}" ]] \
  || die "worker Hermes home must be an absolute non-symlink path"
external_parent="$(dirname -- "${external_hermes_home}")"
assert_canonical_path "${external_parent}" "worker Hermes home parent"
if [[ ! -e "${external_parent}" ]]; then
  install -d -o "${service_user}" -g "${service_user}" -m 0700 "${external_parent}"
fi
assert_canonical_path "${external_parent}" "worker Hermes home parent"
[[ -d "${external_parent}" && ! -L "${external_parent}" ]] \
  || die "worker Hermes home parent is missing or unsafe"
[[ "$(realpath -e -- "${external_parent}")/${external_hermes_home##*/}" == \
   "${external_hermes_home}" ]] \
  || die "worker Hermes home parent resolves through a symlink"
[[ -f "${token_file}" && ! -L "${token_file}" ]] \
  || die "connector token is missing or unsafe"
assert_canonical_path "${token_file}" "connector token"
if [[ "${allow_test_paths}" != 1 ]]; then
  case "${role}:${runtime_root}" in
    dbb3:/usr/local/lib/hermes-agent|wsl:/mnt/d/Hermes/hermes-agent|hk:/opt/hk-team/hermes-agent) ;;
    *) die "fabric runtime root override is not allowed" ;;
  esac
fi
[[ "${runtime_root}" == /* && -d "${runtime_root}" && ! -L "${runtime_root}" ]] \
  || die "fabric runtime root is unsafe"
assert_canonical_path "${runtime_root}" "fabric runtime root"
[[ "$(realpath -e -- "${runtime_root}")" == "${runtime_root}" ]] \
  || die "fabric runtime root or one of its parents is a symlink"
generation_root="${runtime_root}/.fabric-generations"
current_link="${runtime_root}/.fabric-current"
assert_canonical_path "${generation_root}" "fabric generation root"
install -d -o root -g root -m 0755 "${generation_root}"
assert_canonical_path "${generation_root}" "fabric generation root"
[[ -d "${generation_root}" && ! -L "${generation_root}" ]] \
  || die "fabric generation root is unsafe"

worker_ws_timeout="${HERMES_FABRIC_WORKER_WS_TIMEOUT_SECONDS:-120}"
[[ "${worker_ws_timeout}" =~ ^[1-9][0-9]*$ ]] \
  && (( worker_ws_timeout >= 10 && worker_ws_timeout <= 300 )) \
  || die "worker WebSocket deployment timeout must be between 10 and 300 seconds"

evidence_file="$(mktemp /run/hermes-fabric-evidence.XXXXXX)"
curl_config="$(mktemp /run/hermes-fabric-curl.XXXXXX)"
transaction_root="$(mktemp -d "/run/hermes-fabric-${role}.XXXXXX")"
preflight_root="${state_root}/preflight.$$"
preflight_service_home="${state_root}/service-preflight.$$"
automation_script_target="${HERMES_FABRIC_AUTOMATION_SCRIPT_TARGET:-/usr/local/lib/hermes-agent/update-fabric-node.sh}"
automation_service_target="${HERMES_FABRIC_AUTOMATION_SERVICE_TARGET:-/etc/systemd/system/hermes-fabric-update.service}"
automation_timer_target="${HERMES_FABRIC_AUTOMATION_TIMER_TARGET:-/etc/systemd/system/hermes-fabric-update.timer}"
if [[ "${allow_test_paths}" != 1 ]]; then
  [[ "${automation_script_target}" == /usr/local/lib/hermes-agent/update-fabric-node.sh \
      && "${automation_service_target}" == /etc/systemd/system/hermes-fabric-update.service \
      && "${automation_timer_target}" == /etc/systemd/system/hermes-fabric-update.timer ]] \
    || die "automation target override is not allowed"
fi
automation_script_temp="${automation_script_target}.new.$$"
automation_service_temp="${automation_service_target}.new.$$"
automation_timer_temp="${automation_timer_target}.new.$$"
assert_canonical_path "$(dirname -- "${automation_script_target}")" "fabric automation script directory"
assert_canonical_path "$(dirname -- "${automation_service_target}")" "fabric automation service directory"
assert_canonical_path "$(dirname -- "${automation_timer_target}")" "fabric automation timer directory"
automation_backup="${transaction_root}/automation-backup"
connector_handle="${transaction_root}/connector-rollback-handle"
receiver_handle="${transaction_root}/receiver-rollback-handle"
gateway_backup="${transaction_root}/gateway-backup"
candidate_generation=""
snapshot=""
quarantine_generation=""
generation_created=0
generation_repaired_active=0
current_swapped=0
previous_current_present=0
previous_current_target=""
previous_current_resolved=""
automation_swapped=0
connector_installed=0
receiver_installed=0
gateway_touched=0
gateway_was_active=0
gateway_was_enabled=0
evidence_published=0
deployed_published=0
transaction_committed=0

restart_role_services() {
  systemctl start "user@${service_uid}.service" >/dev/null 2>&1 || true
  case "${role}" in
    dbb3)
      user_systemctl restart dbb3-cloud-connector.service >/dev/null 2>&1 || true
      systemctl restart hermes-managed-installation-receiver.service >/dev/null 2>&1 || true
      ;;
    wsl)
      user_systemctl restart pc-cloud-connector.service \
        hermes-wsl-managed-installation-receiver.service \
        hermes-wsl-managed-installation-tunnel.service >/dev/null 2>&1 || true
      ;;
    hk)
      user_systemctl restart hk-cloud-connector.service \
        hermes-gateway-hk-worker.service >/dev/null 2>&1 || true
      systemctl restart hermes-hk-managed-node-recovery.service \
        hermes-hk-managed-node-recovery-tunnel.service >/dev/null 2>&1 || true
      ;;
  esac
}

restore_gateway() {
  local unit_file="${service_home}/.config/systemd/user/hermes-gateway-hk-worker.service"
  user_systemctl disable --now hermes-gateway-hk-worker.service >/dev/null 2>&1 || true
  if [[ -f "${gateway_backup}/unit.present" ]]; then
    install -o "${service_user}" -g "${service_user}" -m 0644 \
      "${gateway_backup}/unit" "${unit_file}.rollback.$$" \
      && mv -f -- "${unit_file}.rollback.$$" "${unit_file}" \
      || return 1
  elif [[ -f "${gateway_backup}/unit.absent" ]]; then
    rm -f -- "${unit_file}" || return 1
  else
    return 1
  fi
  user_systemctl daemon-reload >/dev/null 2>&1 || return 1
  if (( gateway_was_enabled )); then
    user_systemctl enable hermes-gateway-hk-worker.service >/dev/null 2>&1 || return 1
  fi
  if (( gateway_was_active )); then
    user_systemctl start hermes-gateway-hk-worker.service >/dev/null 2>&1 || return 1
  fi
}

restore_current() {
  local rollback_link="${current_link}.rollback.$$"
  rm -f -- "${rollback_link}"
  if (( previous_current_present )); then
    ln -s -- "${previous_current_target}" "${rollback_link}" \
      && mv -Tf -- "${rollback_link}" "${current_link}"
  else
    rm -f -- "${current_link}"
  fi
}

cleanup() {
  local status=$?
  local rollback_failed=0
  local connector_backup receiver_backup
  trap - EXIT
  set +e
  if (( receiver_installed && ! transaction_committed )); then
    receiver_backup="$(cat -- "${receiver_handle}" 2>/dev/null)"
    case "${role}" in
      dbb3)
        env HERMES_DBB3_AGENT_ROOT="${current_link}" \
          HERMES_DBB3_RUNTIME_PYTHON="${current_link}/.venv/bin/python" \
          HERMES_DBB3_HOME="${external_hermes_home}" \
          bash "${preflight_root}/deploy/recovery/install-dbb3-managed-installation-receiver.sh" \
          "${preflight_root}" "--rollback-backup=${receiver_backup}" \
          || rollback_failed=1
        ;;
      wsl)
        env HERMES_WSL_AGENT_ROOT="${current_link}" \
          HERMES_WSL_RUNTIME_PYTHON="${current_link}/.venv/bin/python" \
          HERMES_WSL_HOME="${external_hermes_home}" \
          bash "${preflight_root}/deploy/recovery/install-wsl-managed-installation.sh" \
          "${preflight_root}" "${wsl_secret_stage}/installation-token" \
          "${wsl_secret_stage}/installation-key" \
          "--rollback-backup=${receiver_backup}" \
          || rollback_failed=1
        ;;
      hk)
        env HERMES_HK_AGENT_ROOT="${current_link}" \
          HERMES_HK_RUNTIME_PYTHON="${current_link}/.venv/bin/python" \
          bash "${preflight_root}/deploy/recovery/install-hk-managed-recovery.sh" \
          "${preflight_root}" "${hk_secret_stage}/recovery-token" \
          "${hk_secret_stage}/recovery-key" \
          "${hk_secret_stage}/recovery-known-hosts" \
          "--rollback-backup=${receiver_backup}" \
          || rollback_failed=1
        ;;
    esac
  fi
  if (( connector_installed && ! transaction_committed )); then
    connector_backup="$(cat -- "${connector_handle}" 2>/dev/null)"
    case "${role}" in
      dbb3)
        env HERMES_CONNECTOR_RUNTIME_PYTHON="${current_link}/.venv/bin/python" \
          HERMES_CONNECTOR_HERMES_HOME="${external_hermes_home}" \
          DBB3_CONNECTOR_ARTIFACT_ROOTS="${external_hermes_home}" \
          bash "${preflight_root}/deploy/dbb3/install-dbb3-cloud-connector-user.sh" \
          "${preflight_root}/deploy/dbb3/dbb3_cloud_connector.py" \
          "--rollback-backup=${connector_backup}" \
          || rollback_failed=1
        ;;
      wsl)
        env HERMES_WSL_AGENT_ROOT="${current_link}" \
          HERMES_WSL_RUNTIME_PYTHON="${current_link}/.venv/bin/python" \
          PC_CONNECTOR_HERMES_HOME="${external_hermes_home}" \
          PC_CONNECTOR_ARTIFACT_ROOTS="${external_hermes_home}" \
          bash "${preflight_root}/deploy/pc/install-pc-cloud-connector-user.sh" \
          "--rollback-backup=${connector_backup}" \
          || rollback_failed=1
        ;;
      hk)
        env HERMES_HK_AGENT_ROOT="${current_link}" \
          HERMES_HK_RUNTIME_PYTHON="${current_link}/.venv/bin/python" \
          HK_CONNECTOR_HERMES_HOME="${external_hermes_home}" \
          HK_CONNECTOR_ARTIFACT_ROOTS="${external_hermes_home}" \
          bash "${preflight_root}/deploy/hk/install-hk-cloud-connector-user.sh" \
          "--rollback-backup=${connector_backup}" \
          || rollback_failed=1
        ;;
    esac
  fi
  if (( gateway_touched && ! transaction_committed )); then
    restore_gateway || rollback_failed=1
  fi
  if (( evidence_published && ! transaction_committed )); then
    if [[ -f "${transaction_root}/previous-release-evidence.present" ]]; then
      install -o root -g root -m 0644 \
        "${transaction_root}/previous-release-evidence.json" \
        "${release_evidence_file}.rollback.$$" \
        && mv -f -- "${release_evidence_file}.rollback.$$" \
          "${release_evidence_file}" \
        || rollback_failed=1
    else
      rm -f -- "${release_evidence_file}" || rollback_failed=1
    fi
  fi
  if (( deployed_published && ! transaction_committed )); then
    if [[ -f "${transaction_root}/previous-deployed-commit.present" ]]; then
      install -o root -g root -m 0600 \
        "${transaction_root}/previous-deployed-commit" \
        "${deployed_file}.rollback.$$" \
        && mv -f -- "${deployed_file}.rollback.$$" "${deployed_file}" \
        || rollback_failed=1
    else
      rm -f -- "${deployed_file}" || rollback_failed=1
    fi
  fi
  if (( automation_swapped && ! transaction_committed )); then
    for target in "${automation_script_target}" "${automation_service_target}" \
      "${automation_timer_target}"; do
      case "${target}" in
        "${automation_script_target}") relative="update-fabric-node.sh" ;;
        "${automation_service_target}") relative="systemd/hermes-fabric-update.service" ;;
        "${automation_timer_target}") relative="systemd/hermes-fabric-update.timer" ;;
      esac
      backup_target="${automation_backup}/${relative}"
      if [[ -f "${backup_target}.present" ]]; then
        rollback_mode=0644
        [[ "${target}" != "${automation_script_target}" ]] || rollback_mode=0755
        install -o root -g root -m "${rollback_mode}" \
          "${backup_target}" "${target}.rollback.$$" \
          && mv -f -- "${target}.rollback.$$" "${target}" \
          || rollback_failed=1
      elif [[ -f "${backup_target}.absent" ]]; then
        rm -f -- "${target}" || rollback_failed=1
      else
        rollback_failed=1
      fi
    done
    systemctl daemon-reload >/dev/null 2>&1 || rollback_failed=1
    systemctl enable --now hermes-fabric-update.timer >/dev/null 2>&1 \
      || rollback_failed=1
  fi
  if (( (current_swapped || generation_repaired_active) \
      && ! transaction_committed )); then
    restore_current || rollback_failed=1
    restart_role_services
  fi
  if (( generation_created && ! generation_repaired_active \
      && ! transaction_committed )); then
    if [[ -n "${snapshot}" && "${snapshot}" == "${generation_root}/"* \
        && -d "${snapshot}" && ! -L "${snapshot}" ]]; then
      rm -rf -- "${snapshot}" || rollback_failed=1
    else
      rollback_failed=1
    fi
  fi
  if [[ -n "${candidate_generation}" \
      && "${candidate_generation}" == "${generation_root}/."* \
      && -d "${candidate_generation}" && ! -L "${candidate_generation}" ]]; then
    rm -rf -- "${candidate_generation}" || rollback_failed=1
  fi
  rm -rf -- "${transaction_root}" "${preflight_root}" \
    "${preflight_service_home}"
  rm -f -- "${evidence_file}" "${curl_config}" \
    "${automation_script_temp}" "${automation_service_temp}" \
    "${automation_timer_temp}" "${release_evidence_temp:-}" \
    "${deployed_file}.new.$$" "${deployed_file}.rollback.$$" \
    "${current_link}.new.$$" "${current_link}.rollback.$$"
  (( rollback_failed == 0 )) || status=70
  exit "${status}"
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
readarray -t release_identity < <(python3 - "${evidence_file}" \
  "${connector_id}" "${worker_node_id}" "${role}" <<'PY'
import json
import re
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload.get("ok") is True
assert str(payload.get("connector_id") or "") == sys.argv[2]
release = payload.get("release") or {}
commit = str(release.get("commit") or "")
version = str(release.get("version") or "")
assert re.fullmatch(r"[0-9a-f]{40}", commit)
assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
worker = payload.get("worker_channel") or {}
generation = str(worker.get("connection_generation") or "")
worker_release = worker.get("release") or {}
worker_current = (
    worker.get("online") is True
    and worker.get("fresh") is True
    and str(worker.get("node_id") or "") == sys.argv[3]
    and str(worker.get("managed_node_id") or "") == sys.argv[4]
    and str(worker_release.get("commit") or "") == commit
    and str(worker_release.get("version") or "") == version
)
print(commit)
print(version)
print(generation)
print("1" if worker_current else "0")
PY
)
release_commit="${release_identity[0]:-}"
release_version="${release_identity[1]:-}"
previous_connection_generation="${release_identity[2]:-}"
initial_worker_current="${release_identity[3]:-0}"
[[ "${release_commit}" =~ ^[0-9a-f]{40}$ ]] || die "release commit is invalid"
[[ "${release_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || die "release version is invalid"

verify_generation() {
  local root="$1"
  local verify_index="${transaction_root}/source-index"
  [[ -d "${root}" && ! -L "${root}" \
      && "$(cat -- "${root}/.hermes-source-commit" 2>/dev/null)" == "${release_commit}" \
      && "$(cat -- "${root}/.hermes-source-tree" 2>/dev/null)" == "${source_tree}" \
      && -f "${root}/pyproject.toml" && ! -L "${root}/pyproject.toml" \
      && -f "${root}/uv.lock" && ! -L "${root}/uv.lock" \
      && -f "${root}/gateway/run.py" \
      && -f "${root}/hermes_cli/main.py" \
      && -f "${root}/run_agent.py" \
      && -x "${root}/.venv/bin/python" \
      && -x "${root}/.venv/bin/hermes" ]] \
    || return 1
  rm -f -- "${verify_index}"
  if ! GIT_INDEX_FILE="${verify_index}" \
      git --git-dir="${mirror}" --work-tree="${root}" \
        read-tree "${release_commit}"; then
    rm -f -- "${verify_index}"
    return 1
  fi
  local result=0
  GIT_INDEX_FILE="${verify_index}" \
    git -c core.fileMode=false --git-dir="${mirror}" --work-tree="${root}" \
      diff-files --quiet --ignore-submodules=none \
    || result=$?
  rm -f -- "${verify_index}"
  return "${result}"
}

role_services_healthy() {
  systemctl is-active --quiet hermes-fabric-update.timer || return 1
  systemctl start "user@${service_uid}.service" >/dev/null 2>&1 || return 1
  user_systemctl is-active --quiet "${connector_unit}" || return 1
  case "${role}" in
    dbb3)
      systemctl is-active --quiet hermes-managed-installation-receiver.service
      ;;
    wsl)
      user_systemctl is-active --quiet \
        hermes-wsl-managed-installation-receiver.service \
        hermes-wsl-managed-installation-tunnel.service
      ;;
    hk)
      user_systemctl is-active --quiet hermes-gateway-hk-worker.service \
        && systemctl is-active --quiet \
          hermes-hk-managed-node-recovery.service \
          hermes-hk-managed-node-recovery-tunnel.service
      ;;
  esac
}

local_generation_current() {
  [[ -L "${current_link}" ]] || return 1
  local resolved marker_tree
  resolved="$(readlink -f -- "${current_link}")" || return 1
  [[ "${resolved}" == "${generation_root}/${release_commit}" \
      && -d "${resolved}" && ! -L "${resolved}" \
      && -x "${resolved}/.venv/bin/python" \
      && -x "${resolved}/.venv/bin/hermes" \
      && -f "${resolved}/pyproject.toml" \
      && -f "${resolved}/uv.lock" \
      && "$(cat -- "${resolved}/.hermes-source-commit" 2>/dev/null)" == "${release_commit}" ]] \
    || return 1
  marker_tree="$(cat -- "${resolved}/.hermes-source-tree" 2>/dev/null)"
  [[ "${marker_tree}" =~ ^[0-9a-f]{40}$ ]] || return 1
  [[ -d "${mirror}" && ! -L "${mirror}" ]] || return 1
  git --git-dir="${mirror}" cat-file -e "${release_commit}^{commit}" \
    || return 1
  git --git-dir="${mirror}" merge-base --is-ancestor \
    "${release_commit}" refs/remotes/origin/main \
    || return 1
  source_tree="$(git --git-dir="${mirror}" rev-parse "${release_commit}^{tree}")" \
    || return 1
  [[ "${source_tree}" =~ ^[0-9a-f]{40}$ \
      && "${marker_tree}" == "${source_tree}" ]] \
    || return 1
  verify_generation "${resolved}" || return 1
  [[ -f "${deployed_file}" && ! -L "${deployed_file}" \
      && "$(cat -- "${deployed_file}")" == "${release_commit}" \
      && -f "${release_evidence_file}" && ! -L "${release_evidence_file}" ]] \
    || return 1
  python3 - "${release_evidence_file}" "${role}" "${release_commit}" \
    "${release_version}" "${resolved}" "${marker_tree}" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
source = data.get("source") or {}
assert data.get("schema") == "hermes.fabric-release.v1"
assert data.get("node_id") == sys.argv[2]
assert data.get("commit") == sys.argv[3]
assert data.get("version") == sys.argv[4]
assert source == {
    "commit": sys.argv[3],
    "generation": sys.argv[5],
    "lock": "uv.lock",
    "tree": sys.argv[6],
}
PY
}

if [[ "${initial_worker_current}" == 1 ]] \
  && local_generation_current \
  && role_services_healthy; then
  printf 'role=%s\ncommit=%s\nversion=%s\nstate=current\n' \
    "${role}" "${release_commit}" "${release_version}"
  exit 0
fi

if [[ ! -d "${mirror}" ]]; then
  temporary_mirror="${state_root}/repository.git.new.$$"
  rm -rf -- "${temporary_mirror}"
  run_network_git clone --mirror -- "${repository_url}" "${temporary_mirror}"
  mv -f -- "${temporary_mirror}" "${mirror}"
fi
[[ -d "${mirror}" && ! -L "${mirror}" ]] || die "repository mirror is unsafe"
git --git-dir="${mirror}" remote set-url origin "${repository_url}"
if ! run_network_git --git-dir="${mirror}" fetch --force --prune origin \
  "+refs/heads/main:refs/remotes/origin/main"; then
  if git --git-dir="${mirror}" cat-file -e "${release_commit}^{commit}" \
    && git --git-dir="${mirror}" merge-base --is-ancestor \
      "${release_commit}" refs/remotes/origin/main; then
    printf 'update-fabric-node: repository refresh unavailable; using verified mirror commit %s\n' \
      "${release_commit}" >&2
  else
    die "repository mirror refresh failed and release is not already verified"
  fi
fi
git --git-dir="${mirror}" cat-file -e "${release_commit}^{commit}"
git --git-dir="${mirror}" merge-base --is-ancestor \
  "${release_commit}" refs/remotes/origin/main \
  || die "committed release is not part of the approved main branch"
source_tree="$(git --git-dir="${mirror}" rev-parse "${release_commit}^{tree}")"
[[ "${source_tree}" =~ ^[0-9a-f]{40}$ ]] || die "release source tree is invalid"

snapshot="${generation_root}/${release_commit}"
if [[ -e "${snapshot}" || -L "${snapshot}" ]]; then
  [[ -d "${snapshot}" && ! -L "${snapshot}" ]] \
    || die "release generation target is unsafe"
  if ! verify_generation "${snapshot}"; then
    quarantine_generation="${generation_root}/.${release_commit}.invalid.$$"
    [[ ! -e "${quarantine_generation}" && ! -L "${quarantine_generation}" ]] \
      || die "release generation quarantine target already exists"
    if [[ "$(readlink -f -- "${current_link}" 2>/dev/null || true)" == "${snapshot}" ]]; then
      mv -T -- "${snapshot}" "${quarantine_generation}"
      previous_current_target="${quarantine_generation}"
      previous_current_present=1
      previous_current_resolved="${quarantine_generation}"
      generation_repaired_active=1
      ln -s -- "${quarantine_generation}" "${current_link}.new.$$"
      mv -Tf -- "${current_link}.new.$$" "${current_link}"
    else
      mv -T -- "${snapshot}" "${quarantine_generation}"
      rm -rf -- "${quarantine_generation}"
      quarantine_generation=""
    fi
  fi
fi
if [[ ! -e "${snapshot}" && ! -L "${snapshot}" ]]; then
  command -v uv >/dev/null 2>&1 \
    || die "uv is required to build the locked release generation"
  candidate_generation="${generation_root}/.${release_commit}.new.$$"
  [[ ! -e "${candidate_generation}" && ! -L "${candidate_generation}" ]] \
    || die "release generation candidate already exists"
  assert_canonical_path "${candidate_generation}" "release generation candidate"
  install -d -o root -g root -m 0755 "${candidate_generation}"
  assert_canonical_path "${candidate_generation}" "release generation candidate"
  # No path list is supplied: every tracked file in the ancestry-verified Git
  # tree is materialized. Release evidence therefore names a real source tree,
  # rather than a handful of copied modules wearing the target SHA.
  git --git-dir="${mirror}" archive --format=tar "${release_commit}" \
    | tar -xf - -C "${candidate_generation}"
  # Reject links/submodules from the complete archive before dependency
  # installation can follow a tracked path outside the candidate tree.
  tracked_tree_listing="$(git --git-dir="${mirror}" ls-tree -r --full-tree \
    "${release_commit}")"
  while IFS=$'\t' read -r tracked_metadata _; do
    tracked_mode="${tracked_metadata%% *}"
    case "${tracked_mode}" in
      120000|160000)
        die "approved release tree contains a tracked symlink or submodule"
        ;;
    esac
  done <<<"${tracked_tree_listing}"
  unset tracked_tree_listing tracked_metadata tracked_mode
  for required in pyproject.toml uv.lock gateway/run.py hermes_cli/main.py \
    run_agent.py deploy/automation/update-fabric-node.sh; do
    [[ -f "${candidate_generation}/${required}" ]] \
      || die "complete source archive is missing ${required}"
  done
  (
    umask 022
    UV_NO_CONFIG=1 uv sync --locked --python 3.11 --extra all \
      --extra hindsight --no-dev --directory "${candidate_generation}"
  )
  [[ -x "${candidate_generation}/.venv/bin/python" \
      && -x "${candidate_generation}/.venv/bin/hermes" ]] \
    || die "locked generation runtime was not created"
  ln -s .venv "${candidate_generation}/venv"
  printf '%s\n' "${release_commit}" >"${candidate_generation}/.hermes-source-commit"
  printf '%s\n' "${source_tree}" >"${candidate_generation}/.hermes-source-tree"
  chmod 0444 "${candidate_generation}/.hermes-source-commit" \
    "${candidate_generation}/.hermes-source-tree"
  chmod -R a+rX,go-w "${candidate_generation}"
  verify_generation "${candidate_generation}" \
    || die "locked release generation differs from the approved Git tree"
  assert_canonical_path "${preflight_service_home}" "fabric preflight service home"
  [[ ! -e "${preflight_service_home}" && ! -L "${preflight_service_home}" ]] \
    || die "candidate preflight home already exists"
  install -d -o "${service_user}" -g "${service_user}" -m 0700 \
    "${preflight_service_home}" \
    "${preflight_service_home}/.hermes/profiles/${worker_profile}"
  assert_canonical_path "${preflight_service_home}" "fabric preflight service home"
  (
    cd "${candidate_generation}"
    runuser -u "${service_user}" -- env \
      HOME="${preflight_service_home}" \
      HERMES_HOME="${preflight_service_home}/.hermes/profiles/${worker_profile}" \
      PYTHONDONTWRITEBYTECODE=1 \
      PYTHONPATH="${candidate_generation}" \
      "${candidate_generation}/.venv/bin/python" -c \
        'import gateway.run; import hermes_cli.managed_node_recovery_service; from websockets.sync.client import connect; assert connect'
    runuser -u "${service_user}" -- env \
      HOME="${preflight_service_home}" \
      HERMES_HOME="${preflight_service_home}/.hermes/profiles/${worker_profile}" \
      PYTHONDONTWRITEBYTECODE=1 \
      "${candidate_generation}/.venv/bin/hermes" --version >/dev/null
  )
  rm -rf -- "${preflight_service_home}"
  mv -T -- "${candidate_generation}" "${snapshot}"
  candidate_generation=""
  generation_created=1
fi

assert_canonical_path "${preflight_root}" "fabric preflight root"
[[ ! -e "${preflight_root}" && ! -L "${preflight_root}" ]] \
  || die "deployment preflight path already exists"
install -d -o root -g root -m 0755 "${preflight_root}"
cp -a -- "${snapshot}/deploy" "${preflight_root}/deploy"
chmod -R a+rX,go-w "${preflight_root}"

rewrite_unit_runtime() {
  local path="$1" legacy_root="$2"
  [[ -f "${path}" && ! -L "${path}" ]] || die "unit template is missing: ${path}"
  python3 - "${path}" "${legacy_root}" "${current_link}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
legacy = sys.argv[2]
current = sys.argv[3]
text = path.read_text(encoding="utf-8")
placeholder = "__HERMES_FABRIC_CURRENT_RUNTIME__"
if placeholder in text:
    raise RuntimeError(f"reserved runtime placeholder present in {path}")
text = text.replace(legacy + "/.fabric-current", placeholder)
text = text.replace(legacy + "/.venv", placeholder + "/.venv")
text = text.replace(legacy + "/venv", placeholder + "/.venv")
text = text.replace(legacy, placeholder)
text = text.replace(placeholder, current)
if current + "/.fabric-current" in text:
    raise RuntimeError(f"fabric runtime prefix was duplicated in {path}")
if current + "/.venv/bin/python" not in text:
    raise RuntimeError(f"unit does not select the locked fabric runtime: {path}")
path.write_text(text, encoding="utf-8")
PY
}
case "${role}" in
  dbb3)
    rewrite_unit_runtime \
      "${preflight_root}/deploy/dbb3/dbb3-cloud-connector.service" \
      /usr/local/lib/hermes-agent
    rewrite_unit_runtime \
      "${preflight_root}/deploy/recovery/hermes-managed-installation-receiver.service" \
      /usr/local/lib/hermes-agent
    ;;
  wsl)
    rewrite_unit_runtime \
      "${preflight_root}/deploy/pc/pc-cloud-connector.service" \
      /mnt/d/Hermes/hermes-agent
    rewrite_unit_runtime \
      "${preflight_root}/deploy/recovery/hermes-wsl-managed-installation-receiver.service" \
      /mnt/d/Hermes/hermes-agent
    ;;
  hk)
    rewrite_unit_runtime \
      "${preflight_root}/deploy/hk/hk-cloud-connector.service" \
      /opt/hk-team/hermes-agent
    rewrite_unit_runtime \
      "${preflight_root}/deploy/recovery/hermes-hk-managed-node-recovery.service" \
      /opt/hk-team/hermes-agent
    ;;
esac

automation_assets=(
  "deploy/automation/update-fabric-node.sh"
  "deploy/automation/hermes-fabric-update.service"
  "deploy/automation/hermes-fabric-update.timer"
)
for relative in "${automation_assets[@]}"; do
  [[ -f "${snapshot}/${relative}" && ! -L "${snapshot}/${relative}" ]] \
    || die "missing or unsafe automation asset ${relative}"
done
install -d -o root -g root -m 0700 \
  "${automation_backup}" "${automation_backup}/systemd"
for target in "${automation_script_target}" "${automation_service_target}" \
  "${automation_timer_target}"; do
  [[ ! -L "${target}" ]] || die "automation target is a symlink: ${target}"
  case "${target}" in
    "${automation_script_target}") backup_relative="update-fabric-node.sh" ;;
    "${automation_service_target}") backup_relative="systemd/hermes-fabric-update.service" ;;
    "${automation_timer_target}") backup_relative="systemd/hermes-fabric-update.timer" ;;
  esac
  if [[ -f "${target}" ]]; then
    cp -a -- "${target}" "${automation_backup}/${backup_relative}"
    : >"${automation_backup}/${backup_relative}.present"
  elif [[ ! -e "${target}" ]]; then
    : >"${automation_backup}/${backup_relative}.absent"
  else
    die "automation target is not a regular file: ${target}"
  fi
done

if [[ -L "${current_link}" ]]; then
  previous_current_target="$(readlink -- "${current_link}")"
  [[ -n "${previous_current_target}" && "${previous_current_target}" != *$'\n'* ]] \
    || die "current generation link target is invalid"
  previous_current_present=1
  previous_current_resolved="$(readlink -f -- "${current_link}")" \
    || die "current generation link cannot be resolved"
elif [[ -e "${current_link}" ]]; then
  die "current generation path is not a symlink"
fi
ln -s -- "${snapshot}" "${current_link}.new.$$"
mv -Tf -- "${current_link}.new.$$" "${current_link}"
current_swapped=1
[[ "$(readlink -f -- "${current_link}")" == "${snapshot}" ]] \
  || die "current generation switch did not select the target release"
[[ "${HERMES_FABRIC_FAILPOINT:-}" != after-generation ]] \
  || die "injected fabric failure after generation"

case "${role}" in
  dbb3)
    env HERMES_CONNECTOR_RUNTIME_PYTHON="${current_link}/.venv/bin/python" \
      HERMES_CONNECTOR_HERMES_HOME="${external_hermes_home}" \
      HERMES_CONNECTOR_PROFILE_TEMPLATE_ROOT="${preflight_root}/deploy/dbb3/profile" \
      DBB3_CONNECTOR_ARTIFACT_ROOTS="${external_hermes_home}" \
      bash "${preflight_root}/deploy/dbb3/install-dbb3-cloud-connector-user.sh" \
      "${preflight_root}/deploy/dbb3/dbb3_cloud_connector.py" \
      "--handle-file=${connector_handle}"
    connector_installed=1
    [[ "${HERMES_FABRIC_FAILPOINT:-}" != after-connector ]] \
      || die "injected fabric failure after connector"
    env HERMES_DBB3_AGENT_ROOT="${current_link}" \
      HERMES_DBB3_RUNTIME_PYTHON="${current_link}/.venv/bin/python" \
      HERMES_DBB3_HOME="${external_hermes_home}" \
      bash "${preflight_root}/deploy/recovery/install-dbb3-managed-installation-receiver.sh" \
      "${preflight_root}" "--handle-file=${receiver_handle}"
    receiver_installed=1
    ;;
  wsl)
    env HERMES_WSL_AGENT_ROOT="${current_link}" \
      HERMES_WSL_RUNTIME_PYTHON="${current_link}/.venv/bin/python" \
      HERMES_WSL_HOME="${external_hermes_home}" \
      PC_CONNECTOR_HERMES_HOME="${external_hermes_home}" \
      PC_CONNECTOR_ARTIFACT_ROOTS="${external_hermes_home}" \
      bash "${preflight_root}/deploy/pc/install-pc-cloud-connector-user.sh" \
      "--handle-file=${connector_handle}"
    connector_installed=1
    [[ "${HERMES_FABRIC_FAILPOINT:-}" != after-connector ]] \
      || die "injected fabric failure after connector"
    wsl_secret_stage="${transaction_root}/wsl-secrets"
    install -d -o root -g root -m 0700 "${wsl_secret_stage}"
    wsl_installation_token="${HERMES_WSL_INSTALLATION_TOKEN_FILE:-/etc/pc-team/managed-installation-token}"
    wsl_receiver_user="${HERMES_FABRIC_SERVICE_USER:-hermes}"
    wsl_user_home="$(getent passwd "${wsl_receiver_user}" | cut -d: -f6)"
    wsl_installation_key="${HERMES_WSL_INSTALLATION_KEY_FILE:-${wsl_user_home}/.ssh/aliyun_hermes_ed25519}"
    if [[ "${allow_test_paths}" != 1 ]]; then
      [[ "${wsl_installation_token}" == /etc/pc-team/managed-installation-token ]] \
        || die "WSL installation token override is not allowed"
      [[ -z "${HERMES_WSL_INSTALLATION_KEY_FILE:-}" ]] \
        || die "WSL installation key override is not allowed"
    fi
    [[ -f "${wsl_installation_token}" && ! -L "${wsl_installation_token}" ]] \
      || die "WSL managed installation token is missing or unsafe"
    [[ -f "${wsl_installation_key}" && ! -L "${wsl_installation_key}" ]] \
      || die "WSL managed installation key is missing or unsafe"
    install -o root -g root -m 0600 \
      "${wsl_installation_token}" "${wsl_secret_stage}/installation-token"
    install -o root -g root -m 0600 \
      "${wsl_installation_key}" "${wsl_secret_stage}/installation-key"
    env HERMES_WSL_AGENT_ROOT="${current_link}" \
      HERMES_WSL_RUNTIME_PYTHON="${current_link}/.venv/bin/python" \
      bash "${preflight_root}/deploy/recovery/install-wsl-managed-installation.sh" \
      "${preflight_root}" "${wsl_secret_stage}/installation-token" \
      "${wsl_secret_stage}/installation-key" \
      "--handle-file=${receiver_handle}"
    receiver_installed=1
    ;;
  hk)
    env HERMES_HK_AGENT_ROOT="${current_link}" \
      HERMES_HK_RUNTIME_PYTHON="${current_link}/.venv/bin/python" \
      HERMES_HK_HOME="${external_hermes_home}" \
      HK_CONNECTOR_HERMES_HOME="${external_hermes_home}" \
      HK_CONNECTOR_ARTIFACT_ROOTS="${external_hermes_home}" \
      bash "${preflight_root}/deploy/hk/install-hk-cloud-connector-user.sh" \
      "--handle-file=${connector_handle}"
    connector_installed=1
    [[ "${HERMES_FABRIC_FAILPOINT:-}" != after-connector ]] \
      || die "injected fabric failure after connector"
    hk_recovery_token="${HERMES_HK_RECOVERY_TOKEN_FILE:-/etc/hk-team/recovery_token}"
    hk_recovery_key="${HERMES_HK_RECOVERY_KEY_FILE:-/home/hermes/.ssh/hk_recovery_ed25519}"
    hk_recovery_known_hosts="${HERMES_HK_RECOVERY_KNOWN_HOSTS_FILE:-/home/hermes/.ssh/hk_recovery_known_hosts}"
    if [[ "${allow_test_paths}" != 1 ]]; then
      [[ "${hk_recovery_token}" == /etc/hk-team/recovery_token \
          && "${hk_recovery_key}" == /home/hermes/.ssh/hk_recovery_ed25519 \
          && "${hk_recovery_known_hosts}" == /home/hermes/.ssh/hk_recovery_known_hosts ]] \
        || die "HK recovery credential override is not allowed"
    fi
    for secret in "${hk_recovery_token}" "${hk_recovery_key}" \
      "${hk_recovery_known_hosts}"; do
      [[ -f "${secret}" && ! -L "${secret}" ]] \
        || die "HK recovery credential is missing or unsafe: ${secret}"
    done
    hk_secret_stage="${transaction_root}/hk-recovery-secrets"
    install -d -o root -g root -m 0700 "${hk_secret_stage}"
    install -o root -g root -m 0600 \
      "${hk_recovery_token}" "${hk_secret_stage}/recovery-token"
    install -o root -g root -m 0600 \
      "${hk_recovery_key}" "${hk_secret_stage}/recovery-key"
    install -o root -g root -m 0600 \
      "${hk_recovery_known_hosts}" "${hk_secret_stage}/recovery-known-hosts"
    env HERMES_HK_AGENT_ROOT="${current_link}" \
      HERMES_HK_RUNTIME_PYTHON="${current_link}/.venv/bin/python" \
      bash "${preflight_root}/deploy/recovery/install-hk-managed-recovery.sh" \
      "${preflight_root}" "${hk_secret_stage}/recovery-token" \
      "${hk_secret_stage}/recovery-key" \
      "${hk_secret_stage}/recovery-known-hosts" \
      "--handle-file=${receiver_handle}"
    receiver_installed=1
    ;;
esac
[[ "${HERMES_FABRIC_FAILPOINT:-}" != after-receiver ]] \
  || die "injected fabric failure after receiver"

if [[ "${role}" == hk ]]; then
  [[ -f "${external_hermes_home}/config.yaml" \
      && -f "${external_hermes_home}/SOUL.md" \
      && -d "${external_hermes_home}/skills" ]] \
    || die "HK worker profile was not installed transactionally"
  systemctl start "user@${service_uid}.service"
  gateway_unit="${service_home}/.config/systemd/user/hermes-gateway-hk-worker.service"
  install -d -o "${service_user}" -g "${service_user}" -m 0700 \
    "$(dirname "${gateway_unit}")"
  [[ ! -L "${gateway_unit}" ]] || die "HK gateway unit target is a symlink"
  install -d -o root -g root -m 0700 "${gateway_backup}"
  if [[ -f "${gateway_unit}" ]]; then
    cp -a -- "${gateway_unit}" "${gateway_backup}/unit"
    : >"${gateway_backup}/unit.present"
  elif [[ ! -e "${gateway_unit}" ]]; then
    : >"${gateway_backup}/unit.absent"
  else
    die "HK gateway unit target is unsafe"
  fi
  user_systemctl is-active --quiet hermes-gateway-hk-worker.service \
    && gateway_was_active=1
  user_systemctl is-enabled --quiet hermes-gateway-hk-worker.service \
    && gateway_was_enabled=1
  gateway_touched=1
  runuser -u "${service_user}" -- env -u HERMES_HOME \
    HOME="${service_home}" \
    PATH="${current_link}/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
    XDG_RUNTIME_DIR="${user_runtime}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=${user_runtime}/bus" \
    PYTHONDONTWRITEBYTECODE=1 \
    "${current_link}/.venv/bin/hermes" -p hk-worker gateway install \
      --force --start-now --start-on-login
  [[ -f "${gateway_unit}" && ! -L "${gateway_unit}" ]] \
    || die "HK gateway service was not installed"
  grep -Fq -- '--profile hk-worker' "${gateway_unit}" \
    || die "HK gateway unit does not select the hk-worker profile"
  grep -Fq -- ' gateway run' "${gateway_unit}" \
    || die "HK gateway unit does not run the Hermes gateway"
  user_systemctl is-active --quiet hermes-gateway-hk-worker.service \
    || die "HK gateway did not become active"
fi
[[ "${HERMES_FABRIC_FAILPOINT:-}" != after-gateway ]] \
  || die "injected fabric failure after gateway"

automation_swapped=1
install -o root -g root -m 0755 \
  "${snapshot}/deploy/automation/update-fabric-node.sh" \
  "${automation_script_temp}"
install -o root -g root -m 0644 \
  "${snapshot}/deploy/automation/hermes-fabric-update.service" \
  "${automation_service_temp}"
install -o root -g root -m 0644 \
  "${snapshot}/deploy/automation/hermes-fabric-update.timer" \
  "${automation_timer_temp}"
mv -f -- "${automation_script_temp}" "${automation_script_target}"
mv -f -- "${automation_service_temp}" "${automation_service_target}"
mv -f -- "${automation_timer_temp}" "${automation_timer_target}"
systemctl daemon-reload
systemctl enable --now hermes-fabric-update.timer
[[ "${HERMES_FABRIC_FAILPOINT:-}" != after-automation ]] \
  || die "injected fabric failure after automation"

release_evidence_temp="${release_evidence_file}.new.$$"
python3 - "${release_evidence_temp}" "${role}" "${release_commit}" \
  "${release_version}" "${source_tree}" "${snapshot}" <<'PY'
import json
import pathlib
import sys

payload = {
    "schema": "hermes.fabric-release.v1",
    "node_id": sys.argv[2],
    "commit": sys.argv[3],
    "version": sys.argv[4],
    "source": {
        "commit": sys.argv[3],
        "generation": sys.argv[6],
        "lock": "uv.lock",
        "tree": sys.argv[5],
    },
}
pathlib.Path(sys.argv[1]).write_text(
    json.dumps(payload, sort_keys=True, ensure_ascii=True) + "\n",
    encoding="utf-8",
)
PY
chmod 0644 "${release_evidence_temp}"
if [[ -f "${release_evidence_file}" && ! -L "${release_evidence_file}" ]]; then
  cp -a -- "${release_evidence_file}" \
    "${transaction_root}/previous-release-evidence.json"
  : >"${transaction_root}/previous-release-evidence.present"
elif [[ -e "${release_evidence_file}" || -L "${release_evidence_file}" ]]; then
  die "fabric release evidence target is unsafe"
fi
mv -f -- "${release_evidence_temp}" "${release_evidence_file}"
evidence_published=1
[[ "${HERMES_FABRIC_FAILPOINT:-}" != after-evidence ]] \
  || die "injected fabric failure after evidence"

role_services_healthy || die "connector, timer, or recovery service is not active"
if [[ "${role}" == hk ]]; then
  gateway_main_pid="$(user_systemctl show hermes-gateway-hk-worker.service \
    -p MainPID --value)"
  [[ "${gateway_main_pid}" =~ ^[1-9][0-9]*$ ]] \
    || die "HK gateway has no live main process"
  if [[ "${allow_test_paths}" != 1 ]]; then
    [[ -r "/proc/${gateway_main_pid}/cmdline" ]] \
      || die "HK gateway main process cannot be inspected"
    gateway_cmdline="$(tr '\0' ' ' <"/proc/${gateway_main_pid}/cmdline")"
    [[ "${gateway_cmdline}" == *"gateway run"* \
        && "${gateway_cmdline}" == *"--profile hk-worker"* \
        && ( "${gateway_cmdline}" == *"${snapshot}/.venv/bin/python"* \
          || "${gateway_cmdline}" == *"${current_link}/.venv/bin/python"* ) ]] \
      || die "HK gateway is not running from the target source generation"
  fi
  runuser -u "${service_user}" -- env -u HERMES_HOME \
    HOME="${service_home}" \
    PATH="${current_link}/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
    XDG_RUNTIME_DIR="${user_runtime}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=${user_runtime}/bus" \
    "${current_link}/.venv/bin/hermes" -p hk-worker gateway status >/dev/null \
    || die "HK gateway status probe failed"
fi

# A running unit alone does not prove the new connector transport joined the
# backend. The authenticated worker must advertise both a fresh connection
# generation and this exact source-backed release before commit publication.
worker_ws_deadline=$(( $(date +%s) + worker_ws_timeout ))
while true; do
  if curl --fail --silent --show-error --max-time 15 --noproxy '*' \
      --config "${curl_config}" \
      -o "${evidence_file}" "${cloud_url}/connector/deployment-health" \
    && python3 - "${evidence_file}" "${connector_id}" "${worker_node_id}" \
      "${role}" "${release_commit}" "${release_version}" \
      "${previous_connection_generation}" <<'PY'
import json
import re
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload.get("ok") is True
assert str(payload.get("connector_id") or "") == sys.argv[2]
worker = payload.get("worker_channel") or {}
assert worker.get("online") is True
assert worker.get("fresh") is True
assert str(worker.get("node_id") or "") == sys.argv[3]
assert str(worker.get("managed_node_id") or "") == sys.argv[4]
generation = str(worker.get("connection_generation") or "")
assert re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", generation)
previous_generation = sys.argv[7]
assert not previous_generation or generation != previous_generation
release = worker.get("release") or {}
assert str(release.get("commit") or "") == sys.argv[5]
assert str(release.get("version") or "") == sys.argv[6]
PY
  then
    break
  fi
  (( $(date +%s) < worker_ws_deadline )) \
    || die "connector restarted but target worker WebSocket generation/release did not become healthy"
  sleep 2
done

verify_generation "${snapshot}" \
  || die "target source generation changed during deployment"
[[ "$(readlink -f -- "${current_link}")" == "${snapshot}" ]] \
  || die "target source generation is no longer current"
[[ ! -L "${deployed_file}" ]] || die "deployed commit target is a symlink"
if [[ -f "${deployed_file}" ]]; then
  cp -a -- "${deployed_file}" "${transaction_root}/previous-deployed-commit"
  : >"${transaction_root}/previous-deployed-commit.present"
elif [[ -e "${deployed_file}" ]]; then
  die "deployed commit target is not a regular file"
fi
printf '%s\n' "${release_commit}" >"${deployed_file}.new.$$"
chmod 0600 "${deployed_file}.new.$$"
deployed_published=1
mv -f -- "${deployed_file}.new.$$" "${deployed_file}"
[[ "${HERMES_FABRIC_FAILPOINT:-}" != after-deployed ]] \
  || die "injected fabric failure after deployed commit publication"
transaction_committed=1
if [[ -n "${quarantine_generation}" \
    && "${quarantine_generation}" == "${generation_root}/.${release_commit}.invalid."* \
    && -d "${quarantine_generation}" && ! -L "${quarantine_generation}" ]]; then
  rm -rf -- "${quarantine_generation}" \
    || printf 'update-fabric-node: repaired generation quarantine cleanup failed\n' >&2
fi

# A complete locked environment is intentionally kept per release, but an
# unbounded history would eventually exhaust worker disks. Keep the active and
# immediately previous generations; remove only canonical, root-owned SHA
# directories after the new release is healthy and committed. Crash remnants
# use a narrowly validated hidden name and are never retained as generations.
prune_old_generations() {
  local generation generation_name
  while IFS= read -r -d '' generation; do
    generation_name="${generation##*/}"
    if [[ "${generation_name}" =~ ^[0-9a-f]{40}$ ]]; then
      [[ "${generation}" != "${snapshot}" \
          && "${generation}" != "${previous_current_resolved}" ]] \
        || continue
    elif [[ ! "${generation_name}" =~ ^\.[0-9a-f]{40}\.(invalid|new)\.[0-9]+$ ]]; then
      continue
    fi
    [[ -d "${generation}" && ! -L "${generation}" \
        && "$(stat -c '%u' "${generation}")" == 0 ]] \
      || continue
    rm -rf -- "${generation}" || return 1
  done < <(find "${generation_root}" -mindepth 1 -maxdepth 1 -type d -print0)
}
prune_old_generations \
  || printf 'update-fabric-node: old generation pruning failed\n' >&2
printf 'role=%s\ncommit=%s\nversion=%s\nsource_tree=%s\ngeneration=%s\nstate=updated\n' \
  "${role}" "${release_commit}" "${release_version}" "${source_tree}" "${snapshot}"
