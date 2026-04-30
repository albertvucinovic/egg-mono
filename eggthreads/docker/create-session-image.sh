#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="${1:-egg-rlm-session}"

docker build -t "$IMAGE_NAME" -f "$SCRIPT_DIR/Dockerfile.session" "$SCRIPT_DIR"
docker image inspect "$IMAGE_NAME" >/dev/null
echo "Built $IMAGE_NAME"
