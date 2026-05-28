#!/usr/bin/env bash
#
# deploy-piscope.sh — sync piscope-radar to the Pi and restart the service.
#
# WHY THIS EXISTS (permissions audit, 2026-05): replaces the recurring
#   cd ~/projects/piscope-radar && rsync ... piaware:/opt/piscope/... \
#     && ssh piaware '<arbitrary remote command incl. sudo systemctl restart>'
# with a single bounded command, safe to allowlist as:
#
#   "Bash(./deploy-piscope.sh:*)"
#
# PREREQUISITES:
#   - `piaware` is an SSH config alias (Host piaware -> the Pi, ~10.0.0.231).
#     Keep host resolution in ~/.ssh/config, not hardcoded here.
#   - sudoers NOPASSWD on the Pi for the restart, matching EXACTLY, e.g.:
#       <piuser> ALL=(root) NOPASSWD: /usr/bin/systemctl restart piscope
#     (the transcripts used bare `sudo systemctl restart piscope`; switch to a
#      pinned absolute path so `sudo -n` works non-interactively)
#
set -euo pipefail

# --- config ---------------------------------------------------------------
HOST="piaware"
REMOTE_BASE="/opt/piscope"
SERVICE="piscope"
VERSION_URL="http://127.0.0.1:8765/piscope/api/version"   # checked ON the Pi
SRC_DIR="${PISCOPE_SRC_DIR:-$HOME/projects/piscope-radar}"
EXCLUDES=(--exclude='__pycache__' --exclude='*.pyc')
# Which trees to sync: "app", "static", or "both" (default).
SCOPE="${1:-both}"
# --------------------------------------------------------------------------

cd "$SRC_DIR"

# Syntax gate: never ship Python that won't import. Fail before touching the Pi.
echo "==> py_compile check"
find app -name '*.py' -print0 | xargs -0 python3 -m py_compile
echo "    syntax OK"

sync_tree() {  # $1 = local subdir (app|static)
  echo "==> rsync ${1}/ -> ${HOST}:${REMOTE_BASE}/${1}/"
  # --delete is scoped to the app/ and static/ subtrees only (venv/, data/
  # live elsewhere under /opt/piscope and are never touched).
  rsync -a --delete "${EXCLUDES[@]}" --info=NAME \
    "${1}/" "${HOST}:${REMOTE_BASE}/${1}/"
}

case "$SCOPE" in
  app)    sync_tree app ;;
  static) sync_tree static ;;
  both)   sync_tree app; sync_tree static ;;
  *) echo "usage: $0 [app|static|both]" >&2; exit 2 ;;
esac

echo "==> Restart ${SERVICE} + verify"
# Single fixed remote command: pinned sudo restart, then a read-only health check.
ssh -o ConnectTimeout=5 "$HOST" "
  set -e
  sudo -n /usr/bin/systemctl restart ${SERVICE}
  sleep 3
  systemctl is-active ${SERVICE}
  echo -n 'version: '; curl -s -m 5 '${VERSION_URL}' || echo '(version endpoint unreachable)'
"
echo "==> done"
