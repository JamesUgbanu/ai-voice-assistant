#!/usr/bin/env bash
# Build, verify and publish the voice-service image to a PRIVATE GHCR package.
# Run from your MacBook only when releasing. The server never builds; it pulls
# an immutable version tag (or digest) at boot.
#
# Usage:
#   GHCR_OWNER=<github-user-or-org> VERSION=0.2.0 scripts/release.sh
#   GHCR_OWNER=... VERSION=0.2.0 scripts/release.sh --stable
#
# --stable additionally MOVES the `stable` tag to this release. Production
# automation must pin the immutable VERSION tag (or digest), never `stable`/
# `latest`. Requires: docker buildx, `docker login ghcr.io` already done.
#
# No secrets are read or written by this script.
set -euo pipefail

VERSION="${VERSION:-}"
OWNER="${GHCR_OWNER:-}"
IMAGE_NAME="${IMAGE_NAME:-james-voice-service}"
PLATFORM="${PLATFORM:-linux/amd64}"   # Hetzner server is amd64 (verify if ARM)
BUILDER="${BUILDER:-desktop-linux}"   # trusted LOCAL builder (never a remote builder)
STABLE=""
for arg in "$@"; do
  case "$arg" in
    --stable) STABLE="1" ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [[ -z "$VERSION" || -z "$OWNER" ]]; then
  echo "ERROR: set GHCR_OWNER and VERSION (immutable, e.g. 0.2.0)" >&2
  exit 2
fi
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]]; then
  echo "ERROR: VERSION must be an immutable semver tag (got '$VERSION')" >&2
  exit 2
fi
if [[ "$VERSION" == "latest" || "$VERSION" == "stable" ]]; then
  echo "ERROR: refusing mutable tag '$VERSION'" >&2
  exit 2
fi

IMAGE="ghcr.io/${OWNER}/${IMAGE_NAME}:${VERSION}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Option A preflight: the authorized voice-cloning reference is baked into the
# PRIVATE image. Verify it exists, is non-trivial, and is git-ignored. Print only
# its byte size — never its contents or path-derived metadata.
REF="deploy/reference/voice-ref.wav"
if [[ ! -s "$REF" ]]; then
  echo "ERROR: missing $REF. Option A bakes the authorized reference into the private image." >&2
  exit 2
fi
REF_BYTES="$(wc -c < "$REF" | tr -d ' ')"
if [[ "${REF_BYTES:-0}" -lt 100000 ]]; then
  echo "ERROR: $REF looks empty/truncated (${REF_BYTES} bytes)" >&2
  exit 2
fi
if ! git check-ignore -q "$REF" 2>/dev/null; then
  echo "ERROR: $REF is not git-ignored; refusing to publish" >&2
  exit 2
fi
echo "==> reference preflight OK (${REF_BYTES} bytes; contents not printed)"

echo "==> pre-publish checks"
python3 -m py_compile app/*.py tests/*.py
python3 -m unittest discover -s tests

echo "==> build + push ${PLATFORM} image ${IMAGE}"
docker buildx build --builder "$BUILDER" --platform "$PLATFORM" -t "$IMAGE" --push .

DIGEST="$(docker buildx imagetools inspect "$IMAGE" --format '{{json .Manifest.Digest}}' | tr -d '"')"
if [[ -z "$DIGEST" ]]; then
  echo "ERROR: could not resolve pushed digest" >&2
  exit 1
fi
echo "pushed ${IMAGE}"
echo "digest: ${DIGEST}"

if [[ -n "$STABLE" ]]; then
  echo "==> moving 'stable' -> ${VERSION}@${DIGEST}"
  docker buildx imagetools create -t "ghcr.io/${OWNER}/${IMAGE_NAME}:stable" "${IMAGE}@${DIGEST}"
fi

cat <<EOF

Released:
  image:  ${IMAGE}
  digest: ${DIGEST}
Deploy pin (use the immutable tag or digest in the scheduler config):
  VOICE_IMAGE=${IMAGE}
  VOICE_IMAGE_DIGEST=${DIGEST}
EOF
