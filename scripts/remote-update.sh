#!/usr/bin/env bash
# Pull latest main on the GCP VM, refresh the venv, and restart bot services.
# Invoked by GitHub Actions (stdin) or manually on the server:
#   bash scripts/remote-update.sh ~/crypto-trade-bot
set -euo pipefail

DEPLOY_PATH="${1:-${DEPLOY_PATH:-$HOME/crypto-trade-bot}}"
case "$DEPLOY_PATH" in
  "~"|"~/"*) DEPLOY_PATH="${HOME}${DEPLOY_PATH#~}" ;;
esac
cd "$DEPLOY_PATH"

echo "==> Updating repo at ${DEPLOY_PATH}"
git fetch origin main
git reset --hard origin/main

echo "==> Refreshing Python environment"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt

echo "==> Restarting systemd services (if enabled)"
restart_if_enabled() {
  local svc="$1"
  if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
    echo "    restarting ${svc}"
    sudo systemctl restart "$svc"
  else
    echo "    skipping ${svc} (not enabled)"
  fi
}

restart_if_enabled crypto-bot
restart_if_enabled crypto-dashboard
restart_if_enabled crypto-telegram

echo "==> Deploy complete"
systemctl is-active crypto-bot 2>/dev/null && systemctl status crypto-bot --no-pager -l || true
