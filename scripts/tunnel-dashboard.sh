#!/usr/bin/env bash
# SSH tunnel: open the VM dashboard at http://127.0.0.1:8000 (reads live logs on the server).
#
# Setup: same scripts/.deploy.local as sync-logs.sh
# Usage: bash scripts/tunnel-dashboard.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${ROOT}/scripts/.deploy.local"

if [[ -f "$CONFIG" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG"
fi

: "${GCP_SSH_HOST:?Set GCP_SSH_HOST in scripts/.deploy.local or the environment}"
: "${GCP_SSH_USER:?Set GCP_SSH_USER in scripts/.deploy.local or the environment}"

LOCAL_PORT="${GCP_LOCAL_DASHBOARD_PORT:-8000}"
REMOTE_PORT="${GCP_DASHBOARD_PORT:-8000}"

echo "Tunneling localhost:${LOCAL_PORT} -> ${GCP_SSH_HOST}:${REMOTE_PORT}"
echo "Open http://127.0.0.1:${LOCAL_PORT} (Ctrl+C to close)"
exec ssh -N -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" "${GCP_SSH_USER}@${GCP_SSH_HOST}"
