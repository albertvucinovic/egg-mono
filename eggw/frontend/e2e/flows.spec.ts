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

// Helper to wait for page to be fully loaded
async function waitForPageLoad(page: Page) {
  // Wait for the page to have the header
  await page.waitForSelector('h1:has-text("eggw")', { timeout: 15000 });
  // Wait for a thread to be auto-selected (current UI auto-selects most recent thread)
  await page.waitForTimeout(2000);
}

// Helper to ensure we have a thread (creates one if none exists)
async function ensureThread(page: Page): Promise<void> {
  // Wait for page to load
  await waitForPageLoad(page);

  // Check if thread info panel is visible (indicates a thread is selected)
  const threadInfo = page.locator('text=Thread Info');
  try {
    await expect(threadInfo).toBeVisible({ timeout: 3000 });
  } catch {
    // No thread selected - create one via Ctrl+N
    await page.keyboard.press('Control+n');
    await page.waitForTimeout(1000);
    await expect(threadInfo).toBeVisible({ timeout: 5000 });
  }
}

// Helper to send a message
async function sendMessage(page: Page, content: string) {
  const input = page.locator('[data-testid="message-input"]');
  await expect(input).toBeVisible({ timeout: 5000 });
  await input.fill(content);
  await input.press('Enter');
}

test.describe('Basic Operations', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await waitForPageLoad(page);
  });

  test('page loads with correct title', async ({ page }) => {
    await expect(page).toHaveTitle(/eggw/i);
  });

  test('can see header with eggw title', async ({ page }) => {
    await expect(page.locator('h1:has-text("eggw")')).toBeVisible();
  });

  test('can see system log panel', async ({ page }) => {
    await expect(page.locator('text=System Log')).toBeVisible({ timeout: 5000 });
  });
});

test.describe('Thread Operations', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await waitForPageLoad(page);
  });

  test('can create a new thread via /newThread command', async ({ page }) => {
    // Use /newThread command to create a new thread
    const input = page.locator('[data-testid="message-input"]');
    await expect(input).toBeVisible({ timeout: 5000 });
    await input.fill('/newThread');
    await input.press('Enter');

    // Should see system log about created thread (text is "Created new thread: XXXX")
    await expect(page.locator('text=Created new thread')).toBeVisible({ timeout: 5000 });
  });

  test('can send a message', async ({ page }) => {
    // Ensure we have a thread
    await ensureThread(page);

    // Wait for message input
    const input = page.locator('[data-testid="message-input"]');
    await expect(input).toBeVisible({ timeout: 5000 });

    // Type and send message
    await input.fill('Hello, this is a test message');
    await input.press('Enter');

    // Should see "Message sent" in system log
    await expect(page.locator('text=Message sent')).toBeVisible({ timeout: 5000 });
  });
});

test.describe('Streaming', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await ensureThread(page);
  });

  test('shows SSE connected after thread selection', async ({ page }) => {
    // Should see "SSE connected" in system log
    await expect(page.locator('text=SSE connected')).toBeVisible({ timeout: 5000 });
  });

  test('shows streaming when receiving response', async ({ page }) => {
    const input = page.locator('[data-testid="message-input"]');
    await expect(input).toBeVisible({ timeout: 5000 });
    await input.fill('Say "Hello World"');
    await input.press('Enter');

    // Should see streaming indicator or "Streaming" log
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
});

test.describe('Settings and Controls', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await ensureThread(page);
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

  test('shows token stats', async ({ page }) => {
    await expect(page.locator('text=Token Stats')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('text=Input:')).toBeVisible();
    await expect(page.locator('text=Output:')).toBeVisible();
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
});

test.describe('Keyboard Shortcuts', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await ensureThread(page);
  });

  test('help modal opens via button click', async ({ page }) => {
    // Click the "Press ? for help" button to open help modal
    await page.click('text=Press ? for help');
    await expect(page.locator('text=Keyboard Shortcuts')).toBeVisible({ timeout: 3000 });
  });

  test('help modal closes with Close button', async ({ page }) => {
    // Click the "Press ? for help" button to open help modal
    await page.click('text=Press ? for help');
    await expect(page.locator('text=Keyboard Shortcuts')).toBeVisible({ timeout: 3000 });
    // Click the Close button
    await page.click('button:has-text("Close")');
    await expect(page.locator('text=Keyboard Shortcuts')).not.toBeVisible({ timeout: 2000 });
  });

  test('i focuses input', async ({ page }) => {
    // First click somewhere to unfocus input
    await page.click('h1');
    await page.keyboard.press('i');

    const input = page.locator('[data-testid="message-input"]');
    await expect(input).toBeFocused({ timeout: 2000 });
  });
});

test.describe('Commands', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await ensureThread(page);
  });

  test('slash shows autocomplete', async ({ page }) => {
    const input = page.locator('[data-testid="message-input"]');
    await expect(input).toBeVisible({ timeout: 5000 });

    // Type /
    await input.fill('/');

    // Should see autocomplete suggestions
    await expect(page.locator('[data-testid="autocomplete"]').or(page.locator('text=/newThread'))).toBeVisible({ timeout: 2000 });
  });

  test('/help command works', async ({ page }) => {
    const input = page.locator('[data-testid="message-input"]');
    await expect(input).toBeVisible({ timeout: 5000 });

    // Type /help
    await input.fill('/help');
    await input.press('Enter');

    // Should see help response
    await expect(page.locator('text=Available commands').or(page.locator('text=/help'))).toBeVisible({ timeout: 5000 });
  });
});
