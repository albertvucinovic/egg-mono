#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CALLER_CWD="$(pwd)"

cd $CALLER_CWD && source $SCRIPT_DIR/venv/bin/activate && set -a && source $SCRIPT_DIR/.env && set +a && python $SCRIPT_DIR/egg-textual.py
