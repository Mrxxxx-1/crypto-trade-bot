#!/usr/bin/env bash
# Pull live bot logs from the GCP VM into ./logs for local inspection.
#
# Setup (once):
#   cp scripts/deploy.local.example scripts/.deploy.local
#   # edit scripts/.deploy.local with VM IP, username, and identity key
#
# Usage:
#   bash scripts/sync-logs.sh
#   bash scripts/sync-logs.sh --tail   # sync + show last 5 lines of each file
#   bash scripts/sync-logs.sh --force  # overwrite local even if mtime looks newer
set -euo pipefail

LOG_FILES=(events.jsonl trades.jsonl briefings.jsonl control.json briefing_state.json)

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${ROOT}/scripts/deploy-local-env.sh"

DEPLOY_PATH="${GCP_DEPLOY_PATH:-~/crypto-trade-bot}"
REMOTE_LOGS="${GCP_REMOTE_LOGS:-logs}"
LOCAL_LOGS="${GCP_LOCAL_LOGS:-${ROOT}/logs}"
REMOTE="${GCP_SSH_USER}@${GCP_SSH_HOST}:${DEPLOY_PATH}/${REMOTE_LOGS}/"

mkdir -p "$LOCAL_LOGS"

FORCE=false
TAIL=false
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=true ;;
    --tail) TAIL=true ;;
  esac
done

RSYNC_EXTRA=()
if [[ "$FORCE" == true ]]; then
  RSYNC_EXTRA+=(--ignore-times)
fi

echo "Syncing ${REMOTE} -> ${LOCAL_LOGS}/"
copied=0
for name in "${LOG_FILES[@]}"; do
  src="${GCP_SSH_USER}@${GCP_SSH_HOST}:${DEPLOY_PATH}/${REMOTE_LOGS}/${name}"
  if command -v rsync >/dev/null 2>&1; then
    if rsync -avz -e "$GCP_RSYNC_RSH" "${RSYNC_EXTRA[@]}" "$src" "${LOCAL_LOGS}/"; then
      copied=$((copied + 1))
    else
      echo "Skipped ${name} (missing on VM or rsync failed)" >&2
    fi
  elif scp "${GCP_SSH_OPTS[@]}" "$src" "${LOCAL_LOGS}/"; then
    copied=$((copied + 1))
  else
    echo "Skipped ${name} (missing on VM or scp failed)" >&2
  fi
done

if [[ "$copied" -eq 0 ]]; then
  echo "No log files copied — check SSH access and GCP_SSH_IDENTITY_FILE in scripts/.deploy.local" >&2
  exit 1
fi
echo "Copied ${copied} file(s)."

echo "Done. Local logs:"
ls -la "$LOCAL_LOGS"

if [[ "$TAIL" == true ]]; then
  for f in events trades briefings; do
    path="${LOCAL_LOGS}/${f}.jsonl"
    if [[ -f "$path" ]]; then
      echo ""
      echo "--- tail ${f}.jsonl ---"
      tail -n 5 "$path"
    fi
  done
fi
