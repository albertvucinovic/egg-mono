#!/bin/bash
set -euo pipefail

# Resolve symlinks to get the real script directory
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
MONO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CALLER_CWD="$(pwd)"
VENV_DIR="$MONO_ROOT/venv"

# Configuration - can be overridden via environment variables
BACKEND_PORT="${EGGW_BACKEND_PORT:-8000}"
FRONTEND_PORT="${EGGW_FRONTEND_PORT:-3000}"

# Function to check if a port is available
is_port_available() {
    ! nc -z localhost "$1" 2>/dev/null
}

# Function to find an available port starting from a given port
find_available_port() {
    local port=$1
    local max_attempts=100
    local attempt=0
    while [ $attempt -lt $max_attempts ]; do
        if is_port_available $port; then
            echo $port
            return 0
        fi
        port=$((port + 1))
        attempt=$((attempt + 1))
    done
    echo "Error: Could not find available port after $max_attempts attempts" >&2
    return 1
}

# Find available ports
BACKEND_PORT=$(find_available_port $BACKEND_PORT) || exit 1
FRONTEND_PORT=$(find_available_port $FRONTEND_PORT) || exit 1

# Make sure frontend port doesn't collide with backend port
if [ "$FRONTEND_PORT" -eq "$BACKEND_PORT" ]; then
    FRONTEND_PORT=$(find_available_port $((BACKEND_PORT + 1))) || exit 1
fi

echo "Using ports: backend=$BACKEND_PORT, frontend=$FRONTEND_PORT"

# Export database path for backend (uses caller's .egg folder)
export EGG_DB_PATH="$CALLER_CWD/.egg/threads.sqlite"
export EGG_CWD="$CALLER_CWD"

# Keep a handle to the real terminal stdout so log prefixing continues to
# print there even when helper functions are called from command contexts.
exec 3>&1

# PIDs for cleanup
BACKEND_PID=""
FRONTEND_PID=""
CLEANUP_RUNNING=0
STARTED_PID=""

# Start a long-running command in its own session so we can reliably
# terminate the full process group on Ctrl+C. Output is prefixed
# without turning the main command PID into a shell pipeline PID.
start_prefixed() {
    local prefix="$1"
    shift

    setsid "$@" > >(sed -u "s/^/[$prefix] /" >&3) 2>&1 &
    STARTED_PID=$!
}

# Terminate a process group started by start_prefixed().
terminate_group() {
    local pid="${1:-}"
    [ -n "$pid" ] || return 0

    if kill -0 "$pid" 2>/dev/null; then
        kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
        sleep 0.5
    fi

    if kill -0 "$pid" 2>/dev/null; then
        kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi

    wait "$pid" 2>/dev/null || true
}

# Cleanup function
cleanup() {
    if [ "$CLEANUP_RUNNING" -eq 1 ]; then
        return 0
    fi
    CLEANUP_RUNNING=1

    echo ""
    echo "Stopping eggw..."

    terminate_group "$BACKEND_PID"
    terminate_group "$FRONTEND_PID"

    # Extra fallback for anything still attached to this shell.
    jobs -pr | xargs -r kill 2>/dev/null || true
}

on_sigint() {
    cleanup
    exit 130
}

on_sigterm() {
    cleanup
    exit 143
}

trap on_sigint SIGINT
trap on_sigterm SIGTERM
trap cleanup EXIT

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
if [ -f "$CALLER_CWD/.env" ]; then
    set -a && source "$CALLER_CWD/.env" && set +a
elif [ -f "$MONO_ROOT/.env" ]; then
    set -a && source "$MONO_ROOT/.env" && set +a
fi

# Check if .egg directory exists, create if needed
if [ ! -d "$CALLER_CWD/.egg" ]; then
    echo "Creating .egg directory in $CALLER_CWD"
    mkdir -p "$CALLER_CWD/.egg"
fi

# Start backend
echo "Starting backend on port $BACKEND_PORT (HTTP/2)..."
cd "$CALLER_CWD"
start_prefixed backend env PYTHONSAFEPATH=1 hypercorn eggw.main:app --bind 0.0.0.0:$BACKEND_PORT
BACKEND_PID="$STARTED_PID"

# Wait a moment for backend to start
sleep 2

# Check if backend is running
if ! kill -0 $BACKEND_PID 2>/dev/null; then
    echo "Error: Backend failed to start"
    exit 1
fi

# Start frontend
echo "Starting frontend on port $FRONTEND_PORT..."
cd "$SCRIPT_DIR/frontend"

# Ensure node_modules exists and is up to date
if [ ! -d "node_modules" ] || [ "package.json" -nt "node_modules" ]; then
    echo "Installing frontend dependencies..."
    npm install
    touch node_modules  # Update timestamp
fi

# Run from the actual frontend directory to avoid Turbopack workspace issues
start_prefixed frontend env NEXT_PUBLIC_API_URL="http://localhost:$BACKEND_PORT" npm run dev -- -p $FRONTEND_PORT
FRONTEND_PID="$STARTED_PID"

# Wait a moment for frontend to start
sleep 3

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  eggw started successfully                                   ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Web UI:   http://localhost:$FRONTEND_PORT                          ║"
echo "║  API:      http://localhost:$BACKEND_PORT                           ║"
echo "║  Database: $EGG_DB_PATH"
echo "║  CWD:      $CALLER_CWD"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Press Ctrl+C to stop                                        ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Wait for either process to exit
wait -n $BACKEND_PID $FRONTEND_PID 2>/dev/null || true

# If we get here, one of the processes died
echo "One of the servers stopped unexpectedly"
cleanup
