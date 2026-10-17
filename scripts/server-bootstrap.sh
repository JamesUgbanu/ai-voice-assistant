#!/usr/bin/env bash
# Provision/refresh a disposable Hetzner voice host for a PINNED image.
#
# Idempotent: safe to re-run after a partial/failed provision. Secrets are read
# from root-only files provided by the provisioning step (AWS SSM -> host over
# the restricted bootstrap channel); they are never baked into the image, never
# passed through cloud-init user-data, and never printed.
#
# Run on the server as root:
#   VOICE_IMAGE=ghcr.io/<owner>/james-voice-service:<version-or-digest> \
#   GHCR_USER=<github-user> \
#   GHCR_TOKEN_FILE=/run/voice/ghcr-token \
#   VOICE_ENV_FILE=/etc/voice/voice.env \
#   scripts/server-bootstrap.sh
#
# Readiness: /healthz must report "warm":true (models loaded), not merely an open
# port. The service is only considered available after that.
set -euo pipefail

IMAGE="${VOICE_IMAGE:?set VOICE_IMAGE to a pinned tag or digest}"
GHCR_USER="${GHCR_USER:?set GHCR_USER}"
GHCR_TOKEN_FILE="${GHCR_TOKEN_FILE:?set GHCR_TOKEN_FILE (root-only, read token)}"
ENV_FILE="${VOICE_ENV_FILE:?set VOICE_ENV_FILE (root-only runtime env)}"
HEALTH_URL="${VOICE_HEALTH_URL:-http://127.0.0.1:8000/healthz}"
NAME="${VOICE_CONTAINER:-james-voice}"

# 1. Docker (installed only if missing; re-run is a no-op)
if ! command -v docker >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends ca-certificates curl
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

# 2. Registry auth with a READ-ONLY package token (never a push credential)
[ -r "$GHCR_TOKEN_FILE" ] || { echo "missing/invalid GHCR token file" >&2; exit 1; }
REGISTRY="${IMAGE%%/*}"
docker login "$REGISTRY" --username "$GHCR_USER" --password-stdin < "$GHCR_TOKEN_FILE" >/dev/null

# 3. Pull the pinned image (tag or @sha256 digest)
docker pull "$IMAGE"

# 4. Persistent host directories for models + SQLite (outside the container layer)
install -d -m 0750 /srv/voice/models /srv/voice/data
[ -r "$ENV_FILE" ] || { echo "missing runtime env file" >&2; exit 1; }
chmod 600 "$ENV_FILE"

# 5. (Re)start the single container. Models download into /srv/voice/models on
#    first boot via HF_HOME, so a code-only release does not re-download them.
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --restart unless-stopped \
  --env-file "$ENV_FILE" \
  -v /srv/voice/models:/srv/voice/models \
  -v /srv/voice/data:/srv/voice/data \
  -p 127.0.0.1:8000:8000 \
  "$IMAGE"

# 6. Real readiness: models loaded ("warm":true), bounded wait.
for _ in $(seq 1 90); do
  if curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null | grep -q '"warm":true'; then
    echo "voice service ready"
    exit 0
  fi
  sleep 2
done
echo "readiness FAILED (models/RAG not ready); recent logs:" >&2
docker logs --tail 50 "$NAME" >&2 || true
exit 1
