#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ "$(id -u)" == 0 ]] || {
  printf 'fabric updater harness must run as root\n' >&2
  exit 1
}
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work="$(mktemp -d /tmp/hermes-fabric-updater-test.XXXXXX)"
trap 'rm -rf -- "${work}"' EXIT
fake_bin="${work}/bin"
archive="${work}/archive"
mkdir -p "${fake_bin}" "${archive}/deploy/automation" \
  "${archive}/deploy/dbb3/profile" "${archive}/deploy/pc/profile" \
  "${archive}/deploy/hk/profile" "${archive}/deploy/recovery" \
  "${archive}/gateway" "${archive}/hermes_cli"

cat >"${fake_bin}/git" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "${1:-}" == clone && "${2:-}" == --mirror ]]; then
  mkdir -p "${@: -1}"
  exit 0
fi
if [[ "$*" == *" rev-parse "*"^{tree}"* ]]; then
  printf '%040d\n' 7
  exit 0
fi
if [[ "$*" == *" archive --format=tar "* ]]; then
  [[ "$*" != *" -- "* ]] || {
    printf 'full source archive unexpectedly received a path allowlist\n' >&2
    exit 2
  }
  tar -C "${FAKE_ARCHIVE_ROOT}" -cf - .
  exit 0
fi
if [[ "$*" == *" read-tree "* ]]; then
  : >"${GIT_INDEX_FILE}"
  exit 0
fi
if [[ "$*" == *" diff-files "* ]]; then
  exit 0
fi
exit 0
SH
cat >"${fake_bin}/uv" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
directory=""
next=0
for argument in "$@"; do
  if (( next )); then directory="${argument}"; next=0
  elif [[ "${argument}" == --directory ]]; then next=1
  fi
done
[[ -n "${directory}" ]]
mkdir -p "${directory}/.venv/bin"
cat >"${directory}/.venv/bin/python" <<'PYTHON'
#!/usr/bin/env bash
if [[ "${1:-}" == -c ]]; then exit 0; fi
exec /usr/bin/python3 "$@"
PYTHON
cat >"${directory}/.venv/bin/hermes" <<'HERMES'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "$*" == *"gateway install"* ]]; then
  unit_dir="${HOME}/.config/systemd/user"
  mkdir -p "${unit_dir}"
  runtime_python="${0%/bin/hermes}/bin/python"
  cat >"${unit_dir}/hermes-gateway-hk-worker.service" <<EOF
[Service]
ExecStart=${runtime_python} -m hermes_cli.main --profile hk-worker gateway run
EOF
fi
exit 0
HERMES
chmod 0755 "${directory}/.venv/bin/python" "${directory}/.venv/bin/hermes"
SH
cat >"${fake_bin}/curl" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
output=""
next=0
for argument in "$@"; do
  if (( next )); then output="${argument}"; next=0
  elif [[ "${argument}" == -o ]]; then next=1
  fi
done
count=0
if [[ -f "${FAKE_CURL_COUNT_FILE}" ]]; then
  count="$(cat "${FAKE_CURL_COUNT_FILE}")"
fi
count=$((count + 1))
printf '%s\n' "${count}" >"${FAKE_CURL_COUNT_FILE}"
generation=old-generation
(( count > 1 )) && generation=new-generation
case "${HERMES_FABRIC_ROLE}" in
  dbb3) worker_node=dbb3-worker ;;
  wsl) worker_node=pc-worker ;;
  hk) worker_node=hk-worker ;;
esac
payload="{\"ok\":true,\"connector_id\":\"${FAKE_CONNECTOR_ID}\",\"release\":{\"commit\":\"${FAKE_RELEASE_COMMIT}\",\"version\":\"${FAKE_RELEASE_VERSION}\"},\"worker_channel\":{\"node_id\":\"${worker_node}\",\"managed_node_id\":\"${HERMES_FABRIC_ROLE}\",\"online\":true,\"fresh\":true,\"connection_generation\":\"${generation}\",\"release\":{\"commit\":\"${FAKE_RELEASE_COMMIT}\",\"version\":\"${FAKE_RELEASE_VERSION}\"}}}"
if [[ -n "${output}" ]]; then printf '%s\n' "${payload}" >"${output}"
else printf '%s\n' "${payload}"
fi
SH
cat >"${fake_bin}/systemctl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FAKE_SYSTEMCTL_LOG}"
if [[ "$*" == *" show "* && "$*" == *" MainPID "* ]]; then
  printf '4242\n'
fi
exit 0
SH
cat >"${fake_bin}/runuser" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ "${1:-}" == -u ]] && shift 2
[[ "${1:-}" == -- ]] && shift
exec "$@"
SH
cat >"${fake_bin}/getent" <<'SH'
#!/usr/bin/env bash
printf 'hermes:x:1000:1000:Hermes:%s:/bin/bash\n' "${FAKE_USER_HOME}"
SH
cat >"${fake_bin}/id" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == -u ]]; then
  if [[ -n "${2:-}" ]]; then printf '1000\n'; else printf '0\n'; fi
fi
exit 0
SH
cat >"${fake_bin}/flock" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod 0755 "${fake_bin}"/*

printf '[project]\nname = "fixture"\nversion = "1.2.3"\n' >"${archive}/pyproject.toml"
printf 'version = 1\n' >"${archive}/uv.lock"
printf 'FULL SOURCE\n' >"${archive}/full-source-sentinel.txt"
printf '# gateway runtime\n' >"${archive}/gateway/run.py"
printf '# cli runtime\n' >"${archive}/hermes_cli/main.py"
printf '# recovery runtime\n' >"${archive}/hermes_cli/managed_node_recovery_service.py"
printf '# agent runtime\n' >"${archive}/run_agent.py"
for asset in update-fabric-node.sh hermes-fabric-update.service hermes-fabric-update.timer; do
  printf '# verified automation asset\n' >"${archive}/deploy/automation/${asset}"
done
printf '# connector source\n' >"${archive}/deploy/dbb3/dbb3_cloud_connector.py"
printf 'ExecStart=/usr/local/lib/hermes-agent/venv/bin/python connector.py\n' \
  >"${archive}/deploy/dbb3/dbb3-cloud-connector.service"
printf 'ExecStart=/mnt/d/Hermes/hermes-agent/venv/bin/python connector.py\n' \
  >"${archive}/deploy/pc/pc-cloud-connector.service"
printf 'ExecStart=/opt/hk-team/hermes-agent/.fabric-current/.venv/bin/python connector.py\n' \
  >"${archive}/deploy/hk/hk-cloud-connector.service"
for worker in dbb3 pc hk; do
  printf 'gateway:\n  platforms: {}\n' \
    >"${archive}/deploy/${worker}/profile/config.yaml.example"
  printf '# %s worker\n' "${worker}" \
    >"${archive}/deploy/${worker}/profile/SOUL.md"
done
for asset in hermes-managed-installation-receiver.service \
  hermes-wsl-managed-installation-receiver.service \
  hermes-wsl-managed-installation-tunnel.service \
  hermes-hk-managed-node-recovery.service \
  hermes-hk-managed-node-recovery-tunnel.service; do
  printf '# receiver unit\n' >"${archive}/deploy/recovery/${asset}"
done
printf 'ExecStart=/usr/local/lib/hermes-agent/venv/bin/python receiver.py\n' \
  >"${archive}/deploy/recovery/hermes-managed-installation-receiver.service"
printf 'ExecStart=/mnt/d/Hermes/hermes-agent/venv/bin/python receiver.py\n' \
  >"${archive}/deploy/recovery/hermes-wsl-managed-installation-receiver.service"
printf 'ExecStart=/opt/hk-team/hermes-agent/.fabric-current/.venv/bin/python receiver.py\n' \
  >"${archive}/deploy/recovery/hermes-hk-managed-node-recovery.service"
printf '{}\n' >"${archive}/deploy/recovery/managed-installations.dbb3.json"
printf '{}\n' >"${archive}/deploy/recovery/managed-installations.wsl.json"
printf '{"nodes":[]}\n' >"${archive}/deploy/recovery/managed-nodes.hk.json"
printf '#!/usr/bin/env bash\nexit 0\n' >"${archive}/deploy/recovery/recover-hk.sh"

cat >"${archive}/deploy/dbb3/install-dbb3-cloud-connector-user.sh" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
control="${2:-}"
role_home="${HERMES_CONNECTOR_HERMES_HOME:?missing isolated connector home}"
[[ "${DBB3_CONNECTOR_ARTIFACT_ROOTS:?missing isolated artifact root}" == "${role_home}" ]]
if [[ "${control}" == --rollback-backup=* ]]; then
  backup="${control#--rollback-backup=}"
  cp "${backup}/state" "${FAKE_CONNECTOR_STATE}"
  if [[ -f "${backup}/profile-home.absent" ]]; then
    rm -rf -- "${role_home}"
  fi
  printf 'connector rollback\n' >>"${FAKE_COMPONENT_LOG}"
  exit 0
fi
case "${HERMES_FABRIC_ROLE}" in
  dbb3)
    unit="$(dirname "${BASH_SOURCE[0]}")/dbb3-cloud-connector.service"
    profile_assets="$(dirname "${BASH_SOURCE[0]}")/profile"
    ;;
  wsl)
    unit="$(dirname "${BASH_SOURCE[0]}")/../pc/pc-cloud-connector.service"
    profile_assets="$(dirname "${BASH_SOURCE[0]}")/../pc/profile"
    ;;
  hk)
    unit="$(dirname "${BASH_SOURCE[0]}")/../hk/hk-cloud-connector.service"
    profile_assets="$(dirname "${BASH_SOURCE[0]}")/../hk/profile"
    ;;
esac
runtime_python="${HERMES_CONNECTOR_RUNTIME_PYTHON:-${HERMES_WSL_RUNTIME_PYTHON:-${HERMES_HK_RUNTIME_PYTHON:-}}}"
[[ -n "${runtime_python}" ]]
grep -Fq -- "${runtime_python}" "${unit}"
! grep -Fq -- '.fabric-current/.fabric-current' "${unit}"
backup="$(mktemp -d "${FAKE_BACKUP_ROOT}/connector.XXXXXX")"
cp "${FAKE_CONNECTOR_STATE}" "${backup}/state"
if [[ -d "${role_home}" ]]; then
  : >"${backup}/profile-home.present"
else
  : >"${backup}/profile-home.absent"
fi
mkdir -p "${role_home}/skills"
cp "${profile_assets}/config.yaml.example" "${role_home}/config.yaml"
cp "${profile_assets}/SOUL.md" "${role_home}/SOUL.md"
printf '%s\n' "${backup}" >"${control#--handle-file=}"
printf 'new\n' >"${FAKE_CONNECTOR_STATE}"
printf 'connector install\n' >>"${FAKE_COMPONENT_LOG}"
SH
cat >"${archive}/deploy/pc/install-pc-cloud-connector-user.sh" <<'SH'
#!/usr/bin/env bash
HERMES_CONNECTOR_HERMES_HOME="${PC_CONNECTOR_HERMES_HOME:?}" \
  DBB3_CONNECTOR_ARTIFACT_ROOTS="${PC_CONNECTOR_ARTIFACT_ROOTS:?}" \
  exec bash "$(dirname "${BASH_SOURCE[0]}")/../dbb3/install-dbb3-cloud-connector-user.sh" ignored "${1:-}"
SH
cat >"${archive}/deploy/hk/install-hk-cloud-connector-user.sh" <<'SH'
#!/usr/bin/env bash
HERMES_CONNECTOR_HERMES_HOME="${HK_CONNECTOR_HERMES_HOME:?}" \
  DBB3_CONNECTOR_ARTIFACT_ROOTS="${HK_CONNECTOR_ARTIFACT_ROOTS:?}" \
  exec bash "$(dirname "${BASH_SOURCE[0]}")/../dbb3/install-dbb3-cloud-connector-user.sh" ignored "${1:-}"
SH
cat >"${archive}/deploy/recovery/install-dbb3-managed-installation-receiver.sh" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
control="${2:-}"
if [[ "${control}" == --rollback-backup=* ]]; then
  cp "${control#--rollback-backup=}/state" "${FAKE_RECEIVER_STATE}"
  printf 'receiver rollback\n' >>"${FAKE_COMPONENT_LOG}"
  exit 0
fi
backup="$(mktemp -d "${FAKE_BACKUP_ROOT}/receiver.XXXXXX")"
cp "${FAKE_RECEIVER_STATE}" "${backup}/state"
printf '%s\n' "${backup}" >"${control#--handle-file=}"
printf 'new\n' >"${FAKE_RECEIVER_STATE}"
printf 'receiver install\n' >>"${FAKE_COMPONENT_LOG}"
SH
cat >"${archive}/deploy/recovery/install-wsl-managed-installation.sh" <<'SH'
#!/usr/bin/env bash
exec bash "$(dirname "${BASH_SOURCE[0]}")/install-dbb3-managed-installation-receiver.sh" ignored "${4:-}"
SH
cat >"${archive}/deploy/recovery/install-hk-managed-recovery.sh" <<'SH'
#!/usr/bin/env bash
exec bash "$(dirname "${BASH_SOURCE[0]}")/install-dbb3-managed-installation-receiver.sh" ignored "${5:-}"
SH
chmod 0755 "${archive}/deploy/dbb3/install-dbb3-cloud-connector-user.sh" \
  "${archive}/deploy/pc/install-pc-cloud-connector-user.sh" \
  "${archive}/deploy/hk/install-hk-cloud-connector-user.sh" \
  "${archive}/deploy/recovery/install-dbb3-managed-installation-receiver.sh" \
  "${archive}/deploy/recovery/install-wsl-managed-installation.sh" \
  "${archive}/deploy/recovery/install-hk-managed-recovery.sh" \
  "${archive}/deploy/recovery/recover-hk.sh"

release_commit="0123456789abcdef0123456789abcdef01234567"
release_version="1.2.3"
source_tree="$(printf '%040d' 7)"
run_case() {
  local role="$1" failpoint="${2:-}"
  local root="${work}/${role}-${failpoint:-success}"
  mkdir -p "${root}/runtime" "${root}/automation" \
    "${root}/home/.ssh" "${root}/home/.config/systemd/user" \
    "${root}/backups" "${root}/state"
  local old_generation="${root}/runtime/.fabric-generations/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  mkdir -p "${old_generation}"
  printf 'legacy runtime preserved\n' >"${root}/runtime/legacy-source.txt"
  for asset in update-fabric-node.sh hermes-fabric-update.service hermes-fabric-update.timer; do
    printf 'old automation\n' >"${root}/automation/${asset}"
  done
  printf 'old gateway\n' \
    >"${root}/home/.config/systemd/user/hermes-gateway-hk-worker.service"
  printf 'old\n' >"${root}/connector.state"
  printf 'old\n' >"${root}/receiver.state"
  printf '%064d\n' 0 >"${root}/cloud.token"
  printf '%064d\n' 1 >"${root}/installation.token"
  printf '%064d\n' 2 >"${root}/hk-recovery.token"
  printf 'test-hk-recovery-private-key\n' >"${root}/home/.ssh/hk_recovery_ed25519"
  printf '10.66.0.1 ssh-ed25519 test-public-key\n' >"${root}/home/.ssh/hk_recovery_known_hosts"
  printf 'test-private-key\n' >"${root}/home/.ssh/aliyun_hermes_ed25519"
  : >"${root}/components.log"
  : >"${root}/systemctl.log"
  : >"${root}/curl.count"
  case "${role}" in
    dbb3) fake_connector_id=dbb3-primary ;;
    wsl) fake_connector_id=pc-primary ;;
    hk) fake_connector_id=hk-primary ;;
  esac
  set +e
  PATH="${fake_bin}:/usr/sbin:/usr/bin:/sbin:/bin" \
  FAKE_ARCHIVE_ROOT="${archive}" \
  FAKE_RELEASE_COMMIT="${release_commit}" \
  FAKE_RELEASE_VERSION="${release_version}" \
  FAKE_CONNECTOR_ID="${fake_connector_id}" \
  FAKE_CURL_COUNT_FILE="${root}/curl.count" \
  FAKE_USER_HOME="${root}/home" \
  FAKE_BACKUP_ROOT="${root}/backups" \
  FAKE_CONNECTOR_STATE="${root}/connector.state" \
  FAKE_RECEIVER_STATE="${root}/receiver.state" \
  FAKE_COMPONENT_LOG="${root}/components.log" \
  FAKE_SYSTEMCTL_LOG="${root}/systemctl.log" \
  HERMES_FABRIC_ALLOW_TEST_PATHS=1 \
  HERMES_FABRIC_ROLE="${role}" \
  HERMES_FABRIC_STATE_ROOT="${root}/state/${role}" \
  HERMES_CLOUD_TOKEN_FILE="${root}/cloud.token" \
  HERMES_DBB3_AGENT_ROOT="${root}/runtime" \
  HERMES_DBB3_HOME="${root}/external-home" \
  HERMES_WSL_AGENT_ROOT="${root}/runtime" \
  HERMES_WSL_HOME="${root}/external-home" \
  HERMES_HK_AGENT_ROOT="${root}/runtime" \
  HERMES_WSL_INSTALLATION_TOKEN_FILE="${root}/installation.token" \
  HERMES_WSL_INSTALLATION_KEY_FILE="${root}/home/.ssh/aliyun_hermes_ed25519" \
  HERMES_HK_RECOVERY_TOKEN_FILE="${root}/hk-recovery.token" \
  HERMES_HK_RECOVERY_KEY_FILE="${root}/home/.ssh/hk_recovery_ed25519" \
  HERMES_HK_RECOVERY_KNOWN_HOSTS_FILE="${root}/home/.ssh/hk_recovery_known_hosts" \
  HERMES_FABRIC_AUTOMATION_SCRIPT_TARGET="${root}/automation/update-fabric-node.sh" \
  HERMES_FABRIC_AUTOMATION_SERVICE_TARGET="${root}/automation/hermes-fabric-update.service" \
  HERMES_FABRIC_AUTOMATION_TIMER_TARGET="${root}/automation/hermes-fabric-update.timer" \
  HERMES_FABRIC_FAILPOINT="${failpoint}" \
  bash "${repo}/deploy/automation/update-fabric-node.sh" \
    >"${root}/stdout" 2>"${root}/stderr"
  status=$?
  set -e

  if [[ -z "${failpoint}" && "${status}" != 0 ]]; then
    printf 'fabric updater success case failed for role=%s (exit=%s)\n' \
      "${role}" "${status}" >&2
    cat "${root}/stdout" >&2
    cat "${root}/stderr" >&2
  elif [[ -n "${failpoint}" && "${status}" == 0 ]]; then
    printf 'fabric updater failpoint unexpectedly succeeded for role=%s failpoint=%s\n' \
      "${role}" "${failpoint}" >&2
  fi

  if [[ -z "${failpoint}" ]]; then
    [[ "${status}" == 0 ]]
    generation="${root}/runtime/.fabric-generations/${release_commit}"
    [[ "$(readlink -f "${root}/runtime/.fabric-current")" == "${generation}" ]]
    [[ "$(cat "${generation}/.hermes-source-commit")" == "${release_commit}" ]]
    [[ "$(cat "${generation}/.hermes-source-tree")" == "${source_tree}" ]]
    [[ -f "${generation}/full-source-sentinel.txt" ]]
    [[ -f "${generation}/gateway/run.py" && -f "${generation}/run_agent.py" ]]
    [[ -f "${generation}/uv.lock" && -x "${generation}/.venv/bin/hermes" ]]
    grep -Fxq 'connector install' "${root}/components.log"
    grep -Fxq 'receiver install' "${root}/components.log"
    python3 - "${root}/state/${role}/release.json" \
      "${role}" "${release_commit}" "${release_version}" \
      "${source_tree}" "${generation}" <<'PY'
import json
import sys
assert json.load(open(sys.argv[1], encoding="utf-8")) == {
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
PY
    [[ "$(cat "${root}/state/${role}/deployed-commit")" == "${release_commit}" ]]
    [[ ! -e "${old_generation}" ]]
    [[ "$(cat "${root}/runtime/legacy-source.txt")" == "legacy runtime preserved" ]]
    if [[ "${role}" == hk ]]; then
      profile_root="${root}/home/.hermes/profiles/hk-worker"
      grep -Fq -- '--profile hk-worker gateway run' \
        "${root}/home/.config/systemd/user/hermes-gateway-hk-worker.service"
    else
      profile_root="${root}/external-home"
    fi
    [[ -f "${profile_root}/config.yaml" ]]
    [[ -f "${profile_root}/SOUL.md" ]]
    [[ -d "${profile_root}/skills" ]]
  else
    [[ "${status}" != 0 ]]
    [[ "$(cat "${root}/connector.state")" == old ]]
    [[ "$(cat "${root}/receiver.state")" == old ]]
    grep -Fxq 'receiver rollback' "${root}/components.log"
    grep -Fxq 'connector rollback' "${root}/components.log"
    [[ "$(cat "${root}/runtime/legacy-source.txt")" == "legacy runtime preserved" ]]
    [[ ! -e "${root}/runtime/.fabric-current" ]]
    [[ ! -e "${root}/runtime/.fabric-generations/${release_commit}" ]]
    [[ ! -e "${root}/state/${role}/release.json" ]]
    [[ ! -e "${root}/state/${role}/deployed-commit" ]]
    [[ -d "${old_generation}" ]]
    if [[ "${role}" == hk ]]; then
      profile_root="${root}/home/.hermes/profiles/hk-worker"
    else
      profile_root="${root}/external-home"
    fi
    [[ ! -e "${profile_root}" ]]
    if [[ "${failpoint}" == after-automation ]]; then
      for asset in update-fabric-node.sh hermes-fabric-update.service hermes-fabric-update.timer; do
        grep -Fxq 'old automation' "${root}/automation/${asset}"
      done
    fi
    if [[ "${role}" == hk ]]; then
      grep -Fxq 'old gateway' \
        "${root}/home/.config/systemd/user/hermes-gateway-hk-worker.service"
    fi
  fi
}

run_case dbb3
run_case wsl
run_case hk
run_case dbb3 after-receiver
run_case dbb3 after-automation
run_case dbb3 after-deployed
run_case hk after-gateway
run_case hk after-automation
printf 'fabric updater transaction harness passed\n'
