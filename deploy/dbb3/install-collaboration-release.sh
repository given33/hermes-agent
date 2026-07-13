#!/usr/bin/env bash
set -euo pipefail
umask 077

release_snapshot=""
health_file=""
cleanup() {
  if [[ -n "${health_file}" ]]; then
    rm -f -- "${health_file}"
  fi
  case "${release_snapshot}" in
    /run/hermes-collaboration-release.*)
      rm -rf -- "${release_snapshot}"
      ;;
  esac
}
trap cleanup EXIT

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This release installer must run as root." >&2
  exit 1
fi

version="${1:-}"
stage="${2:-}"
if [[ ! "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Invalid collaboration release version." >&2
  exit 1
fi

expected_stage="/home/hermes/.hermes/deploy/collaboration-${version}"
if [[ "${stage}" != "${expected_stage}" ]]; then
  echo "Release stage must be ${expected_stage}." >&2
  exit 1
fi
stage="$(realpath -e -- "${stage}")"
if [[ "${stage}" != "${expected_stage}" ]]; then
  echo "Release stage resolved outside the allowed directory." >&2
  exit 1
fi
if [[ "$(stat -c '%U' "${stage}")" != "hermes" ]]; then
  echo "Release stage must be owned by hermes." >&2
  exit 1
fi

required_files=(
  "plugin/manifest.json"
  "plugin/plugin_api.py"
  "plugin/dist/index.js"
  "plugin/dist/style.css"
  "web/index.html"
)
required_directories=("web/assets")
for relative in "${required_files[@]}"; do
  source_file="${stage}/${relative}"
  if [[ ! -f "${source_file}" || -L "${source_file}" ]]; then
    echo "Missing or unsafe release file: ${relative}" >&2
    exit 1
  fi
done
for relative in "${required_directories[@]}"; do
  source_dir="${stage}/${relative}"
  if [[ ! -d "${source_dir}" || -L "${source_dir}" ]]; then
    echo "Missing or unsafe release directory: ${relative}" >&2
    exit 1
  fi
done

# Root never installs directly from a directory the hermes user can mutate.
# The archive is read with hermes privileges, then extracted into a private,
# root-owned snapshot so a symlink swap cannot expose privileged files.
archive_paths=("${required_files[@]}" "${required_directories[@]}")
for file in manifest.webmanifest apple-touch-icon.png hermes-official.png favicon.ico; do
  if [[ -f "${stage}/web/${file}" && ! -L "${stage}/web/${file}" ]]; then
    archive_paths+=("web/${file}")
  fi
done
release_snapshot="$(mktemp -d /run/hermes-collaboration-release.XXXXXX)"
if ! /usr/bin/setpriv \
  --reuid=hermes --regid=hermes --init-groups -- \
  /usr/bin/tar -C "${stage}" -cf - -- "${archive_paths[@]}" \
  | /usr/bin/tar --no-same-owner -C "${release_snapshot}" -xf -; then
  echo "Failed to create a private collaboration release snapshot." >&2
  exit 1
fi
unsafe_entry="$(find "${release_snapshot}" ! -type d ! -type f -print -quit)"
if [[ -n "${unsafe_entry}" ]]; then
  echo "Unsafe release entry: ${unsafe_entry}" >&2
  exit 1
fi
for relative in "${required_files[@]}"; do
  snapshot_file="${release_snapshot}/${relative}"
  if [[ ! -f "${snapshot_file}" || -L "${snapshot_file}" ]]; then
    echo "Missing or unsafe snapshot file: ${relative}" >&2
    exit 1
  fi
done

python3 -c 'import json,sys; data=json.load(open(sys.argv[1])); assert data["version"] == sys.argv[2]' \
  "${release_snapshot}/plugin/manifest.json" "${version}"
python3 -c 'import pathlib,sys; source=pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"); compile(source, sys.argv[1], "exec")' \
  "${release_snapshot}/plugin/plugin_api.py"
node --check "${release_snapshot}/plugin/dist/index.js"

plugin_target="/usr/local/lib/hermes-agent/plugins/collaboration/dashboard"
web_target="/usr/local/lib/hermes-agent/hermes_cli/web_dist"
stamp="$(date +%Y%m%d-%H%M%S)"
backup="/home/hermes/.hermes/backups/collaboration-${version}-${stamp}"

mkdir -p "${backup}"
cp -a "${plugin_target}" "${backup}/dashboard"
cp -a "${web_target}" "${backup}/web_dist"

install -m 0644 "${release_snapshot}/plugin/manifest.json" "${plugin_target}/manifest.json"
install -m 0755 "${release_snapshot}/plugin/plugin_api.py" "${plugin_target}/plugin_api.py"
install -m 0644 "${release_snapshot}/plugin/dist/index.js" "${plugin_target}/dist/index.js"
install -m 0644 "${release_snapshot}/plugin/dist/style.css" "${plugin_target}/dist/style.css"

if [[ -d "${web_target}/assets" ]]; then
  mv "${web_target}/assets" "${web_target}/assets.before-${stamp}"
fi
install -d -o root -g root -m 0755 "${web_target}/assets"
cp -R --no-preserve=mode,ownership \
  "${release_snapshot}/web/assets/." "${web_target}/assets/"
find "${web_target}/assets" -type d -exec chmod 0755 {} +
find "${web_target}/assets" -type f -exec chmod 0644 {} +
chown -R root:root "${web_target}/assets"
for file in manifest.webmanifest apple-touch-icon.png hermes-official.png favicon.ico; do
  if [[ -f "${release_snapshot}/web/${file}" ]]; then
    install -m 0644 "${release_snapshot}/web/${file}" "${web_target}/${file}"
  fi
done
install -m 0644 "${release_snapshot}/web/index.html" "${web_target}/index.html"

/usr/bin/systemctl restart hermes-dashboard.service
health_file="$(mktemp /run/hermes-dashboard-status.XXXXXX.json)"
for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 http://127.0.0.1:9119/api/status \
    >"${health_file}"; then
    break
  fi
  sleep 1
done

test -s "${health_file}"
python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["auth_required"] is True' \
  "${health_file}"
grep -q "\"version\": \"${version}\"" "${plugin_target}/manifest.json"
printf 'dashboard=active\nplugin_version=%s\nbackup=%s\n' "${version}" "${backup}"
