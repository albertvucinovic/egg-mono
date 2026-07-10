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

test.describe('Browser authentication', () => {
  test('public/manual mode gates API access until the operator token is verified', async ({ page }) => {
    await page.route('**/api/eggw-bootstrap', (route) => route.fulfill({ status: 404, json: { detail: 'disabled' } }));
    const apiRequests: Array<{ authorization: string | undefined; url: string }> = [];
    await page.route(`${TEST_API_BASE}/api/threads`, async (route, request) => {
      apiRequests.push({ authorization: request.headers()['authorization'], url: request.url() });
      const authorized = request.headers()['authorization'] === 'Bearer ' + 'm'.repeat(48);
      await route.fulfill({
        status: authorized ? 200 : 401,
        headers: mockApiHeaders,
        json: authorized ? [] : { detail: 'Invalid or missing API token' },
      });
    });

    await page.goto('/');
    const tokenInput = page.getByTestId('api-token-input');
    await expect(tokenInput).toBeVisible();
    await tokenInput.fill('m'.repeat(48));
    await page.getByRole('button', { name: 'Connect' }).click();

    await expect.poll(() => apiRequests.length).toBeGreaterThan(0);
    expect(apiRequests[0].authorization).toBe('Bearer ' + 'm'.repeat(48));
    expect(apiRequests[0].url).not.toContain('m'.repeat(48));
    await expect(tokenInput).not.toBeVisible();
  });
});

// Wait for the authenticated shell and its auto-created thread composer.
async function waitForPageLoad(page: Page) {
  await page.waitForSelector('h1:has-text("eggw")', { timeout: 15000 });
  await expect(page.getByTestId('message-input')).toBeVisible({ timeout: 15000 });
}

async function ensureThread(page: Page): Promise<void> {
  await waitForPageLoad(page);
}

async function showSystemPanel(page: Page): Promise<void> {
  if (await page.getByText('System Log', { exact: true }).isVisible()) return;
  await page.getByRole('button', { name: 'Show sidebar' }).click();
  await expect(page.getByText('System Log', { exact: true })).toBeVisible();
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
  'access-control-allow-headers': 'authorization, content-type',
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

async function mockThreadShell(
  page: Page,
  threadId: string,
  options: { messages?: unknown[]; tools?: unknown[]; onApprove?: (payload: Record<string, unknown>) => void } = {},
) {
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
    await route.fulfill({ status: 200, headers: mockApiHeaders, json: { items: options.messages || [], snapshot_cursor: 0, next_before: null } });
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
    await route.fulfill({ status: 200, headers: mockApiHeaders, json: options.tools || [] });
  });
  await page.route(`${TEST_API_BASE}/api/threads/${threadId}/tools/approve`, async (route, request) => {
    options.onApprove?.(request.postDataJSON() as Record<string, unknown>);
    await route.fulfill({ status: 200, headers: mockApiHeaders, json: { status: 'ok' } });
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

  test('can open the system log panel', async ({ page }) => {
    await showSystemPanel(page);
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
    const sourcePath = new URL(page.url()).pathname;
    await input.fill('/newThread');
    await input.press('Enter');

    // A successful command selects the newly-created thread.
    await expect(page).toHaveURL((url) => url.pathname !== sourcePath);
    await expect(page.getByTestId('message-input')).toBeVisible();
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

    // The persisted user turn is the authoritative send result.
    await expect(page.getByTestId('chat-panel')).toContainText('Hello, this is a test message', { timeout: 5000 });
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

    const chat = page.getByTestId('chat-panel');
    await expect(chat).toContainText('See attached', { timeout: 5000 });
    await expect(page.getByTestId('staged-attachments')).not.toBeVisible({ timeout: 5000 });
    await expect(chat).toContainText('Attachment');
    await expect(chat).toContainText('note.txt');
  });

});

test.describe('Attachment Composer UX', () => {
  test('can drag and drop an image attachment into staging with mocked backend', async ({ page }) => {
    const threadId = 'drop-thread-1';
    let uploadCalled = false;
    let previewRequested = false;

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
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { items: [], snapshot_cursor: 0, next_before: null } });
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
      previewRequested = true;
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
    await expect(stagedPreview).toBeVisible();
    await expect.poll(() => previewRequested).toBe(true);
    await expect(stagedPreview).toHaveAttribute('src', /^blob:/);
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
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route, request) => {
      messagesRequests.push(request.method());
      if (imageGenerated) messagesRequestsAfterGeneration.push(request.method());
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          items: imageGenerated ? [generatedMessage, attachmentMessage] : [],
          snapshot_cursor: imageGenerated ? 1 : 0,
          next_before: null,
        },
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
    await expect(page.getByText('Provider artifact', { exact: true })).toBeVisible({ timeout: 5000 });
    await expect(page.getByText('generated-egg.png', { exact: true })).toBeVisible({ timeout: 5000 });
    const preview = page.getByTestId('provider-artifact-preview');
    await expect(preview).toBeVisible({ timeout: 5000 });
    await expect(preview).toHaveAttribute('src', /^blob:/);
    await expect(preview).toHaveAttribute('loading', 'lazy');
    const attachmentPreview = page.getByTestId('attachment-preview');
    await expect(attachmentPreview).toBeVisible({ timeout: 5000 });
    await expect(attachmentPreview).toHaveAttribute('src', /^blob:/);
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

test.describe('Output Optimizer Observability', () => {
  test('shows optimizer badge only on optimized tool outputs', async ({ page }) => {
    const threadId = 'optimizer-observability-thread-1';
    await mockThreadShell(page, threadId, {
      messages: [
        {
          id: 'optimized-tool-message',
          role: 'tool',
          name: 'bash',
          tool_call_id: 'call-optimized-ui',
          content: 'optimized preview',
          content_text: 'optimized preview',
          output_optimizer: {
            optimized: true,
            summary: 'Egg optimized · 95% saved · raw available',
            summary_with_artifact: 'Egg optimized · 95% saved · raw artifact rawabc123',
            raw_available: true,
            artifact_available: true,
            artifact_id: 'rawabc123',
            raw_hint: "read_long_tool_output('rawabc123', chunk_number=1)",
          },
        },
        {
          id: 'plain-tool-message',
          role: 'tool',
          name: 'bash',
          tool_call_id: 'call-plain-ui',
          content: 'plain preview',
          content_text: 'plain preview',
        },
      ],
    });

    await page.goto(`/${threadId}`);

    const badges = page.getByTestId('output-optimizer-badge');
    await expect(badges).toHaveCount(1, { timeout: 5000 });
    await expect(badges.first()).toContainText('Egg optimized · 95% saved · raw artifact rawabc123');
    await expect(page.getByTestId('raw-output-affordance')).toContainText("read_long_tool_output('rawabc123', chunk_number=1)");
    await expect(page.getByText('plain preview')).toBeVisible();
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
          message: "/editAnswer failed: Selector 'SAME' matched multiple messages; use a longer msg_id.",
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

    await input.fill('/editAnswer SAME');
    await input.press('Enter');

    await expect(page.getByTestId('edit-answer-modal')).not.toBeVisible({ timeout: 5000 });
    await expect(page.getByTestId('chat-panel-content')).toContainText("Error: /editAnswer failed: Selector 'SAME' matched multiple messages; use a longer msg_id.");
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
    await showSystemPanel(page);
    await expect(page.getByText(/SSE connected/).last()).toBeVisible({ timeout: 5000 });
  });

  test('persists a message even when streaming completes before it can be observed', async ({ page }) => {
    const input = page.locator('[data-testid="message-input"]');
    await expect(input).toBeVisible({ timeout: 5000 });
    await input.fill('Say "Hello World"');
    await input.press('Enter');

    // Streaming can finish between browser frames; the persisted user turn is stable.
    await expect(page.getByTestId('chat-panel')).toContainText('Say "Hello World"', { timeout: 5000 });
  });
});

test.describe('Per-thread transcript state', () => {
  test('preserves paginated thread A while navigating to thread B and back', async ({ page }) => {
    await page.addInitScript(() => {
      window.sessionStorage.setItem('eggw.apiToken', 'test-eggw-browser-token-' + 'a'.repeat(48));
    });
    const threadA = 'pagination-thread-a';
    const threadB = 'pagination-thread-b';
    const routes = async (threadId: string) => {
      await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), (route) => route.fulfill({
        status: 200,
        headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body: '',
      }));
      await page.route(`${TEST_API_BASE}/api/threads/${threadId}/open`, (route) => route.fulfill({ status: 200, headers: mockApiHeaders, json: { status: 'opened' } }));
      await page.route(`${TEST_API_BASE}/api/threads/${threadId}/state`, (route) => route.fulfill({ status: 200, headers: mockApiHeaders, json: { state: 'waiting_user', streaming_invoke_id: null, active_get_user_wait: false } }));
      await page.route(`${TEST_API_BASE}/api/threads/${threadId}/tools`, (route) => route.fulfill({ status: 200, headers: mockApiHeaders, json: [] }));
      await page.route(`${TEST_API_BASE}/api/threads/${threadId}/sandbox`, (route) => route.fulfill({ status: 200, headers: mockApiHeaders, json: { enabled: false, effective: false, available: false, user_control_enabled: true } }));
      await page.route(`${TEST_API_BASE}/api/threads/${threadId}/settings`, (route) => route.fulfill({ status: 200, headers: mockApiHeaders, json: { auto_approval: false } }));
      await page.route(`${TEST_API_BASE}/api/threads/${threadId}/children`, (route) => route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: threadId === threadA ? [{ id: threadB, name: threadB, has_children: false }] : [],
      }));
      await page.route(`${TEST_API_BASE}/api/threads/${threadId}/stats`, (route) => route.fulfill({ status: 200, headers: mockApiHeaders, json: { context_tokens: 0, cost_usd: 0 } }));
      await page.route(`${TEST_API_BASE}/api/threads/${threadId}`, (route) => route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { id: threadId, name: threadId, has_children: threadId === threadA, ...(threadId === threadB ? { parent_id: threadA } : {}) },
      }));
    };
    await routes(threadA);
    await routes(threadB);
    await page.route(`${TEST_API_BASE}/api/models`, (route) => route.fulfill({ status: 200, headers: mockApiHeaders, json: { models: [] } }));
    await page.route(`${TEST_API_BASE}/api/image-models`, (route) => route.fulfill({ status: 200, headers: mockApiHeaders, json: { models: [] } }));
    await page.route(`${TEST_API_BASE}/api/threads/roots`, (route) => route.fulfill({ status: 200, headers: mockApiHeaders, json: [
      { id: threadA, name: threadA, has_children: false },
      { id: threadB, name: threadB, has_children: false },
    ] }));
    await page.route(`${TEST_API_BASE}/api/threads`, (route) => route.fulfill({ status: 200, headers: mockApiHeaders, json: [] }));
    await page.route(new RegExp(`/api/threads/${threadA}/messages(?:\\?.*)?$`), async (route, request) => {
      const url = new URL(request.url());
      const beforeId = url.searchParams.get('before_id');
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: beforeId
        ? { items: [{ id: 'a-old', role: 'user', content: 'thread a old' }], snapshot_cursor: 7, next_before: null }
        : { items: [{ id: 'a-new', role: 'user', content: 'thread a new' }], snapshot_cursor: 7, next_before: 'a-new' } });
    });
    await page.route(new RegExp(`/api/threads/${threadB}/messages(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: { items: [{ id: 'b-new', role: 'user', content: 'thread b only' }], snapshot_cursor: 9, next_before: null },
    }));

    await page.goto(`/${threadA}`);
    await expect(page.getByTestId('chat-panel')).toContainText('thread a new');
    await page.getByTestId('load-older-messages').click();
    await expect(page.getByTestId('chat-panel')).toContainText('thread a old');

    await page.getByRole('button', { name: new RegExp(threadB) }).click();
    await expect(page).toHaveURL(new RegExp(`/${threadB}$`));
    await expect(page.getByTestId('chat-panel')).toContainText('thread b only');
    await expect(page.getByTestId('chat-panel')).not.toContainText('thread a old');

    await page.getByRole('button', { name: /Parent/ }).click();
    await expect(page).toHaveURL(new RegExp(`/${threadA}$`));
    await expect(page.getByTestId('chat-panel')).toContainText('thread a old');
    await expect(page.getByTestId('chat-panel')).toContainText('thread a new');
    await expect(page.getByTestId('chat-panel')).not.toContainText('thread b only');
  });
});

test.describe('Tool approval integration', () => {
  test('submits a TC4 output decision through the shared approval endpoint', async ({ page }) => {
    await page.addInitScript(() => {
      window.sessionStorage.setItem('eggw.apiToken', 'test-eggw-browser-token-' + 'a'.repeat(48));
    });
    const threadId = 'approval-thread';
    let approval: Record<string, unknown> | undefined;
    await mockThreadShell(page, threadId, {
      tools: [{
        id: 'approval-tool-call',
        name: 'bash',
        arguments: { script: 'echo approved' },
        state: 'TC4',
        output: 'inspectable output',
      }],
      onApprove: (payload) => { approval = payload; },
    });

    await page.goto(`/${threadId}`);
    await expect(page.getByText('Pending Approvals', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Whole' }).click();
    await expect.poll(() => approval).toMatchObject({
      tool_call_id: 'approval-tool-call',
      approved: true,
      output_decision: 'whole',
    });
  });
});

test.describe('SSE reconnect integration', () => {
  test('resumes with Last-Event-ID and renders a replayed delta once', async ({ page }) => {
    await page.addInitScript(() => {
      window.sessionStorage.setItem('eggw.apiToken', 'test-eggw-browser-token-' + 'a'.repeat(48));
    });
    const threadId = 'reconnect-thread';
    const invokeId = 'reconnect-invoke';
    const cursors: string[] = [];
    let connection = 0;
    await mockThreadShell(page, threadId, {
      messages: [{ id: 'reconnect-user', role: 'user', content: 'resume stream' }],
    });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route, request) => {
      connection += 1;
      cursors.push(request.headers()['last-event-id'] || new URL(request.url()).searchParams.get('after_seq') || '');
      const startedAt = new Date().toISOString();
      const envelope = (eventSeq: number, type: string, payload: Record<string, unknown>) => JSON.stringify({
        event_id: `reconnect-event-${eventSeq}`,
        event_seq: eventSeq,
        type,
        ts: startedAt,
        msg_id: null,
        invoke_id: invokeId,
        chunk_seq: type === 'stream.delta' ? 0 : null,
        payload,
      });
      const nextInvokeId = connection === 1 ? invokeId : 'reconnect-invoke-new';
      const frame = (eventSeq: number, type: string, payload: Record<string, unknown>) => JSON.stringify({
        event_id: `reconnect-${nextInvokeId}-${eventSeq}`,
        event_seq: eventSeq,
        type,
        ts: startedAt,
        msg_id: null,
        invoke_id: nextInvokeId,
        chunk_seq: type === 'stream.delta' ? eventSeq : null,
        payload,
      });
      const frames = connection === 1
        ? ['id: 1', 'event: stream.open', `data: ${frame(1, 'stream.open', { stream_kind: 'llm' })}`, '', '']
        : [
            // Replayed cursor frame is transport-deduplicated; the ordered new
            // stream.open adopts the replacement invocation before its delta.
            'id: 1', 'event: stream.open', `data: ${envelope(1, 'stream.open', { stream_kind: 'llm' })}`, '',
            'id: 2', 'event: stream.open', `data: ${frame(2, 'stream.open', { stream_kind: 'llm' })}`, '',
            'id: 3', 'event: stream.delta', `data: ${frame(3, 'stream.delta', { text: 'resumed exactly once' })}`, '', '',
          ];
      await route.fulfill({
        status: 200,
        headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body: frames.join('\n'),
      });
    });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/state`);
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: { state: 'running', streaming_kind: 'llm', streaming_invoke_id: invokeId, live_replay_cursor: 0, active_get_user_wait: false },
    }));

    await page.goto(`/${threadId}`);
    await expect.poll(() => cursors.some((cursor, index) => index > 0 && Number(cursor) > 0), { timeout: 8000 }).toBe(true);
    await expect(page.getByTestId('chat-panel')).toContainText('resumed exactly once');
    await expect(page.getByTestId('chat-panel')).not.toContainText('resumed exactly onceresumed exactly once');
  });
});

test.describe('Live Tool Streaming', () => {
  test('keeps tool arguments visible while the tool is running', async ({ page }) => {
    await page.addInitScript(() => {
      window.sessionStorage.setItem('eggw.apiToken', 'test-eggw-browser-token-' + 'a'.repeat(48));
    });
    const threadId = 'running-tool-args-thread';
    const toolCallId = 'call-running-tool-args';
    const cursors: string[] = [];

    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route, request) => {
      cursors.push(new URL(request.url()).searchParams.get('after_seq') || request.headers()['last-event-id'] || '');
      const startedAt = new Date().toISOString();
      await route.fulfill({
        status: 200,
        headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body: [
          'id: 1',
          'event: stream.open',
          `data: ${JSON.stringify({
            event_id: 'event-stream-open',
            event_seq: 1,
            type: 'stream.open',
            ts: startedAt,
            msg_id: null,
            invoke_id: 'invoke-running-tool',
            chunk_seq: null,
            payload: { stream_kind: 'tool' },
          })}`,
          '',
          'id: 2',
          'event: tool_call.execution_started',
          `data: ${JSON.stringify({
            event_id: 'event-tool-started',
            event_seq: 2,
            type: 'tool_call.execution_started',
            ts: startedAt,
            msg_id: null,
            invoke_id: 'invoke-running-tool',
            chunk_seq: null,
            payload: {
              tool_call_id: toolCallId,
              name: 'bash',
              arguments: JSON.stringify({ script: 'echo visible args; sleep 30', timeout: 300 }),
              timeout: 300,
            },
          })}`,
          '',
          '',
        ].join('\n'),
      });
    });

    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/open`, async (route) => {
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { status: 'opened' } });
    });
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { items: [{ id: 'user-before-running-tool', role: 'user', content: 'run slow tool', content_text: 'run slow tool' }], snapshot_cursor: 10, next_before: null },
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
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { state: 'running', streaming_kind: 'tool', streaming_invoke_id: 'invoke-running-tool', live_replay_cursor: 0, active_get_user_wait: false },
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

    // The durable snapshot cursor is 10, but active replay starts immediately
    // before stream.open so its tool lifecycle frames are consumed.
    await expect.poll(() => cursors[0], { timeout: 5000 }).toBe("0");

    await expect(page.getByTestId('chat-panel')).toContainText('Tool', { timeout: 5000 });
    await expect(page.getByTestId('chat-panel')).toContainText('bash', { timeout: 5000 });
    await expect(page.getByTestId('chat-panel')).toContainText('$ echo visible args; sleep 30', { timeout: 5000 });
    await expect(page.getByTestId('chat-panel')).toContainText('streaming output...', { timeout: 5000 });
  });
});

test.describe('Atomic Live Tool Continuity', () => {
  test('keeps the canonical tool card through immediate close and a stale refetch at every verbosity', async ({ page }) => {
    await page.addInitScript(() => {
      window.sessionStorage.setItem('eggw.apiToken', 'test-eggw-browser-token-' + 'a'.repeat(48));
    });
    const threadId = 'tool-continuity-thread';
    const toolCallId = 'call-continuity';
    let messageRequests = 0;
    await mockThreadShell(page, threadId, {
      messages: [{ id: 'user-before-tool', role: 'user', content: 'run tool', content_text: 'run tool' }],
    });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/state`);
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: { state: 'running', streaming_kind: 'tool', streaming_invoke_id: 'invoke-continuity', live_replay_cursor: 0, active_get_user_wait: false },
    }));
    await page.unroute(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route) => {
      messageRequests += 1;
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          // Every response deliberately lags the consumed msg.create. The
          // event-installed card must survive these stale HTTP snapshots.
          items: [{ id: 'user-before-tool', role: 'user', content: 'run tool', content_text: 'run tool' }],
          snapshot_cursor: messageRequests === 1 ? 0 : 2,
          next_before: null,
        },
      });
    });
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route) => {
      const ts = new Date().toISOString();
      const envelope = (event_seq: number, type: string, payload: Record<string, unknown>, msg_id: string | null = null) => JSON.stringify({
        event_id: `continuity-${event_seq}`,
        event_seq,
        type,
        ts,
        msg_id,
        invoke_id: 'invoke-continuity',
        chunk_seq: type === 'stream.delta' ? event_seq : null,
        payload,
      });
      await route.fulfill({
        status: 200,
        headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body: [
          'id: 1', 'event: stream.open', `data: ${envelope(1, 'stream.open', { stream_kind: 'tool' })}`, '',
          'id: 2', 'event: stream.delta', `data: ${envelope(2, 'stream.delta', { tool_call: { id: toolCallId, name: 'bash', arguments_delta: '{"script":"echo continuity"}' } })}`, '',
          'id: 3', 'event: msg.create', `data: ${envelope(3, 'msg.create', { role: 'assistant', content: '', tool_calls: [{ id: toolCallId, name: 'bash', arguments: '{"script":"echo continuity"}' }] }, 'assistant-continuity')}`, '',
          'id: 4', 'event: stream.close', `data: ${envelope(4, 'stream.close', {})}`, '', '',
        ].join('\n'),
      });
    });

    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/command`, async (route, request) => {
      const command = String((request.postDataJSON() as { command?: string }).command || "");
      const verbosity = command.split(/\s+/).at(-1);
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          success: true,
          message: `Display verbosity set to ${verbosity}`,
          command_id: `verbosity-${verbosity}`,
          command_name: "displayVerbosity",
          data: { action: "set_display_verbosity", display_verbosity: verbosity },
        },
      });
    });

    await page.goto(`/${threadId}`);
    const input = page.getByTestId('message-input');
    const chat = page.getByTestId('chat-panel');
    for (const verbosity of ['max', 'medium', 'min'] as const) {
      await input.fill(`/displayVerbosity ${verbosity}`);
      await input.press('Enter');
      await expect(chat).toContainText(verbosity === 'min' ? 'Executed 1 tool' : 'bash', { timeout: 5000 });
      await expect(chat).not.toContainText('No messages yet');
    }
    expect(messageRequests).toBeGreaterThan(1);
  });
});

test.describe('Settings and Controls', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await ensureThread(page);
    await showSystemPanel(page);
  });

  test('shows thread info in system panel', async ({ page }) => {
    await expect(page.locator('text=Thread Info')).toBeVisible({ timeout: 5000 });
    await expect(page.getByText('ID:', { exact: true })).toBeVisible();
  });

  test('shows model selector', async ({ page }) => {
    await expect(page.locator('text=Model:')).toBeVisible({ timeout: 5000 });
    await expect(page.getByText('Model:', { exact: true }).locator('..').getByRole('combobox')).toBeVisible();
  });

  test('shows token stats', async ({ page }) => {
    await expect(page.locator('text=Token Stats')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('text=Input:')).toBeVisible();
    await expect(page.locator('text=Output:')).toBeVisible();
  });

  test('can toggle auto-approval', async ({ page }) => {
    const autoApprovalToggle = page.getByTitle(/Auto-approval (?:ON|OFF)/);
    await expect(autoApprovalToggle).toBeVisible({ timeout: 5000 });
    const initialTitle = await autoApprovalToggle.getAttribute('title');
    await autoApprovalToggle.click();
    await expect(autoApprovalToggle).toHaveAttribute(
      'title',
      initialTitle === 'Auto-approval ON' ? 'Auto-approval OFF' : 'Auto-approval ON',
    );
  });
});

test.describe('Keyboard Shortcuts', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await ensureThread(page);
  });

  test('help modal opens via button click', async ({ page }) => {
    await page.getByTitle('Help (?)').click();
    await expect(page.getByRole('heading', { name: 'Keyboard Shortcuts' })).toBeVisible();
  });

  test('help modal closes with Close button', async ({ page }) => {
    await page.getByTitle('Help (?)').click();
    const heading = page.getByRole('heading', { name: 'Keyboard Shortcuts' });
    await expect(heading).toBeVisible();
    await page.getByRole('button', { name: 'Close', exact: true }).last().evaluate((button: HTMLButtonElement) => button.click());
    await expect(heading).not.toBeVisible();
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

    // /help is the first visible suggestion; /newThread may be below the popup scrollport.
    await expect(page.getByText('/help', { exact: true })).toBeVisible();
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
    await expect(page.getByText('Pending Approvals', { exact: true })).toBeVisible({ timeout: 20000 });
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
