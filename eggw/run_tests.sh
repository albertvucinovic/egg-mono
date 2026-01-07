#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== eggw Test Runner ==="
echo ""

# Check for test type argument
TEST_TYPE="${1:-all}"

run_backend_tests() {
    echo ">>> Running Backend API Tests..."
    cd "$SCRIPT_DIR/backend"

    # Activate virtualenv
    if [ -f "$SCRIPT_DIR/../egg/venv/bin/activate" ]; then
        source "$SCRIPT_DIR/../egg/venv/bin/activate"
    fi

    # Install test dependencies if needed
    pip install -q pytest httpx httpx-sse 2>/dev/null || true

    # Run tests
    pytest test_api.py -v --tb=short
    echo ""
}

run_frontend_tests() {
    echo ">>> Running Frontend E2E Tests..."
    cd "$SCRIPT_DIR/frontend"

    # Install dependencies if needed
    if [ ! -d "node_modules/@playwright" ]; then
        echo "Installing Playwright..."
        npm install
        npx playwright install chromium
    fi

    # Run tests
    npx playwright test --reporter=list
    echo ""
}

case "$TEST_TYPE" in
    backend)
        run_backend_tests
        ;;
    frontend|e2e)
        run_frontend_tests
        ;;
    all)
        run_backend_tests
        run_frontend_tests
        ;;
    *)
        echo "Usage: $0 [backend|frontend|all]"
        echo ""
        echo "  backend  - Run backend API tests (pytest)"
        echo "  frontend - Run frontend E2E tests (Playwright)"
        echo "  all      - Run all tests (default)"
        exit 1
        ;;
esac

echo "=== All tests completed ==="
