#!/usr/bin/env bash
# Build + tag the agent-base image, content-addressed by its devbox.lock: the lock pins the exact
# nixpkgs closure, so the same lock → the same toolchain → a reproducible tag. Mirrors the CI
# workflow (.github/workflows/build-image.yaml) so `local == CI`. Push to ghcr.
#
#   bash scripts/build-image.sh             # build only
#   PUSH=true bash scripts/build-image.sh   # build + push (requires `docker login ghcr.io`)
set -euo pipefail
cd "$(dirname "$0")/.."

REGISTRY="${REGISTRY:-ghcr.io/teststuffstash}"
IMAGE="$REGISTRY/agent-base"
LOCKHASH="$(sha256sum agent-base/devbox.lock | cut -c1-8)"
TAG="$(date -u +%Y-%m-%d)-${LOCKHASH}"

echo "→ building $IMAGE:$TAG (+ :latest)"
docker build -f agent-base/Dockerfile -t "$IMAGE:$TAG" -t "$IMAGE:latest" .

if [ "${PUSH:-false}" = "true" ]; then
  docker push "$IMAGE:$TAG"
  docker push "$IMAGE:latest"
fi

# --- SLSA-L2 stubs (homelab docs/slsa.md) — enable in the supply-chain phase ---
# syft  "$IMAGE:$TAG" -o spdx-json > sbom.spdx.json
# cosign attest --predicate sbom.spdx.json --type spdxjson "$IMAGE:$TAG"
# cosign sign "$IMAGE:$TAG"      # keyless (Fulcio) or --key (self-hosted, see slsa.md)

echo "→ done. tag=$TAG  push=${PUSH:-false}"
