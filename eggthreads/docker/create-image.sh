#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Build the Docker image used by both the ephemeral sandbox provider and
# persistent explicit-RLM sessions.
docker build -t egg-sandbox -t egg-rlm-session -f "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR"

# Verify the image was created
docker images | grep egg-sandbox
docker images | grep egg-rlm-session

