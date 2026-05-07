#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MONO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CALLER_CWD="$(pwd)"
VENV_DIR="$MONO_ROOT/venv"

# Create venv and install monorepo packages on first run
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "First run — creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    source "$VENV_DIR/bin/activate"
    echo "Installing egg-mono packages..."
    make -C "$MONO_ROOT" install
else
    source "$VENV_DIR/bin/activate"
fi

# Load .env if present (API keys, etc.)
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a && source "$SCRIPT_DIR/.env" && set +a
elif [ -f "$MONO_ROOT/.env" ]; then
    set -a && source "$MONO_ROOT/.env" && set +a
fi

RELOAD_EXIT_CODE=75
RELOAD_STATE_FILE="$(mktemp "${TMPDIR:-/tmp}/egg-reload.XXXXXX")"
export EGG_RELOAD_EXIT_CODE
export EGG_RELOAD_STATE_FILE

set +e
(cd "$CALLER_CWD" && PYTHONSAFEPATH=1 python -c "from egg.app import main; main()" "$@")
status=$?
set -e

if [ "$status" -eq "$RELOAD_EXIT_CODE" ] && [ -s "$RELOAD_STATE_FILE" ]; then
    export EGG_RELOAD_THREAD_ID="$(cat "$RELOAD_STATE_FILE")"
    rm -f "$RELOAD_STATE_FILE"
    exec "$SCRIPT_DIR/egg.sh" "$@"
fi

rm -f "$RELOAD_STATE_FILE"
exit "$status"
