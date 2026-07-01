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

const TEST_API_BASE = 'http://localhost:8099';

const mockApiHeaders = {
  'access-control-allow-origin': '*',
  'access-control-allow-methods': 'GET, POST, OPTIONS',
  'access-control-allow-headers': 'content-type',
};

function mockGeneratedImageMessage(threadId: string, prompt: string) {
  const artifactPart = {
    type: 'artifact',
    artifact_id: 'abc12345',
    owner_thread_id: threadId,
    presentation: 'image',
    mime_type: 'image/png',
    filename: 'generated-egg.png',
    size_bytes: 1234,
    sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
    provenance: {
      kind: 'openai_image_generation',
      provider: 'mock-provider',
      model_key: 'Mock Image Backend',
    },
    options: {},
  };
  const textPart = {
    type: 'text',
    text: `Generated 1 image artifact via Mock Image Backend (mock-image-model).\nPrompt: ${prompt}`,
  };

  return {
    id: 'img-message-1',
    role: 'assistant',
    content: [textPart, artifactPart],
    content_text: `${textPart.text}\n[Provider artifact: image generated-egg.png image/png 1.21 KB sha256:01234567 artifact_id:abc12345]`,
  };
}



async function expectMonacoDraft(page: Page, visibleText: string) {
  const draft = page.getByTestId('edit-answer-draft');
  await expect(draft).toHaveAttribute('data-editor', 'monaco', { timeout: 15000 });
  await expect(draft.locator('.view-lines')).toContainText(visibleText, { timeout: 10000 });
}

async function replaceMonacoDraft(page: Page, value: string) {
  const draft = page.getByTestId('edit-answer-draft');
  await expect(draft).toHaveAttribute('data-editor', 'monaco', { timeout: 15000 });
  await draft.locator('.monaco-editor').click();
  await page.keyboard.press('Control+A');
  await page.keyboard.type(value);
  await expect(draft.locator('.view-lines')).toContainText(value.replace(/^>\s*/, ''), { timeout: 10000 });
}

async function mockThreadShell(page: Page, threadId: string, options: { messages?: unknown[] } = {}) {
  await page.route(`${TEST_API_BASE}/api/threads/${threadId}/events`, async (route) => {
    await route.fulfill({
      status: 200,
      headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
      body: '',
    });
  });
  await page.route(`${TEST_API_BASE}/api/threads/${threadId}/open`, async (route) => {
    await route.fulfill({ status: 200, headers: mockApiHeaders, json: { status: 'opened' } });
  });
  await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route) => {
    await route.fulfill({ status: 200, headers: mockApiHeaders, json: options.messages || [] });
  });
  await page.route(`${TEST_API_BASE}/api/threads/${threadId}/stats`, async (route) => {
    await route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: {
        input_tokens: 0,
        output_tokens: 0,
        reasoning_tokens: 0,
        cached_tokens: 0,
        context_tokens: 0,
        full_thread_tokens: 0,
        total_tokens: 0,
        cost_usd: 0,
      },
    });
  });
  await page.route(`${TEST_API_BASE}/api/threads/${threadId}/tools`, async (route) => {
    await route.fulfill({ status: 200, headers: mockApiHeaders, json: [] });
  });
  await page.route(`${TEST_API_BASE}/api/threads/${threadId}/sandbox`, async (route) => {
    await route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: { enabled: false, effective: false, available: false, user_control_enabled: true },
    });
  });
  await page.route(`${TEST_API_BASE}/api/threads/${threadId}/state`, async (route) => {
    await route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: { state: 'waiting_user', active_get_user_wait: false },
    });
  });
  await page.route(`${TEST_API_BASE}/api/threads/${threadId}/settings`, async (route) => {
    await route.fulfill({ status: 200, headers: mockApiHeaders, json: { auto_approval: false } });
  });
  await page.route(`${TEST_API_BASE}/api/threads/${threadId}/children`, async (route) => {
    await route.fulfill({ status: 200, headers: mockApiHeaders, json: [] });
  });
  await page.route(`${TEST_API_BASE}/api/threads/${threadId}`, async (route) => {
    await route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: { id: threadId, name: 'Edit Answer UI Test', has_children: false },
    });
  });
  await page.route(`${TEST_API_BASE}/api/threads/roots`, async (route) => {
    await route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: [{ id: threadId, name: 'Edit Answer UI Test', has_children: false }],
    });
  });
  await page.route(`${TEST_API_BASE}/api/models`, async (route) => {
    await route.fulfill({ status: 200, headers: mockApiHeaders, json: { models: [], default_model: null } });
  });
  await page.route(`${TEST_API_BASE}/api/threads`, async (route) => {
    await route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: [{ id: threadId, name: 'Edit Answer UI Test', has_children: false }],
    });
  });
}

function mockImageAttachmentMessage(threadId: string) {
  const attachmentPart = {
    type: 'attachment',
    input_id: 'input123',
    owner_thread_id: threadId,
    presentation: 'image',
    mime_type: 'image/png',
    filename: 'attached-egg.png',
    size_bytes: 1234,
    sha256: 'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789',
    options: {},
  };
  const textPart = { type: 'text', text: 'Attached image for preview' };

  return {
    id: 'attachment-message-1',
    role: 'user',
    content: [textPart, attachmentPart],
    content_text: `${textPart.text}\n[Attachment: image attached-egg.png image/png 1.21 KB sha256:abcdef01]`,
  };
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

  test('can upload, stage, and send an attachment', async ({ page }) => {
    await ensureThread(page);

    const input = page.locator('[data-testid="message-input"]');
    await expect(input).toBeVisible({ timeout: 5000 });

    await page.locator('[data-testid="attachment-file-input"]').setInputFiles({
      name: 'note.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('hello attachment'),
    });

    await expect(page.locator('[data-testid="staged-attachments"]')).toContainText('note.txt', { timeout: 5000 });
    await input.fill('See attached');
    await input.press('Enter');

    await expect(page.locator('text=Message sent')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('[data-testid="staged-attachments"]')).not.toBeVisible({ timeout: 5000 });
    await expect(page.locator('text=Attachment')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('text=note.txt')).toBeVisible({ timeout: 5000 });
  });

});

test.describe('Attachment Composer UX', () => {
  test('can drag and drop an image attachment into staging with mocked backend', async ({ page }) => {
    const threadId = 'drop-thread-1';
    let uploadCalled = false;

    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/events`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body: '',
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/open`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { status: 'opened' } });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/messages`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: [] });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/attachments`, async (route, request) => {
      uploadCalled = true;
      expect(request.method()).toBe('POST');
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          input_id: 'drop1234',
          metadata: {
            input_id: 'drop1234',
            owner_thread_id: threadId,
            filename: 'drop-egg.png',
            mime_type: 'image/png',
            presentation: 'image',
            size_bytes: 3,
            sha256: 'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789',
          },
          content_part: {
            type: 'attachment',
            input_id: 'drop1234',
            owner_thread_id: threadId,
            filename: 'drop-egg.png',
            mime_type: 'image/png',
            presentation: 'image',
            size_bytes: 3,
            sha256: 'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789',
            options: {},
          },
          content_text: '[Attachment: image drop-egg.png image/png 3 B sha256:abcdef01]',
        },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/attachments/drop1234`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: { ...mockApiHeaders, 'content-type': 'image/png' },
        body: Buffer.from(
          'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/axS6S0AAAAASUVORK5CYII=',
          'base64',
        ),
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/stats`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          input_tokens: 0,
          output_tokens: 0,
          reasoning_tokens: 0,
          cached_tokens: 0,
          context_tokens: 0,
          full_thread_tokens: 0,
          total_tokens: 0,
          cost_usd: 0,
        },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/tools`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: [] });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/sandbox`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { enabled: false, effective: false, available: false, user_control_enabled: true },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/state`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { state: 'waiting_user', active_get_user_wait: false },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/settings`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { auto_approval: false } });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/children`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: [] });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { id: threadId, name: 'Drop UI Test', has_children: false },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/roots`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: [{ id: threadId, name: 'Drop UI Test', has_children: false }],
      });
    });
    await page.route(`${TEST_API_BASE}/api/models`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: [] });
    });
    await page.route(`${TEST_API_BASE}/api/threads`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: [{ id: threadId, name: 'Drop UI Test', has_children: false }],
      });
    });

    await page.goto(`/${threadId}`);
    const composer = page.getByTestId('message-composer');
    await expect(composer).toBeVisible({ timeout: 5000 });

    const dataTransfer = await page.evaluateHandle(() => {
      const transfer = new DataTransfer();
      transfer.items.add(new File([new Uint8Array([1, 2, 3])], 'drop-egg.png', { type: 'image/png' }));
      return transfer;
    });
    await composer.dispatchEvent('dragenter', { dataTransfer: dataTransfer as any });
    await expect(page.getByTestId('attachment-drop-overlay')).toBeVisible({ timeout: 5000 });
    await composer.dispatchEvent('drop', { dataTransfer: dataTransfer as any });

    await expect.poll(() => uploadCalled).toBe(true);
    await expect(page.getByTestId('staged-attachments')).toContainText('drop-egg.png', { timeout: 5000 });
    const stagedPreview = page.getByTestId('staged-attachment-preview');
    await expect(stagedPreview).toHaveAttribute('src', `${TEST_API_BASE}/api/threads/${threadId}/attachments/drop1234`);
    await expect(stagedPreview).toHaveAttribute('loading', 'lazy');
  });
});

test.describe('Image Generation UI', () => {
  test('can generate an image from the composer with mocked backend', async ({ page }) => {
    const threadId = 'image-thread-1';
    const prompt = 'A tiny egg robot painting pixels';
    const generatedMessage = mockGeneratedImageMessage(threadId, prompt);
    const attachmentMessage = mockImageAttachmentMessage(threadId);
    const messagesRequests: string[] = [];
    const messagesRequestsAfterGeneration: string[] = [];
    let imageGenerationRequest: Record<string, unknown> | undefined;
    let imageGenerated = false;

    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/events`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body: '',
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/open`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { status: 'opened' } });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/messages`, async (route, request) => {
      messagesRequests.push(request.method());
      if (imageGenerated) messagesRequestsAfterGeneration.push(request.method());
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: imageGenerated ? [generatedMessage, attachmentMessage] : [],
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/image-generation`, async (route, request) => {
      if (request.method() === 'OPTIONS') {
        await route.fulfill({ status: 204, headers: mockApiHeaders });
        return;
      }
      imageGenerationRequest = request.postDataJSON() as Record<string, unknown>;
      imageGenerated = true;
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          message_id: generatedMessage.id,
          prompt,
          model_key: 'Mock Image Backend',
          provider_name: 'mock-provider',
          model_name: 'mock-image-model',
          metadata: [
            {
              artifact_id: 'abc12345',
              owner_thread_id: threadId,
              filename: 'generated-egg.png',
              mime_type: 'image/png',
              presentation: 'image',
              size_bytes: 1234,
              sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
            },
          ],
          content_parts: generatedMessage.content,
          content_text: generatedMessage.content_text,
          response_metadata: { id: 'mock-response' },
        },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/provider-output/abc12345`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: { ...mockApiHeaders, 'content-type': 'image/png' },
        body: Buffer.from(
          'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/axS6S0AAAAASUVORK5CYII=',
          'base64',
        ),
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/attachments/input123`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: { ...mockApiHeaders, 'content-type': 'image/png' },
        body: Buffer.from(
          'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/axS6S0AAAAASUVORK5CYII=',
          'base64',
        ),
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/stats`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          input_tokens: 0,
          output_tokens: 0,
          reasoning_tokens: 0,
          cached_tokens: 0,
          context_tokens: 0,
          full_thread_tokens: 0,
          total_tokens: 0,
          cost_usd: 0,
        },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/tools`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: [] });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/sandbox`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { enabled: false, effective: false, available: false, user_control_enabled: true },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/state`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { state: 'waiting_user', active_get_user_wait: false },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/settings`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { auto_approval: false } });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/children`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: [] });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { id: threadId, name: 'Image UI Test', has_children: false },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/roots`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: [{ id: threadId, name: 'Image UI Test', has_children: false }],
      });
    });
    await page.route(`${TEST_API_BASE}/api/models`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: [] });
    });
    await page.route(`${TEST_API_BASE}/api/threads`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: [{ id: threadId, name: 'Image UI Test', has_children: false }],
      });
    });

    await page.goto('/image-thread-1');
    await page.getByTitle('Show sidebar').click();
    await expect(page.locator('text=System Log')).toBeVisible({ timeout: 5000 });

    await page.getByTestId('image-generation-toggle').click();
    await expect(page.getByTestId('image-generation-form')).toBeVisible({ timeout: 5000 });

    await page.getByTestId('image-generation-prompt').fill(prompt);
    await page.getByTestId('image-generation-model').fill('Mock Image Backend');
    await page.getByTestId('image-generation-count').selectOption('2');
    await page.getByTestId('image-generation-size').fill('1024x1024');
    await page.getByTestId('image-generation-submit').click();

    await expect.poll(() => imageGenerationRequest).toEqual({
      prompt,
      model: 'Mock Image Backend',
      n: 2,
      size: '1024x1024',
    });
    await expect.poll(() => messagesRequests.length).toBeGreaterThanOrEqual(1);
    await expect.poll(() => messagesRequestsAfterGeneration.length).toBeGreaterThanOrEqual(1);
    await expect(page.locator('text=Generated 1 artifact; appended result to transcript')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('text=Provider artifact')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('text=generated-egg.png')).toBeVisible({ timeout: 5000 });
    const preview = page.getByTestId('provider-artifact-preview');
    await expect(preview).toBeVisible({ timeout: 5000 });
    await expect(preview).toHaveAttribute('src', `${TEST_API_BASE}/api/threads/${threadId}/provider-output/abc12345`);
    await expect(preview).toHaveAttribute('loading', 'lazy');
    const attachmentPreview = page.getByTestId('attachment-preview');
    await expect(attachmentPreview).toBeVisible({ timeout: 5000 });
    await expect(attachmentPreview).toHaveAttribute('src', `${TEST_API_BASE}/api/threads/${threadId}/attachments/input123`);
    await expect(attachmentPreview).toHaveAttribute('loading', 'lazy');
  });
});



test.describe('Command Transcript Ordering', () => {
  test('keeps local command output visible when backend transcript is empty', async ({ page }) => {
    const threadId = 'command-empty-thread-1';
    await mockThreadShell(page, threadId, { messages: [] });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/command`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          success: true,
          message: 'Help output stays visible',
          command_id: 'command-empty-1',
          command_name: 'help',
          started_at: '2026-01-01T00:00:00.000Z',
          finished_at: '2026-01-01T00:00:00.000Z',
          elapsed_sec: 0.01,
          data: { action: 'help' },
        },
      });
    });

    await page.goto(`/${threadId}`);
    const input = page.getByTestId('message-input');
    await expect(input).toBeVisible({ timeout: 5000 });
    await input.fill('/help');
    await input.press('Enter');

    await expect(page.getByTestId('chat-panel-content')).toContainText('Help output stays visible', { timeout: 5000 });
  });

  test('inserts local command output by response timestamp', async ({ page }) => {
    const threadId = 'command-order-thread-1';
    const beforeTimestamp = '2026-01-01T00:00:00.000Z';
    const commandTimestamp = '2026-01-01T00:00:01.000Z';
    const afterTimestamp = '2026-01-01T00:00:02.000Z';

    await mockThreadShell(page, threadId, {
      messages: [
        { id: 'message-before-command', role: 'user', timestamp: beforeTimestamp, content: 'Before command', content_text: 'Before command' },
        { id: 'message-after-command', role: 'assistant', timestamp: afterTimestamp, content: 'After command', content_text: 'After command' },
      ],
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/command`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          success: true,
          message: 'Command output in timestamp position',
          command_id: 'command-order-1',
          command_name: 'attachments',
          started_at: commandTimestamp,
          finished_at: commandTimestamp,
          elapsed_sec: 0.01,
          data: { action: 'list_attachments' },
        },
      });
    });

    await page.goto(`/${threadId}`);
    const input = page.getByTestId('message-input');
    await expect(input).toBeVisible({ timeout: 5000 });
    await input.fill('/attachments');
    await input.press('Enter');

    const chatContent = page.getByTestId('chat-panel-content');
    await expect(chatContent).toContainText('Command output in timestamp position', { timeout: 5000 });
    const orderedText = await chatContent.innerText();
    expect(orderedText.indexOf('Before command')).toBeLessThan(orderedText.indexOf('Command output in timestamp position'));
    expect(orderedText.indexOf('Command output in timestamp position')).toBeLessThan(orderedText.indexOf('After command'));
  });
});

test.describe('Edit Answer Modal', () => {
  test('typing /editAnswer opens modal and loading draft populates composer without transcript pollution', async ({ page }) => {
    const threadId = 'edit-answer-thread-1';
    let commandRequest: Record<string, unknown> | undefined;
    const messages = [{ id: 'assistant-1', role: 'assistant', content: 'Original answer', content_text: 'Original answer' }];

    await mockThreadShell(page, threadId, { messages });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/command`, async (route, request) => {
      commandRequest = request.postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          success: true,
          message: 'Prepared quoted assistant answer stant-1.',
          command_id: 'cmd-edit-answer-1',
          command_name: 'editAnswer',
          started_at: new Date().toISOString(),
          finished_at: new Date().toISOString(),
          elapsed_sec: 0.01,
          data: {
            action: 'open_edit_answer_modal',
            draft: '> Original answer',
            source_msg_id: 'assistant-1',
            source_kind: 'assistant_answer',
            source_suffix: 'stant-1',
            source_label: 'assistant answer',
            suppress_transcript: true,
            message: 'Prepared quoted assistant answer stant-1.',
          },
        },
      });
    });

    await page.goto(`/${threadId}`);
    const input = page.getByTestId('message-input');
    await expect(input).toBeVisible({ timeout: 5000 });

    await input.fill('/editAnswer');
    await input.press('Enter');

    await expect.poll(() => commandRequest).toMatchObject({ command: '/editAnswer' });
    await expect(page.getByTestId('edit-answer-modal')).toBeVisible({ timeout: 5000 });
    await expectMonacoDraft(page, 'Original answer');
    await expect(page.getByTestId('chat-panel-content')).not.toContainText('Prepared quoted assistant answer');

    await replaceMonacoDraft(page, '> Edited in Monaco');
    await page.getByTestId('edit-answer-load').click();
    await expect(page.getByTestId('edit-answer-modal')).not.toBeVisible({ timeout: 5000 });
    await expect(input).toHaveValue('> Edited in Monaco');
  });

  test('does not silently overwrite unrelated composer text', async ({ page }) => {
    const threadId = 'edit-answer-thread-2';
    await mockThreadShell(page, threadId, {
      messages: [{ id: 'assistant-2', role: 'assistant', content: 'Answer two', content_text: 'Answer two' }],
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/command`, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 300));
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          success: true,
          message: 'Prepared quoted assistant answer stant-2.',
          command_id: 'cmd-edit-answer-2',
          command_name: 'editAnswer',
          started_at: new Date().toISOString(),
          finished_at: new Date().toISOString(),
          elapsed_sec: 0.01,
          data: {
            action: 'open_edit_answer_modal',
            draft: '> Answer two',
            source_msg_id: 'assistant-2',
            source_kind: 'assistant_answer',
            source_suffix: 'stant-2',
            source_label: 'assistant answer',
            suppress_transcript: true,
          },
        },
      });
    });

    await page.goto(`/${threadId}`);
    const input = page.getByTestId('message-input');
    await expect(input).toBeVisible({ timeout: 5000 });

    await input.fill('/editAnswer');
    await input.press('Enter');
    await expect(input).toHaveValue('');
    await input.fill('Keep this draft');

    await expect(page.getByTestId('edit-answer-modal')).toBeVisible({ timeout: 5000 });
    await expectMonacoDraft(page, 'Answer two');
    await expect(page.getByTestId('edit-answer-overwrite-warning')).toBeVisible();
    await expect(page.getByTestId('edit-answer-load')).not.toBeVisible();
    await page.getByTestId('edit-answer-append').click();

    await expect(input).toHaveValue('Keep this draft\n\n> Answer two');
  });

  test('failed /editAnswer displays an error without opening the modal', async ({ page }) => {
    const threadId = 'edit-answer-thread-3';
    await mockThreadShell(page, threadId, {
      messages: [{ id: 'assistant-3', role: 'assistant', content: 'Answer three', content_text: 'Answer three' }],
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/command`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          success: false,
          message: "/editAnswer failed: No assistant answer matched selector 'missing'.",
          command_id: 'cmd-edit-answer-3',
          command_name: 'editAnswer',
          started_at: new Date().toISOString(),
          finished_at: new Date().toISOString(),
          elapsed_sec: 0.01,
          data: null,
        },
      });
    });

    await page.goto(`/${threadId}`);
    const input = page.getByTestId('message-input');
    await expect(input).toBeVisible({ timeout: 5000 });

    await input.fill('/editAnswer missing');
    await input.press('Enter');

    await expect(page.getByTestId('edit-answer-modal')).not.toBeVisible({ timeout: 5000 });
    await expect(page.getByTestId('chat-panel-content')).toContainText("Error: /editAnswer failed: No assistant answer matched selector 'missing'.");
    await expect(input).toHaveValue('');
  });
});


test.describe('Quote/Edit Button', () => {
  test('shows Quote/Edit only on assistant answers and Assistant Notes', async ({ page }) => {
    const threadId = 'quote-button-thread-1';
    await mockThreadShell(page, threadId, {
      messages: [
        { id: 'user-message-1', role: 'user', content: 'Question', content_text: 'Question' },
        { id: 'assistant-answer-1', role: 'assistant', content: 'Answer', content_text: 'Answer' },
        { id: 'assistant-note-1', role: 'assistant', content: 'Waiting note', content_text: 'Waiting note', answer_user_preserve_turn: true },
        { id: 'tool-message-1', role: 'tool', content: 'Tool output', content_text: 'Tool output' },
        { id: 'system-message-1', role: 'system', content: 'System output', content_text: 'System output' },
      ],
    });

    await page.goto(`/${threadId}`);

    const quoteButtons = page.getByTestId('quote-edit-button');
    await expect(quoteButtons).toHaveCount(2);
    await expect(page.getByRole('button', { name: /Quote\/Edit Assistant assistant-answer-1/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Quote\/Edit Assistant Note assistant-note-1/ })).toBeVisible();
    await expect(page.getByText('User').locator('..').getByTestId('quote-edit-button')).toHaveCount(0);
  });

  test('clicking Quote/Edit calls exact source_msg_id endpoint and opens the existing modal', async ({ page }) => {
    const threadId = 'quote-button-thread-2';
    let draftRequest: Record<string, unknown> | undefined;
    await mockThreadShell(page, threadId, {
      messages: [
        { id: 'assistant-answer-2', role: 'assistant', content: 'Selected answer', content_text: 'Selected answer' },
      ],
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/edit-answer-draft`, async (route, request) => {
      draftRequest = request.postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          action: 'open_edit_answer_modal',
          draft: '> Selected answer',
          source_msg_id: 'assistant-answer-2',
          source_kind: 'assistant_answer',
          source_suffix: 'answer-2',
          source_label: 'assistant answer',
          suppress_transcript: true,
          message: 'Prepared quoted assistant answer answer-2.',
        },
      });
    });

    await page.goto(`/${threadId}`);
    await page.getByTestId('quote-edit-button').click();

    await expect.poll(() => draftRequest).toEqual({ source_msg_id: 'assistant-answer-2' });
    await expect(page.getByTestId('edit-answer-modal')).toBeVisible({ timeout: 5000 });
    await expectMonacoDraft(page, 'Selected answer');
    await expect(page.getByTestId('chat-panel-content')).not.toContainText('Prepared quoted assistant answer');
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

test.describe('Live Tool Streaming', () => {
  test('keeps tool arguments visible while the tool is running', async ({ page }) => {
    const threadId = 'running-tool-args-thread';
    const toolCallId = 'call-running-tool-args';

    await page.addInitScript(({ tcId }) => {
      class MockEventSource {
        url: string;
        readyState = 1;
        onopen: ((event: Event) => void) | null = null;
        onerror: ((event: Event) => void) | null = null;
        private listeners: Record<string, Array<(event: MessageEvent) => void>> = {};

        constructor(url: string) {
          this.url = url;
          window.setTimeout(() => {
            this.onopen?.(new Event('open'));
            this.dispatch('stream.open', {
              event_type: 'stream.open',
              ts: new Date().toISOString(),
              payload: { stream_kind: 'tool' },
            });
            this.dispatch('tool_call.execution_started', {
              event_type: 'tool_call.execution_started',
              ts: new Date().toISOString(),
              payload: {
                tool_call_id: tcId,
                name: 'bash',
                arguments: JSON.stringify({ script: 'echo visible args; sleep 30', timeout: 300 }),
                timeout: 300,
              },
            });
          }, 25);
        }

        addEventListener(type: string, listener: (event: MessageEvent) => void) {
          this.listeners[type] = [...(this.listeners[type] || []), listener];
        }

        removeEventListener(type: string, listener: (event: MessageEvent) => void) {
          this.listeners[type] = (this.listeners[type] || []).filter((item) => item !== listener);
        }

        close() {
          this.readyState = 2;
        }

        private dispatch(type: string, data: unknown) {
          const event = new MessageEvent(type, { data: JSON.stringify(data) });
          for (const listener of this.listeners[type] || []) listener(event);
        }
      }

      (window as unknown as { EventSource: typeof EventSource }).EventSource = MockEventSource as unknown as typeof EventSource;
    }, { tcId: toolCallId });

    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/open`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { status: 'opened' } });
    });
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: [{ id: 'user-before-running-tool', role: 'user', content: 'run slow tool', content_text: 'run slow tool' }],
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/stats`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          input_tokens: 0,
          output_tokens: 0,
          reasoning_tokens: 0,
          cached_tokens: 0,
          context_tokens: 0,
          full_thread_tokens: 0,
          total_tokens: 0,
          cost_usd: 0,
        },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/tools`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: [] });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/sandbox`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { enabled: false, effective: false, available: false, user_control_enabled: true },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/state`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { state: 'running', active_get_user_wait: false },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/settings`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { auto_approval: false } });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/children`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: [] });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { id: threadId, name: 'Running Tool Args', has_children: false },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/roots`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: [{ id: threadId, name: 'Running Tool Args', has_children: false }],
      });
    });
    await page.route(`${TEST_API_BASE}/api/models`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { models: [] } });
    });
    await page.route(`${TEST_API_BASE}/api/image-models`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { models: [] } });
    });
    await page.route(`${TEST_API_BASE}/api/threads`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: [{ id: threadId, name: 'Running Tool Args', has_children: false }],
      });
    });

    await page.goto(`/${threadId}`);

    await expect(page.getByTestId('chat-panel')).toContainText('Tool', { timeout: 5000 });
    await expect(page.getByTestId('chat-panel')).toContainText('bash', { timeout: 5000 });
    await expect(page.getByTestId('chat-panel')).toContainText('$ echo visible args; sleep 30', { timeout: 5000 });
    await expect(page.getByTestId('chat-panel')).toContainText('streaming output...', { timeout: 5000 });
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

test.describe('Mock LLM Responses', () => {
  // These tests use the MockLLMClient (EGG_TEST_MODE=true)
  // Note: Tests create a new thread each time to ensure clean state

  test('receives mock LLM text response', async ({ page }) => {
    await page.goto('/');

    // Create a fresh thread via /newThread command
    const input = page.locator('[data-testid="message-input"]');
    await page.waitForSelector('[data-testid="message-input"]', { timeout: 15000 });
    await input.fill('/newThread');
    await input.press('Enter');
    await page.waitForTimeout(1000);

    // Send a simple message that triggers "hello" response
    await input.fill('Hello there!');
    await input.press('Enter');

    // Wait for mock LLM response - the mock returns "Hello! I'm a mock LLM for testing"
    await expect(
      page.locator('text=mock LLM for testing').first()
    ).toBeVisible({ timeout: 20000 });
  });

  test('assistant response appears in chat', async ({ page }) => {
    await page.goto('/');

    // Create a fresh thread
    const input = page.locator('[data-testid="message-input"]');
    await page.waitForSelector('[data-testid="message-input"]', { timeout: 15000 });
    await input.fill('/newThread');
    await input.press('Enter');
    await page.waitForTimeout(1000);

    // Send a message
    await input.fill('What is this?');
    await input.press('Enter');

    // Should see an assistant message appear
    await expect(
      page.locator('text=Assistant').first()
    ).toBeVisible({ timeout: 20000 });
  });
});

test.describe('Mock LLM Tool Calls', () => {
  // These tests verify tool call flow with MockLLMClient
  // Each test creates a fresh thread to ensure clean state

  test('triggers bash tool call from command request', async ({ page }) => {
    await page.goto('/');

    // Create a fresh thread
    const input = page.locator('[data-testid="message-input"]');
    await page.waitForSelector('[data-testid="message-input"]', { timeout: 15000 });
    await input.fill('/newThread');
    await input.press('Enter');
    await page.waitForTimeout(1000);

    // Send a message that triggers bash tool
    await input.fill('Please run the command: $ ls -la');
    await input.press('Enter');

    // Should see the bash tool call appear (either in approval panel or chat)
    await expect(
      page.locator('text=bash').first()
    ).toBeVisible({ timeout: 20000 });
  });

  test('tool call shows in approval panel', async ({ page }) => {
    await page.goto('/');

    // Create a fresh thread
    const input = page.locator('[data-testid="message-input"]');
    await page.waitForSelector('[data-testid="message-input"]', { timeout: 15000 });
    await input.fill('/newThread');
    await input.press('Enter');
    await page.waitForTimeout(1000);

    // Send a message that triggers a tool call
    await input.fill('Execute command $ pwd');
    await input.press('Enter');

    // Should see Pending Approvals or Approve button
    await expect(
      page.locator('text=Pending Approvals').or(page.locator('text=Approve'))
    ).toBeVisible({ timeout: 20000 });
  });

  test('tool execution with auto-approve shows result', async ({ page }) => {
    await page.goto('/');

    // Create a fresh thread
    const input = page.locator('[data-testid="message-input"]');
    await page.waitForSelector('[data-testid="message-input"]', { timeout: 15000 });
    await input.fill('/newThread');
    await input.press('Enter');
    await page.waitForTimeout(1000);

    // Enable auto-approve via command
    await input.fill('/toggleAutoApproval');
    await input.press('Enter');
    await page.waitForTimeout(500);

    // Send a message that triggers a tool call
    await input.fill('Run command $ echo test');
    await input.press('Enter');

    // With auto-approve, should see Tool Result or execution
    await expect(
      page.locator('text=Tool Result').first()
    ).toBeVisible({ timeout: 20000 });
  });
});
