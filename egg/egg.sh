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
INITIAL_RELOAD_THREAD_ID="${EGG_RELOAD_THREAD_ID:-}"
MAX_RELOADS="${EGG_MAX_RELOADS:-8}"
case "$MAX_RELOADS" in
    ''|*[!0-9]*)
        echo "egg.sh: EGG_MAX_RELOADS must be a non-negative integer" >&2
        exit 2
        ;;
esac

# The wrapper owns one state file and one bounded reload loop. It preserves an
# inherited thread id only for app.py's direct-restart fallback, while replacing
# stale outer state-file/exit-code ownership with this invocation's values.
RELOAD_STATE_FILE="$(mktemp "${TMPDIR:-/tmp}/egg-reload.XXXXXX")"
export EGG_RELOAD_EXIT_CODE="$RELOAD_EXIT_CODE"
export EGG_RELOAD_STATE_FILE="$RELOAD_STATE_FILE"
if [ -n "$INITIAL_RELOAD_THREAD_ID" ]; then
    export EGG_RELOAD_THREAD_ID="$INITIAL_RELOAD_THREAD_ID"
else
    unset EGG_RELOAD_THREAD_ID
fi

child_pid=""
child_pgid=""
cleanup() {
    local signal="${1:-}"
    if [ -n "$signal" ] && [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then
        if [ -n "$child_pgid" ]; then
            kill -"$signal" -- "-$child_pgid" 2>/dev/null || true
        else
            kill -"$signal" "$child_pid" 2>/dev/null || true
        fi
    fi
    rm -f -- "$RELOAD_STATE_FILE"
}
trap 'cleanup' EXIT
trap 'cleanup HUP; trap - EXIT HUP INT TERM; exit 129' HUP
trap 'cleanup INT; trap - EXIT HUP INT TERM; exit 130' INT
trap 'cleanup TERM; trap - EXIT HUP INT TERM; exit 143' TERM

reload_count=0
while :; do
    : > "$RELOAD_STATE_FILE"
    set +e
    if command -v setsid >/dev/null 2>&1; then
        (
            cd "$CALLER_CWD"
            exec setsid env PYTHONSAFEPATH=1 "${EGG_PYTHON_BIN:-python}" \
                -c "from egg.app import main; main()" "$@"
        ) &
        child_pid=$!
        child_pgid="$child_pid"
    else
        (
            cd "$CALLER_CWD"
            exec env PYTHONSAFEPATH=1 "${EGG_PYTHON_BIN:-python}" \
                -c "from egg.app import main; main()" "$@"
        ) &
        child_pid=$!
        child_pgid=""
    fi
    wait "$child_pid"
    status=$?
    child_pid=""
    child_pgid=""
    set -e

    if [ "$status" -ne "$RELOAD_EXIT_CODE" ]; then
        exit "$status"
    fi
    if [ ! -s "$RELOAD_STATE_FILE" ]; then
        echo "egg.sh: reload requested without a saved thread id" >&2
        exit "$RELOAD_EXIT_CODE"
    fi
    if [ "$reload_count" -ge "$MAX_RELOADS" ]; then
        echo "egg.sh: reload limit ($MAX_RELOADS) exceeded" >&2
        exit "$RELOAD_EXIT_CODE"
    fi

    IFS= read -r EGG_RELOAD_THREAD_ID < "$RELOAD_STATE_FILE" || true
    if [ -z "$EGG_RELOAD_THREAD_ID" ]; then
        echo "egg.sh: reload state did not contain a thread id" >&2
        exit "$RELOAD_EXIT_CODE"
    fi
    export EGG_RELOAD_THREAD_ID
    reload_count=$((reload_count + 1))
done
