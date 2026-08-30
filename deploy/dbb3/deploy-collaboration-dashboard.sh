#!/usr/bin/env bash
set -euo pipefail

remote="${HERMES_DBB3_REMOTE:-dbb3-hermes}"
version="2.1.49"
repo="${HERMES_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
dbb3_home="${HERMES_DBB3_HOME:-/home/hermes/.hermes/profiles/dbb3-worker}"
[[ "${dbb3_home}" =~ ^/[A-Za-z0-9._/-]+$ && ! -L "${dbb3_home}" ]] \
  || { printf 'unsafe DBB3 worker Hermes home: %s\n' "${dbb3_home}" >&2; exit 1; }
stage="${dbb3_home}/deploy/collaboration-${version}"

ssh "${remote}" \
  "mkdir -p '${stage}/plugin/dist' '${stage}/core/hermes_cli' '${stage}/web' '${dbb3_home}/runtime'"
scp \
  "${repo}/plugins/collaboration/dashboard/manifest.json" \
  "${repo}/plugins/collaboration/dashboard/plugin_api.py" \
  "${remote}:${stage}/plugin/"
scp \
  "${repo}/plugins/collaboration/dashboard/dist/index.js" \
  "${repo}/plugins/collaboration/dashboard/dist/style.css" \
  "${remote}:${stage}/plugin/dist/"
scp \
  "${repo}/hermes_cli/cloud_file_library.py" \
  "${remote}:${stage}/core/hermes_cli/"
scp \
  "${repo}/hermes_cli/cloud_file_library.py" \
  "${remote}:${dbb3_home}/runtime/cloud_file_library.py"
ssh "${remote}" "chmod 0600 '${dbb3_home}/runtime/cloud_file_library.py'"
scp -r "${repo}/hermes_cli/web_dist/." "${remote}:${stage}/web/"

ssh "${remote}" \
  "sudo -n /usr/local/sbin/hermes-install-collaboration-release '${version}' '${stage}'"
