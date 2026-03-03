import { defineConfig, devices } from '@playwright/test';
import path from 'path';
import os from 'os';

// Use different ports for testing to avoid conflicts
const TEST_BACKEND_PORT = 8099;
const TEST_FRONTEND_PORT = 3099;

// Use isolated test database in temp directory
const TEST_DB_PATH = path.join(os.tmpdir(), 'eggw-test', 'threads.sqlite');

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false, // Run tests sequentially for predictable state
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: 'html',

  use: {
    baseURL: `http://localhost:${TEST_FRONTEND_PORT}`,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  // Run backend and frontend before tests on dedicated test ports
  webServer: [
    {
      // Enable test mode for mock LLM responses
      command: `mkdir -p "${path.dirname(TEST_DB_PATH)}" && cd ../backend && EGG_TEST_MODE=true EGG_DB_PATH="${TEST_DB_PATH}" uvicorn main:app --host 0.0.0.0 --port ${TEST_BACKEND_PORT}`,
      url: `http://localhost:${TEST_BACKEND_PORT}/health`,
      reuseExistingServer: false, // Always start fresh for tests
      timeout: 30000,
    },
    {
      command: `NEXT_PUBLIC_API_URL=http://localhost:${TEST_BACKEND_PORT} npm run dev -- -p ${TEST_FRONTEND_PORT}`,
      url: `http://localhost:${TEST_FRONTEND_PORT}`,
      reuseExistingServer: false, // Always start fresh for tests
      timeout: 60000,
    },
  ],
});
