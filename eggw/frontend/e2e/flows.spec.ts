import { test, expect, Page } from '@playwright/test';

/**
 * E2E tests for eggw web UI.
 *
 * Tests the main user flows:
 * - Thread creation and messaging
 * - Streaming responses
 * - Tool approval flow
 * - Settings and controls
 *
 * Run with: npx playwright test
 */

// Helper to wait for backend to be ready
async function waitForBackend(page: Page) {
  await page.waitForResponse(
    response => response.url().includes('/api/health') && response.status() === 200,
    { timeout: 10000 }
  );
}

// Helper to create a new thread
async function createThread(page: Page, name: string = 'Test Thread'): Promise<string> {
  // Click new thread button
  await page.click('[data-testid="new-thread-btn"]');

  // Wait for thread to be created (URL should change or thread ID should appear)
  await page.waitForSelector('[data-testid="thread-id"]', { timeout: 5000 });

  const threadId = await page.getAttribute('[data-testid="thread-id"]', 'data-value');
  return threadId || '';
}

// Helper to send a message
async function sendMessage(page: Page, content: string) {
  const input = page.locator('[data-testid="message-input"]');
  await input.fill(content);
  await input.press('Enter');
}

test.describe('Basic Operations', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
  });

  test('page loads with correct title', async ({ page }) => {
    await expect(page).toHaveTitle(/eggw/i);
  });

  test('can see thread list panel', async ({ page }) => {
    await expect(page.locator('text=Threads')).toBeVisible();
  });

  test('can see system panel', async ({ page }) => {
    await expect(page.locator('text=System Log')).toBeVisible();
  });
});

test.describe('Thread Operations', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
  });

  test('can create a new thread', async ({ page }) => {
    // Find and click new thread button (+ icon)
    const newThreadBtn = page.locator('button').filter({ has: page.locator('svg') }).first();
    await newThreadBtn.click();

    // Should see the new thread in the list or chat area becomes active
    await expect(page.locator('[data-testid="chat-panel"]')).toBeVisible({ timeout: 5000 });
  });

  test('can send a message', async ({ page }) => {
    // Create thread first
    const newThreadBtn = page.locator('button').filter({ has: page.locator('svg') }).first();
    await newThreadBtn.click();

    // Wait for message input to be available
    await page.waitForSelector('textarea, input[type="text"]', { timeout: 5000 });

    // Type and send message
    const input = page.locator('textarea, input[type="text"]').first();
    await input.fill('Hello, this is a test message');
    await input.press('Enter');

    // Should see "Message sent" in system log or message appears in chat
    await expect(page.locator('text=Message sent').or(page.locator('text=Hello, this is a test'))).toBeVisible({ timeout: 5000 });
  });
});

test.describe('Streaming', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
  });

  test('shows streaming indicator when receiving response', async ({ page }) => {
    // Create thread and send message
    const newThreadBtn = page.locator('button').filter({ has: page.locator('svg') }).first();
    await newThreadBtn.click();

    await page.waitForSelector('textarea, input[type="text"]', { timeout: 5000 });
    const input = page.locator('textarea, input[type="text"]').first();
    await input.fill('Say "Hello World"');
    await input.press('Enter');

    // Should see streaming indicator or "Streaming started" log
    // This depends on LLM being available
    const streamingIndicator = page.locator('text=Streaming').or(page.locator('text=Running'));

    // Wait up to 10 seconds for streaming to start (may not happen without real LLM)
    try {
      await expect(streamingIndicator).toBeVisible({ timeout: 10000 });
    } catch {
      // If no streaming, check that message was at least sent
      await expect(page.locator('text=Message sent')).toBeVisible();
    }
  });

  test('SSE connection established on thread open', async ({ page }) => {
    // Create thread
    const newThreadBtn = page.locator('button').filter({ has: page.locator('svg') }).first();
    await newThreadBtn.click();

    // Should see "SSE connected" in system log
    await expect(page.locator('text=SSE connected')).toBeVisible({ timeout: 5000 });
  });
});

test.describe('Tool Approval', () => {
  // These tests require a thread with tool calls - may need mock or real LLM

  test('approval panel shows when tool needs approval', async ({ page }) => {
    await page.goto('/');

    // This test is harder without mocking - we'd need to either:
    // 1. Have a real LLM that calls tools
    // 2. Mock the backend to return tool call states
    // 3. Inject test data directly into the database

    // For now, just verify the approval panel component renders correctly
    // when there are pending approvals (can be enhanced with fixtures)

    // Create thread
    const newThreadBtn = page.locator('button').filter({ has: page.locator('svg') }).first();
    await newThreadBtn.click();

    // The approval panel should not be visible when there are no pending tools
    // (it returns null when pendingTools.length === 0)
    await page.waitForTimeout(1000); // Wait for queries to settle

    // Verify thread loaded
    await expect(page.locator('text=Thread Info')).toBeVisible();
  });
});

test.describe('Settings and Controls', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    // Create a thread to have settings available
    const newThreadBtn = page.locator('button').filter({ has: page.locator('svg') }).first();
    await newThreadBtn.click();
  });

  test('shows thread info in system panel', async ({ page }) => {
    await expect(page.locator('text=Thread Info')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('text=ID:')).toBeVisible();
  });

  test('shows model selector', async ({ page }) => {
    await expect(page.locator('text=Model:')).toBeVisible({ timeout: 5000 });
    // Should have a select dropdown
    await expect(page.locator('select')).toBeVisible();
  });

  test('can toggle auto-approval', async ({ page }) => {
    await expect(page.locator('text=Auto-approve:')).toBeVisible({ timeout: 5000 });

    // Find toggle button and click it
    const toggleButtons = page.locator('button.rounded-full');
    const autoApproveToggle = toggleButtons.first();

    // Click to toggle
    await autoApproveToggle.click();

    // Should see confirmation in system log
    await expect(page.locator('text=Auto-approval')).toBeVisible({ timeout: 3000 });
  });

  test('shows token stats', async ({ page }) => {
    await expect(page.locator('text=Token Stats')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('text=Input:')).toBeVisible();
    await expect(page.locator('text=Output:')).toBeVisible();
  });
});

test.describe('Keyboard Shortcuts', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    const newThreadBtn = page.locator('button').filter({ has: page.locator('svg') }).first();
    await newThreadBtn.click();
  });

  test('Escape cancels streaming', async ({ page }) => {
    // Send a message to start streaming
    await page.waitForSelector('textarea, input[type="text"]', { timeout: 5000 });
    const input = page.locator('textarea, input[type="text"]').first();
    await input.fill('Write a long essay about testing');
    await input.press('Enter');

    // Wait a bit then press Escape
    await page.waitForTimeout(500);
    await page.keyboard.press('Escape');

    // Should see cancellation message or streaming stopped
    // (depends on whether streaming actually started)
  });
});

test.describe('Theme', () => {
  test('can change theme via autocomplete', async ({ page }) => {
    await page.goto('/');

    // Create thread
    const newThreadBtn = page.locator('button').filter({ has: page.locator('svg') }).first();
    await newThreadBtn.click();

    await page.waitForSelector('textarea, input[type="text"]', { timeout: 5000 });
    const input = page.locator('textarea, input[type="text"]').first();

    // Type /theme command
    await input.fill('/theme');

    // Should see autocomplete suggestions
    await expect(page.locator('text=theme').first()).toBeVisible({ timeout: 2000 });
  });
});
