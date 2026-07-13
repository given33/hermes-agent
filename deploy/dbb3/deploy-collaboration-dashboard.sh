#!/usr/bin/env bash
set -euo pipefail

remote="${HERMES_DBB3_REMOTE:-dbb3-hermes}"
version="2.1.33"
repo="${HERMES_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
stage="/home/hermes/.hermes/deploy/collaboration-${version}"

ssh "${remote}" "mkdir -p '${stage}/plugin/dist' '${stage}/web'"
scp \
  "${repo}/plugins/collaboration/dashboard/manifest.json" \
  "${repo}/plugins/collaboration/dashboard/plugin_api.py" \
  "${remote}:${stage}/plugin/"
scp \
  "${repo}/plugins/collaboration/dashboard/dist/index.js" \
  "${repo}/plugins/collaboration/dashboard/dist/style.css" \
  "${remote}:${stage}/plugin/dist/"
scp -r "${repo}/hermes_cli/web_dist/." "${remote}:${stage}/web/"

ssh "${remote}" \
  "sudo -n /usr/local/sbin/hermes-install-collaboration-release '${version}' '${stage}'"
