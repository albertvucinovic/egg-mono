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

cd "$CALLER_CWD" && PYTHONSAFEPATH=1 python -c "from egg.app import main; main()" "$@"
