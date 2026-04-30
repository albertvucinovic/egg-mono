#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Build the ephemeral sandbox image.
docker build -t egg-sandbox -f "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR"

# Build the persistent explicit-RLM session image.
docker build -t egg-rlm-session -f "$SCRIPT_DIR/Dockerfile.session" "$SCRIPT_DIR"

# Verify the image was created
docker images | grep egg-sandbox
docker images | grep egg-rlm-session

