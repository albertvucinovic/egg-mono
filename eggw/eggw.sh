#!/bin/bash
set -euo pipefail

# Resolve symlinks to get the real script directory
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
CALLER_CWD="$(pwd)"

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

# PIDs for cleanup
BACKEND_PID=""
FRONTEND_PID=""

# Cleanup function
cleanup() {
    echo ""
    echo "Stopping eggw..."
    if [ -n "$BACKEND_PID" ]; then
        kill $BACKEND_PID 2>/dev/null || true
    fi
    if [ -n "$FRONTEND_PID" ]; then
        kill $FRONTEND_PID 2>/dev/null || true
    fi
    # Kill any child processes
    jobs -p | xargs -r kill 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM EXIT

# Try to activate Python virtual environment (use egg's venv if available)
VENV_PATH="$SCRIPT_DIR/../egg/venv/bin/activate"
if [ -f "$VENV_PATH" ]; then
    echo "Using egg's virtual environment"
    source "$VENV_PATH"
else
    echo "No venv found, using system Python"
    # Verify uvicorn is available
    if ! command -v uvicorn &> /dev/null; then
        echo "Error: uvicorn not found. Install it with: pip install uvicorn"
        exit 1
    fi
fi

# Check if .egg directory exists, create if needed
if [ ! -d "$CALLER_CWD/.egg" ]; then
    echo "Creating .egg directory in $CALLER_CWD"
    mkdir -p "$CALLER_CWD/.egg"
fi

# Start backend
echo "Starting backend on port $BACKEND_PORT..."
# Get api keys
source .env
cd "$SCRIPT_DIR/backend"
uvicorn main:app --host 0.0.0.0 --port $BACKEND_PORT 2>&1 | sed 's/^/[backend] /' &
BACKEND_PID=$!

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
NEXT_PUBLIC_API_URL="http://localhost:$BACKEND_PORT" npm run dev -- -p $FRONTEND_PORT 2>&1 | sed 's/^/[frontend] /' &
FRONTEND_PID=$!

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
