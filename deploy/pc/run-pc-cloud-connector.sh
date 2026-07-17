#!/usr/bin/env bash
set -euo pipefail
umask 077

state_root="${PC_CONNECTOR_STATE_ROOT:-/home/hermes/.local/state/pc-cloud-connector}"
source_file="${PC_CONNECTOR_SOURCE:-/opt/pc-team/pc_cloud_connector.py}"
token_file="${HERMES_CLOUD_TOKEN_FILE:-/etc/pc-team/cloud_connector_token}"
cloud_url="${HERMES_CLOUD_URL:-https://daxueshenmai.top/api/plugins/collaboration}"
hermes_home="${HERMES_HOME:-/mnt/d/Hermes/home}"

mkdir -p "${state_root}"
chmod 0700 "${state_root}"
exec 9>"${state_root}/connector.lock"
flock -n 9 || exit 0

export HERMES_HOME="${hermes_home}"
export HERMES_CLOUD_URL="${cloud_url}"
export HERMES_CLOUD_TOKEN_FILE="${token_file}"
export DBB3_CONNECTOR_ID="pc-primary"
export DBB3_CONNECTOR_STATE_FILE="${state_root}/checkpoint.json"
export DBB3_CONNECTOR_ARTIFACT_ROOTS="${hermes_home}:/home/hermes/.hermes"

exec /usr/bin/python3 "${source_file}" \
  --interval 2 \
  --quiet \
  --state-file "${state_root}/checkpoint.json" \
  >>"${state_root}/connector.log" 2>&1
