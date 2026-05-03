#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SANDBOX_IMAGE="${1:-egg-sandbox}"
SESSION_IMAGE="${2:-egg-rlm-session}"

# Build the common Egg Docker runtime used by sandboxed tool calls and as the
# filesystem/toolchain base for persistent Docker REPL sessions.
docker build -t "$SANDBOX_IMAGE" -f "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR"

# Keep the session image as a tiny wrapper so it retains the REPL/sessiond
# default command without duplicating the shared development toolchain.
docker build \
  -t "$SESSION_IMAGE" \
  --build-arg "BASE_IMAGE=$SANDBOX_IMAGE" \
  -f "$SCRIPT_DIR/Dockerfile.session" \
  "$SCRIPT_DIR"

# Verify the images were created.
docker image inspect "$SANDBOX_IMAGE" >/dev/null
docker image inspect "$SESSION_IMAGE" >/dev/null
echo "Built $SANDBOX_IMAGE and $SESSION_IMAGE"
