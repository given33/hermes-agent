#!/usr/bin/env bash
set -euo pipefail

remote="${HERMES_DBB3_REMOTE:-dbb3-hermes}"
version="2.1.24"
repo="${HERMES_REPO:-/mnt/d/Hermes/hermes-agent}"
stage="/home/hermes/.hermes/deploy/collaboration-${version}"
plugin_target="/usr/local/lib/hermes-agent/plugins/collaboration/dashboard"
web_target="/usr/local/lib/hermes-agent/hermes_cli/web_dist"

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

ssh "${remote}" bash -s -- "${version}" "${stage}" "${plugin_target}" "${web_target}" <<'REMOTE'
set -euo pipefail
version="$1"
stage="$2"
plugin_target="$3"
web_target="$4"
stamp="$(date +%Y%m%d-%H%M%S)"
backup="/home/hermes/.hermes/backups/collaboration-${version}-${stamp}"

python3 -m py_compile "${stage}/plugin/plugin_api.py"
python3 -c 'import json,sys; data=json.load(open(sys.argv[1])); assert data["version"] == sys.argv[2]' \
  "${stage}/plugin/manifest.json" "${version}"
node --check "${stage}/plugin/dist/index.js"

mkdir -p "${backup}"
cp -a "${plugin_target}" "${backup}/dashboard"
cp -a "${web_target}" "${backup}/web_dist"

install -m 0644 "${stage}/plugin/manifest.json" "${plugin_target}/manifest.json"
install -m 0755 "${stage}/plugin/plugin_api.py" "${plugin_target}/plugin_api.py"
install -m 0644 "${stage}/plugin/dist/index.js" "${plugin_target}/dist/index.js"
install -m 0644 "${stage}/plugin/dist/style.css" "${plugin_target}/dist/style.css"

if [[ -d "${web_target}/assets" ]]; then
  mv "${web_target}/assets" "${web_target}/assets.before-${stamp}"
fi
mkdir -p "${web_target}/assets"
cp -R "${stage}/web/assets/." "${web_target}/assets/"
for file in index.html manifest.webmanifest apple-touch-icon.png hermes-official.png favicon.ico; do
  if [[ -f "${stage}/web/${file}" ]]; then
    install -m 0644 "${stage}/web/${file}" "${web_target}/${file}"
  fi
done

sudo -n /usr/bin/systemctl restart hermes-dashboard.service

for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 http://127.0.0.1:9119/api/status >/tmp/hermes-dashboard-status.json; then
    break
  fi
  sleep 1
done

test -s /tmp/hermes-dashboard-status.json
python3 -c 'import json; assert json.load(open("/tmp/hermes-dashboard-status.json"))["auth_required"] is True'
grep -q "\"version\": \"${version}\"" "${plugin_target}/manifest.json"
grep -q 'hc-activity-timeline' "${plugin_target}/dist/style.css"
grep -q 'subagent.tool' "${plugin_target}/dist/index.js"
printf 'dashboard=active\nplugin_version=%s\nbackup=%s\n' "${version}" "${backup}"
REMOTE
