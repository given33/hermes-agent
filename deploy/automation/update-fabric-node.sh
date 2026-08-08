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
install -d -o root -g root -m 0755 "$(dirname "${state_root}")" "${state_root}"
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
preflight_root="${state_root}/preflight.$$"
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
automation_backup="${stage}/automation-backup"
runtime_swapped=0
automation_swapped=0
transaction_committed=0
connector_installed=0
receiver_installed=0
evidence_published=0
connector_handle="${stage}/connector-rollback-handle"
receiver_handle="${stage}/receiver-rollback-handle"
cleanup() {
  local status=$?
  trap - EXIT
  set +e
  rollback_failed=0
  if (( receiver_installed && ! transaction_committed )); then
    receiver_backup="$(cat -- "${receiver_handle}" 2>/dev/null)"
    case "${role}" in
      dbb3)
        bash "${stage}/deploy/recovery/install-dbb3-managed-installation-receiver.sh" \
          "${stage}" "--rollback-backup=${receiver_backup}" \
          || rollback_failed=1
        ;;
      wsl)
        bash "${stage}/deploy/recovery/install-wsl-managed-installation.sh" \
          "${stage}" "${wsl_secret_stage}/installation-token" \
          "${wsl_secret_stage}/installation-key" \
          "--rollback-backup=${receiver_backup}" \
          || rollback_failed=1
        ;;
    esac
  fi
  if (( connector_installed && ! transaction_committed )); then
    connector_backup="$(cat -- "${connector_handle}" 2>/dev/null)"
    case "${role}" in
      dbb3)
        bash "${preflight_root}/deploy/dbb3/install-dbb3-cloud-connector-user.sh" \
          "${preflight_root}/deploy/dbb3/dbb3_cloud_connector.py" \
          "--rollback-backup=${connector_backup}" \
          || rollback_failed=1
        ;;
      wsl)
        bash "${preflight_root}/deploy/pc/install-pc-cloud-connector-user.sh" \
          "--rollback-backup=${connector_backup}" \
          || rollback_failed=1
        ;;
    esac
  fi
  if (( evidence_published && ! transaction_committed )); then
    if [[ -f "${stage}/previous-release-evidence.present" ]]; then
      install -o root -g root -m 0644 \
        "${stage}/previous-release-evidence.json" \
        "${release_evidence_file}.rollback.$$" \
        && mv -f -- "${release_evidence_file}.rollback.$$" \
          "${release_evidence_file}" \
        || rollback_failed=1
    else
      rm -f -- "${release_evidence_file}" || rollback_failed=1
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
        install -o root -g root -m 0644 "${backup_target}" "${target}.rollback.$$" \
          && mv -f -- "${target}.rollback.$$" "${target}" \
          || rollback_failed=1
      elif [[ -f "${backup_target}.absent" ]]; then
        rm -f -- "${target}" || rollback_failed=1
      else
        rollback_failed=1
      fi
    done
    systemctl daemon-reload >/dev/null 2>&1 || rollback_failed=1
    systemctl enable --now hermes-fabric-update.timer >/dev/null 2>&1 || rollback_failed=1
  fi
  if (( runtime_swapped && ! transaction_committed )); then
    for relative in "${runtime_assets[@]:-}"; do
      target="${runtime_root}/${relative}"
      backup_target="${runtime_backup}/${relative}"
      if [[ -f "${backup_target}.present" ]]; then
        install -o root -g root -m 0644 "${backup_target}" "${target}.rollback.$$" \
          && mv -f -- "${target}.rollback.$$" "${target}"
      elif [[ -f "${backup_target}.absent" ]]; then
        rm -f -- "${target}"
      fi
    done
    case "${role}" in
      dbb3) systemctl restart hermes-managed-installation-receiver.service >/dev/null 2>&1 || true ;;
      wsl)
        if id hermes >/dev/null 2>&1; then
          uid="$(id -u hermes)"
          runuser -u hermes -- env XDG_RUNTIME_DIR="/run/user/${uid}" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
            systemctl --user restart hermes-wsl-managed-installation-receiver.service \
            >/dev/null 2>&1 || true
        fi
        ;;
    esac
  fi
  rm -rf -- "${stage}" "${preflight_root}"
  rm -f -- "${evidence_file}" "${curl_config}" \
    "${automation_script_temp}" "${automation_service_temp}" \
    "${automation_timer_temp}" "${release_evidence_temp:-}"
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
readarray -t release_identity < <(python3 - "${evidence_file}" <<'PY'
import json
import re
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload.get("ok") is True
release = payload.get("release") or {}
commit = str(release.get("commit") or "")
version = str(release.get("version") or "")
assert re.fullmatch(r"[0-9a-f]{40}", commit)
assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
print(commit)
print(version)
PY
)
release_commit="${release_identity[0]:-}"
release_version="${release_identity[1]:-}"
[[ "${release_commit}" =~ ^[0-9a-f]{40}$ ]] || die "release commit is invalid"
[[ "${release_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "release version is invalid"
if [[ -f "${deployed_file}" \
    && "$(cat -- "${deployed_file}")" == "${release_commit}" \
    && -f "${release_evidence_file}" ]] \
  && python3 - "${release_evidence_file}" "${role}" "${release_commit}" "${release_version}" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data == {
    "commit": sys.argv[3],
    "node_id": sys.argv[2],
    "schema": "hermes.fabric-release.v1",
    "version": sys.argv[4],
}
PY
then
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
  # A transient GitHub outage must not strand a node when the public release
  # is already present in its last verified mirror. Continue only for that
  # exact commit and ancestry; unknown releases remain fail-closed below.
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
archive_paths=(
  "deploy/automation/update-fabric-node.sh"
  "deploy/automation/hermes-fabric-update.service"
  "deploy/automation/hermes-fabric-update.timer"
  "deploy/dbb3/install-dbb3-cloud-connector-user.sh"
  "deploy/dbb3/dbb3_cloud_connector.py"
  "deploy/dbb3/dbb3-cloud-connector.service"
  "deploy/recovery/install-dbb3-managed-installation-receiver.sh"
  "deploy/recovery/install-wsl-managed-installation.sh"
  "deploy/recovery/hermes-managed-installation-receiver.service"
  "deploy/recovery/hermes-wsl-managed-installation-receiver.service"
  "deploy/recovery/hermes-wsl-managed-installation-tunnel.service"
  "deploy/recovery/managed-installations.dbb3.json"
  "deploy/recovery/managed-installations.wsl.json"
  "hermes_cli/managed_installations.py"
  "hermes_cli/managed_nodes.py"
  "hermes_cli/managed_node_recovery_service.py"
  "hermes_cli/sqlite_util.py"
  "hermes_cli/__init__.py"
  "hermes_runtime"
  "hermes_services"
  "hermes_auth_errors.py"
  "hermes_constants.py"
  "hermes_secret_compare.py"
  "utils.py"
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
# Some WSL/DrvFs environments preserve the mktemp directory's restrictive
# traversal bit across recursive chmod; make every extracted directory
# explicitly traversable so the delegated service-account preflight can read
# the snapshot before it is removed.
find "${stage}" -type d -exec chmod a+rx {} +

# Connector preflight runs as the service account.  Keep that delegated read
# path outside the private /run snapshot: some systemd/DrvFs combinations
# retain restrictive traversal semantics for a root-created temporary tree
# even after chmod.  Copy only the connector assets, with no credentials, into
# a root-owned stable directory and remove it with the transaction cleanup.
[[ ! -e "${preflight_root}" && ! -L "${preflight_root}" ]] \
  || die "connector preflight path already exists"
case "${role}" in
  dbb3)
    install -d -o root -g root -m 0755 \
      "${preflight_root}/deploy/dbb3"
    install -o root -g root -m 0755 \
      "${stage}/deploy/dbb3/install-dbb3-cloud-connector-user.sh" \
      "${preflight_root}/deploy/dbb3/install-dbb3-cloud-connector-user.sh"
    install -o root -g root -m 0644 \
      "${stage}/deploy/dbb3/dbb3_cloud_connector.py" \
      "${preflight_root}/deploy/dbb3/dbb3_cloud_connector.py"
    install -o root -g root -m 0644 \
      "${stage}/deploy/dbb3/dbb3-cloud-connector.service" \
      "${preflight_root}/deploy/dbb3/dbb3-cloud-connector.service"
    ;;
  wsl)
    install -d -o root -g root -m 0755 \
      "${preflight_root}/deploy/pc" "${preflight_root}/deploy/dbb3"
    install -o root -g root -m 0755 \
      "${stage}/deploy/pc/install-pc-cloud-connector-user.sh" \
      "${preflight_root}/deploy/pc/install-pc-cloud-connector-user.sh"
    install -o root -g root -m 0644 \
      "${stage}/deploy/pc/pc-cloud-connector.service" \
      "${preflight_root}/deploy/pc/pc-cloud-connector.service"
    install -o root -g root -m 0755 \
      "${stage}/deploy/dbb3/install-dbb3-cloud-connector-user.sh" \
      "${preflight_root}/deploy/dbb3/install-dbb3-cloud-connector-user.sh"
    install -o root -g root -m 0644 \
      "${stage}/deploy/dbb3/dbb3_cloud_connector.py" \
      "${preflight_root}/deploy/dbb3/dbb3_cloud_connector.py"
    install -o root -g root -m 0644 \
      "${stage}/deploy/dbb3/dbb3-cloud-connector.service" \
      "${preflight_root}/deploy/dbb3/dbb3-cloud-connector.service"
    ;;
esac

automation_assets=(
  "deploy/automation/update-fabric-node.sh"
  "deploy/automation/hermes-fabric-update.service"
  "deploy/automation/hermes-fabric-update.timer"
)
for relative in "${automation_assets[@]}"; do
  [[ -f "${stage}/${relative}" && ! -L "${stage}/${relative}" ]] \
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

runtime_assets=(
  "hermes_cli/__init__.py"
  "hermes_cli/managed_installations.py"
  "hermes_cli/managed_nodes.py"
  "hermes_cli/managed_node_recovery_service.py"
  "hermes_cli/sqlite_util.py"
)
for runtime_root_path in hermes_runtime hermes_services; do
  while IFS= read -r -d '' runtime_path; do
    runtime_assets+=("${runtime_path#"${stage}/"}")
  done < <(find "${stage}/${runtime_root_path}" -type f -name '*.py' -print0 2>/dev/null)
done
runtime_assets+=("hermes_auth_errors.py" "hermes_constants.py" "hermes_secret_compare.py" "utils.py")
(( ${#runtime_assets[@]} > 8 )) || die "managed runtime package is incomplete"
case "${role}" in
  dbb3) runtime_root="${HERMES_DBB3_AGENT_ROOT:-/usr/local/lib/hermes-agent}" ;;
  wsl) runtime_root="${HERMES_WSL_AGENT_ROOT:-/mnt/d/Hermes/hermes-agent}" ;;
esac
if [[ "${allow_test_paths}" != 1 ]]; then
  case "${role}:${runtime_root}" in
    dbb3:/usr/local/lib/hermes-agent|wsl:/mnt/d/Hermes/hermes-agent) ;;
    *) die "managed receiver runtime root override is not allowed" ;;
  esac
fi
[[ "${runtime_root}" == /* && -d "${runtime_root}" && ! -L "${runtime_root}" ]] \
  || die "managed receiver runtime root is unsafe"
[[ "$(realpath -e -- "${runtime_root}")" == "${runtime_root}" ]] \
  || die "managed receiver runtime root or one of its parents is a symlink"
[[ -d "${runtime_root}/hermes_cli" \
    && ! -L "${runtime_root}/hermes_cli" \
    && "$(realpath -e -- "${runtime_root}/hermes_cli")" == "${runtime_root}/hermes_cli" ]] \
  || die "managed receiver package root or one of its parents is unsafe"
runtime_backup="${stage}/runtime-backup"
install -d -o root -g root -m 0700 "${runtime_backup}/hermes_cli"
for relative in "${runtime_assets[@]}"; do
  source_file="${stage}/${relative}"
  target="${runtime_root}/${relative}"
  backup_target="${runtime_backup}/${relative}"
  [[ -f "${source_file}" && ! -L "${source_file}" ]] \
    || die "missing or unsafe managed receiver runtime asset ${relative}"
  [[ ! -L "${target}" ]] || die "managed receiver runtime target is a symlink: ${target}"
  install -d -o root -g root -m 0700 "$(dirname "${backup_target}")"
  if [[ -f "${target}" ]]; then
    cp -a -- "${target}" "${backup_target}"
    : >"${backup_target}.present"
  elif [[ ! -e "${target}" ]]; then
    : >"${backup_target}.absent"
  else
    die "managed receiver runtime target is not a file: ${target}"
  fi
done
staged_runtime_modules=()
for relative in "${runtime_assets[@]}"; do
  staged_runtime_modules+=("${stage}/${relative}")
done
python3 - "${staged_runtime_modules[@]}" <<'PY'
import pathlib, sys
for name in sys.argv[1:]:
    compile(pathlib.Path(name).read_text(encoding="utf-8"), name, "exec")
PY
# Compilation alone cannot catch a missing transitive module. Import the
# receiver entry point from the staged tree before replacing the live runtime,
# so a fabric update cannot restart into an ImportError and disappear from the
# public installation routes.
(
  cd "${stage}"
  PYTHONPATH="${stage}${PYTHONPATH:+:${PYTHONPATH}}" \
    python3 -c 'import hermes_cli.managed_node_recovery_service'
)
runtime_swapped=1
for relative in "${runtime_assets[@]}"; do
  target="${runtime_root}/${relative}"
  install -d -o root -g root -m 0755 "$(dirname "${target}")"
  install -o root -g root -m 0644 "${stage}/${relative}" "${target}.new.$$"
  mv -f -- "${target}.new.$$" "${target}"
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
    bash "${preflight_root}/deploy/dbb3/install-dbb3-cloud-connector-user.sh" \
      "${preflight_root}/deploy/dbb3/dbb3_cloud_connector.py" \
      "--handle-file=${connector_handle}"
    connector_installed=1
    [[ "${HERMES_FABRIC_FAILPOINT:-}" != after-connector ]] \
      || die "injected fabric failure after connector"
    bash "${stage}/deploy/recovery/install-dbb3-managed-installation-receiver.sh" \
      "${stage}" "--handle-file=${receiver_handle}"
    receiver_installed=1
    ;;
  wsl)
    bash "${preflight_root}/deploy/pc/install-pc-cloud-connector-user.sh" \
      "--handle-file=${connector_handle}"
    connector_installed=1
    [[ "${HERMES_FABRIC_FAILPOINT:-}" != after-connector ]] \
      || die "injected fabric failure after connector"
    wsl_secret_stage="${stage}/wsl-secrets"
    install -d -o root -g root -m 0700 "${wsl_secret_stage}"
    wsl_installation_token="${HERMES_WSL_INSTALLATION_TOKEN_FILE:-/etc/pc-team/managed-installation-token}"
    wsl_installation_key="${HERMES_WSL_INSTALLATION_KEY_FILE:-${wsl_user_home:-}/.ssh/aliyun_hermes_ed25519}"
    if [[ "${allow_test_paths}" != 1 ]]; then
      [[ "${wsl_installation_token}" == /etc/pc-team/managed-installation-token ]] \
        || die "WSL installation token override is not allowed"
      [[ -z "${HERMES_WSL_INSTALLATION_KEY_FILE:-}" ]] \
        || die "WSL installation key override is not allowed"
    fi
    [[ -f "${wsl_installation_token}" && ! -L "${wsl_installation_token}" ]] \
      || die "WSL managed installation token is missing or unsafe"
    install -o root -g root -m 0600 \
      "${wsl_installation_token}" "${wsl_secret_stage}/installation-token"
    wsl_receiver_user="${HERMES_FABRIC_SERVICE_USER:-hermes}"
    wsl_user_home="$(getent passwd "${wsl_receiver_user}" | cut -d: -f6)"
    if [[ -z "${HERMES_WSL_INSTALLATION_KEY_FILE:-}" ]]; then
      wsl_installation_key="${wsl_user_home}/.ssh/aliyun_hermes_ed25519"
    fi
    [[ -f "${wsl_installation_key}" && ! -L "${wsl_installation_key}" ]] \
      || die "WSL managed installation key is missing or unsafe"
    install -o root -g root -m 0600 \
      "${wsl_installation_key}" "${wsl_secret_stage}/installation-key"
    bash "${stage}/deploy/recovery/install-wsl-managed-installation.sh" \
      "${stage}" "${wsl_secret_stage}/installation-token" \
      "${wsl_secret_stage}/installation-key" \
      "--handle-file=${receiver_handle}"
    receiver_installed=1
    ;;
esac
[[ "${HERMES_FABRIC_FAILPOINT:-}" != after-receiver ]] \
  || die "injected fabric failure after receiver"

# A fabric release also advances the updater itself. Install only the three
# root-owned automation assets from the ancestry-verified snapshot; the
# running shell keeps its open script while the next timer invocation uses the
# new implementation.
automation_swapped=1
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
  "${automation_script_target}"
mv -f -- "${automation_service_temp}" \
  "${automation_service_target}"
mv -f -- "${automation_timer_temp}" \
  "${automation_timer_target}"
systemctl daemon-reload
systemctl enable --now hermes-fabric-update.timer
[[ "${HERMES_FABRIC_FAILPOINT:-}" != after-automation ]] \
  || die "injected fabric failure after automation"

release_evidence_temp="${release_evidence_file}.new.$$"
python3 - "${release_evidence_temp}" "${role}" "${release_commit}" "${release_version}" <<'PY'
import json, pathlib, sys
payload = {
    "schema": "hermes.fabric-release.v1",
    "node_id": sys.argv[2],
    "commit": sys.argv[3],
    "version": sys.argv[4],
}
pathlib.Path(sys.argv[1]).write_text(
    json.dumps(payload, sort_keys=True, ensure_ascii=True) + "\n",
    encoding="utf-8",
)
PY
chmod 0644 "${release_evidence_temp}"
if [[ -f "${release_evidence_file}" && ! -L "${release_evidence_file}" ]]; then
  cp -a -- "${release_evidence_file}" "${stage}/previous-release-evidence.json"
  : >"${stage}/previous-release-evidence.present"
elif [[ -e "${release_evidence_file}" || -L "${release_evidence_file}" ]]; then
  die "fabric release evidence target is unsafe"
fi
mv -f -- "${release_evidence_temp}" "${release_evidence_file}"
evidence_published=1
[[ "${HERMES_FABRIC_FAILPOINT:-}" != after-evidence ]] \
  || die "injected fabric failure after evidence"
printf '%s\n' "${release_commit}" >"${deployed_file}.new.$$"
chmod 0600 "${deployed_file}.new.$$"
mv -f -- "${deployed_file}.new.$$" "${deployed_file}"
transaction_committed=1
printf 'role=%s\ncommit=%s\nversion=%s\nstate=updated\n' \
  "${role}" "${release_commit}" "${release_version}"
