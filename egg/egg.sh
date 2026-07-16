#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MONO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CALLER_CWD="$(pwd)"
VENV_DIR="$MONO_ROOT/venv"
RELOAD_EXIT_CODE=75
INITIAL_RELOAD_THREAD_ID="${EGG_RELOAD_THREAD_ID:-}"
if [ "${EGG_MAX_RELOADS+x}" = x ]; then
    MAX_RELOADS="$EGG_MAX_RELOADS"
else
    MAX_RELOADS=8
fi

case "$MAX_RELOADS" in
    ''|*[!0-9]*)
        echo "egg.sh: EGG_MAX_RELOADS must be a decimal integer from 0 to 100" >&2
        exit 2
        ;;
esac
# Lexical bounds avoid overflowing Bash arithmetic on hostile digit strings.
MAX_RELOADS="${MAX_RELOADS#${MAX_RELOADS%%[!0]*}}"
MAX_RELOADS="${MAX_RELOADS:-0}"
if [ "${#MAX_RELOADS}" -gt 3 ] || {
    [ "${#MAX_RELOADS}" -eq 3 ] && [[ "$MAX_RELOADS" > "100" ]];
}; then
    echo "egg.sh: EGG_MAX_RELOADS must be a decimal integer from 0 to 100" >&2
    exit 2
fi

RELOAD_STATE_FILE="$(mktemp "${TMPDIR:-/tmp}/egg-reload.XXXXXX")"
chmod 600 "$RELOAD_STATE_FILE"
child_pid=""
cleanup() {
    rm -f -- "$RELOAD_STATE_FILE"
}
trap cleanup EXIT HUP INT TERM

# Tests/embedders may provide an explicit interpreter/entrypoint. In that case
# do not provision or activate the ignored repository venv before launching it.
if [ -n "${EGG_PYTHON_BIN:-}" ]; then
    PYTHON_BIN="$EGG_PYTHON_BIN"
    SUPERVISOR_PYTHON="${EGG_SUPERVISOR_PYTHON:-python3}"
else
    if [ ! -f "$VENV_DIR/bin/activate" ]; then
        echo "First run — creating virtual environment..."
        python3 -m venv "$VENV_DIR"
        source "$VENV_DIR/bin/activate"
        echo "Installing egg-mono packages..."
        make -C "$MONO_ROOT" install
    else
        source "$VENV_DIR/bin/activate"
    fi
    PYTHON_BIN="python"
    SUPERVISOR_PYTHON="$PYTHON_BIN"
fi

# Load .env once per wrapper lifecycle, never once per reload generation.
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a && source "$SCRIPT_DIR/.env" && set +a
elif [ -f "$MONO_ROOT/.env" ]; then
    set -a && source "$MONO_ROOT/.env" && set +a
fi

export EGG_RELOAD_EXIT_CODE="$RELOAD_EXIT_CODE"
export EGG_RELOAD_STATE_FILE="$RELOAD_STATE_FILE"
if [ -n "$INITIAL_RELOAD_THREAD_ID" ]; then
    export EGG_RELOAD_THREAD_ID="$INITIAL_RELOAD_THREAD_ID"
else
    unset EGG_RELOAD_THREAD_ID
fi

exec env PYTHONSAFEPATH=1 "$SUPERVISOR_PYTHON" \
    "$SCRIPT_DIR/egg/launcher_supervisor.py" \
    --cwd "$CALLER_CWD" \
    --state-file "$RELOAD_STATE_FILE" \
    --reload-exit-code "$RELOAD_EXIT_CODE" \
    --max-reloads "$MAX_RELOADS" \
    -- \
    "$PYTHON_BIN" -c "from egg.app import main; main()" "$@"
