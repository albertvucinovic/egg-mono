#!/bin/bash
# Generate HTML documentation using pdoc3
#
# Usage:
#   ./scripts/generate_pdoc.sh          # Generate static HTML to docs/
#   ./scripts/generate_pdoc.sh serve    # Start interactive server on :8080
#
# Install pdoc3 first:
#   pip install pdoc3
#   # or: pip install -e ".[dev]"

set -e
cd "$(dirname "$0")/.."

if ! command -v pdoc &> /dev/null; then
    echo "pdoc3 not found. Install with: pip install pdoc3"
    exit 1
fi

if [ "$1" = "serve" ]; then
    echo "Starting pdoc server at http://localhost:8080"
    pdoc --http :8080 eggflow
else
    echo "Generating HTML documentation to docs/"
    pdoc --html eggflow --output-dir ./docs --force
    echo "Done. Open docs/eggflow/index.html to view."
fi
