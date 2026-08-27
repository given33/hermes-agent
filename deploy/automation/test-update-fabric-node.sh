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
  "${archive}/deploy/dbb3" "${archive}/deploy/pc" \
  "${archive}/deploy/hk/profile" \
  "${archive}/deploy/recovery" "${archive}/hermes_cli" \
  "${archive}/hermes_runtime" "${archive}/hermes_services"

cat >"${fake_bin}/git" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "${1:-}" == clone && "${2:-}" == --mirror ]]; then
  mkdir -p "${@: -1}"
  exit 0
fi
if [[ "$*" == *" archive --format=tar "* ]]; then
  paths=()
  after_separator=0
  for argument in "$@"; do
    if (( after_separator )); then
      paths+=("${argument}")
    elif [[ "${argument}" == -- ]]; then
      after_separator=1
    fi
  done
  tar -C "${FAKE_ARCHIVE_ROOT}" -cf - -- "${paths[@]}"
  exit 0
fi
exit 0
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
payload="{\"ok\":true,\"release\":{\"commit\":\"${FAKE_RELEASE_COMMIT}\",\"version\":\"${FAKE_RELEASE_VERSION}\"}}"
if [[ -n "${output}" ]]; then printf '%s\n' "${payload}" >"${output}"
else printf '%s\n' "${payload}"
fi
SH
cat >"${fake_bin}/systemctl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FAKE_SYSTEMCTL_LOG}"
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
if [[ "${1:-}" == -u ]]; then printf '0\n'; fi
exit 0
SH
cat >"${fake_bin}/flock" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod 0755 "${fake_bin}"/*

for asset in update-fabric-node.sh hermes-fabric-update.service hermes-fabric-update.timer; do
  printf '# verified automation asset\n' >"${archive}/deploy/automation/${asset}"
done
printf '# connector source\n' >"${archive}/deploy/dbb3/dbb3_cloud_connector.py"
printf '# connector unit\n' >"${archive}/deploy/dbb3/dbb3-cloud-connector.service"
printf '# connector unit\n' >"${archive}/deploy/pc/pc-cloud-connector.service"
printf '# connector unit\n' >"${archive}/deploy/hk/hk-cloud-connector.service"
printf '#!/usr/bin/env bash\nexec bash "$(dirname "${BASH_SOURCE[0]}")/../dbb3/install-dbb3-cloud-connector-user.sh" ignored "${1:-}"\n' >"${archive}/deploy/hk/install-hk-cloud-connector-user.sh"
printf 'profile: hk-worker\n' >"${archive}/deploy/hk/profile/config.yaml.example"
printf '# HK worker\n' >"${archive}/deploy/hk/profile/SOUL.md"
for asset in hermes-managed-installation-receiver.service \
  hermes-wsl-managed-installation-receiver.service \
  hermes-wsl-managed-installation-tunnel.service; do
  printf '# receiver unit\n' >"${archive}/deploy/recovery/${asset}"
done
printf '{}\n' >"${archive}/deploy/recovery/managed-installations.dbb3.json"
printf '{}\n' >"${archive}/deploy/recovery/managed-installations.wsl.json"
for module in __init__.py managed_installations.py managed_nodes.py managed_node_recovery_service.py sqlite_util.py; do
  printf 'RELEASE = "new"\n' >"${archive}/hermes_cli/${module}"
done
for module in __init__.py config.py; do
  printf 'RELEASE = "new"\n' >"${archive}/hermes_runtime/${module}"
done
for module in __init__.py resource_catalog.py; do
  printf 'RELEASE = "new"\n' >"${archive}/hermes_services/${module}"
done
printf 'RELEASE = "new"\n' >"${archive}/hermes_constants.py"
printf 'RELEASE = "new"\n' >"${archive}/hermes_auth_errors.py"
printf 'RELEASE = "new"\n' >"${archive}/hermes_secret_compare.py"
printf 'RELEASE = "new"\n' >"${archive}/utils.py"

cat >"${archive}/deploy/dbb3/install-dbb3-cloud-connector-user.sh" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
control="${2:-}"
if [[ "${control}" == --rollback-backup=* ]]; then
  cp "${control#--rollback-backup=}/state" "${FAKE_CONNECTOR_STATE}"
  printf 'connector rollback\n' >>"${FAKE_COMPONENT_LOG}"
  exit 0
fi
backup="$(mktemp -d "${FAKE_BACKUP_ROOT}/connector.XXXXXX")"
cp "${FAKE_CONNECTOR_STATE}" "${backup}/state"
printf '%s\n' "${backup}" >"${control#--handle-file=}"
printf 'new\n' >"${FAKE_CONNECTOR_STATE}"
printf 'connector install\n' >>"${FAKE_COMPONENT_LOG}"
SH
cat >"${archive}/deploy/pc/install-pc-cloud-connector-user.sh" <<'SH'
#!/usr/bin/env bash
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
set -Eeuo pipefail
control="${4:-}"
exec bash "$(dirname "${BASH_SOURCE[0]}")/install-dbb3-managed-installation-receiver.sh" ignored "${control}"
SH
chmod 0755 "${archive}/deploy/dbb3/install-dbb3-cloud-connector-user.sh" \
  "${archive}/deploy/pc/install-pc-cloud-connector-user.sh" \
  "${archive}/deploy/hk/install-hk-cloud-connector-user.sh" \
  "${archive}/deploy/recovery/install-dbb3-managed-installation-receiver.sh" \
  "${archive}/deploy/recovery/install-wsl-managed-installation.sh"

release_commit="0123456789abcdef0123456789abcdef01234567"
release_version="1.2.3"
run_case() {
  local role="$1" failpoint="${2:-}"
  local root="${work}/${role}-${failpoint:-success}"
  mkdir -p "${root}/runtime/hermes_cli" "${root}/runtime/hermes_runtime" \
    "${root}/runtime/hermes_services" "${root}/automation" \
    "${root}/home/.ssh" "${root}/backups" "${root}/state"
  for module in __init__.py managed_installations.py managed_nodes.py managed_node_recovery_service.py sqlite_util.py; do
    printf 'RELEASE = "old"\n' >"${root}/runtime/hermes_cli/${module}"
  done
  for module in __init__.py config.py; do
    printf 'RELEASE = "old"\n' >"${root}/runtime/hermes_runtime/${module}"
  done
  for module in __init__.py resource_catalog.py; do
    printf 'RELEASE = "old"\n' >"${root}/runtime/hermes_services/${module}"
  done
  printf 'RELEASE = "old"\n' >"${root}/runtime/hermes_constants.py"
  printf 'RELEASE = "old"\n' >"${root}/runtime/hermes_auth_errors.py"
  printf 'RELEASE = "old"\n' >"${root}/runtime/hermes_secret_compare.py"
  printf 'RELEASE = "old"\n' >"${root}/runtime/utils.py"
  for asset in update-fabric-node.sh hermes-fabric-update.service hermes-fabric-update.timer; do
    printf 'old automation\n' >"${root}/automation/${asset}"
  done
  printf 'old\n' >"${root}/connector.state"
  printf 'old\n' >"${root}/receiver.state"
  printf '%064d\n' 0 >"${root}/cloud.token"
  printf '%064d\n' 1 >"${root}/installation.token"
  printf 'test-private-key\n' >"${root}/home/.ssh/aliyun_hermes_ed25519"
  : >"${root}/components.log"
  : >"${root}/systemctl.log"
  set +e
  PATH="${fake_bin}:/usr/sbin:/usr/bin:/sbin:/bin" \
  FAKE_ARCHIVE_ROOT="${archive}" \
  FAKE_RELEASE_COMMIT="${release_commit}" \
  FAKE_RELEASE_VERSION="${release_version}" \
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
  HERMES_WSL_AGENT_ROOT="${root}/runtime" \
  HERMES_HK_AGENT_ROOT="${root}/runtime" \
  HERMES_WSL_INSTALLATION_TOKEN_FILE="${root}/installation.token" \
  HERMES_WSL_INSTALLATION_KEY_FILE="${root}/home/.ssh/aliyun_hermes_ed25519" \
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
    cat "${root}/stdout" >&2
    cat "${root}/stderr" >&2
  fi

  if [[ -z "${failpoint}" ]]; then
    [[ "${status}" == 0 ]]
    grep -Fxq 'connector install' "${root}/components.log"
    if [[ "${role}" != hk ]]; then
      grep -Fxq 'receiver install' "${root}/components.log"
    fi
    python3 - "${root}/state/${role}/release.json" \
      "${role}" "${release_commit}" "${release_version}" <<'PY'
import json, sys
assert json.load(open(sys.argv[1], encoding="utf-8")) == {
    "schema": "hermes.fabric-release.v1",
    "node_id": sys.argv[2],
    "commit": sys.argv[3],
    "version": sys.argv[4],
}
PY
    [[ "$(cat "${root}/state/${role}/deployed-commit")" == "${release_commit}" ]]
    grep -Fxq 'RELEASE = "new"' "${root}/runtime/hermes_runtime/config.py"
    grep -Fxq 'RELEASE = "new"' "${root}/runtime/hermes_services/resource_catalog.py"
    grep -Fxq 'RELEASE = "new"' "${root}/runtime/utils.py"
  else
    [[ "${status}" != 0 ]]
    [[ "$(cat "${root}/connector.state")" == old ]]
    [[ "$(cat "${root}/receiver.state")" == old ]]
    if [[ "${role}" != hk ]]; then
      grep -Fxq 'receiver rollback' "${root}/components.log"
    fi
    grep -Fxq 'connector rollback' "${root}/components.log"
    grep -Fq 'RELEASE = "old"' "${root}/runtime/hermes_cli/managed_installations.py"
    grep -Fq 'RELEASE = "old"' "${root}/runtime/hermes_runtime/config.py"
    grep -Fq 'RELEASE = "old"' "${root}/runtime/hermes_services/resource_catalog.py"
    grep -Fq 'RELEASE = "old"' "${root}/runtime/utils.py"
    if [[ "${failpoint}" == after-automation ]]; then
      for asset in update-fabric-node.sh hermes-fabric-update.service hermes-fabric-update.timer; do
        grep -Fxq 'old automation' "${root}/automation/${asset}"
      done
    fi
    [[ ! -e "${root}/state/${role}/release.json" ]]
    [[ ! -e "${root}/state/${role}/deployed-commit" ]]
  fi
}

run_case dbb3
run_case wsl
run_case hk
run_case dbb3 after-receiver
run_case dbb3 after-automation
printf 'fabric updater transaction harness passed\n'
