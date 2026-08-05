#!/usr/bin/env bash
set -Eeuo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
installer="${here}/install-dbb3-cloud-connector-user.sh"
source_file="${here}/dbb3_cloud_connector.py"
unit_template="${here}/dbb3-cloud-connector.service"

[[ "$(id -u)" == 0 ]] || {
  printf 'rollback harness must run as root\n' >&2
  exit 1
}

make_fakes() {
  local root="$1"
  local fakebin="${root}/fakebin"
  mkdir -p "${fakebin}"

  cat >"${fakebin}/runuser" <<'SH'
#!/usr/bin/env bash
set -e
[[ "$1" == "-u" ]] && shift 2
[[ "${1:-}" == "--" ]] && shift
for argument in "$@"; do
  [[ "${argument}" == "--probe" ]] && exit 0
done
exec "$@"
SH

  cat >"${fakebin}/getent" <<'SH'
#!/usr/bin/env bash
set -e
if [[ "${1:-}" == "passwd" ]]; then
  printf 'root:x:0:0:root:%s:/bin/bash\n' "${FAKE_USER_HOME}"
  exit 0
fi
exec /usr/bin/getent "$@"
SH

  cat >"${fakebin}/loginctl" <<'SH'
#!/usr/bin/env bash
set -e
case "${1:-}" in
  show-user)
    [[ "$(cat "${FAKE_SYSTEMD_STATE_DIR}/linger")" == "1" ]] && printf 'yes\n' || printf 'no\n'
    ;;
  enable-linger)
    printf '1\n' >"${FAKE_SYSTEMD_STATE_DIR}/linger"
    ;;
  disable-linger)
    printf '0\n' >"${FAKE_SYSTEMD_STATE_DIR}/linger"
    ;;
  *) exit 2 ;;
esac
SH

  cat >"${fakebin}/systemctl" <<'SH'
#!/usr/bin/env bash
set -e
scope=root
args=()
for argument in "$@"; do
  if [[ "${argument}" == "--user" ]]; then
    scope=user
  else
    args+=("${argument}")
  fi
done
command="${args[0]:-}"
active="${FAKE_SYSTEMD_STATE_DIR}/${scope}.active"
enabled="${FAKE_SYSTEMD_STATE_DIR}/${scope}.enabled"
fail_marker="${FAKE_SYSTEMD_STATE_DIR}/failed.${INSTALL_FAIL_STAGE:-none}"
case "${command}" in
  is-active)
    [[ "$(cat "${active}")" == "1" ]]
    ;;
  is-enabled)
    [[ "$(cat "${enabled}")" == "1" ]]
    ;;
  disable)
    printf '0\n' >"${enabled}"
    for argument in "${args[@]}"; do
      [[ "${argument}" == "--now" ]] && printf '0\n' >"${active}"
    done
    if [[ "${scope}" == "root" && "${INSTALL_FAIL_STAGE:-}" == "root-stop" && ! -e "${fail_marker}" ]]; then
      : >"${fail_marker}"
      exit 71
    fi
    ;;
  enable)
    printf '1\n' >"${enabled}"
    ;;
  restart)
    printf '1\n' >"${active}"
    if [[ "${scope}" == "user" && "${INSTALL_FAIL_STAGE:-}" == "user-start" && ! -e "${fail_marker}" ]]; then
      : >"${fail_marker}"
      printf '0\n' >"${active}"
      exit 72
    fi
    ;;
  start)
    printf '1\n' >"${active}"
    ;;
  stop)
    printf '0\n' >"${active}"
    ;;
  daemon-reload|status)
    ;;
  *)
    printf 'unexpected fake systemctl command: %s\n' "${args[*]}" >&2
    exit 2
    ;;
esac
SH

  cat >"${fakebin}/mv" <<'SH'
#!/usr/bin/env bash
set -e
destination="${@: -1}"
/usr/bin/mv "$@"
case "${INSTALL_FAIL_STAGE:-}" in
  source-replace) match='/dbb3_cloud_connector.py' ;;
  env-replace) match='/cloud_connector.env' ;;
  unit-replace) match='/dbb3-cloud-connector.service' ;;
  *) match='' ;;
esac
marker="${FAKE_SYSTEMD_STATE_DIR}/failed.${INSTALL_FAIL_STAGE:-none}"
if [[ -n "${match}" && "${destination}" == *"${match}" && ! -e "${marker}" ]]; then
  : >"${marker}"
  exit 73
fi
SH

  cat >"${fakebin}/sleep" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  chmod 0755 "${fakebin}"/*
}

run_case() {
  local stage="$1"
  local injected_stage="${stage}"
  local files_were_present=1
  if [[ "${stage}" == "first-install-user-start" ]]; then
    injected_stage="user-start"
    files_were_present=0
  fi
  local root
  root="$(mktemp -d)"
  trap 'rm -rf -- "${root}"' RETURN
  mkdir -p \
    "${root}/home/.config/dbb3-team" \
    "${root}/home/.config/systemd/user" \
    "${root}/home/.local/state/dbb3-cloud-connector" \
    "${root}/opt" \
    "${root}/etc" \
    "${root}/backups" \
    "${root}/systemd"
  if (( files_were_present )); then
    printf 'old-source\n' >"${root}/opt/dbb3_cloud_connector.py"
    printf 'old-env\n' >"${root}/home/.config/dbb3-team/cloud_connector.env"
    printf 'old-unit\n' >"${root}/home/.config/systemd/user/dbb3-cloud-connector.service"
  fi
  printf 'test-token\n' >"${root}/etc/cloud_connector_token"
  printf '1\n' >"${root}/systemd/root.active"
  printf '1\n' >"${root}/systemd/root.enabled"
  printf '0\n' >"${root}/systemd/user.active"
  printf '0\n' >"${root}/systemd/user.enabled"
  printf '0\n' >"${root}/systemd/linger"
  chmod 0644 "${root}/etc/cloud_connector_token"
  make_fakes "${root}"

  set +e
  PATH="${root}/fakebin:/usr/sbin:/usr/bin:/sbin:/bin" \
  FAKE_USER_HOME="${root}/home" \
  FAKE_SYSTEMD_STATE_DIR="${root}/systemd" \
  INSTALL_FAIL_STAGE="${injected_stage}" \
  DBB3_CONNECTOR_USER=root \
  HERMES_CLOUD_TOKEN_FILE="${root}/etc/cloud_connector_token" \
  DBB3_CONNECTOR_SOURCE_TARGET="${root}/opt/dbb3_cloud_connector.py" \
  DBB3_CONNECTOR_UNIT_TEMPLATE="${unit_template}" \
  DBB3_CONNECTOR_BACKUP_ROOT="${root}/backups" \
  bash "${installer}" "${source_file}" >"${root}/stdout" 2>"${root}/stderr"
  result=$?
  set -e

  if [[ "${stage}" == "success" ]]; then
    [[ ${result} -eq 0 ]] || {
      cat "${root}/stderr" >&2
      return 1
    }
    ! grep -Fxq 'old-source' "${root}/opt/dbb3_cloud_connector.py"
    grep -Fq 'HERMES_CLOUD_URL=' "${root}/home/.config/dbb3-team/cloud_connector.env"
    cmp -s "${unit_template}" "${root}/home/.config/systemd/user/dbb3-cloud-connector.service"
    grep -Fxq '0' "${root}/systemd/root.active"
    grep -Fxq '0' "${root}/systemd/root.enabled"
    grep -Fxq '1' "${root}/systemd/user.active"
    grep -Fxq '1' "${root}/systemd/user.enabled"
    grep -Fxq '1' "${root}/systemd/linger"
    ! grep -Fq 'rollback complete' "${root}/stderr"
    trap - RETURN
    rm -rf -- "${root}"
    return
  fi

  [[ ${result} -ne 0 ]] || {
    printf 'failure stage %s unexpectedly succeeded\n' "${stage}" >&2
    return 1
  }
  if (( files_were_present )); then
    grep -Fxq 'old-source' "${root}/opt/dbb3_cloud_connector.py"
    grep -Fxq 'old-env' "${root}/home/.config/dbb3-team/cloud_connector.env"
    grep -Fxq 'old-unit' "${root}/home/.config/systemd/user/dbb3-cloud-connector.service"
  else
    [[ ! -e "${root}/opt/dbb3_cloud_connector.py" ]]
    [[ ! -e "${root}/home/.config/dbb3-team/cloud_connector.env" ]]
    [[ ! -e "${root}/home/.config/systemd/user/dbb3-cloud-connector.service" ]]
  fi
  grep -Fxq '1' "${root}/systemd/root.active"
  grep -Fxq '1' "${root}/systemd/root.enabled"
  grep -Fxq '0' "${root}/systemd/user.active"
  grep -Fxq '0' "${root}/systemd/user.enabled"
  grep -Fxq '0' "${root}/systemd/linger"
  grep -Fq 'rollback complete' "${root}/stderr"
  ! find "${root}" -type f -name '*.new.*' -print -quit | grep -q .
  trap - RETURN
  rm -rf -- "${root}"
}

for stage in success source-replace env-replace unit-replace root-stop user-start first-install-user-start; do
  run_case "${stage}"
done
