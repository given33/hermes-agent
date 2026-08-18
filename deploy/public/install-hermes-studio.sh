#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Install and run the Hermes Studio web UI server (group chat + workflows +
# collaboration realtime) behind the existing nginx on the Hermes host.
#
# Usage: sudo bash install-hermes-studio.sh
# Requires: Node >= 23 (installed below via NodeSource when missing).

die() { printf 'install-hermes-studio: %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "must run as root"

studio_root="${HERMES_STUDIO_ROOT:-/opt/hermes-studio}"
studio_repository="${HERMES_STUDIO_REPOSITORY:-https://github.com/EKKOLearnAI/hermes-studio.git}"
# Pinned revision: reinstalls must produce the code this script was validated
# against, not whatever origin/HEAD happens to point at that day. Override
# only with an explicit full commit SHA after validating the new build.
studio_ref="${HERMES_STUDIO_REF:-4751fd36e3b6fde93e356b2c47b04bfe433722cc}"
studio_user="${HERMES_STUDIO_USER:-hermes}"
studio_port="${HERMES_STUDIO_PORT:-8647}"
nginx_conf="/etc/nginx/conf.d/hermes-studio.conf"

# 1. Node >= 23.
if ! command -v node >/dev/null 2>&1 || [[ "$(node --version 2>/dev/null | sed 's/^v//; s/\..*//')" -lt 23 ]]; then
  die "Node >= 23 is required; install it first (e.g. https://nodejs.org or nvm)"
fi
command -v npm >/dev/null 2>&1 || die "npm is required"

# 2. Clone or refresh the studio tree at the pinned revision.
if [[ ! -d "${studio_root}/.git" ]]; then
  install -d -o "${studio_user}" -g "${studio_user}" -m 0755 "$(dirname "${studio_root}")"
  sudo -u "${studio_user}" git clone --depth 1 "${studio_repository}" "${studio_root}"
  sudo -u "${studio_user}" git -C "${studio_root}" fetch --depth 1 origin "${studio_ref}"
  sudo -u "${studio_user}" git -C "${studio_root}" checkout -q -f "${studio_ref}"
else
  sudo -u "${studio_user}" git -C "${studio_root}" fetch --depth 1 origin "${studio_ref}"
  sudo -u "${studio_user}" git -C "${studio_root}" checkout -q -f "${studio_ref}"
fi

# 3. Build (dist/server + web assets).
cd "${studio_root}"
sudo -u "${studio_user}" npm ci --no-audit --no-fund || die "npm ci failed"
sudo -u "${studio_user}" npm run build || die "studio build failed"
[[ -f "dist/server/index.js" ]] || die "studio server bundle is missing"

# 4. Systemd service (unprivileged).
service_file="/etc/systemd/system/hermes-studio.service"
cat >"${service_file}" <<EOF
[Unit]
Description=Hermes Studio web UI server (group chat / workflows / collaboration)
After=network-online.target hermes-agent.service
Wants=network-online.target

[Service]
User=${studio_user}
WorkingDirectory=${studio_root}
Environment=NODE_ENV=production
Environment=PORT=${studio_port}
ExecStart=/usr/bin/env node dist/server/index.js
Restart=always
RestartSec=3
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now hermes-studio.service || die "studio service failed to start"
sleep 2
systemctl is-active --quiet hermes-studio.service || {
  journalctl -u hermes-studio --no-pager -n 20
  die "hermes-studio is not active"
}

# 5. Nginx: forward group chat REST + socket.io with WebSocket upgrade, and
# the collaboration/group-chat API under /api/hermes (auth stays on the
# studio side via the mobile bearer token).
cat >"${nginx_conf}" <<EOF
# Hermes Studio realtime + group-chat surfaces. The main /api/* surface and
# the dashboard stay on the FastAPI backend; only the Studio-owned paths are
# forwarded here.
location ~ ^/socket\.io/ {
    proxy_pass http://127.0.0.1:${studio_port};
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 120s;
}

location ^~ /api/hermes/ {
    proxy_pass http://127.0.0.1:${studio_port};
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
}

location = /workflow {
    proxy_pass http://127.0.0.1:${studio_port};
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 120s;
}
EOF
nginx -t || die "nginx config test failed"
systemctl reload nginx || die "nginx reload failed"

printf 'install-hermes-studio: ok (root=%s port=%s)\n' "${studio_root}" "${studio_port}"
