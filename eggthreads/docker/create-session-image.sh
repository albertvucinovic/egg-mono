#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="${1:-egg-rlm-session}"
BASE_IMAGE="${2:-egg-sandbox}"

if [ "$BASE_IMAGE" = "egg-sandbox" ]; then
  echo "Building/updating base image $BASE_IMAGE."
  docker build -t "$BASE_IMAGE" -f "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR"
elif ! docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
  echo "Base image $BASE_IMAGE not found. Build it first, or omit the second argument to use egg-sandbox." >&2
  exit 1
fi

docker build \
  -t "$IMAGE_NAME" \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  -f "$SCRIPT_DIR/Dockerfile.session" \
  "$SCRIPT_DIR"
docker image inspect "$IMAGE_NAME" >/dev/null
echo "Built $IMAGE_NAME from $BASE_IMAGE"
