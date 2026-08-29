# Shared setup for local GCP scripts (sync-logs, tunnel-dashboard).
# Source from repo scripts after setting ROOT to the repository root.
#
# Sets: GCP_SSH_HOST, GCP_SSH_USER, GCP_SSH_OPTS (array), GCP_RSYNC_RSH (string)

: "${ROOT:?ROOT must be set before sourcing deploy-local-env.sh}"

CONFIG="${ROOT}/scripts/.deploy.local"
if [[ -f "$CONFIG" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG"
fi

: "${GCP_SSH_HOST:?Set GCP_SSH_HOST in scripts/.deploy.local or the environment}"
: "${GCP_SSH_USER:?Set GCP_SSH_USER in scripts/.deploy.local or the environment}"

GCP_SSH_OPTS=()
GCP_RSYNC_RSH="ssh"
if [[ -n "${GCP_SSH_IDENTITY_FILE:-}" ]]; then
  _identity="${GCP_SSH_IDENTITY_FILE}"
  if [[ "$_identity" != /* ]]; then
    _identity="${ROOT}/${_identity}"
  fi
  if [[ ! -f "$_identity" ]]; then
    echo "GCP_SSH_IDENTITY_FILE not found: ${_identity}" >&2
    exit 1
  fi
  GCP_SSH_OPTS=(-i "$_identity")
  GCP_RSYNC_RSH="ssh -i ${_identity}"
fi
