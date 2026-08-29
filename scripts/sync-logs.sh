#!/usr/bin/env bash
# Pull live bot logs from the GCP VM into ./logs for local inspection.
#
# Setup (once):
#   cp scripts/deploy.local.example scripts/.deploy.local
#   # edit scripts/.deploy.local with your VM IP and username
#
# Usage:
#   bash scripts/sync-logs.sh
#   bash scripts/sync-logs.sh --tail   # also print last 5 lines of each file
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${ROOT}/scripts/.deploy.local"

if [[ -f "$CONFIG" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG"
fi

: "${GCP_SSH_HOST:?Set GCP_SSH_HOST in scripts/.deploy.local or the environment}"
: "${GCP_SSH_USER:?Set GCP_SSH_USER in scripts/.deploy.local or the environment}"

DEPLOY_PATH="${GCP_DEPLOY_PATH:-~/crypto-trade-bot}"
REMOTE_LOGS="${GCP_REMOTE_LOGS:-logs}"
LOCAL_LOGS="${GCP_LOCAL_LOGS:-${ROOT}/logs}"
REMOTE="${GCP_SSH_USER}@${GCP_SSH_HOST}:${DEPLOY_PATH}/${REMOTE_LOGS}/"

mkdir -p "$LOCAL_LOGS"

echo "Syncing ${REMOTE} -> ${LOCAL_LOGS}/"
if command -v rsync >/dev/null 2>&1; then
  rsync -avz --include='*.jsonl' --include='*.json' --exclude='*' "$REMOTE" "$LOCAL_LOGS/"
else
  scp "${GCP_SSH_USER}@${GCP_SSH_HOST}:${DEPLOY_PATH}/${REMOTE_LOGS}/*.jsonl" "$LOCAL_LOGS/" 2>/dev/null || true
  scp "${GCP_SSH_USER}@${GCP_SSH_HOST}:${DEPLOY_PATH}/${REMOTE_LOGS}/*.json" "$LOCAL_LOGS/" 2>/dev/null || true
fi

echo "Done. Local logs:"
ls -la "$LOCAL_LOGS"

if [[ "${1:-}" == "--tail" ]]; then
  for f in events trades briefings; do
    path="${LOCAL_LOGS}/${f}.jsonl"
    if [[ -f "$path" ]]; then
      echo ""
      echo "--- tail ${f}.jsonl ---"
      tail -n 5 "$path"
    fi
  done
fi
