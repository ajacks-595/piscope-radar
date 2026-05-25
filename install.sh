#!/usr/bin/env bash
# PiScope Radar installer for Raspberry Pi (Debian / Raspberry Pi OS).
#
# Usage:
#   sudo bash install.sh            # install or update
#   sudo bash install.sh --uninstall # remove everything
#
# Environment overrides:
#   SERVICE_USER     user to run the service as (default: pi, falls back to current sudo-er)
#   SERVICE_PORT     uvicorn port behind nginx (default: 8765)
#   INSTALL_DIR      where to install (default: /opt/piscope)
#   ENABLE_NGINX_SITE  set to 0 to skip auto-enabling the standalone nginx site
#
# Re-running this script is safe — it upgrades files in INSTALL_DIR and restarts the service.
# Your piscope.db is backed up to piscope.db.bak before each upgrade.

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/piscope}"
SERVICE_PORT="${SERVICE_PORT:-8765}"
ENABLE_NGINX_SITE="${ENABLE_NGINX_SITE:-1}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Figure out the service user automatically: prefer `pi` if it exists, otherwise the
# user running sudo (avoids running as root by default).
if [[ -z "${SERVICE_USER:-}" ]]; then
  if id pi >/dev/null 2>&1; then
    SERVICE_USER="pi"
  elif [[ -n "${SUDO_USER:-}" ]] && [[ "${SUDO_USER}" != "root" ]]; then
    SERVICE_USER="${SUDO_USER}"
  else
    SERVICE_USER="root"
  fi
fi

log()  { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; }

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    err "Please run as root (sudo bash install.sh)."
    exit 1
  fi
}

require_debian_like() {
  if [[ ! -f /etc/debian_version ]]; then
    warn "This script targets Debian / Raspberry Pi OS. Detected: $(uname -a)"
    warn "Continuing, but apt-based dependency installs may fail."
  fi
}

uninstall() {
  require_root
  log "Stopping and disabling piscope.service"
  systemctl stop piscope.service skywatch-web.service 2>/dev/null || true
  systemctl disable piscope.service skywatch-web.service 2>/dev/null || true
  rm -f /etc/systemd/system/piscope.service /etc/systemd/system/skywatch-web.service
  systemctl daemon-reload
  log "Removing reverse-proxy site (nginx and/or lighttpd)"
  # nginx
  rm -f /etc/nginx/sites-enabled/piscope \
        /etc/nginx/sites-enabled/piscope-standalone \
        /etc/nginx/sites-enabled/skywatch \
        /etc/nginx/sites-enabled/skywatch-standalone \
        /etc/nginx/sites-available/piscope \
        /etc/nginx/sites-available/piscope-standalone \
        /etc/nginx/sites-available/skywatch \
        /etc/nginx/sites-available/skywatch-standalone
  nginx -t >/dev/null 2>&1 && systemctl reload nginx || warn "nginx reload skipped"
  # lighttpd
  if dpkg -s lighttpd >/dev/null 2>&1; then
    rm -f /etc/lighttpd/conf-enabled/99-piscope.conf \
          /etc/lighttpd/conf-enabled/99-skywatch.conf \
          /etc/lighttpd/conf-available/99-piscope.conf \
          /etc/lighttpd/conf-available/99-skywatch.conf
    systemctl reload lighttpd 2>/dev/null || true
  fi
  log "Removing /usr/local/bin/piscope"
  rm -f /usr/local/bin/piscope /usr/local/bin/skywatch
  if [[ -d "${INSTALL_DIR}" ]]; then
    warn "Leaving ${INSTALL_DIR} on disk (contains your database)."
    warn "Delete it manually with:  sudo rm -rf ${INSTALL_DIR}"
  fi
  ok "PiScope Radar uninstalled."
  exit 0
}

if [[ "${1:-}" == "--uninstall" ]]; then
  uninstall
fi

require_root
require_debian_like

# Make sure the service user actually exists.
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  err "Service user '${SERVICE_USER}' does not exist. Re-run with SERVICE_USER=<existing-user> sudo bash install.sh"
  exit 1
fi
log "Installing as user: ${SERVICE_USER}"

# ----- 1. Detect existing web server ----------------------------------------
# PiAware (and many off-the-shelf Pi ADS-B images) already run lighttpd on port 80.
# If we find it, we'll configure PiScope Radar as a lighttpd proxy and skip nginx entirely.
# Otherwise we fall through to installing+configuring nginx as a standalone vhost.
WEB_SERVER=""
if dpkg -s lighttpd >/dev/null 2>&1; then
  WEB_SERVER="lighttpd"
  log "Detected lighttpd already installed — will use it as the reverse proxy."
else
  WEB_SERVER="nginx"
  log "No lighttpd found — will install and configure nginx."
fi

# ----- 1. Dependencies -------------------------------------------------------
log "Installing system dependencies"
apt-get update -y
# We deliberately do NOT install python3-dev. All of our pip deps (fastapi / uvicorn /
# httpx / websockets / python-multipart) ship pure-Python wheels — nothing needs to
# compile on the Pi, so the dev headers (and their ~20 MB of transitive packages) are
# unnecessary baggage. If a future dep ever requires it, add `python3-dev` here.
APT_PACKAGES=(python3 python3-pip python3-venv rsync ca-certificates curl)
if [[ "${WEB_SERVER}" == "nginx" ]]; then
  APT_PACKAGES+=(nginx)
fi
apt-get install -y --no-install-recommends "${APT_PACKAGES[@]}"

# ----- 2. Application directory ---------------------------------------------
log "Syncing application files to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"

# Back up the live DB before overwriting any code, so a bad install can be rolled back.
if [[ -f "${INSTALL_DIR}/piscope.db" ]]; then
  cp "${INSTALL_DIR}/piscope.db" "${INSTALL_DIR}/piscope.db.bak"
  ok "Existing piscope.db backed up to piscope.db.bak"
fi

# Sync code (deletes stale files in app/static, keeps DB).
rsync -a --delete "${PROJECT_DIR}/app/" "${INSTALL_DIR}/app/"
rsync -a --delete "${PROJECT_DIR}/static/" "${INSTALL_DIR}/static/"
[[ -f "${PROJECT_DIR}/CHECKPOINT.md" ]] && cp "${PROJECT_DIR}/CHECKPOINT.md" "${INSTALL_DIR}/"

# Ensure DB exists and is writeable by service user.
touch "${INSTALL_DIR}/piscope.db"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

# ----- 3. Python virtual environment ----------------------------------------
log "Creating / refreshing Python virtualenv"
if [[ ! -d "${INSTALL_DIR}/venv" ]]; then
  sudo -u "${SERVICE_USER}" python3 -m venv "${INSTALL_DIR}/venv"
fi
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
# Pinned via requirements.txt for reproducible installs (no surprise upstream breakage
# between deploys). Plain `uvicorn` (no [standard] extras) — the extras pull in
# uvloop/httptools/watchfiles which don't ship binary wheels for armv7l and require C
# compilation. Plain asyncio is plenty fast for our ~30 polls/min workload, and the pinned
# `websockets` is the WS implementation we actually want.
if [[ -f "${PROJECT_DIR}/requirements.txt" ]]; then
  sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade \
    -r "${PROJECT_DIR}/requirements.txt"
else
  # Fallback for older checkouts without the pinned file.
  log "WARNING: requirements.txt not found — installing unpinned latest"
  sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade \
    fastapi uvicorn httpx websockets python-multipart
fi

# ----- 4. systemd service ----------------------------------------------------
log "Writing systemd unit (piscope.service)"
SERVICE_FILE="/etc/systemd/system/piscope.service"
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=PiScope Radar — self-hosted ADS-B interface
After=network-online.target
Wants=network-online.target
# If tar1090 is on this host, prefer to start after it. Harmless if not installed.
After=tar1090.service

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port ${SERVICE_PORT}
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
# Light hardening (LAN-only deployment).
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
PrivateTmp=true
LimitNOFILE=4096

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable piscope.service
systemctl restart piscope.service

# ----- 5. Reverse proxy ------------------------------------------------------
# Two flavours: lighttpd (PiAware default — sits alongside tar1090) and nginx (everything else).
configure_lighttpd() {
  log "Configuring lighttpd proxy for /piscope"
  # If a previous install put nginx on this box and it's no longer wanted, make sure it
  # isn't competing for :80. Leave the package installed for the user to apt-get remove
  # if they want — we just stop it from auto-starting.
  if systemctl is-enabled nginx >/dev/null 2>&1; then
    warn "nginx is enabled from a previous install; disabling so it can't fight lighttpd for :80."
    systemctl disable --now nginx >/dev/null 2>&1 || true
  fi
  # Enable mod_proxy (idempotent on PiAware — already loaded if tar1090 was installed via apt).
  lighttpd-enable-mod proxy >/dev/null 2>&1 || true
  local CONF="/etc/lighttpd/conf-available/99-piscope.conf"
  cat > "${CONF}" <<EOF
# PiScope Radar — proxy /piscope and /piscope/ws to uvicorn on 127.0.0.1:${SERVICE_PORT}.
# Lighttpd 1.4.46+ handles WebSocket upgrades via mod_proxy when "upgrade" => "enable".
\$HTTP["url"] =~ "^/piscope" {
    proxy.server = ( "" => ( ( "host" => "127.0.0.1", "port" => ${SERVICE_PORT} ) ) )
    proxy.header = ( "upgrade" => "enable" )
}
EOF
  # Symlink directly into conf-enabled. `lighttpd-enable-mod` chokes on the numeric
  # `99-` prefix on some PiAware images (silently no-ops), so we don't rely on it.
  mkdir -p /etc/lighttpd/conf-enabled
  ln -sf "${CONF}" /etc/lighttpd/conf-enabled/99-piscope.conf
  if lighttpd -tt -f /etc/lighttpd/lighttpd.conf >/dev/null 2>&1; then
    systemctl reload lighttpd
    ok "lighttpd config OK and reloaded"
  else
    warn "lighttpd -tt failed — check 'lighttpd -tt' output. PiScope Radar is still running on :${SERVICE_PORT}."
  fi
}

configure_nginx() {
  log "Configuring nginx site"
  local NGINX_SITE="/etc/nginx/sites-available/piscope"
  cat > "${NGINX_SITE}" <<EOF
# PiScope Radar — drop this in any existing nginx server { } block via:
#     include ${NGINX_SITE};
location /piscope/ws {
    proxy_pass http://127.0.0.1:${SERVICE_PORT}/piscope/ws;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_read_timeout 1d;
    proxy_send_timeout 1d;
}

location /piscope {
    proxy_pass http://127.0.0.1:${SERVICE_PORT};
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    client_max_body_size 25m;
}
EOF

  local NGINX_STANDALONE="/etc/nginx/sites-available/piscope-standalone"
  cat > "${NGINX_STANDALONE}" <<EOF
# PiScope Radar — standalone server block (auto-installed on fresh systems).
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    include ${NGINX_SITE};
    location = / { return 302 /piscope; }
}
EOF

  if [[ "${ENABLE_NGINX_SITE}" == "1" ]] && [[ -d /etc/nginx/sites-enabled ]]; then
    if ! grep -RIl --include='*' 'default_server' /etc/nginx/sites-enabled/ >/dev/null 2>&1; then
      ln -sf "${NGINX_STANDALONE}" /etc/nginx/sites-enabled/piscope-standalone
      rm -f /etc/nginx/sites-enabled/default
      ok "Auto-enabled standalone nginx site on :80"
    else
      warn "Another nginx site already claims :80; not auto-enabling the standalone server."
      warn "To serve PiScope Radar alongside it, add this line inside that site's server { } block:"
      warn "    include ${NGINX_SITE};"
    fi
  fi

  if nginx -t >/dev/null 2>&1; then
    systemctl reload nginx
    ok "nginx config OK and reloaded"
  else
    warn "nginx -t failed — your config has an error somewhere."
    warn "Run 'nginx -t' to see the details. PiScope Radar itself is still running on :${SERVICE_PORT}."
  fi
}

case "${WEB_SERVER}" in
  lighttpd) configure_lighttpd ;;
  nginx)    configure_nginx ;;
esac

# ----- 6. Admin CLI ---------------------------------------------------------
log "Installing 'piscope' helper command"
CLI_PATH="/usr/local/bin/piscope"
cat > "${CLI_PATH}" <<'CLI'
#!/usr/bin/env bash
# PiScope Radar admin helper. Wraps the most common day-to-day ops.
set -euo pipefail
INSTALL_DIR="/opt/piscope"
SERVICE="piscope-radar"
case "${1:-help}" in
  status)   systemctl status "${SERVICE}" --no-pager ;;
  start)    sudo systemctl start "${SERVICE}" ;;
  stop)     sudo systemctl stop "${SERVICE}" ;;
  restart)  sudo systemctl restart "${SERVICE}" ;;
  logs)     journalctl -u "${SERVICE}" -f --no-pager ;;
  health)   curl -fsS http://127.0.0.1:8765/piscope/api/health | python3 -m json.tool ;;
  backup)
    out="${HOME}/piscope-$(date +%Y%m%d-%H%M%S).zip"
    curl -fsS http://127.0.0.1:8765/piscope/api/export -o "${out}"
    echo "Saved ${out}"
    ;;
  restore)
    [[ -z "${2:-}" ]] && { echo "Usage: piscope restore <backup.zip>"; exit 1; }
    curl -fsS -F "file=@${2}" http://127.0.0.1:8765/piscope/api/import
    ;;
  url)
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo "http://${ip:-localhost}/piscope"
    ;;
  db-size)  du -h "${INSTALL_DIR}/piscope.db" "${INSTALL_DIR}/piscope.db-wal" 2>/dev/null || true ;;
  *)
    cat <<USAGE
PiScope Radar admin commands:
  piscope status   — service status (systemctl)
  piscope start    — start the service
  piscope stop     — stop the service
  piscope restart  — restart the service
  piscope logs     — tail journal output
  piscope health   — JSON health from the running service
  piscope backup   — download a DB backup zip to your home directory
  piscope restore <file.zip>  — restore from a backup zip
  piscope db-size  — show on-disk DB size
  piscope url      — print the LAN URL to open
USAGE
    ;;
esac
CLI
chmod +x "${CLI_PATH}"

# ----- 7. Done --------------------------------------------------------------
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
ok "PiScope Radar installed."
log "Open in a browser:   http://${IP:-localhost}/piscope"
log "Service status:      systemctl status piscope-radar"
log "Tail logs:           journalctl -u piscope-radar -f"
log "Admin command:       piscope help"
log "Local API port:      127.0.0.1:${SERVICE_PORT} (${WEB_SERVER} proxies it to /piscope)"
echo
if systemctl is-active piscope.service --quiet; then
  ok "Service is RUNNING."
else
  err "Service did not come up — run 'journalctl -u piscope-radar --no-pager -n 50' to investigate."
fi
