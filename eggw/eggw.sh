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
RELOAD_EXIT_CODE=75
RELOAD_STATE_FILE="$(mktemp "${TMPDIR:-/tmp}/eggw-reload.XXXXXX")"
export EGGW_RELOAD_EXIT_CODE="$RELOAD_EXIT_CODE"
export EGGW_RELOAD_STATE_FILE="$RELOAD_STATE_FILE"

# Preserve shell argument boundaries in one backend-only payload. The backend
# claims it only for the landing page's fresh thread; /reload keeps the same argv
# but EGGW_RELOAD_THREAD_ID prevents draft replacement or file restaging.
if [ -z "${EGGW_RELOAD_THREAD_ID:-}" ]; then
    EGGW_QUICK_START_ARGS_JSON="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@")"
else
    EGGW_QUICK_START_ARGS_JSON='[]'
fi

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

curl_fetch_complete() {
    local url="$1"
    local output_path="$2"
    local timeout_sec="${3:-20}"

    curl \
        --fail \
        --silent \
        --show-error \
        --http1.1 \
        --max-time "$timeout_sec" \
        -H "Accept-Encoding: identity" \
        -H "Connection: close" \
        -o "$output_path" \
        "$url" >/dev/null
}

probe_url() {
    local url="$1"
    local timeout_sec="${2:-1}"

    if command -v curl >/dev/null 2>&1; then
        curl \
            --fail \
            --silent \
            --http1.1 \
            --noproxy '*' \
            --max-time "$timeout_sec" \
            -H "Accept-Encoding: identity" \
            -H "Connection: close" \
            -o /dev/null \
            "$url" 2>/dev/null
        return
    fi

    # The launcher has already activated its Python environment. Use a
    # proxy-free local request rather than falling back to PID liveness, which
    # does not prove that Hypercorn completed ASGI lifespan startup.
    python - "$url" "$timeout_sec" >/dev/null 2>&1 <<'PY'
import sys
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
request = urllib.request.Request(
    sys.argv[1],
    headers={"Accept-Encoding": "identity", "Connection": "close"},
)
with opener.open(request, timeout=float(sys.argv[2])) as response:
    if not 200 <= response.status < 300:
        raise SystemExit(1)
PY
}

wait_for_backend_startup() {
    local backend_url="$1"
    local timeout_sec="${EGGW_BACKEND_STARTUP_TIMEOUT:-90}"
    local deadline
    local status
    case "$timeout_sec" in
        ''|*[!0-9]*)
            echo "Error: EGGW_BACKEND_STARTUP_TIMEOUT must be a positive integer" >&2
            return 1
            ;;
    esac
    if [ "$timeout_sec" -le 0 ]; then
        echo "Error: EGGW_BACKEND_STARTUP_TIMEOUT must be a positive integer" >&2
        return 1
    fi
    deadline=$((SECONDS + timeout_sec))

    echo "Waiting for backend health..."
    while true; do
        if probe_url "$backend_url/health" 1; then
            echo "Backend ready."
            return 0
        fi
        if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
            status=0
            wait "$BACKEND_PID" 2>/dev/null || status=$?
            echo "Error: Backend exited during startup (status $status)" >&2
            return 1
        fi
        if [ "$SECONDS" -ge "$deadline" ]; then
            break
        fi
        sleep 0.2
    done

    echo "Error: Backend did not become healthy within ${timeout_sec}s" >&2
    return 1
}

js_chunk_complete() {
    local path="$1"
    local min_bytes="${2:-1024}"
    local size

    [ -s "$path" ] || return 1
    size=$(wc -c < "$path")
    [ "$size" -ge "$min_bytes" ] || return 1

    # Next dev chunks should end with the webpack chunk closure.  This catches
    # short/partial transfers even when the HTTP status was 200.
    tail -c 128 "$path" | grep -qF ']);'
}

wait_for_frontend_warmup() {
    local frontend_url="$1"
    local backend_url="$2"
    local timeout_sec="${EGGW_FRONTEND_WARMUP_TIMEOUT:-90}"
    local deadline=$((SECONDS + timeout_sec))
    local tmpdir

    if ! command -v curl >/dev/null 2>&1; then
        echo "Warning: curl is unavailable; using fixed frontend startup delay."
        sleep 3
        return 0
    fi

    tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/eggw-frontend-warmup.XXXXXX")"
    echo "Warming frontend before opening browser..."

    while [ "$SECONDS" -lt "$deadline" ]; do
        if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
            rm -rf "$tmpdir"
            echo "Error: Backend stopped during frontend startup"
            return 1
        fi
        if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
            rm -rf "$tmpdir"
            echo "Error: Frontend stopped during startup"
            return 1
        fi

        if \
            probe_url "$backend_url/health" 1 && \
            curl_fetch_complete "$frontend_url/" "$tmpdir/root.html" 20 && \
            grep -q '/_next/static/chunks/app/layout.js' "$tmpdir/root.html" && \
            curl_fetch_complete "$frontend_url/_next/static/chunks/app/layout.js" "$tmpdir/layout.js" 30 && \
            js_chunk_complete "$tmpdir/layout.js" 100000 && \
            curl_fetch_complete "$frontend_url/_next/static/chunks/app/page.js" "$tmpdir/home-page.js" 30 && \
            js_chunk_complete "$tmpdir/home-page.js" 10000 && \
            curl_fetch_complete "$frontend_url/__eggw_warmup_thread__" "$tmpdir/thread.html" 30 && \
            curl_fetch_complete "$frontend_url/_next/static/chunks/app/%5BthreadId%5D/page.js" "$tmpdir/thread-page.js" 90 && \
            js_chunk_complete "$tmpdir/thread-page.js" 100000
        then
            rm -rf "$tmpdir"
            echo "Frontend warmup complete."
            return 0
        fi

        sleep 0.5
    done

    rm -rf "$tmpdir"
    echo "Warning: Frontend warmup did not complete within ${timeout_sec}s; opening UI anyway."
    return 0
}

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

    rm -f "${EGGW_RELOAD_STATE_FILE:-}" 2>/dev/null || true
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

# Secure launch defaults. Public mode must use an operator-provided token;
# loopback-only mode may provision a fresh capability automatically.
PUBLIC_LISTEN="${EGGW_PUBLIC:-0}"
if [ "$PUBLIC_LISTEN" != "0" ] && [ "$PUBLIC_LISTEN" != "1" ]; then
    echo "Error: EGGW_PUBLIC must be 0 or 1" >&2
    exit 1
fi
if [ "$PUBLIC_LISTEN" = "1" ] && [ -z "${EGGW_API_TOKEN:-}" ]; then
    echo "Error: EGGW_PUBLIC=1 requires an explicit EGGW_API_TOKEN" >&2
    exit 1
fi
if [ -z "${EGGW_API_TOKEN:-}" ]; then
    EGGW_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
fi
export EGGW_API_TOKEN
if [ -n "${EGGW_BIND_HOST:-}" ]; then
    BACKEND_HOST="$EGGW_BIND_HOST"
else
    BACKEND_HOST="127.0.0.1"
fi
case "$BACKEND_HOST" in
    localhost|127.*|::1|\[::1\]) ;;
    *)
        if [ "$PUBLIC_LISTEN" != "1" ]; then
            echo "Error: non-loopback EGGW_BIND_HOST requires explicit EGGW_PUBLIC=1" >&2
            exit 1
        fi
        ;;
esac

# Always probe through a local client address. Wildcard bind addresses are
# listener configuration, not portable request destinations; raw IPv6 literals
# also need URL brackets.
case "$BACKEND_HOST" in
    0.0.0.0|\*) BACKEND_PROBE_HOST="127.0.0.1" ;;
    ::|\[::\]) BACKEND_PROBE_HOST="[::1]" ;;
    \[*\]) BACKEND_PROBE_HOST="$BACKEND_HOST" ;;
    *:*) BACKEND_PROBE_HOST="[$BACKEND_HOST]" ;;
    *) BACKEND_PROBE_HOST="$BACKEND_HOST" ;;
esac

# The private browser bootstrap is safe only when the frontend listener itself
# is loopback-only. Public mode never receives a bootstrap token; its listener
# also stays loopback unless the operator explicitly selects a remote bind.
if [ -n "${EGGW_FRONTEND_BIND_HOST:-}" ]; then
    FRONTEND_HOST="$EGGW_FRONTEND_BIND_HOST"
else
    FRONTEND_HOST="127.0.0.1"
fi
case "$FRONTEND_HOST" in
    localhost|127.*|::1|\[::1\]) ;;
    *)
        if [ "$PUBLIC_LISTEN" != "1" ]; then
            echo "Error: non-loopback EGGW_FRONTEND_BIND_HOST requires explicit EGGW_PUBLIC=1" >&2
            exit 1
        fi
        ;;
esac

# The launcher-owned frontend is the default browser origin. Deployments may
# set EGGW_ALLOWED_ORIGINS to a comma-separated explicit allowlist.
export EGGW_FRONTEND_PORT="$FRONTEND_PORT"
if [ "$PUBLIC_LISTEN" = "1" ] && [ -z "${EGGW_ALLOWED_ORIGINS:-}" ]; then
    echo "Error: EGGW_PUBLIC=1 requires explicit EGGW_ALLOWED_ORIGINS" >&2
    exit 1
fi
if [ -z "${EGGW_ALLOWED_ORIGINS:-}" ]; then
    export EGGW_ALLOWED_ORIGINS="http://localhost:$FRONTEND_PORT,http://127.0.0.1:$FRONTEND_PORT"
fi

# Listener addresses are not browser addresses: localhost in browser code
# refers to the user's machine. Public mode therefore requires the operator to
# provide the externally reachable HTTPS API URL instead of guessing one from
# a bind host.
if [ "$PUBLIC_LISTEN" = "1" ]; then
    if [ -z "${NEXT_PUBLIC_API_URL:-}" ]; then
        echo "Error: EGGW_PUBLIC=1 requires explicit NEXT_PUBLIC_API_URL" >&2
        exit 1
    fi
    case "$NEXT_PUBLIC_API_URL" in
        https://*) ;;
        *)
            echo "Error: public NEXT_PUBLIC_API_URL must use https://" >&2
            exit 1
            ;;
    esac
    BROWSER_API_URL="$NEXT_PUBLIC_API_URL"
else
    BROWSER_API_URL="http://localhost:$BACKEND_PORT"
fi

# Check if .egg directory exists, create if needed
if [ ! -d "$CALLER_CWD/.egg" ]; then
    echo "Creating .egg directory in $CALLER_CWD"
    mkdir -p "$CALLER_CWD/.egg"
fi

# Start backend
echo "Starting backend on $BACKEND_HOST:$BACKEND_PORT (HTTP/2)..."
cd "$CALLER_CWD"
start_prefixed backend env PYTHONSAFEPATH=1 EGGW_QUICK_START_ARGS_JSON="$EGGW_QUICK_START_ARGS_JSON" "${EGGW_HYPERCORN_BIN:-hypercorn}" eggw.main:app --bind "$BACKEND_HOST:$BACKEND_PORT"
BACKEND_PID="$STARTED_PID"

wait_for_backend_startup "http://$BACKEND_PROBE_HOST:$BACKEND_PORT"

# Start frontend
echo "Starting frontend on port $FRONTEND_PORT..."
cd "$SCRIPT_DIR/frontend"

# Ensure node_modules exists and is up to date
if [ ! -d "node_modules" ] || [ "package.json" -nt "node_modules" ]; then
    echo "Installing frontend dependencies..."
    npm install
    touch node_modules  # Update timestamp
fi

# Run from the actual frontend directory to keep Next's project layout simple.
FRONTEND_BOOTSTRAP_TOKEN=""
if [ "$PUBLIC_LISTEN" = "0" ]; then
    FRONTEND_BOOTSTRAP_TOKEN="$EGGW_API_TOKEN"
fi
start_prefixed frontend env -u EGGW_API_TOKEN \
    NEXT_PUBLIC_API_URL="$BROWSER_API_URL" \
    EGGW_PRIVATE_BOOTSTRAP_TOKEN="$FRONTEND_BOOTSTRAP_TOKEN" \
    "${EGGW_NPM_BIN:-npm}" run dev -- -H "$FRONTEND_HOST" -p "$FRONTEND_PORT"
FRONTEND_PID="$STARTED_PID"

if [ "${EGGW_SKIP_FRONTEND_WARMUP:-}" != "1" ]; then
    wait_for_frontend_warmup "http://localhost:$FRONTEND_PORT" "http://localhost:$BACKEND_PORT"
else
    # Backwards-compatible escape hatch for debugging the raw Next dev server.
    sleep 3
fi

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

# Open Web UI in the default browser (skip with EGGW_NO_BROWSER=1)
if [ "${EGGW_NO_BROWSER:-}" != "1" ]; then
    case "$(uname -s)" in
        Darwin)  open "http://localhost:$FRONTEND_PORT" ;;
        Linux)   command -v xdg-open >/dev/null 2>&1 && xdg-open "http://localhost:$FRONTEND_PORT" &>/dev/null & ;;
        CYGWIN*|MINGW*|MSYS*) start "http://localhost:$FRONTEND_PORT" &>/dev/null & ;;
    esac
fi

# Wait for either process to exit
set +e
wait -n $BACKEND_PID $FRONTEND_PID 2>/dev/null
status=$?
set -e

if [ -s "$RELOAD_STATE_FILE" ]; then
    export EGGW_RELOAD_THREAD_ID="$(cat "$RELOAD_STATE_FILE")"
    rm -f "$RELOAD_STATE_FILE"
    cleanup
    cd "$CALLER_CWD"
    exec "$SCRIPT_DIR/eggw.sh" "$@"
fi

# If we get here, one of the processes died
echo "One of the servers stopped unexpectedly"
cleanup
