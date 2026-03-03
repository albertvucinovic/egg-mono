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

    # Install test dependencies if needed
    pip install -q pytest httpx httpx-sse 2>/dev/null || true

    # Run tests (uses isolated test database automatically)
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

    # Clean test database to ensure isolation
    rm -rf /tmp/eggw-test

    # Run tests (uses isolated test database on ports 8099/3099)
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
        echo ""
        echo "Tests use isolated databases and ports (8099/3099) to avoid"
        echo "conflicts with running instances."
        exit 1
        ;;
esac

echo "=== All tests completed ==="
