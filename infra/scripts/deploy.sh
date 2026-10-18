#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy.sh — provider-neutral application deployment/update for the voice
# service. Uses ONLY ssh/rsync and Docker Compose; it never calls a provider
# API, so the same script works on any compatible VPS.
#
# It is idempotent: rsync transfers only changed files and `docker compose
# up -d` recreates only changed containers. Rerunning does not corrupt
# configuration or needlessly restart healthy services.
#
# Secrets are uploaded from a LOCAL, untracked file and never printed.
#
# Usage:
#   ./deploy.sh --host <ip-or-host> [options]
#
# Options:
#   --host H            server address (public IP or hostname)         [required]
#   --user U            SSH user (default: deploy)
#   --port N            SSH port (default: 22)
#   --app-dir PATH      remote app dir (default: /opt/james-voice-service)
#   --source PATH       local source dir (default: repository root)
#   --secrets PATH      local env file to install as remote .env (optional)
#   --hostname FQDN     public hostname for the post-deploy health check
#   --prune             rsync --delete (remove remote files not in source)
#   --skip-build        skip `docker compose build`
#   --skip-health       skip the health verification step
#   --dry-run           print actions without changing the server
#   -h, --help          show this help
#
# Exit codes: 0 success; non-zero on any failed step. Failures print a stage
# name only — never secret values.
# ---------------------------------------------------------------------------
set -euo pipefail

log()  { printf '[deploy] %s\n' "$*"; }
fail() { printf '[deploy][ERROR] %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_SOURCE="$(cd "${SCRIPT_DIR}/../.." && pwd)"

HOST=""
USER="deploy"
PORT="22"
APP_DIR="/opt/james-voice-service"
SOURCE="${DEFAULT_SOURCE}"
SECRETS=""
HEALTH_HOSTNAME=""
PRUNE=0
SKIP_BUILD=0
SKIP_HEALTH=0
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --host)        HOST="${2:-}"; shift 2 ;;
    --user)        USER="${2:-}"; shift 2 ;;
    --port)        PORT="${2:-}"; shift 2 ;;
    --app-dir)     APP_DIR="${2:-}"; shift 2 ;;
    --source)      SOURCE="${2:-}"; shift 2 ;;
    --secrets)     SECRETS="${2:-}"; shift 2 ;;
    --hostname)    HEALTH_HOSTNAME="${2:-}"; shift 2 ;;
    --prune)       PRUNE=1; shift ;;
    --skip-build)  SKIP_BUILD=1; shift ;;
    --skip-health) SKIP_HEALTH=1; shift ;;
    --dry-run)     DRY_RUN=1; shift ;;
    -h|--help)     sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)             fail "unknown argument: $1 (use --help)" ;;
  esac
done

[ -n "$HOST" ] || fail "--host is required (use --help)"

command -v ssh   >/dev/null 2>&1 || fail "ssh not found locally"
command -v rsync >/dev/null 2>&1 || fail "rsync not found locally"

[ -d "$SOURCE" ] || fail "--source directory does not exist: $SOURCE"
[ -f "$SOURCE/docker-compose.yml" ] || fail "--source does not look like the voice service (no docker-compose.yml): $SOURCE"

SSH_OPTS=(-p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
SSH_CMD=(ssh "${SSH_OPTS[@]}" "${USER}@${HOST}")

run_remote() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log "[dry-run] remote: $*"
  else
    "${SSH_CMD[@]}" "$@"
  fi
}

# ---------------------------------------------------------------------------
# 1. Preflight: confirm the host completed cloud-init bootstrap.
# ---------------------------------------------------------------------------
log "preflight: checking host $HOST (ssh port $PORT, user $USER)"
if [ "$DRY_RUN" -eq 0 ]; then
  "${SSH_CMD[@]}" 'command -v docker >/dev/null 2>&1' \
    || fail "docker not found on host — has infra/ cloud-init bootstrap finished?"
  "${SSH_CMD[@]}" 'docker compose version >/dev/null 2>&1' \
    || fail "docker compose plugin not found on host"
fi

# ---------------------------------------------------------------------------
# 2. Sync application source (no secrets, no private reference audio).
# ---------------------------------------------------------------------------
RSYNC_ARGS=(
  -az
  -e "ssh -p ${PORT} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
  --exclude='.git/'
  --exclude='.env'
  --exclude='infra/'
  --exclude='data/'
  --exclude='models/'
  --exclude='voice-reference/original/'
  --exclude='voice-reference/processed/'
  --exclude='voice-reference/output/'
  --exclude='whisper-captures/'
  --exclude='__pycache__/'
  --exclude='*.pyc'
  --exclude='*.sqlite3*'
  --exclude='.DS_Store'
)
[ "$PRUNE" -eq 1 ] && RSYNC_ARGS+=(--delete)

log "syncing application source -> ${USER}@${HOST}:${APP_DIR}"
run_remote "mkdir -p '${APP_DIR}'"
if [ "$DRY_RUN" -eq 1 ]; then
  log "[dry-run] rsync ${RSYNC_ARGS[*]} '${SOURCE}/' '${USER}@${HOST}:${APP_DIR}/'"
else
  rsync "${RSYNC_ARGS[@]}" "${SOURCE}/" "${USER}@${HOST}:${APP_DIR}/"
fi

# ---------------------------------------------------------------------------
# 3. Install secrets as remote .env (never printed).
# ---------------------------------------------------------------------------
if [ -n "$SECRETS" ]; then
  [ -f "$SECRETS" ] || fail "--secrets file not found: $SECRETS"
  for key in VOICE_RAG_API_URL VOICE_RAG_SERVICE_TOKEN VOICE_HMAC_SECRET; do
    grep -q "^${key}=" "$SECRETS" || fail "--secrets file is missing required key ${key}"
  done
  log "installing secrets as ${APP_DIR}/.env (values not shown)"
  if [ "$DRY_RUN" -eq 1 ]; then
    log "[dry-run] scp '$SECRETS' -> ${USER}@${HOST}:${APP_DIR}/.env (mode 600)"
  else
    TMP_REMOTE="$(ssh "${SSH_OPTS[@]}" "${USER}@${HOST}" 'mktemp /tmp/voice-env.XXXXXX')"
    scp -P "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
      "$SECRETS" "${USER}@${HOST}:${TMP_REMOTE}" >/dev/null
    "${SSH_CMD[@]}" "install -m 600 '${TMP_REMOTE}' '${APP_DIR}/.env' && rm -f '${TMP_REMOTE}'" \
      || fail "failed to install .env"
  fi
else
  log "no --secrets supplied; leaving existing ${APP_DIR}/.env untouched"
fi

# ---------------------------------------------------------------------------
# 4. Build and (re)start containers. Idempotent.
# ---------------------------------------------------------------------------
log "verifying .env exists on host"
run_remote "test -f '${APP_DIR}/.env'" || fail ".env is absent on host; pass --secrets once before first deploy"

if [ "$SKIP_BUILD" -eq 1 ]; then
  log "skipping image build (--skip-build)"
else
  log "building image (docker compose build)"
  run_remote "cd '${APP_DIR}' && docker compose build"
fi

log "starting services (docker compose up -d)"
run_remote "cd '${APP_DIR}' && docker compose up -d"

# ---------------------------------------------------------------------------
# 5. Health verification.
# ---------------------------------------------------------------------------
if [ "$SKIP_HEALTH" -eq 1 ]; then
  log "skipping health checks (--skip-health)"
  exit 0
fi

log "checking container health on host"
run_remote "cd '${APP_DIR}' && docker compose ps"

if [ -n "$HEALTH_HOSTNAME" ] && [ "$DRY_RUN" -eq 0 ]; then
  log "running public health check against ${HEALTH_HOSTNAME}"
  "${SCRIPT_DIR}/health-check.sh" --host "$HEALTH_HOSTNAME" || fail "public health check failed"
else
  log "no --hostname supplied; container status above is the health signal"
fi

log "deploy complete"
