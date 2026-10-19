#!/usr/bin/env bash
# Build, verify, tag, and publish the voice-service image to a PRIVATE GHCR
# package. Immutable version tags are required for deployment; `stable` is an
# opt-in convenience pointer and is NEVER used implicitly by production.
#
# Usage:
#   GHCR_IMAGE=ghcr.io/<owner>/voice-service \
#     scripts/release-image.sh <version> [--stable]
#   e.g. scripts/release-image.sh 2026.10.17.1
#
# Requirements:
#   - docker + buildx (linux/amd64 build on Apple Silicon uses QEMU)
#   - `docker login ghcr.io` already done with a token that can write packages
#   - GHCR_IMAGE points at the private package, lowercase
#
# Nothing is built or pushed until local static checks and the full test suite
# pass. Models are NOT in the image (see Dockerfile / scripts/server-bootstrap.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${1:-}"
STABLE=0
for arg in "${@:2}"; do [ "$arg" = "--stable" ] && STABLE=1; done

if [ -z "$VERSION" ] || ! printf '%s' "$VERSION" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'; then
  echo "usage: $0 <immutable-version-tag> [--stable]" >&2
  echo "  e.g. $0 2026.10.17.1" >&2
  exit 2
fi
if [ "$VERSION" = "latest" ] || [ "$VERSION" = "stable" ]; then
  echo "refusing mutable tag '$VERSION' — release an explicit version" >&2
  exit 2
fi
IMAGE="${GHCR_IMAGE:-}"
if [ -z "$IMAGE" ]; then
  echo "set GHCR_IMAGE=ghcr.io/<owner>/voice-service" >&2
  exit 2
fi
PLATFORM="${PLATFORM:-linux/amd64}"

echo "[1/4] local checks (py_compile + full test suite)"
python3 -m py_compile app/*.py tests/*.py
python3 -m unittest discover -s tests

echo "[2/4] build $PLATFORM -> $IMAGE:$VERSION"
docker buildx build --platform "$PLATFORM" -f Dockerfile -t "$IMAGE:$VERSION" --load .

echo "[3/4] push immutable version tag"
docker push "$IMAGE:$VERSION"

if [ "$STABLE" = "1" ]; then
  echo "[4/4] also tag + push :stable (opt-in deployment pointer)"
  docker tag "$IMAGE:$VERSION" "$IMAGE:stable"
  docker push "$IMAGE:stable"
else
  echo "[4/4] :stable untouched (pass --stable to update it)"
fi

DIGEST="$(docker image inspect "$IMAGE:$VERSION" --format '{{index .RepoDigests 0}}' 2>/dev/null || true)"
echo "released: $IMAGE:$VERSION"
[ -n "$DIGEST" ] && echo "digest  : $DIGEST"
echo "next: pin this tag (or digest) in the infrastructure config; never use latest."
