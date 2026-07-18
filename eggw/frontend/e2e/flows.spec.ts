import { test, expect, Page } from '@playwright/test';
import operationalRecoveryFixture from '../../../eggthreads/tests/fixtures/phase6-operational-recovery-interleaved.json';

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
  await page.getByRole('button', { name: 'Show system panel' }).click();
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

test.describe('Launcher quick start', () => {
  test('landing page owns returned draft and attachment without sending', async ({ page }) => {
    const threadId = 'quick-start-thread';
    const launchAttachment = {
      type: 'attachment', input_id: 'quick-input', owner_thread_id: threadId,
      presentation: 'file', mime_type: 'text/plain', filename: 'launch file.txt',
      size_bytes: 12, sha256: 'a'.repeat(64), provenance: { kind: 'local_path' }, options: {},
    };
    await mockThreadShell(page, threadId);
    await page.unroute(`${TEST_API_BASE}/api/threads`);
    await page.route(`${TEST_API_BASE}/api/threads`, async (route, request) => {
      expect(request.postDataJSON()).toEqual({ claim_quick_start: true });
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          id: threadId, name: 'Quick start', has_children: false,
          initial_draft: 'Tell me a story', initial_attachment: launchAttachment,
        },
      });
    });

    await page.goto('/');

    await expect(page).toHaveURL(new RegExp(`/${threadId}$`));
    await expect(page.getByTestId('message-input')).toHaveValue('Tell me a story');
    await expect(page.getByTestId('staged-attachments')).toContainText('launch file.txt');
    expect(await page.getByTestId('chat-panel-content').innerText()).not.toContain('Tell me a story');
  });
});

test.describe('Fresh display verbosity default', () => {
  test('starts at min and keeps explicit session overrides until reload', async ({ page }) => {
    const threadId = 'fresh-display-verbosity';
    await mockThreadShell(page, threadId, {
      messages: [{
        id: 'fresh-verbosity-assistant',
        role: 'assistant',
        content: 'FRESH VERBOSITY ANSWER',
        reasoning: 'FRESH VERBOSITY REASONING',
      }],
    });

    await page.goto(`/${threadId}`);
    const verbosity = page.locator('select[title="Transcript display verbosity"]');
    await expect(verbosity).toHaveValue('min');
    await expect(page.getByTestId('hidden-details')).toBeVisible();
    await expect(page.getByText('FRESH VERBOSITY REASONING', { exact: true })).not.toBeVisible();

    await verbosity.selectOption('medium');
    await expect(verbosity).toHaveValue('medium');
    await verbosity.selectOption('max');
    await expect(verbosity).toHaveValue('max');
    await expect(page.getByText('FRESH VERBOSITY REASONING', { exact: true })).toBeVisible();

    await page.reload();
    await expect(page.locator('select[title="Transcript display verbosity"]')).toHaveValue('min');
    await expect(page.getByText('FRESH VERBOSITY REASONING', { exact: true })).not.toBeVisible();
  });
});

test.describe('Shared show inspection', () => {
  test('opens the authoritative full record at every verbosity without changing the global level', async ({ page }) => {
    const threadId = 'shared-show-record';
    const messageId = 'show-message-full-00000001';
    const toolCallId = 'show-tool-call-full-00000001';
    await mockThreadShell(page, threadId, {
      messages: [{
        id: messageId,
        role: 'assistant',
        content: 'SHOW ANSWER BODY',
        reasoning: 'SHOW PRIVATE REASONING',
        model_key: 'show-model',
        tokens: 321,
        tps: 12.5,
        timestamp: '2026-01-02T03:04:05.000Z',
        tool_calls: [{ id: toolCallId, name: 'bash', arguments: { script: 'echo SHOW TOOL ARGUMENTS' } }],
      }],
    });
    await page.route(`${TEST_API_BASE}/api/autocomplete**`, async (route, request) => {
      const line = new URL(request.url()).searchParams.get('line') || '';
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          suggestions: line.startsWith('/show ')
            ? [{ display: `[00000001] Tool declaration: bash`, insert: toolCallId, replace: 4, meta: `tool_declaration · ${toolCallId}` }]
            : [],
        },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/command`, async (route, request) => {
      const command = String(request.postDataJSON()?.command || '');
      expect(command).toBe(`/show ${toolCallId}`);
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          success: true,
          message: `Showing Tool declaration: bash ${toolCallId}.`,
          command_id: 'show-command-1',
          command_name: 'show',
          started_at: '2026-01-02T03:04:06.000Z',
          finished_at: '2026-01-02T03:04:06.010Z',
          elapsed_sec: 0.01,
          data: {
            action: 'show_record',
            suppress_transcript: true,
            target: {
              record_id: toolCallId,
              kind: 'tool_declaration',
              thread_id: threadId,
              message_id: messageId,
              tool_call_id: toolCallId,
              event_seq: 42,
              watermark_event_seq: 45,
              label: 'Tool declaration: bash',
              preview: 'bash(echo SHOW TOOL ARGUMENTS)',
              paired_message_ids: ['show-tool-result-message'],
              message: {
                id: messageId,
                role: 'assistant',
                content: 'SHOW ANSWER BODY',
                reasoning: 'SHOW PRIVATE REASONING',
                model_key: 'show-model',
                tokens: 321,
                tps: 12.5,
                timestamp: '2026-01-02T03:04:05.000Z',
                tool_calls: [{ id: toolCallId, name: 'bash', arguments: { script: 'echo SHOW TOOL ARGUMENTS' } }],
              },
              tool_call: { id: toolCallId, name: 'bash', arguments: { script: 'echo SHOW TOOL ARGUMENTS' } },
            },
          },
        },
      });
    });

    await page.goto(`/${threadId}`);
    const verbosity = page.locator('select[title="Transcript display verbosity"]');
    const input = page.getByTestId('message-input');
    for (const level of ['min', 'medium', 'max'] as const) {
      await verbosity.selectOption(level);
      await input.fill('/show 0001');
      await expect(page.getByRole('option', { name: /Tool declaration: bash/ })).toBeVisible();
      await input.press('Tab');
      await expect(input).toHaveValue(`/show ${toolCallId}`);
      await input.press('Enter');

      const modal = page.getByTestId('show-record-modal');
      await expect(modal).toBeVisible();
      await expect(modal).toContainText(`record_id: ${toolCallId}`);
      await expect(modal).toContainText(`message_id: ${messageId}`);
      await expect(modal).toContainText('model: show-model');
      await expect(modal).toContainText('321 tok');
      await expect(modal).toContainText('13 tps');
      await expect(modal).toContainText('Exact paired message IDs: show-tool-result-message');
      await expect(modal).toContainText('echo SHOW TOOL ARGUMENTS');
      await expect(verbosity).toHaveValue(level);
      await modal.getByRole('button', { name: 'Close', exact: true }).click();
      await expect(modal).not.toBeVisible();
      await expect(input).toBeFocused();
    }
  });
});

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

    const authoritativeMessages = [
      { id: 'message-before-command', role: 'user', timestamp: beforeTimestamp, content: 'Before command', content_text: 'Before command' },
      { id: 'message-after-command', role: 'assistant', timestamp: afterTimestamp, content: 'After command', content_text: 'After command' },
    ];
    await mockThreadShell(page, threadId, { messages: authoritativeMessages });
    let messageRequests = 0;
    await page.unroute(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route) => {
      messageRequests += 1;
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { items: authoritativeMessages, snapshot_cursor: messageRequests, next_before: null },
      });
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
          data: { action: 'list_attachments', reload: true },
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
    await expect.poll(() => messageRequests).toBeGreaterThan(1);
    const orderedText = await chatContent.innerText();
    expect(orderedText.indexOf('Before command')).toBeLessThan(orderedText.indexOf('Command output in timestamp position'));
    expect(orderedText.indexOf('Command output in timestamp position')).toBeLessThan(orderedText.indexOf('After command'));
  });

  test('splits compact tool runs around command, user, assistant, and Assistant Note records', async ({ page }) => {
    const threadId = 'min-command-tool-run-boundaries';
    const callA = 'command-boundary-call-a';
    const callB = 'command-boundary-call-b';
    const callC = 'command-boundary-call-c';
    const messages = [
      { id: 'command-boundary-start', role: 'user', content: 'Start grouped checks.' },
      {
        id: 'command-boundary-calls-ab', role: 'assistant', content: '',
        tool_calls: [
          { id: callA, name: 'bash', arguments: { script: 'echo A' } },
          { id: callB, name: 'python_repl', arguments: { code: 'print("B")' } },
        ],
      },
      { id: 'command-boundary-result-a', role: 'tool', name: 'bash', tool_call_id: callA, content: 'RESULT_A' },
      {
        id: 'cmd-command-boundary', role: 'system', command_name: 'help', client_only: 'command',
        content: 'LOCAL COMMAND BOUNDARY',
      },
      { id: 'command-boundary-result-b', role: 'tool', name: 'python_repl', tool_call_id: callB, content: 'RESULT_B' },
      { id: 'command-boundary-user', role: 'user', content: 'USER BOUNDARY' },
      {
        id: 'command-boundary-call-c', role: 'assistant', content: '',
        tool_calls: [{ id: callC, name: 'skill', arguments: { name: 'rlm' } }],
      },
      { id: 'command-boundary-assistant', role: 'assistant', content: 'ASSISTANT BOUNDARY' },
      { id: 'command-boundary-result-c', role: 'tool', name: 'skill', tool_call_id: callC, content: 'RESULT_C' },
      {
        id: 'command-boundary-note', role: 'assistant', answer_user_preserve_turn: true,
        content: 'ASSISTANT NOTE BOUNDARY',
      },
      { id: 'command-boundary-result-d', role: 'tool', name: 'bash', tool_call_id: 'result-only-d', content: 'RESULT_D' },
      { id: 'command-boundary-final', role: 'assistant', content: 'FINISHED' },
    ];
    await mockThreadShell(page, threadId, { messages });

    await page.goto(`/${threadId}`);
    await page.locator('select[title="Transcript display verbosity"]').selectOption('min');
    const records = await page.getByTestId('static-transcript-owner').locator(':scope > *').evaluateAll((nodes) => nodes.map((node) => ({
      id: node.getAttribute('data-message-id'),
      sourceCount: node.getAttribute('data-source-message-count'),
      text: (node.textContent || '').replace(/\s+/g, ' ').trim(),
    })));

    expect(records).toEqual([
      expect.objectContaining({ id: 'command-boundary-start' }),
      expect.objectContaining({ id: null, sourceCount: '2', text: expect.stringContaining('Executed 2 tools, got 1 tool result') }),
      expect.objectContaining({ id: 'cmd-command-boundary', text: expect.stringContaining('LOCAL COMMAND BOUNDARY') }),
      expect.objectContaining({ id: null, sourceCount: '1', text: expect.stringContaining('got 1 tool result') }),
      expect.objectContaining({ id: 'command-boundary-user', text: expect.stringContaining('USER BOUNDARY') }),
      expect.objectContaining({ id: null, sourceCount: '1', text: expect.stringContaining('Executed 1 tool') }),
      expect.objectContaining({ id: 'command-boundary-assistant', text: expect.stringContaining('ASSISTANT BOUNDARY') }),
      expect.objectContaining({ id: null, sourceCount: '1', text: expect.stringContaining('got 1 tool result') }),
      expect.objectContaining({ id: 'command-boundary-note', text: expect.stringContaining('ASSISTANT NOTE BOUNDARY') }),
      expect.objectContaining({ id: null, sourceCount: '1', text: expect.stringContaining('got 1 tool result') }),
      expect.objectContaining({ id: 'command-boundary-final', text: expect.stringContaining('FINISHED') }),
    ]);
    expect(records[1].text).toContain('Tools: bash, python_repl');
    expect(records[3].text).toContain('Tools: python_repl');
    expect(records[5].text).toContain('Tools: skill');
    expect(records[7].text).toContain('Tools: skill');
    expect(records[9].text).toContain('Tools: bash');
  });
});

test.describe('Composer draft and autocomplete ownership', () => {
  test('keeps rapid edits local, persists navigation drafts, and restores an async failed send', async ({ page }) => {
    const threadA = 'composer-thread-a';
    const threadB = 'composer-thread-b';
    for (const threadId of [threadA, threadB]) await mockThreadShell(page, threadId);
    await page.route(`${TEST_API_BASE}/api/threads/${threadA}/children`, (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: [{ id: threadB, name: threadB, parent_id: threadA, has_children: false }],
    }));
    await page.route(`${TEST_API_BASE}/api/threads/${threadA}`, (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: { id: threadA, name: threadA, has_children: true },
    }));

    let resolveFailedSend!: () => void;
    const failedSend = new Promise<void>((resolve) => { resolveFailedSend = resolve; });
    await page.unroute(new RegExp(`/api/threads/${threadA}/messages(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadA}/messages(?:\\?.*)?$`), async (route, request) => {
      if (request.method() === 'POST') {
        await failedSend;
        await route.fulfill({ status: 500, headers: mockApiHeaders, json: { detail: 'failed' } });
        return;
      }
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { items: [], snapshot_cursor: 0, next_before: null } });
    });

    await page.goto(`/${threadA}`);
    const input = page.getByTestId('message-input');
    await expect(input).toBeVisible();
    await input.fill('a'.repeat(200));
    await page.locator('.eggw-thread-link').filter({ hasText: threadB }).click();
    await expect(page).toHaveURL(new RegExp(`/${threadB}$`));
    await page.getByTestId('message-input').fill('thread b draft');
    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`/${threadA}$`));
    await expect(page.getByTestId('message-input')).toHaveValue('a'.repeat(200));

    await page.getByTestId('message-input').fill('failed send');
    await page.getByTestId('message-input').press('Enter');
    await page.getByTestId('message-input').fill('newer local draft');
    resolveFailedSend();
    await expect(page.getByTestId('message-input')).toHaveValue('failed send\n\nnewer local draft');
  });

  test('does not poll settings and refreshes them after a command mutation', async ({ page }) => {
    const threadId = 'settings-invalidation-thread';
    await mockThreadShell(page, threadId);
    let settingsRequests = 0;
    let autoApproval = false;
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/settings`);
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/settings`, async (route) => {
      settingsRequests += 1;
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { auto_approval: autoApproval } });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/command`, async (route) => {
      autoApproval = true;
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          success: true,
          message: 'Auto-approval enabled',
          command_id: 'settings-command',
          command_name: 'toggleAutoApproval',
          finished_at: '2026-01-01T00:00:00Z',
          data: { auto_approval: true, suppress_transcript: true },
        },
      });
    });

    await page.goto(`/${threadId}`);
    await expect(page.getByTitle('Auto-approval OFF')).toBeVisible();
    await page.waitForTimeout(1200);
    const requestsBeforeCommand = settingsRequests;
    expect(requestsBeforeCommand).toBeGreaterThanOrEqual(1);
    await page.getByTestId('message-input').fill('/toggleAutoApproval');
    await page.getByTestId('message-input').press('Enter');
    await expect(page.getByTitle('Auto-approval ON')).toBeVisible();
    expect(settingsRequests).toBe(requestsBeforeCommand + 1);
  });

  test('keyboard shortcuts gate unloaded/racing toggles and preserve the composer draft', async ({ page }) => {
    const threadId = 'safety-shortcuts-thread';
    await mockThreadShell(page, threadId);
    let autoApproval = false;
    let autoApprovalRequests = 0;
    let sandboxEnabled = false;
    let releaseSettings!: () => void;
    const settingsReady = new Promise<void>((resolve) => { releaseSettings = resolve; });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/settings`);
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/settings`, async (route) => {
      await settingsReady;
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { auto_approval: autoApproval } });
    });
    let releaseAutoApproval!: () => void;
    const autoApprovalReady = new Promise<void>((resolve) => { releaseAutoApproval = resolve; });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/settings/auto-approval**`, async (route, request) => {
      autoApprovalRequests += 1;
      autoApproval = new URL(request.url()).searchParams.get('enabled') === 'true';
      await autoApprovalReady;
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { auto_approval: autoApproval } });
    });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/sandbox`);
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/sandbox`, async (route) => {
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { enabled: sandboxEnabled, effective: sandboxEnabled, available: true, user_control_enabled: true },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/command`, async (route, request) => {
      expect(request.postDataJSON()).toMatchObject({ command: '/toggleSandboxing' });
      sandboxEnabled = !sandboxEnabled;
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { success: true, message: `Sandboxing ${sandboxEnabled ? 'ENABLED' : 'DISABLED'}` },
      });
    });

    await page.goto(`/${threadId}`);
    const input = page.getByTestId('message-input');
    await expect(input).toBeVisible();
    await input.fill('unsent draft');
    const dispatchSafetyShortcut = (code: 'KeyA' | 'KeyX', altGraph = false) => page.evaluate(({ code, altGraph }) => {
      const event = new KeyboardEvent('keydown', { code, key: code === 'KeyA' ? 'a' : 'x', ctrlKey: true, altKey: true, bubbles: true });
      Object.defineProperty(event, 'getModifierState', { value: (key: string) => altGraph && key === 'AltGraph' });
      document.dispatchEvent(event);
    }, { code, altGraph });

    // Unknown settings cannot be safely inverted, and AltGraph must never be
    // interpreted as the Ctrl+Alt safety chord used by keyboard layouts.
    await dispatchSafetyShortcut('KeyA');
    await dispatchSafetyShortcut('KeyA', true);
    expect(autoApprovalRequests).toBe(0);
    releaseSettings();
    await expect(page.getByTitle('Auto-approval OFF')).toBeVisible();

    // Two events before React publishes mutation state still produce one true
    // toggle because the handler owns a synchronous operation gate.
    await dispatchSafetyShortcut('KeyA');
    await dispatchSafetyShortcut('KeyA');
    await expect.poll(() => autoApprovalRequests).toBe(1);
    releaseAutoApproval();
    await expect(page.getByTitle('Auto-approval ON')).toBeVisible();
    await expect(input).toHaveValue('unsent draft');

    await dispatchSafetyShortcut('KeyX');
    await expect(page.getByText('Sandbox on')).toBeVisible();
    await expect(input).toHaveValue('unsent draft');
  });

  test('gates ordinary prose and renders only the latest autocomplete response', async ({ page }) => {
    const threadId = 'autocomplete-owner-thread';
    await mockThreadShell(page, threadId);
    const autocompleteLines: string[] = [];
    await page.route(`${TEST_API_BASE}/api/autocomplete**`, async (route, request) => {
      const line = new URL(request.url()).searchParams.get('line') || '';
      autocompleteLines.push(line);
      if (line === '/h') {
        try {
          await new Promise((resolve) => setTimeout(resolve, 300));
          await route.fulfill({ status: 200, headers: mockApiHeaders, json: { suggestions: [{ display: '/history', insert: '/history' }] } });
        } catch {
          // Browser cancellation may reject the route fulfillment.
        }
        return;
      }
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { suggestions: [{ display: '/help latest', insert: '/help' }] } });
    });

    await page.goto(`/${threadId}`);
    const input = page.getByTestId('message-input');
    await input.fill('ordinary prose');
    await page.waitForTimeout(200);
    expect(autocompleteLines).toEqual([]);

    await input.fill('/h');
    await page.waitForTimeout(150);
    await input.fill('/he');
    await expect(page.getByText('/help latest', { exact: true })).toBeVisible();
    await expect(page.getByText('/history', { exact: true })).not.toBeVisible();
    expect(autocompleteLines).toEqual(['/h', '/he']);
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
    await page.locator('select[title="Transcript display verbosity"]').selectOption('max');

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
    await expect(input).toHaveValue('/editAnswer SAME');
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

  test('follows new provider content only while the reader is at the latest content', async ({ page }) => {
    const threadId = 'stream-scroll-follow';
    const messages = Array.from({ length: 36 }, (_, index) => ({
      id: `scroll-message-${index}`,
      role: index % 2 === 0 ? 'user' : 'assistant',
      content: `${index}: ${'long transcript content '.repeat(12)}`,
    }));
    await mockThreadShell(page, threadId, { messages });
    await page.goto(`/${threadId}`);

    const chat = page.getByTestId('chat-panel');
    const content = page.getByTestId('chat-panel-content');
    const geometry = () => chat.evaluate((element) => ({
      top: element.scrollTop,
      distance: element.scrollHeight - element.scrollTop - element.clientHeight,
    }));
    const appendProviderContent = (label: string) => content.evaluate((element, text) => {
      const chunk = document.createElement('div');
      chunk.textContent = text;
      chunk.style.height = '320px';
      element.appendChild(chunk);
    }, label);

    for (const verbosity of ['max', 'medium', 'min'] as const) {
      await page.locator('select[title="Transcript display verbosity"]').selectOption(verbosity);
      await chat.evaluate((element) => { element.scrollTop = element.scrollHeight; });
      await expect.poll(async () => (await geometry()).distance).toBeLessThanOrEqual(16);

      await appendProviderContent(`follow-${verbosity}`);
      await expect.poll(async () => (await geometry()).distance).toBeLessThanOrEqual(16);

      await chat.hover();
      await page.mouse.wheel(0, -700);
      await expect.poll(async () => (await geometry()).distance).toBeGreaterThan(100);
      const detachedTop = (await geometry()).top;

      await appendProviderContent(`detached-${verbosity}`);
      await expect.poll(async () => Math.abs((await geometry()).top - detachedTop)).toBeLessThanOrEqual(2);

      await chat.focus();
      await page.keyboard.press('End');
      await expect.poll(async () => (await geometry()).distance).toBeLessThanOrEqual(16);
      await appendProviderContent(`reattached-${verbosity}`);
      await expect.poll(async () => (await geometry()).distance).toBeLessThanOrEqual(16);
    }
  });
});

test.describe('Scroll intent state machines', () => {
  test('services clamped top demand and reveals loaded history before fetching', async ({ page }) => {
    const threadId = 'scroll-top-demand';
    let messageRequests = 0;
    await mockThreadShell(page, threadId);
    await page.unroute(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route, request) => {
      messageRequests += 1;
      const beforeId = new URL(request.url()).searchParams.get('before_id');
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: beforeId
          ? {
              items: [{ id: 'network-older-message', role: 'user', content: 'NETWORK OLDER PAGE' }],
              snapshot_cursor: 0,
              next_before: null,
            }
          : {
              items: Array.from({ length: 180 }, (_, index) => ({
                id: `loaded-history-${index}`,
                role: index % 2 ? 'assistant' : 'user',
                content: `loaded ${index}`,
              })),
              snapshot_cursor: 0,
              next_before: 'loaded-history-0',
            },
      });
    });

    await page.goto(`/${threadId}`);
    const chat = page.getByTestId('chat-panel');
    await expect(page.locator('.eggw-message-card')).toHaveCount(60);
    await chat.evaluate((element) => {
      const filler = document.createElement('div');
      filler.dataset.testid = 'top-demand-filler';
      filler.style.height = '900px';
      element.append(filler);
      element.scrollTop = 0;
    });
    await page.waitForTimeout(100);
    expect(messageRequests).toBe(1);
    await expect(page.locator('.eggw-message-card')).toHaveCount(60);
    await chat.evaluate((element) => { element.scrollTop = 500; });
    await expect.poll(() => chat.evaluate((element) => element.scrollTop)).toBeGreaterThan(240);
    await chat.hover();

    // One explicit upward input begins above the threshold and lands at top.
    // The post-input boundary check must demand history without a second input.
    await page.mouse.wheel(0, -900);
    await expect(page.locator('.eggw-message-card')).toHaveCount(120);
    expect(messageRequests).toBe(1);
    await expect(chat).not.toContainText('NETWORK OLDER PAGE');
    await expect(page.locator('[data-message-id="loaded-history-65"]')).toBeVisible();
    await chat.evaluate((element) => {
      element.querySelector('[data-testid="top-demand-filler"]')?.remove();
      element.scrollTop = 0;
    });

    // The restoration leaves the scrollport clamped at top. A second upward
    // wheel still carries demand even though it need not emit a scroll event.
    await page.mouse.wheel(0, -900);
    await expect(page.locator('.eggw-message-card')).toHaveCount(120);
    await expect(page.getByTestId('return-to-live-tail')).toBeVisible();
    expect(messageRequests).toBe(1);
    await chat.evaluate((element) => { element.scrollTop = 0; });

    await page.mouse.wheel(0, -900);
    await expect.poll(() => messageRequests).toBe(2);
    await expect(chat).toContainText('NETWORK OLDER PAGE');
  });

  test('keeps a visible live-tail escape while detached history receives canonical SSE messages', async ({ page }) => {
    const threadId = 'detached-history-live-tail-escape';
    const initialMessages = Array.from({ length: 300 }, (_, index) => ({
      id: `detached-history-${index}`,
      role: index % 2 ? 'assistant' : 'user',
      content: `${index}: ${'historical transcript line '.repeat(8)}`,
    }));
    let releaseNewMessages!: () => void;
    const newMessagesReady = new Promise<void>((resolve) => { releaseNewMessages = resolve; });

    await mockThreadShell(page, threadId, { messages: initialMessages });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.unroute(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: { state: 'running', streaming_kind: 'llm', streaming_invoke_id: 'detached-history-invoke', live_replay_cursor: 300 },
    }));
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route) => {
      await newMessagesReady;
      const envelope = (eventSeq: number, msgId: string, content: string) => JSON.stringify({
        event_id: `detached-history-event-${eventSeq}`,
        event_seq: eventSeq,
        type: 'msg.create',
        ts: new Date().toISOString(),
        msg_id: msgId,
        invoke_id: 'detached-history-invoke',
        chunk_seq: null,
        payload: { role: 'assistant', content },
      });
      await route.fulfill({
        status: 200,
        headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body: [
          'id: 301', 'event: msg.create', `data: ${envelope(301, 'detached-newest-301', 'EXACT NEWEST MESSAGE 301')}`, '',
          'id: 302', 'event: msg.create', `data: ${envelope(302, 'detached-newest-302', 'EXACT NEWEST MESSAGE 302')}`, '', '',
        ].join('\n'),
      });
    });

    await page.goto(`/${threadId}`);
    const chat = page.getByTestId('chat-panel');
    await expect(page.locator('[data-message-id="detached-history-299"]')).toBeVisible();
    await chat.hover();
    await chat.evaluate((element) => { element.scrollTop = 0; });
    await page.mouse.wheel(0, -900);
    await expect(page.locator('[data-message-id="detached-history-180"]')).toBeVisible();
    await chat.evaluate((element) => { element.scrollTop = 0; });
    await page.mouse.wheel(0, -900);
    await expect(page.locator('[data-message-id="detached-history-120"]')).toBeVisible();
    await expect(page.getByTestId('live-tail-escape')).toContainText('60 newer messages');
    const frozenIds = await page.getByTestId('static-transcript-owner').locator(':scope > *').evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute('data-message-id') || node.getAttribute('data-source-message-id')),
    );

    await chat.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    releaseNewMessages();
    await expect(page.getByText(/Chat Messages · 302 loaded/)).toBeVisible();
    await expect(page.getByTestId('live-tail-escape')).toContainText('62 newer messages');
    await expect(page.getByTestId('return-to-live-tail')).toBeVisible();
    await expect(page.locator('[data-message-id="detached-newest-302"]')).toHaveCount(0);
    expect(await page.getByTestId('static-transcript-owner').locator(':scope > *').evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute('data-message-id') || node.getAttribute('data-source-message-id')),
    )).toEqual(frozenIds);
    let geometry = await page.getByTestId('live-tail-escape').evaluate((element) => {
      const escapeRect = element.getBoundingClientRect();
      const chatRect = element.parentElement!.getBoundingClientRect();
      return { escapeTop: escapeRect.top, escapeBottom: escapeRect.bottom, chatTop: chatRect.top, chatBottom: chatRect.bottom };
    });
    expect(geometry.escapeTop).toBeGreaterThanOrEqual(geometry.chatTop);
    expect(geometry.escapeBottom).toBeLessThanOrEqual(geometry.chatBottom);

    await chat.evaluate((element) => { element.scrollTop = 0; });
    await expect(page.getByTestId('return-to-live-tail')).toBeVisible();
    geometry = await page.getByTestId('live-tail-escape').evaluate((element) => {
      const escapeRect = element.getBoundingClientRect();
      const chatRect = element.parentElement!.getBoundingClientRect();
      return { escapeTop: escapeRect.top, escapeBottom: escapeRect.bottom, chatTop: chatRect.top, chatBottom: chatRect.bottom };
    });
    expect(geometry.escapeTop).toBeGreaterThanOrEqual(geometry.chatTop);
    expect(geometry.escapeBottom).toBeLessThanOrEqual(geometry.chatBottom);

    await expect(chat).toHaveScreenshot('detached-history-live-tail-escape.png', {
      animations: 'disabled',
      caret: 'hide',
      maxDiffPixelRatio: 0.015,
    });

    // Natural downward traversal advances one bounded window at each local
    // bottom. It must not skip directly to the tail or oscillate backward.
    await chat.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    await chat.hover();
    await page.mouse.wheel(0, 900);
    await expect(page.getByTestId('live-tail-escape')).toContainText('2 newer messages');
    await expect(page.locator('[data-message-id="detached-history-180"]')).toBeVisible();
    await expect.poll(() => chat.evaluate((element) => element.scrollTop)).toBe(0);
    await expect(page.locator('[data-message-id="detached-newest-302"]')).toHaveCount(0);
    expect(await page.getByTestId('static-transcript-owner').locator(':scope > *').count()).toBeLessThanOrEqual(120);

    await chat.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    await chat.hover();
    await page.mouse.wheel(0, 900);
    await expect(page.getByTestId('live-tail-escape')).toHaveCount(0);
    await expect(page.getByTestId('static-transcript-owner').locator(':scope > *').first()).toHaveAttribute('data-message-id', 'detached-history-182');
    await expect(page.getByTestId('static-transcript-owner').locator(':scope > *').last()).toHaveAttribute('data-message-id', 'detached-newest-302');
    expect(await page.getByTestId('static-transcript-owner').locator(':scope > *').count()).toBeLessThanOrEqual(120);

    await expect(page.locator('[data-message-id="detached-newest-301"]')).toBeVisible();
    await expect(page.locator('[data-message-id="detached-newest-302"]')).toBeVisible();
  });

  test('reattaches live following when a large wheel lands at the live bottom', async ({ page }) => {
    const threadId = 'live-bottom-wheel-reattach';
    const messages = Array.from({ length: 60 }, (_, index) => ({
      id: `live-wheel-${index}`,
      role: index % 2 ? 'assistant' : 'user',
      content: `${index}: ${'wheel following content '.repeat(10)}`,
    }));
    let releaseUpdate!: () => void;
    let markConnected!: () => void;
    const updateReady = new Promise<void>((resolve) => { releaseUpdate = resolve; });
    const connected = new Promise<void>((resolve) => { markConnected = resolve; });
    await mockThreadShell(page, threadId, { messages });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.unroute(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({ status: 200, headers: mockApiHeaders, json: { state: 'running', streaming_kind: 'llm', streaming_invoke_id: 'live-wheel-invoke', live_replay_cursor: 60 } }));
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route) => {
      markConnected();
      await updateReady;
      const frame = JSON.stringify({
        event_id: 'live-wheel-61', event_seq: 61, type: 'msg.create', ts: new Date().toISOString(),
        msg_id: 'live-wheel-newest', invoke_id: 'live-wheel-invoke', chunk_seq: null,
        payload: { role: 'assistant', content: 'LIVE WHEEL NEWEST' },
      });
      await route.fulfill({ status: 200, headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' }, body: `id: 61\nevent: msg.create\ndata: ${frame}\n\n` });
    });
    await page.goto(`/${threadId}`);
    const chat = page.getByTestId('chat-panel');
    await connected;
    await chat.hover();
    await page.mouse.wheel(0, -60);
    await expect.poll(() => chat.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeGreaterThan(16);
    await page.mouse.wheel(0, 10_000);
    await expect.poll(() => chat.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThanOrEqual(16);
    releaseUpdate();
    await expect(page.locator('[data-message-id="live-wheel-newest"]')).toBeVisible();
    await expect.poll(() => chat.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThanOrEqual(16);
  });

  test('captures the downward key scrollport and advances one window', async ({ page }) => {
    const threadId = 'downward-key-captured-target';
    const messages = Array.from({ length: 240 }, (_, index) => ({ id: `key-newer-${index}`, role: index % 2 ? 'assistant' : 'user', content: `key ${index}` }));
    await mockThreadShell(page, threadId, { messages });
    await page.goto(`/${threadId}`);
    const chat = page.getByTestId('chat-panel');
    await chat.focus();
    await page.keyboard.press('Home');
    await expect(page.locator('[data-message-id="key-newer-120"]')).toBeVisible();
    await page.keyboard.press('Home');
    await expect(page.locator('[data-message-id="key-newer-60"]')).toBeVisible();
    await chat.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    await chat.focus();
    await page.keyboard.press('PageDown');
    await expect(page.locator('[data-message-id="key-newer-120"]')).toBeVisible();
    await expect(page.locator('[data-message-id="key-newer-239"]')).toBeVisible();
    await expect(page.getByTestId('live-tail-escape')).toHaveCount(0);
  });

  test('coalesces a downward wheel burst to one window', async ({ page }) => {
    const threadId = 'downward-wheel-burst';
    const messages = Array.from({ length: 360 }, (_, index) => ({ id: `burst-newer-${index}`, role: index % 2 ? 'assistant' : 'user', content: `burst ${index}` }));
    await mockThreadShell(page, threadId, { messages });
    await page.goto(`/${threadId}`);
    const chat = page.getByTestId('chat-panel');
    await page.getByTestId('show-more-loaded-messages').click();
    await page.getByTestId('show-more-loaded-messages').click();
    await page.getByTestId('show-more-loaded-messages').click();
    await expect(page.locator('[data-message-id="burst-newer-120"]')).toBeVisible();
    await chat.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    await chat.dispatchEvent('wheel', { deltaY: 900 });
    await chat.dispatchEvent('wheel', { deltaY: 900 });
    await chat.dispatchEvent('wheel', { deltaY: 900 });
    await expect(page.locator('[data-message-id="burst-newer-180"]')).toBeVisible();
    await expect(page.locator('[data-message-id="burst-newer-359"]')).toHaveCount(0);
    await expect(page.getByTestId('live-tail-escape')).toContainText('60 newer messages');
  });

  test('direction reversal invalidates an in-flight older fetch before returning live', async ({ page }) => {
    const threadId = 'history-direction-reversal';
    const messages = Array.from({ length: 60 }, (_, index) => ({ id: `reversal-tail-${index}`, role: index % 2 ? 'assistant' : 'user', content: `tail ${index}` }));
    let releaseOlder!: () => void;
    const olderReady = new Promise<void>((resolve) => { releaseOlder = resolve; });
    await mockThreadShell(page, threadId);
    await page.unroute(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route, request) => {
      const before = new URL(request.url()).searchParams.get('before_id');
      if (!before) {
        await route.fulfill({ status: 200, headers: mockApiHeaders, json: { items: messages, snapshot_cursor: 60, next_before: 'older-frontier' } });
        return;
      }
      await olderReady;
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { items: [{ id: 'late-older-result', role: 'user', content: 'LATE OLDER RESULT' }], snapshot_cursor: 60, next_before: null } });
    });
    await page.goto(`/${threadId}`);
    const chat = page.getByTestId('chat-panel');
    await chat.hover();
    await chat.evaluate((element) => { element.scrollTop = 0; });
    await page.mouse.wheel(0, -900);
    await chat.focus();
    await page.keyboard.press('End');
    releaseOlder();
    await expect(page.locator('[data-message-id="reversal-tail-59"]')).toBeVisible();
    await expect(page.locator('[data-message-id="late-older-result"]')).toHaveCount(0);
    await expect(page.getByTestId('live-tail-escape')).toHaveCount(0);
  });

  test('route switch cancels stale newer boundary work', async ({ page }) => {
    const threadId = 'stale-newer-route-a';
    const otherThread = 'stale-newer-route-b';
    const messages = Array.from({ length: 180 }, (_, index) => ({ id: `route-a-${index}`, role: index % 2 ? 'assistant' : 'user', content: `route a ${index}` }));
    await mockThreadShell(page, threadId, { messages });
    await mockThreadShell(page, otherThread, { messages: [{ id: 'route-b-only', role: 'assistant', content: 'ROUTE B ONLY' }] });
    await page.goto(`/${threadId}`);
    const chat = page.getByTestId('chat-panel');
    await chat.focus();
    await page.keyboard.press('Home');
    await expect(page.locator('[data-message-id="route-a-60"]')).toBeVisible();
    await chat.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    await chat.dispatchEvent('wheel', { deltaY: 900 });
    await page.goto(`/${otherThread}`);
    await expect(page.locator('[data-message-id="route-b-only"]')).toBeVisible();
    await expect(page.locator('[data-message-id^="route-a-"]')).toHaveCount(0);
    await expect(page.getByTestId('live-tail-escape')).toHaveCount(0);
  });

  test('checks the post-key boundary when Home crosses into history demand', async ({ page }) => {
    const threadId = 'scroll-top-key-demand';
    let messageRequests = 0;
    await mockThreadShell(page, threadId);
    await page.unroute(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route, request) => {
      messageRequests += 1;
      const beforeId = new URL(request.url()).searchParams.get('before_id');
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: beforeId
          ? { items: [{ id: 'keyboard-older', role: 'user', content: 'KEYBOARD OLDER PAGE' }], snapshot_cursor: 0, next_before: null }
          : { items: [{ id: 'keyboard-new', role: 'user', content: 'newest' }], snapshot_cursor: 0, next_before: 'keyboard-new' },
      });
    });

    await page.goto(`/${threadId}`);
    const chat = page.getByTestId('chat-panel');
    await chat.evaluate((element) => {
      const filler = document.createElement('div');
      filler.style.height = '900px';
      element.append(filler);
      element.scrollTop = 300;
    });
    await expect.poll(() => chat.evaluate((element) => element.scrollTop)).toBeGreaterThan(240);
    await chat.focus();
    await page.keyboard.press('Home');
    await expect.poll(() => messageRequests).toBe(2);
    await expect(chat).toContainText('KEYBOARD OLDER PAGE');
  });

  test('settles an empty overlap page whose rendered start does not change', async ({ page }) => {
    const threadId = 'scroll-empty-overlap-page';
    let messageRequests = 0;
    await mockThreadShell(page, threadId);
    await page.unroute(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route, request) => {
      messageRequests += 1;
      const beforeId = new URL(request.url()).searchParams.get('before_id');
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: beforeId
          ? { items: [], snapshot_cursor: 0, next_before: null }
          : { items: [{ id: 'overlap-new', role: 'user', content: 'overlap newest' }], snapshot_cursor: 0, next_before: 'overlap-new' },
      });
    });

    await page.goto(`/${threadId}`);
    const chat = page.getByTestId('chat-panel');
    await chat.focus();
    await page.keyboard.press('Home');
    await expect.poll(() => messageRequests).toBe(2);
    await expect(chat).not.toHaveAttribute('aria-busy', 'true');
    await expect(chat).toContainText('overlap newest');
  });

  test('keeps a displaced 300-message tail reachable after a full fresh tail arrives', async ({ page }) => {
    const threadId = 'monotonic-displaced-tail';
    const initialTail = Array.from({ length: 300 }, (_, index) => ({
      id: `displaced-old-${index}`,
      role: index % 2 ? 'assistant' : 'user',
      content: index === 0 ? 'OLDEST DISPLACED TAIL ENTRY' : `old tail ${index}`,
    }));
    const replacementTail = Array.from({ length: 300 }, (_, index) => ({
      id: `displaced-new-${index}`,
      role: index % 2 ? 'assistant' : 'user',
      content: `new tail ${index}`,
    }));
    let request = 0;
    await mockThreadShell(page, threadId);
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/state`);
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200, headers: mockApiHeaders,
      json: { state: 'running', streaming_kind: 'llm', streaming_invoke_id: 'displaced-tail-invoke', live_replay_cursor: 0 },
    }));
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), (route) => {
      const envelope = JSON.stringify({
        event_id: 'displaced-tail-open', event_seq: 1, type: 'stream.open', ts: new Date().toISOString(),
        msg_id: null, invoke_id: 'displaced-tail-invoke', chunk_seq: null, payload: { stream_kind: 'llm' },
      });
      return route.fulfill({
        status: 200, headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body: `id: 1\nevent: stream.open\ndata: ${envelope}\n\n`,
      });
    });
    await page.unroute(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route) => {
      request += 1;
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: {
        items: request === 1 ? initialTail : replacementTail,
        snapshot_cursor: request,
        next_before: 'older-cursor',
      } });
    });

    await page.goto(`/${threadId}`);
    await expect.poll(() => request).toBeGreaterThanOrEqual(2);
    await expect(page.getByText(/Chat Messages · 600 loaded/)).toBeVisible();
    for (let reveal = 0; reveal < 9; reveal += 1) {
      await page.getByTestId('show-more-loaded-messages').click();
    }
    await expect(page.getByTestId('chat-panel')).toContainText('OLDEST DISPLACED TAIL ENTRY');
    await expect(page.getByTestId('chat-panel')).not.toContainText('new tail 299');
    await page.getByTestId('return-to-live-tail').click();
    await expect(page.getByTestId('chat-panel')).toContainText('new tail 299');
    await expect(page.getByTestId('chat-panel')).not.toContainText('OLDEST DISPLACED TAIL ENTRY');
  });

  test('retains loaded pages across a shorter tail refresh and route return', async ({ page }) => {
    const threadId = 'monotonic-page-refresh';
    const otherThread = 'monotonic-page-refresh-other';
    let tailRequests = 0;
    await mockThreadShell(page, threadId);
    await mockThreadShell(page, otherThread, { messages: [{ id: 'other-only', role: 'user', content: 'other route' }] });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/children`);
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/children`, (route) => route.fulfill({
      status: 200, headers: mockApiHeaders,
      json: [{ id: otherThread, name: 'Other monotonic route', parent_id: threadId, has_children: false }],
    }));
    await page.unroute(`${TEST_API_BASE}/api/threads/${otherThread}`);
    await page.route(`${TEST_API_BASE}/api/threads/${otherThread}`, (route) => route.fulfill({
      status: 200, headers: mockApiHeaders,
      json: { id: otherThread, name: otherThread, parent_id: threadId, has_children: false },
    }));
    await page.unroute(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route, request) => {
      const before = new URL(request.url()).searchParams.get('before_id');
      if (before) {
        await route.fulfill({ status: 200, headers: mockApiHeaders, json: {
          items: [{ id: 'monotonic-old', role: 'user', content: 'OLDER PAGE MUST REMAIN' }],
          snapshot_cursor: 4,
          next_before: null,
        } });
        return;
      }
      tailRequests += 1;
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: {
        items: [{ id: `monotonic-tail-${tailRequests}`, role: 'assistant', content: `fresh tail ${tailRequests}` }],
        snapshot_cursor: 4 + tailRequests,
        // The second snapshot is deliberately stale/short and incorrectly says exhausted.
        next_before: tailRequests === 1 ? 'monotonic-tail-1' : null,
      } });
    });

    await page.goto(`/${threadId}`);
    const monotonicChat = page.getByTestId('chat-panel');
    await monotonicChat.focus();
    await page.keyboard.press('Home');
    await expect(monotonicChat).toContainText('OLDER PAGE MUST REMAIN');
    await page.getByRole('button', { name: /Other monotonic route/ }).click();
    await expect(page.getByTestId('chat-panel')).toContainText('other route');
    await page.getByRole('button', { name: /Parent/ }).click();
    await expect.poll(() => tailRequests).toBeGreaterThanOrEqual(2);
    await expect(page.getByTestId('chat-panel')).toContainText('OLDER PAGE MUST REMAIN');
    await expect(page.getByTestId('chat-panel')).toContainText(`fresh tail ${tailRequests}`);
    await expect(page.getByText(new RegExp(`Chat Messages · ${tailRequests + 1} loaded`))).toBeVisible();
  });

  test('keeps following through rapid canonical tool-result reconciliation and lets user-up win', async ({ page }) => {
    const threadId = 'scroll-rapid-tool-results';
    const initialMessages = Array.from({ length: 12 }, (_, index) => ({
      id: `rapid-scroll-message-${index}`,
      role: index % 2 ? 'assistant' : 'user',
      content: `${index}: ${'long transcript content '.repeat(20)}`,
    }));
    await mockThreadShell(page, threadId, { messages: initialMessages });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/state`);
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: { state: 'running', streaming_kind: 'tool', streaming_invoke_id: 'rapid-scroll-invoke', live_replay_cursor: 0 },
    }));

    let releaseResults!: () => void;
    const resultsReady = new Promise<void>((resolve) => { releaseResults = resolve; });
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route) => {
      await resultsReady;
      const ts = new Date().toISOString();
      const envelope = (eventSeq: number, type: string, payload: Record<string, unknown>, msgId: string | null = null) => JSON.stringify({
        event_id: `rapid-scroll-${eventSeq}`,
        event_seq: eventSeq,
        type,
        ts,
        msg_id: msgId,
        invoke_id: 'rapid-scroll-invoke',
        chunk_seq: null,
        payload,
      });
      const block = (eventSeq: number, type: string, payload: Record<string, unknown>, msgId: string | null = null) => [
        `id: ${eventSeq}`, `event: ${type}`, `data: ${envelope(eventSeq, type, payload, msgId)}`, '',
      ];
      await route.fulfill({
        status: 200,
        headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body: [
          ...block(1, 'stream.open', { stream_kind: 'tool' }),
          ...block(2, 'stream.delta', { tool: { id: 'rapid-tool-a', name: 'bash', text: 'live a' } }),
          ...block(3, 'stream.delta', { tool: { id: 'rapid-tool-b', name: 'bash', text: 'live b' } }),
          ...block(4, 'msg.create', { role: 'assistant', content: '', tool_calls: [
            { id: 'rapid-tool-a', name: 'bash', arguments: '{}' },
            { id: 'rapid-tool-b', name: 'bash', arguments: '{}' },
          ] }, 'rapid-tool-calls'),
          ...block(5, 'msg.create', { role: 'tool', tool_call_id: 'rapid-tool-a', name: 'bash', content: `RESULT A ${'a'.repeat(12_000)}` }, 'rapid-result-a'),
          ...block(6, 'msg.create', { role: 'tool', tool_call_id: 'rapid-tool-b', name: 'bash', content: `RESULT B ${'b'.repeat(12_000)}` }, 'rapid-result-b'),
          ...block(7, 'stream.close', {}),
          '',
        ].join('\n'),
      });
    });

    await page.goto(`/${threadId}`);
    const chat = page.getByTestId('chat-panel');
    const geometry = () => chat.evaluate((element) => ({
      top: element.scrollTop,
      distance: element.scrollHeight - element.scrollTop - element.clientHeight,
    }));
    await chat.evaluate((element) => {
      const trace: number[] = [];
      let frames = 0;
      const sample = () => {
        trace.push(element.scrollHeight - element.scrollTop - element.clientHeight);
        if ((frames += 1) < 240) requestAnimationFrame(sample);
      };
      (window as typeof window & { __eggwLiveEdgeTrace?: number[] }).__eggwLiveEdgeTrace = trace;
      requestAnimationFrame(sample);
    });
    await chat.focus();
    await page.keyboard.press('End');
    await expect.poll(async () => (await geometry()).distance).toBeLessThanOrEqual(16);

    releaseResults();
    const compactResults = chat.getByTestId('hidden-details');
    await expect(compactResults).toHaveCount(1, { timeout: 5000 });
    await expect(compactResults).toHaveAttribute('data-source-message-count', '3');
    await expect(compactResults).toContainText('Executed 2 tools, got 2 tool results');
    await expect(compactResults).toContainText('Tools: bash, bash');
    await expect.poll(async () => (await geometry()).distance).toBeLessThanOrEqual(16);
    const liveEdgeTrace = await page.evaluate(() => (
      (window as typeof window & { __eggwLiveEdgeTrace?: number[] }).__eggwLiveEdgeTrace || []
    ));
    expect(liveEdgeTrace.length).toBeGreaterThan(0);
    // No painted frame may expose a transient up-jump while FOLLOWING owns the edge.
    expect(Math.max(0, ...liveEdgeTrace)).toBeLessThanOrEqual(16);
    await page.getByTestId('chat-panel-content').evaluate((element) => {
      const later = document.createElement('div');
      later.style.height = '400px';
      later.textContent = 'later provider result';
      element.append(later);
    });
    await expect.poll(async () => (await geometry()).distance).toBeLessThanOrEqual(16);

    await chat.hover();
    await page.mouse.wheel(0, -700);
    await expect.poll(async () => (await geometry()).distance).toBeGreaterThan(100);
    const detachedTop = (await geometry()).top;
    await page.getByTestId('chat-panel-content').evaluate((element) => {
      const later = document.createElement('div');
      later.style.height = '320px';
      later.textContent = 'must not steal user position';
      element.append(later);
    });
    await expect.poll(async () => Math.abs((await geometry()).top - detachedTop)).toBeLessThanOrEqual(2);
  });
});

test.describe('Destructive transcript authority', () => {
  for (const eventType of ['msg.delete', 'thread.compaction'] as const) {
    test(`${eventType} fences stale same-frontier pages and converges markers`, async ({ page }) => {
      const threadId = `destructive-${eventType.replace('.', '-')}`;
      let messageRequests = 0;
      let eventRequests = 0;
      let releaseStale!: () => void;
      const staleReady = new Promise<void>((resolve) => { releaseStale = resolve; });
      await mockThreadShell(page, threadId);
      await page.unroute(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`));
      await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({ status: 200, headers: mockApiHeaders, json: { state: 'running', streaming_kind: 'llm', streaming_invoke_id: null, live_replay_cursor: 5 } }));
      await page.unroute(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`));
      await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
      await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route) => {
        messageRequests += 1;
        if (messageRequests === 2) {
          // This application-owned refresh starts at the old generation from
          // stream.open. It deliberately ignores cancellation long enough for
          // the destructive event and fresh generation to overtake it.
          await staleReady;
          await route.fulfill({ status: 200, headers: mockApiHeaders, json: {
            items: [{ id: 'stale-page', role: 'user', content: 'STALE MUST NOT RETURN' }],
            snapshot_cursor: 5,
            next_before: null,
          } });
          return;
        }
        await route.fulfill({ status: 200, headers: mockApiHeaders, json: {
          items: messageRequests === 1
            ? [{ id: 'original-page', role: 'user', content: 'ORIGINAL' }]
            : [
                ...(eventType === 'thread.compaction' ? [{
                  id: 'compaction-7', role: 'compaction_marker', kind: 'compaction_marker',
                  content: 'COMPACTION CONVERGED', marker_event_seq: 7, start_event_seq: 1,
                }] : []),
                { id: 'fresh-page', role: 'assistant', content: 'FRESH AUTHORITY' },
              ],
          snapshot_cursor: messageRequests === 1 ? 5 : 7,
          next_before: null,
        } });
      });
      await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route) => {
        eventRequests += 1;
        const streamOpen = {
          event_id: `destructive-open-${eventType}`, event_seq: 6, type: 'stream.open',
          ts: new Date().toISOString(), msg_id: null, invoke_id: `destructive-invoke-${eventType}`,
          chunk_seq: null, payload: { stream_kind: 'llm' },
        };
        const destructive = {
          event_id: `destructive-${eventType}`, event_seq: 7, type: eventType,
          ts: new Date().toISOString(), msg_id: eventType === 'msg.delete' ? 'original-page' : null,
          invoke_id: null, chunk_seq: null,
          payload: eventType === 'thread.compaction'
            ? { start_msg_id: 'fresh-page', start_event_seq: 1 }
            : { reason: 'test' },
        };
        const body = eventRequests === 1
          ? ['id: 6', 'event: stream.open', `data: ${JSON.stringify(streamOpen)}`, '', ''].join('\n')
          : eventRequests === 2
            ? ['id: 7', `event: ${eventType}`, `data: ${JSON.stringify(destructive)}`, '', ''].join('\n')
            : '';
        await route.fulfill({
          status: 200,
          headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
          body,
        });
      });

      await page.goto(`/${threadId}`);
      // The first stream response starts request 2 at generation 0. Its closed
      // transport reconnects and delivers the destructive event, which must
      // cancel/fence request 2 and let generation 1 install request 3 first.
      await expect.poll(() => messageRequests, { timeout: 10_000 }).toBeGreaterThanOrEqual(3);
      const chat = page.getByTestId('chat-panel');
      await expect(chat).toContainText('FRESH AUTHORITY');
      releaseStale();
      await expect(chat).toContainText('FRESH AUTHORITY');
      await expect(chat).not.toContainText('STALE MUST NOT RETURN');
      if (eventType === 'thread.compaction') await expect(chat).toContainText('Compaction boundary');
      else await expect(chat).not.toContainText('ORIGINAL');
    });
  }
});

test.describe('Continuation transcript authority', () => {
  test('rewinds a fully displaced loaded tail, fences stale pages, and reaches pre-boundary history', async ({
    page,
  }) => {
    const threadId = 'continue-disjoint-rewind';
    const skippedTail = Array.from({ length: 300 }, (_, index) => ({
      id: `skipped-tail-${index}`,
      role: index % 2 ? 'assistant' : 'user',
      content: `SKIPPED LOADED ${index}`,
    }));
    let initialTailServed = false;
    let staleTailStarted = false;
    let freshTailRequests = 0;
    const requestedBeforeIds: Array<string | null> = [];
    let releaseContinuation!: () => void;
    const continuationReady = new Promise<void>((resolve) => {
      releaseContinuation = resolve;
    });
    let eventRequests = 0;
    let releaseStaleTail!: () => void;
    const staleTailReady = new Promise<void>((resolve) => {
      releaseStaleTail = resolve;
    });

    await mockThreadShell(page, threadId);
    await page.unroute(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`));
    await page.route(
      new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`),
      (route) =>
        route.fulfill({
          status: 200,
          headers: mockApiHeaders,
          json: {
            state: 'waiting_user',
            streaming_invoke_id: null,
            live_replay_cursor: 40,
            active_get_user_wait: false,
          },
        }),
    );
    await page.unroute(
      new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`),
    );
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.route(
      new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`),
      async (route, request) => {
        const beforeId = new URL(request.url()).searchParams.get('before_id');
        requestedBeforeIds.push(beforeId);
        if (!beforeId && initialTailServed && !staleTailStarted) {
          // A generation-zero refresh started by stream.open. The mock deliberately
          // completes after continuation even though its AbortSignal was cancelled.
          staleTailStarted = true;
          await staleTailReady;
          await route.fulfill({
            status: 200,
            headers: mockApiHeaders,
            json: {
              items: [
                {
                  id: 'stale-skipped-tail',
                  role: 'assistant',
                  content: 'STALE SKIPPED TAIL',
                },
              ],
              snapshot_cursor: 41,
              next_before: null,
            },
          });
          return;
        }
        if (beforeId === 'legitimate-pre-boundary') {
          await route.fulfill({
            status: 200,
            headers: mockApiHeaders,
            json: {
              items: [
                {
                  id: 'legitimate-before',
                  role: 'user',
                  content: 'LEGITIMATE BEFORE BOUNDARY',
                },
              ],
              snapshot_cursor: 43,
              next_before: null,
            },
          });
          return;
        }
        if (!beforeId && initialTailServed) {
          freshTailRequests += 1;
          await route.fulfill({
            status: 200,
            headers: mockApiHeaders,
            json: {
              items: [
                {
                  id: 'continue-boundary',
                  role: 'user',
                  content: 'CONTINUE BOUNDARY',
                },
              ],
              snapshot_cursor: 43,
              next_before: 'legitimate-pre-boundary',
            },
          });
          return;
        }
        initialTailServed = true;
        await route.fulfill({
          status: 200,
          headers: mockApiHeaders,
          json: {
            items: skippedTail,
            snapshot_cursor: 40,
            next_before: 'old-pre-boundary',
          },
        });
      },
    );
    await page.route(
      new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`),
      async (route) => {
        eventRequests += 1;
        const envelope = (
          eventSeq: number,
          type: string,
          payload: Record<string, unknown>,
          msgId: string | null = null,
          invokeId: string | null = null,
        ) => ({
          event_id: `continue-${eventSeq}`,
          event_seq: eventSeq,
          type,
          ts: new Date().toISOString(),
          msg_id: msgId,
          invoke_id: invokeId,
          chunk_seq: null,
          payload,
        });
        const block = (event: ReturnType<typeof envelope>) => [
          `id: ${event.event_seq}`,
          `event: ${event.type}`,
          `data: ${JSON.stringify(event)}`,
          '',
        ];
        if (eventRequests === 2) await continuationReady;
        const body =
          eventRequests === 1
            ? [
                ...block(
                  envelope(
                    41,
                    'stream.open',
                    { stream_kind: 'llm' },
                    null,
                    'continue-old-invoke',
                  ),
                ),
                '',
              ].join('\n')
            : eventRequests === 2
              ? [
                  ...block(
                    envelope(
                      42,
                      'msg.edit',
                      {
                        skipped_on_continue: true,
                        continue_event_id: 'continue-transaction-1',
                      },
                      'skipped-tail-299',
                    ),
                  ),
                  ...block(
                    envelope(43, 'control.interrupt', {
                      purpose: 'continue',
                      reason: 'continue_thread',
                      continue_from_msg_id: 'continue-boundary',
                      skipped_count: 300,
                    }),
                  ),
                  '',
                ].join('\n')
              : '';
        await route.fulfill({
          status: 200,
          headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
          body,
        });
      },
    );

    await page.goto(`/${threadId}`);
    const chat = page.getByTestId('chat-panel');
    await expect(chat).toContainText('SKIPPED LOADED 299');
    releaseContinuation();
    // stream.open starts a generation-zero tail refresh. The delayed response
    // deliberately ignores cancellation so continuation must fence it by generation.
    await expect.poll(() => staleTailStarted).toBe(true);

    // Canonical skipped edit + control boundary must rebuild, not bridge, even
    // though the post-continue tail is completely disjoint from the loaded tail.
    await expect
      .poll(() => freshTailRequests, { timeout: 10_000 })
      .toBeGreaterThanOrEqual(1);
    await expect(chat).toContainText('CONTINUE BOUNDARY');
    await expect(chat).not.toContainText('SKIPPED LOADED');
    await expect(page.getByText(/Chat Messages · 1 loaded/)).toBeVisible();

    releaseStaleTail();
    await expect(chat).toContainText('CONTINUE BOUNDARY');
    await expect(chat).not.toContainText('STALE SKIPPED TAIL');
    await expect(chat).not.toContainText('SKIPPED LOADED');
    await expect(page.getByText(/Chat Messages · 1 loaded/)).toBeVisible();

    // Allow the stale generation to finish its ignored-abort response before
    // requesting legitimate pre-boundary history from the rebuilt frontier.
    await page.waitForTimeout(100);
    await chat.focus();
    await page.keyboard.press('Home');
    await expect
      .poll(() => requestedBeforeIds.filter(Boolean))
      .toContain('legitimate-pre-boundary');
    await expect(chat).toContainText('LEGITIMATE BEFORE BOUNDARY');
    await expect(chat).toContainText('CONTINUE BOUNDARY');
    await expect(chat).not.toContainText('SKIPPED LOADED');
  });
});

test.describe('Continuation command without SSE authority', () => {
  test('rewinds command reload, preserves pre-boundary history, and fences stale tail', async ({ page }) => {
    const threadId = 'continue-command-no-sse';
    const skippedTail = Array.from({ length: 300 }, (_, index) => ({
      id: `command-skipped-${index}`,
      role: index % 2 ? 'assistant' : 'user',
      content: `COMMAND SKIPPED ${index}`,
    }));
    let initialTailServed = false;
    let staleTailStarted = false;
    let commandApplied = false;
    let freshTailRequests = 0;
    let releaseStale!: () => void;
    const staleReady = new Promise<void>((resolve) => { releaseStale = resolve; });
    const requestedBeforeIds: Array<string | null> = [];

    await mockThreadShell(page, threadId);
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.route(`**/api/threads/${threadId}/events*`, async (route) => {
      await route.fulfill({
        status: 503,
        headers: mockApiHeaders,
        json: { detail: 'SSE unavailable' },
      });
    });
    await page.unroute(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route, request) => {
      const beforeId = new URL(request.url()).searchParams.get('before_id');
      requestedBeforeIds.push(beforeId);
      if (beforeId === 'command-pre-boundary') {
        await route.fulfill({
          status: 200,
          headers: mockApiHeaders,
          json: {
            items: [{ id: 'command-legitimate-before', role: 'user', content: 'COMMAND LEGITIMATE BEFORE' }],
            snapshot_cursor: 43,
            next_before: null,
          },
        });
        return;
      }
      if (!beforeId && initialTailServed && !commandApplied && !staleTailStarted) {
        staleTailStarted = true;
        await staleReady;
        await route.fulfill({
          status: 200,
          headers: mockApiHeaders,
          json: {
            items: [{ id: 'command-stale-skipped', role: 'assistant', content: 'COMMAND STALE SKIPPED' }],
            snapshot_cursor: 41,
            next_before: null,
          },
        });
        return;
      }
      if (!beforeId && commandApplied) {
        freshTailRequests += 1;
        await route.fulfill({
          status: 200,
          headers: mockApiHeaders,
          json: {
            items: [{ id: 'command-continue-boundary', role: 'user', content: 'COMMAND CONTINUE BOUNDARY' }],
            snapshot_cursor: 43,
            next_before: 'command-pre-boundary',
          },
        });
        return;
      }
      initialTailServed = true;
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          items: skippedTail,
          snapshot_cursor: 40,
          next_before: 'old-command-history',
        },
      });
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/command`, async (route, request) => {
      const command = String((request.postDataJSON() as { command?: string }).command || '');
      if (command === '/attachments') {
        await route.fulfill({
          status: 200,
          headers: mockApiHeaders,
          json: {
            success: true,
            message: 'Ordinary reload started',
            command_id: 'ordinary-reload-before-continue',
            command_name: 'attachments',
            data: { action: 'list_attachments', reload: true },
          },
        });
        return;
      }
      expect(command).toBe('/continue command-skipped-0');
      commandApplied = true;
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          success: true,
          message: 'Continued from command-skipped-0',
          command_id: 'continue-command-no-sse-result',
          command_name: 'continue',
          data: {
            continue_from: 'command-skipped-0',
            skipped_count: 299,
            reload: true,
            reload_mode: 'continuation',
          },
        },
      });
    });

    await page.goto(`/${threadId}`);
    const chat = page.getByTestId('chat-panel');
    await expect(chat).toContainText('COMMAND SKIPPED 299');

    // SSE remains unavailable; command response is the only continuation authority.
    // Hold an ordinary reload's disjoint response across the continuation result.
    const input = page.getByTestId('message-input');
    await input.fill('/attachments');
    await input.press('Enter');
    await expect.poll(() => staleTailStarted).toBe(true);
    await input.fill('/continue command-skipped-0');
    await input.press('Enter');

    await expect.poll(() => freshTailRequests, { timeout: 10_000 }).toBeGreaterThanOrEqual(1);
    await expect(chat).toContainText('COMMAND CONTINUE BOUNDARY');
    await expect(chat).not.toContainText('COMMAND SKIPPED');

    releaseStale();
    await expect(chat).toContainText('COMMAND CONTINUE BOUNDARY');
    await expect(chat).not.toContainText('COMMAND STALE SKIPPED');
    await expect(chat).not.toContainText('COMMAND SKIPPED');

    await chat.focus();
    await page.keyboard.press('Home');
    await expect.poll(() => requestedBeforeIds.filter(Boolean)).toContain('command-pre-boundary');
    await expect(chat).toContainText('COMMAND LEGITIMATE BEFORE');
    await expect(chat).toContainText('COMMAND CONTINUE BOUNDARY');
    await expect(chat).not.toContainText('COMMAND SKIPPED');
  });
});

test.describe('Children panel identity', () => {
  test('shows every child thread ID with full-ID access when names are ambiguous', async ({ page }) => {
    const parentId = 'children-identity-parent';
    const firstChildId = '01JCHILDIDENTITY00000000000001';
    const secondChildId = '01JCHILDIDENTITY00000000000002';
    const unnamedChildId = '01JCHILDIDENTITY00000000000003';
    await mockThreadShell(page, parentId);
    await page.unroute(`${TEST_API_BASE}/api/threads/${parentId}/children`);
    await page.route(`${TEST_API_BASE}/api/threads/${parentId}/children`, (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: [
        { id: firstChildId, name: 'Duplicate child', parent_id: parentId, model_key: 'provider:first', has_children: false },
        { id: secondChildId, name: 'Duplicate child', parent_id: parentId, model_key: 'provider:second', has_children: true },
        { id: unnamedChildId, parent_id: parentId, has_children: false },
      ],
    }));

    await page.goto(`/${parentId}`);

    const children = page.locator('.eggw-children-list .eggw-thread-row');
    await expect(children).toHaveCount(3);
    await expect(children.nth(0).getByText('Duplicate child', { exact: true })).toBeVisible();
    await expect(children.nth(1).getByText('Duplicate child', { exact: true })).toBeVisible();
    await expect(children.nth(2).getByText('Unnamed child', { exact: true })).toBeVisible();
    const childIds = [firstChildId, secondChildId, unnamedChildId];
    for (let index = 0; index < childIds.length; index += 1) {
      const childId = childIds[index];
      const identity = children.nth(index).locator('code');
      await expect(identity).toHaveText(childId.slice(-8));
      await expect(identity).toHaveAttribute('title', childId);
      await expect(identity).toHaveAttribute('aria-label', `Thread ID ${childId}`);
      await expect(children.nth(index).locator('.eggw-thread-link')).toHaveAttribute('title', `Open child thread ${childId}`);
      await expect(children.nth(index).getByRole('button', { name: `Copy thread ID ${childId}` })).toHaveAttribute('title', 'Copy full thread ID');
    }

    await page.evaluate(() => {
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: { writeText: (value: string) => { (window as typeof window & { copiedThreadId?: string }).copiedThreadId = value; } },
      });
    });
    const copySecondId = children.nth(1).getByRole('button', { name: `Copy thread ID ${secondChildId}` });
    await copySecondId.focus();
    await expect(copySecondId).toBeFocused();
    await copySecondId.press('Enter');
    await expect.poll(() => page.evaluate(() => (window as typeof window & { copiedThreadId?: string }).copiedThreadId)).toBe(secondChildId);
    await expect(page).toHaveURL(new RegExp(`/${parentId}$`));
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
    const threadAChat = page.getByTestId('chat-panel');
    await threadAChat.focus();
    await page.keyboard.press('Home');
    await expect(threadAChat).toContainText('thread a old');

    await page.locator('.eggw-thread-link').filter({ hasText: threadB }).click();
    await expect(page).toHaveURL(new RegExp(`/${threadB}$`));
    await expect(page.getByTestId('chat-panel')).toContainText('thread b only');
    await expect(page.getByTestId('chat-panel')).not.toContainText('thread a old');

    await page.getByRole('button', { name: /Parent/ }).click();
    await expect(page).toHaveURL(new RegExp(`/${threadA}$`));
    await expect(page.getByTestId('chat-panel')).toContainText('thread a old');
    await expect(page.getByTestId('chat-panel')).toContainText('thread a new');
    await expect(page.getByTestId('chat-panel')).not.toContainText('thread b only');
  });

  test('cancels stale SSE setup ownership when route changes mid-snapshot', async ({ page }) => {
    const threadA = 'sse-setup-slow-a';
    const threadB = 'sse-setup-fast-b';
    await mockThreadShell(page, threadA, { messages: [] });
    await mockThreadShell(page, threadB, { messages: [{ id: 'b-visible', role: 'user', content: 'thread b current' }] });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadA}/children`);
    await page.route(`${TEST_API_BASE}/api/threads/${threadA}/children`, (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: [{ id: threadB, name: 'Fast child B', parent_id: threadA, has_children: false }],
    }));

    let releaseThreadA!: () => void;
    const threadAReady = new Promise<void>((resolve) => { releaseThreadA = resolve; });
    await page.unroute(new RegExp(`/api/threads/${threadA}/messages(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadA}/messages(?:\\?.*)?$`), async (route) => {
      await threadAReady;
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { items: [], snapshot_cursor: 7, next_before: null } });
    });

    let stateARequests = 0;
    let eventARequests = 0;
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadA}/state`);
    await page.route(new RegExp(`/api/threads/${threadA}/state(?:\\?.*)?$`), (route) => {
      stateARequests += 1;
      return route.fulfill({ status: 200, headers: mockApiHeaders, json: { state: 'waiting_user', live_replay_cursor: 7 } });
    });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadA}/events`);
    await page.route(new RegExp(`/api/threads/${threadA}/events(?:\\?.*)?$`), (route) => {
      eventARequests += 1;
      return route.fulfill({ status: 200, headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' }, body: '' });
    });

    await page.goto(`/${threadA}`);
    await expect(page.getByRole('button', { name: /Fast child B/ })).toBeVisible();
    const stateRequestsBeforeNavigation = stateARequests;
    const eventRequestsBeforeNavigation = eventARequests;
    await page.getByRole('button', { name: /Fast child B/ }).click();
    await expect(page).toHaveURL(new RegExp(`/${threadB}$`));
    await expect(page.getByTestId('chat-panel')).toContainText('thread b current');

    releaseThreadA();
    await page.waitForTimeout(100);
    expect(stateARequests).toBe(stateRequestsBeforeNavigation);
    expect(eventARequests).toBe(eventRequestsBeforeNavigation);
    await expect(page).toHaveURL(new RegExp(`/${threadB}$`));
  });

  test('hydrates a 60-message window when a Children panel click changes routes', async ({ page }) => {
    const parentId = 'click-hydration-parent';
    const childId = 'click-hydration-child';
    const childMessages = Array.from({ length: 140 }, (_, index) => ({
      id: `child-context-${index}`,
      role: index % 2 ? 'assistant' : 'user',
      content: `child context ${index}`,
    }));
    for (const threadId of [parentId, childId]) {
      await mockThreadShell(page, threadId, { messages: threadId === childId ? childMessages : [] });
    }
    await page.unroute(`${TEST_API_BASE}/api/threads/${parentId}/children`);
    await page.route(`${TEST_API_BASE}/api/threads/${parentId}/children`, (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: [{ id: childId, name: 'Hydration child', parent_id: parentId, has_children: false }],
    }));
    const childRequests: string[] = [];
    await page.unroute(new RegExp(`/api/threads/${childId}/messages(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${childId}/messages(?:\\?.*)?$`), async (route, request) => {
      childRequests.push(request.url());
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { items: childMessages, snapshot_cursor: 140, next_before: null },
      });
    });

    await page.goto(`/${parentId}`);
    await page.getByRole('button', { name: /Hydration child/ }).click();

    await expect(page).toHaveURL(new RegExp(`/${childId}$`));
    await expect(page.getByText(/Chat Messages · 140 loaded/)).toBeVisible();
    await expect(page.locator('.eggw-message-card')).toHaveCount(60);
    await expect(page.locator('[data-message-id="child-context-80"]')).toBeVisible();
    await expect(page.locator('[data-message-id="child-context-139"]')).toBeVisible();
    await expect(page.locator('[data-message-id="child-context-79"]')).toHaveCount(0);
    expect(childRequests).toHaveLength(1);
    expect(new URL(childRequests[0]).searchParams.get('limit')).toBe('300');

    await page.getByTestId('show-more-loaded-messages').click();
    await expect(page.locator('.eggw-message-card')).toHaveCount(120);
    expect(childRequests).toHaveLength(1);
  });

  test('reveals loaded min history before pagination and reaches the system prompt', async ({ page }) => {
    const threadId = 'min-system-prompt-history';
    let messageRequests = 0;
    await mockThreadShell(page, threadId);
    await page.unroute(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route, request) => {
      messageRequests += 1;
      const beforeId = new URL(request.url()).searchParams.get('before_id');
      if (!beforeId) {
        await route.fulfill({
          status: 200,
          headers: mockApiHeaders,
          json: {
            items: Array.from({ length: 70 }, (_, index) => ({
              id: `recent-${index}`,
              role: index % 2 ? 'assistant' : 'user',
              content: `recent message ${index}`,
            })),
            snapshot_cursor: 200,
            next_before: 'recent-0',
          },
        });
        return;
      }
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          items: [
            { id: 'root-system-prompt', role: 'system', content: 'ROOT SYSTEM PROMPT CONTENT' },
            ...Array.from({ length: 89 }, (_, index) => ({
              id: `older-${index}`,
              role: index % 3 === 0 ? 'tool' : (index % 2 ? 'assistant' : 'user'),
              ...(index % 3 === 0
                ? { name: 'bash', tool_call_id: `older-call-${index}`, content: `older tool output ${index}` }
                : { content: `older conversation ${index}` }),
            })),
          ],
          snapshot_cursor: 200,
          next_before: null,
        },
      });
    });

    await page.goto(`/${threadId}`);
    await page.locator('select[title="Transcript display verbosity"]').selectOption('min');
    await expect(page.locator('.eggw-message-card')).toHaveCount(60);

    // Expose the already-loaded 10-message prefix before requesting older data.
    await page.getByTestId('show-more-loaded-messages').click();
    const requestsBeforeOlder = messageRequests;

    const minChat = page.getByTestId('chat-panel');
    await minChat.focus();
    await page.keyboard.press('Home');
    await expect.poll(() => messageRequests).toBe(requestsBeforeOlder + 1);
    await expect(page.getByTestId('show-more-loaded-messages')).toContainText('30 earlier');
    await expect(page.getByTestId('chat-panel')).not.toContainText('ROOT SYSTEM PROMPT CONTENT');

    await page.getByTestId('show-more-loaded-messages').click();
    await expect(page.getByTestId('chat-panel')).toContainText('ROOT SYSTEM PROMPT CONTENT');
    await expect(page.getByText('System', { exact: true }).first()).toBeVisible();
    expect(messageRequests).toBe(requestsBeforeOlder + 1);
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

test.describe('Authoritative same-version transcript refresh', () => {
  test('updates content, optimizer, and consumed metadata for the same id and create sequence', async ({ page }) => {
    const threadId = 'same-version-projection-refresh';
    const getUserName = 'get_user_message_while_preserving_llm_turn';
    let messageRequests = 0;
    let releaseRefresh!: () => void;
    const refreshReady = new Promise<void>((resolve) => { releaseRefresh = resolve; });
    await mockThreadShell(page, threadId);
    await page.unroute(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`));
    await page.unroute(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`));
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route) => {
      messageRequests += 1;
      if (messageRequests > 1) await refreshReady;
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          items: [{
            id: 'same-version-message',
            role: 'user',
            content: messageRequests === 1 ? 'BEFORE AUTHORITATIVE REFRESH' : 'AFTER AUTHORITATIVE REFRESH',
            event_seq: 7,
            ...(messageRequests > 1 ? {
              output_optimizer: { optimized: true, summary: 'OPTIMIZED ON REFRESH' },
              consumed_by_tool_name: getUserName,
              consumed_by_tool_call_id: 'call-refreshed-answer',
            } : {}),
          }],
          snapshot_cursor: messageRequests === 1 ? 7 : 8,
          next_before: null,
        },
      });
    });
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: { state: 'waiting_user', streaming_invoke_id: null, live_replay_cursor: 7, active_get_user_wait: false },
    }));
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route) => {
      releaseRefresh();
      await route.fulfill({
        status: 200,
        headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body: '',
      });
    });

    await page.goto(`/${threadId}`);
    await page.locator('select[title="Transcript display verbosity"]').selectOption('max');
    const message = page.locator('[data-message-id="same-version-message"]');
    await expect(message).toContainText('BEFORE AUTHORITATIVE REFRESH');
    await expect.poll(() => messageRequests).toBeGreaterThanOrEqual(2);
    await expect(message).toContainText('AFTER AUTHORITATIVE REFRESH');
    await expect(message).not.toContainText('BEFORE AUTHORITATIVE REFRESH');
    await expect(message.getByTestId('output-optimizer-badge')).toContainText('OPTIMIZED ON REFRESH');
    await expect(message).toHaveAttribute('data-consumed-by-tool-call-id', 'call-refreshed-answer');
  });
});

test.describe('Live Tool Streaming', () => {
  test('keeps simultaneous live tools separated by exact call identity', async ({ page }) => {
    await page.addInitScript(() => {
      window.sessionStorage.setItem('eggw.apiToken', 'test-eggw-browser-token-' + 'a'.repeat(48));
    });
    const threadId = 'simultaneous-live-tool-identity';
    await mockThreadShell(page, threadId, { messages: [{ id: 'live-tools-user', role: 'user', content: 'run both' }] });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/state`);
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: { state: 'running', streaming_kind: 'tool', streaming_invoke_id: 'invoke-live-pairing', live_replay_cursor: 0 },
    }));
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route) => {
      const ts = new Date().toISOString();
      const envelope = (eventSeq: number, type: string, payload: Record<string, unknown>) => JSON.stringify({
        event_id: `live-pairing-${eventSeq}`,
        event_seq: eventSeq,
        type,
        ts,
        msg_id: null,
        invoke_id: 'invoke-live-pairing',
        chunk_seq: type === 'stream.delta' ? eventSeq : null,
        payload,
      });
      const block = (eventSeq: number, type: string, payload: Record<string, unknown>) => [
        `id: ${eventSeq}`, `event: ${type}`, `data: ${envelope(eventSeq, type, payload)}`, '',
      ];
      await route.fulfill({
        status: 200,
        headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body: [
          ...block(1, 'stream.open', { stream_kind: 'tool' }),
          ...block(2, 'tool_call.execution_started', { tool_call_id: 'call-live-bash', name: 'bash', arguments: '{"script":"echo LIVE_BASH"}' }),
          ...block(3, 'tool_call.execution_started', { tool_call_id: 'call-live-python', name: 'python', arguments: '{"script":"print(1)"}' }),
          ...block(4, 'stream.delta', { tool: { id: 'call-live-python', name: 'python', text: 'OUTPUT_PYTHON' } }),
          ...block(5, 'stream.delta', { tool: { id: 'call-live-bash', name: 'bash', text: 'OUTPUT_BASH' } }),
          ...block(6, 'stream.delta', { tool: { name: 'bash', text: 'MALFORMED_ORPHAN' } }),
          '',
        ].join('\n'),
      });
    });

    await page.goto(`/${threadId}`);
    for (const verbosity of ['max', 'medium', 'min'] as const) {
      await page.locator('select[title="Transcript display verbosity"]').selectOption(verbosity);
      await expect(page.getByTestId('streaming-tool-arguments')).toHaveCount(2);
      await expect(page.getByTestId('streaming-tool-output')).toHaveCount(2);
      await expect(page.getByTestId('chat-panel')).toContainText('LIVE_BASH');
      await expect(page.getByTestId('chat-panel')).toContainText('print(1)');
      await expect(page.getByTestId('chat-panel')).toContainText('OUTPUT_BASH');
      await expect(page.getByTestId('chat-panel')).toContainText('OUTPUT_PYTHON');
      await expect(page.getByTestId('chat-panel')).not.toContainText('MALFORMED_ORPHAN');
      await expect(page.getByText('bash', { exact: true }).last()).toBeVisible();
      await expect(page.getByText('python', { exact: true }).last()).toBeVisible();
    }
  });

  test('countdown ticks keep transcript geometry stable and do not commit the page owner', async ({ page }) => {
    const threadId = 'stable-countdown-geometry';
    const toolId = 'call-stable-countdown';
    await mockThreadShell(page, threadId, {
      messages: Array.from({ length: 18 }, (_, index) => ({
        id: `countdown-message-${index}`,
        role: index % 2 ? 'assistant' : 'user',
        content: `${index}: ${'stable transcript height '.repeat(16)}`,
      })),
    });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/state`);
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200, headers: mockApiHeaders,
      json: { state: 'running', streaming_kind: 'tool', streaming_invoke_id: 'countdown-invoke', live_replay_cursor: 0 },
    }));
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route) => {
      const oldTs = new Date(Date.now() - 98_000).toISOString();
      const envelope = (eventSeq: number, type: string, payload: Record<string, unknown>) => JSON.stringify({
        event_id: `countdown-${eventSeq}`, event_seq: eventSeq, type, ts: oldTs,
        msg_id: null, invoke_id: 'countdown-invoke', chunk_seq: null, payload,
      });
      const block = (eventSeq: number, type: string, payload: Record<string, unknown>) => [
        `id: ${eventSeq}`, `event: ${type}`, `data: ${envelope(eventSeq, type, payload)}`, '',
      ];
      await route.fulfill({ status: 200, headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' }, body: [
        ...block(1, 'stream.open', { stream_kind: 'tool' }),
        ...block(2, 'tool_call.execution_started', { tool_call_id: toolId, name: 'bash', timeout: 120 }),
        '',
      ].join('\n') });
    });

    await page.goto(`/${threadId}`);
    const chat = page.getByTestId('chat-panel');
    const timer = page.getByTestId('streaming-tool-timeout-summary');
    // The fixed-width slot exists on first render, before its first timer effect.
    await expect(timer).toBeAttached();
    await expect(timer).toHaveCSS('width', /[1-9][0-9]*px/);
    await expect(timer).toContainText('timeout in');
    await chat.hover();
    await page.mouse.wheel(0, -700);
    await expect.poll(() => chat.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeGreaterThan(100);
    // Fence every mocked background query before measuring the timing-only leaf.
    // Two animation frames after stable transcript/page counters prove the query
    // owner has settled rather than merely sleeping for an arbitrary interval.
    let settledCounters = { page: -1, transcript: -1 };
    let stableSamples = 0;
    await expect.poll(async () => {
      const next = await page.evaluate(() => ({
        page: window.__EGGW_PERFORMANCE__?.chatPanelCommits || 0,
        transcript: window.__EGGW_PERFORMANCE__?.transcriptCommits || 0,
      }));
      if (next.page === settledCounters.page && next.transcript === settledCounters.transcript) {
        stableSamples += 1;
      } else {
        stableSamples = 0;
      }
      settledCounters = next;
      return stableSamples;
    }, { intervals: [100, 100, 100, 100, 100] }).toBeGreaterThanOrEqual(3);
    await page.evaluate(() => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    }));
    await page.evaluate(() => {
      if (!window.__EGGW_PERFORMANCE__) return;
      window.__EGGW_PERFORMANCE__.chatPanelCommits = 0;
      window.__EGGW_PERFORMANCE__.transcriptCommits = 0;
      window.__EGGW_PERFORMANCE__.liveTimingCommits = 0;
    });
    const before = await page.evaluate(() => ({
      top: document.querySelector<HTMLElement>('[data-testid="chat-panel"]')!.scrollTop,
      height: document.querySelector<HTMLElement>('[data-testid="chat-panel"]')!.scrollHeight,
      pageCommits: window.__EGGW_PERFORMANCE__?.chatPanelCommits || 0,
      transcriptCommits: window.__EGGW_PERFORMANCE__?.transcriptCommits || 0,
      timingCommits: window.__EGGW_PERFORMANCE__?.liveTimingCommits || 0,
      timerWidth: document.querySelector<HTMLElement>('[data-testid="streaming-tool-timeout-summary"]')!.getBoundingClientRect().width,
      transcriptNode: document.querySelector('[data-testid="static-transcript-owner"]'),
    }));
    const timerBefore = await timer.innerText();
    await expect.poll(() => timer.innerText(), { timeout: 3_000 }).not.toBe(timerBefore);
    const after = await page.evaluate(() => ({
      top: document.querySelector<HTMLElement>('[data-testid="chat-panel"]')!.scrollTop,
      height: document.querySelector<HTMLElement>('[data-testid="chat-panel"]')!.scrollHeight,
      pageCommits: window.__EGGW_PERFORMANCE__?.chatPanelCommits || 0,
      transcriptCommits: window.__EGGW_PERFORMANCE__?.transcriptCommits || 0,
      timingCommits: window.__EGGW_PERFORMANCE__?.liveTimingCommits || 0,
      timerWidth: document.querySelector<HTMLElement>('[data-testid="streaming-tool-timeout-summary"]')!.getBoundingClientRect().width,
      transcriptNode: document.querySelector('[data-testid="static-transcript-owner"]'),
    }));
    expect(after.timerWidth).toBe(before.timerWidth);
    expect(after.transcriptNode).toBe(before.transcriptNode);
    expect(after.top).toBeCloseTo(before.top, 0);
    expect(after.height).toBe(before.height);
    expect(after.timingCommits).toBeGreaterThan(before.timingCommits);
    // One semantic metadata publication may re-evaluate the memoized static
    // transcript; timing ticks themselves must not replace its DOM owner.
    expect(after.transcriptCommits - before.transcriptCommits).toBeLessThanOrEqual(1);
    // Timing commits are isolated to the fixed-geometry leaf; static transcript
    // identity, geometry, and scroll ownership remain unchanged.
    await expect(timer).toHaveCSS('font-variant-numeric', 'tabular-nums');
  });

  test('terminalizes only the exact timed-out wait card and refreshes its durable transcript', async ({ page }) => {
    await page.addInitScript(() => {
      window.sessionStorage.setItem('eggw.apiToken', 'test-eggw-browser-token-' + 'a'.repeat(48));
    });
    const threadId = 'timed-out-wait-terminal-thread';
    const waitId = 'call-wait-timeout-60';
    const siblingId = 'call-sibling-live-bash';
    let transcriptRequests = 0;

    await mockThreadShell(page, threadId, {
      messages: [{ id: 'before-timeout-wait', role: 'user', content: 'wait for child' }],
    });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/state`);
    await page.unroute(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: { state: 'running', streaming_kind: 'tool', streaming_invoke_id: 'invoke-wait-timeout', live_replay_cursor: 0 },
    }));
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route) => {
      transcriptRequests += 1;
      const terminal = transcriptRequests > 1;
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: {
          items: terminal ? [
            { id: 'before-timeout-wait', role: 'user', content: 'wait for child' },
            { id: 'durable-wait-timeout', role: 'tool', name: 'wait', tool_call_id: waitId, content: '--- TIMEOUT ---\nWait timed out after 60 seconds.' },
          ] : [{ id: 'before-timeout-wait', role: 'user', content: 'wait for child' }],
          snapshot_cursor: terminal ? 5 : 0,
          next_before: null,
        },
      });
    });
    let eventConnection = 0;
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route) => {
      eventConnection += 1;
      const oldStart = new Date(Date.now() - 355_000).toISOString();
      const now = new Date().toISOString();
      const envelope = (eventSeq: number, type: string, payload: Record<string, unknown>, ts: string) => JSON.stringify({
        event_id: `wait-timeout-${eventSeq}`, event_seq: eventSeq, type, ts, msg_id: null,
        invoke_id: 'invoke-wait-timeout', chunk_seq: null, payload,
      });
      const block = (eventSeq: number, type: string, payload: Record<string, unknown>, ts: string) => [
        `id: ${eventSeq}`, `event: ${type}`, `data: ${envelope(eventSeq, type, payload, ts)}`, '',
      ];
      const frames = eventConnection === 1 ? [
        ...block(1, 'stream.open', { stream_kind: 'tool' }, oldStart),
        ...block(2, 'tool_call.execution_started', { tool_call_id: waitId, name: 'wait', arguments: '{"thread_ids":["child"],"timeout":60}', timeout: 60 }, oldStart),
        ...block(3, 'tool_call.execution_started', { tool_call_id: siblingId, name: 'bash', arguments: '{"script":"sleep 300"}', timeout: 300 }, now),
        '',
      ] : [
        ...block(4, 'tool_call.finished', { tool_call_id: waitId, reason: 'timeout', output: '--- TIMEOUT ---\nWait timed out after 60 seconds.' }, now),
        '',
      ];
      await route.fulfill({
        status: 200,
        headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body: frames.join('\n'),
      });
    });

    await page.goto(`/${threadId}`);
    const outputCards = page.getByTestId('streaming-tool-output').locator('..').locator('..');
    const waitCard = outputCards.filter({ hasText: waitId.slice(-8) });
    const siblingCard = outputCards.filter({ hasText: siblingId.slice(-8) });
    await expect(waitCard).toContainText('streaming output...');
    await expect(waitCard).toContainText('running 355s');
    await expect(waitCard).toContainText('timeout in 0s (limit 60s)');
    await expect(siblingCard).toContainText('streaming output...');

    await expect(waitCard).toContainText('finished');
    await expect(waitCard).not.toContainText('streaming output...');
    await expect(waitCard).not.toContainText('running ');
    await expect(waitCard).not.toContainText('timeout in ');
    await expect(siblingCard).toContainText('streaming output...');
    await expect.poll(() => transcriptRequests).toBeGreaterThan(1);
    const compactTimeout = page.getByTestId('chat-panel').getByTestId('hidden-details');
    await expect(compactTimeout).toContainText('got 1 tool result');
    await expect(compactTimeout).toContainText('Tools: wait');
    await compactTimeout.getByRole('button', { name: 'wait' }).click();
    await expect(page.getByRole('dialog')).toContainText('Wait timed out after 60 seconds.');
  });

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
    const liveToolOutput = page.getByTestId('streaming-tool-output');
    await expect(page.getByTestId('chat-panel')).toContainText('streaming output...', { timeout: 5000 });

    const select = page.locator('select[title="Transcript display verbosity"]');
    const args = page.getByTestId('streaming-tool-arguments');
    await select.selectOption('medium');
    await expect(args).toBeVisible();
    await expect(args).toContainText('$ echo visible args; sleep 30');
    await expect(liveToolOutput).toBeVisible();
    await expect(page.getByTestId('chat-panel')).toContainText('$ echo visible args; sleep 30');
    await select.selectOption('min');
    await expect(args).toBeVisible();
    await expect(args).toContainText('$ echo visible args; sleep 30');
    await expect(liveToolOutput).toBeVisible();
    await expect(page.getByTestId('chat-panel')).toContainText('bash');
    await select.selectOption('max');
    await expect(args).toBeVisible();
    await expect(args).toContainText('$ echo visible args; sleep 30');
    await expect(liveToolOutput).toBeVisible();
  });
});

test.describe('Message header parity', () => {
  test('keeps canonical message headers inspectable at every verbosity', async ({ page }) => {
    const threadId = 'message-header-parity';
    const timestamp = '2026-07-17T04:05:06.000Z';
    const messages = [
      {
        id: 'header-user-message-00000001',
        role: 'user',
        content: 'HEADER USER BODY',
        model_key: 'provider:user-model',
        tokens: 11,
        timestamp,
      },
      {
        id: 'header-assistant-message-00000002',
        role: 'assistant',
        content: 'HEADER ASSISTANT BODY',
        model_key: 'provider:assistant-model',
        tokens: 22,
        tps: 4.2,
        timestamp,
      },
      {
        id: 'header-assistant-note-00000003',
        role: 'assistant',
        content: 'HEADER ASSISTANT NOTE BODY',
        answer_user_preserve_turn: true,
        model_key: 'provider:note-model',
        tokens: 33,
        tps: 5.3,
        timestamp,
      },
      {
        id: 'header-tool-result-00000004',
        role: 'tool',
        content: 'HEADER TOOL RESULT BODY',
        name: 'bash',
        tool_call_id: 'header-tool-call-00000004',
        model_key: 'provider:tool-model',
        tokens: 44,
        tps: 6.4,
        timestamp,
      },
      {
        id: 'header-recovery-notice-00000005',
        role: 'system',
        content: 'HEADER RECOVERY BODY',
        recovery_notice: true,
        model_key: 'provider:recovery-model',
        tokens: 55,
        timestamp,
      },
    ];
    await mockThreadShell(page, threadId, { messages });
    await page.goto(`/${threadId}`);
    const verbosity = page.locator('select[title="Transcript display verbosity"]');
    const timestampText = await page.evaluate((value) => new Date(value).toLocaleString(undefined, {
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
    }), timestamp);

    for (const level of ['max', 'medium', 'min'] as const) {
      await verbosity.selectOption(level);
      for (const message of messages.filter((message) => level !== 'min' || message.role !== 'tool')) {
        const card = page.locator(`[data-message-id="${message.id}"]`);
        await expect(card).toHaveCount(1);
        await expect(card.getByTestId('message-model')).toHaveAttribute('title', `Model: ${message.model_key}`);
        await expect(card.getByTestId('message-tokens')).toHaveText(`${message.tokens} tok`);
        await expect(card.getByTestId('message-timestamp')).toHaveText(timestampText);
        const messageId = card.getByTestId('message-id');
        await expect(messageId).toHaveAttribute('title', `Click to copy msg_id: ${message.id}`);
        await expect(messageId).toHaveText(level === 'max' ? `msg_id: ${message.id}` : message.id.slice(-8));
        if (typeof message.tps === 'number') await expect(card.getByTestId('message-tps')).toHaveText(`${message.tps.toFixed(1)} tps`);
      }
      if (level !== 'min') {
        const toolCard = page.locator('[data-message-id="header-tool-result-00000004"]');
        const toolCallId = toolCard.getByTestId('tool-call-id');
        await expect(toolCallId).toHaveAttribute('title', 'Click to copy tool_call_id: header-tool-call-00000004');
        await expect(toolCallId).toHaveText(level === 'max' ? 'tool_call_id: header-tool-call-00000004' : '00000004');
      }
      await expect(page.locator('[data-message-id="header-assistant-note-00000003"]')).toContainText('Assistant Note');
      await expect(page.locator('[data-message-id="header-recovery-notice-00000005"]')).toContainText('Continue Status');
    }

    await verbosity.selectOption('min');
    const compactToolSummary = page.getByTestId('hidden-details');
    await expect(compactToolSummary).toContainText('got 1 tool result');
    await expect(compactToolSummary).toContainText('Tools: bash');
    await expect(compactToolSummary).not.toContainText('header-tool-call-00000004');
  });
});

test.describe('Operational recovery presentation', () => {
  test('keeps interleaved error/recovery/system chronology and compacts only generic notices at min', async ({ page }) => {
    const threadId = 'operational-recovery-interleaved';
    const records = operationalRecoveryFixture.records;
    const messages = records.map((record) => ({
      id: record.id,
      role: record.role,
      content: record.content,
      event_seq: record.event_seq,
      ...("recovery_notice" in record && record.recovery_notice ? { recovery_notice: true } : {}),
    }));
    await mockThreadShell(page, threadId, { messages });
    await page.goto(`/${threadId}`);

    const verbosity = page.locator('select[title="Transcript display verbosity"]');
    const transcript = page.getByTestId('static-transcript-owner');
    const expectedOrder = records.map((record) => record.id);
    for (const level of ['max', 'medium', 'min'] as const) {
      await verbosity.selectOption(level);
      await expect.poll(() => transcript.locator(':scope > .eggw-message-card').evaluateAll((cards) =>
        cards.map((card) => card.getAttribute('data-message-id')),
      )).toEqual(expectedOrder);
      for (const record of records) {
        const card = transcript.locator(`[data-message-id="${record.id}"]`);
        await expect(card.locator('.eggw-role-label')).toHaveText(record.expected_presentation.label);
        if (record.role !== 'system') continue;
        await expect(card.locator('pre')).toHaveText(
          level === 'min'
            ? record.expected_presentation.min_content
            : record.expected_presentation.medium_max_content,
        );
      }
    }
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

  test('keeps a live tool run correctly split around an interleaved note through durable completion', async ({ page }) => {
    const threadId = 'min-live-interleaved-chronology';
    const toolCallId = 'live-chronology-tool';
    const initialMessages = [
      { id: 'live-chronology-user', role: 'user', content: 'Run the live check.', event_seq: 10 },
      {
        id: 'live-chronology-call',
        role: 'assistant',
        content: '',
        tool_calls: [{ id: toolCallId, name: 'bash', arguments: { script: 'echo LIVE_RESULT' } }],
        event_seq: 20,
      },
      {
        id: 'live-chronology-note',
        role: 'assistant',
        content: 'LIVE NOTE AFTER DECLARATION',
        answer_user_preserve_turn: true,
        event_seq: 30,
      },
    ];
    let durable = false;
    let releaseDurable!: () => void;
    const durableReady = new Promise<void>((resolve) => { releaseDurable = resolve; });
    await mockThreadShell(page, threadId, { messages: initialMessages });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/state`);
    await page.unroute(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: { state: 'running', streaming_kind: 'tool', streaming_invoke_id: 'invoke-live-chronology', live_replay_cursor: 30, active_get_user_wait: false },
    }));
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: {
        items: durable ? [
          ...initialMessages,
          { id: 'live-chronology-result', role: 'tool', name: 'bash', tool_call_id: toolCallId, content: 'LIVE_RESULT', event_seq: 50 },
        ] : initialMessages,
        snapshot_cursor: durable ? 50 : 30,
        next_before: null,
      },
    }));
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route) => {
      const envelope = (eventSeq: number, type: string, payload: Record<string, unknown>, msgId: string | null = null) => JSON.stringify({
        event_id: `live-chronology-${eventSeq}`,
        event_seq: eventSeq,
        type,
        ts: '2026-07-17T04:00:00.000Z',
        msg_id: msgId,
        invoke_id: 'invoke-live-chronology',
        chunk_seq: type === 'stream.delta' ? eventSeq : null,
        payload,
      });
      await durableReady;
      await route.fulfill({
        status: 200,
        headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body: [
          'id: 40', 'event: tool_call.execution_started', `data: ${envelope(40, 'tool_call.execution_started', { tool_call_id: toolCallId, name: 'bash', arguments: { script: 'echo LIVE_RESULT' } })}`, '',
          'id: 41', 'event: stream.delta', `data: ${envelope(41, 'stream.delta', { tool: { id: toolCallId, name: 'bash', text: 'LIVE_RESULT' } })}`, '',
          'id: 42', 'event: tool_call.finished', `data: ${envelope(42, 'tool_call.finished', { tool_call_id: toolCallId, reason: 'success', output: 'LIVE_RESULT' })}`, '',
          'id: 50', 'event: msg.create', `data: ${envelope(50, 'msg.create', { role: 'tool', name: 'bash', tool_call_id: toolCallId, content: 'LIVE_RESULT' }, 'live-chronology-result')}`, '',
          'id: 51', 'event: stream.close', `data: ${envelope(51, 'stream.close', {})}`, '',
          '',
        ].join('\n'),
      });
      durable = true;
    });

    await page.goto(`/${threadId}`);
    await page.locator('select[title="Transcript display verbosity"]').selectOption('min');
    const transcript = page.getByTestId('static-transcript-owner');
    const summaries = transcript.getByTestId('hidden-details');
    const note = transcript.locator('[data-message-id="live-chronology-note"]');
    await expect(summaries).toHaveCount(1);
    await expect(summaries.first()).toContainText('Executed 1 tool');
    await expect(summaries.first()).toContainText('Tools: bash');
    await expect(summaries.first()).toContainText('Tool Calls');
    await expect(summaries.first()).toHaveAttribute('data-source-message-id', 'live-chronology-call');
    await expect(note).toBeVisible();
    expect(await summaries.first().evaluate((element) => element.compareDocumentPosition(document.querySelector('[data-message-id="live-chronology-note"]')!) & Node.DOCUMENT_POSITION_FOLLOWING)).toBeTruthy();

    releaseDurable();
    await expect(summaries).toHaveCount(2);
    await expect(summaries.nth(1)).toContainText('got 1 tool result');
    await expect(summaries.nth(1)).toContainText('Tools: bash');
    await expect(summaries.nth(1)).toContainText('Tool Result: bash');
    await expect(summaries.nth(1)).toHaveAttribute('data-source-message-id', 'live-chronology-result');
    await expect.poll(() => transcript.locator(':scope > .eggw-message-card').evaluateAll((cards) => cards.map((card) => ({
      role: card.getAttribute('data-message-role') || 'hidden-details',
      messageId: card.getAttribute('data-message-id') || card.getAttribute('data-source-message-id'),
      text: (card.textContent || '').replace(/\s+/g, ' ').trim(),
    })))).toEqual([
      expect.objectContaining({ messageId: 'live-chronology-user' }),
      expect.objectContaining({ messageId: 'live-chronology-call', role: 'hidden-details', text: expect.stringContaining('Tool Calls') }),
      expect.objectContaining({ messageId: 'live-chronology-note' }),
      expect.objectContaining({ messageId: 'live-chronology-result', role: 'hidden-details', text: expect.stringContaining('Tool Result: bash') }),
    ]);
    await expect(page.locator('[role="status"][aria-label="Tool streaming"]')).toHaveCount(0);
  });

  test('uses monotonic detail levels for completed transcript content', async ({ page }) => {
    const threadId = 'verbosity-semantics';
    await mockThreadShell(page, threadId, {
      messages: [
        { id: 'ordinary-system', role: 'system', content: 'private provider setup instructions' },
        { id: 'verbosity-user', role: 'user', content: 'inspect the repository' },
        {
          id: 'verbosity-assistant',
          role: 'assistant',
          content: 'The repository looks healthy.',
          reasoning: 'raw private reasoning body',
          model_key: 'provider-model',
          tool_calls: [{ id: 'verbosity-tool', name: 'bash', arguments: { script: 'git status' } }],
        },
        { id: 'verbosity-result', role: 'tool', name: 'bash', tool_call_id: 'verbosity-tool', content: 'clean working tree result' },
        { id: 'verbosity-command', role: 'system', command_name: 'displayVerbosity', content: 'Display verbosity changed.' },
      ],
    });
    await page.goto(`/${threadId}`);
    const chat = page.getByTestId('chat-panel');
    const select = page.locator('select[title="Transcript display verbosity"]');

    await select.selectOption('max');
    await expect(chat).toContainText('raw private reasoning body');
    await expect(chat).toContainText('git status');
    await expect(chat).toContainText('clean working tree result');
    await expect(chat).toContainText('provider-model');

    await select.selectOption('medium');
    await expect(chat).toContainText('The repository looks healthy.');
    await expect(chat.getByText('raw private reasoning body')).not.toBeVisible();
    await expect(chat.locator('pre').getByText('clean working tree result', { exact: true })).not.toBeVisible();
    await expect(chat).toContainText('git status');
    await chat.getByText('Reasoning', { exact: false }).first().click();
    await expect(chat.getByText('raw private reasoning body')).toBeVisible();

    await select.selectOption('min');
    await expect(chat).toContainText('inspect the repository');
    await expect(chat).toContainText('The repository looks healthy.');
    await expect(chat).toContainText('Display verbosity changed.');
    await expect(chat).toContainText('Executed 1 tool');
    await expect(chat).toContainText('got 1 tool result');
    await expect(chat).toContainText('Tools: bash');
    await expect(chat).toContainText('private provider setup instructions');
    await expect(chat.getByTestId('message-model').first()).toContainText('provider-model');
    await expect(chat).not.toContainText('raw private reasoning body');
  });

  test('keeps simultaneous durable tools named and paired by ID after reload at every verbosity', async ({ page }) => {
    const threadId = 'durable-simultaneous-tool-pairing';
    await mockThreadShell(page, threadId, {
      messages: [
        { id: 'durable-tools-user', role: 'user', content: 'run both tools' },
        {
          id: 'durable-tools-calls',
          role: 'assistant',
          content: '',
          tool_calls: [
            { id: 'call-bash', name: 'bash', arguments: { script: 'echo ARG_BASH' } },
            { id: 'call-python', function: { name: 'python', arguments: '{"script":"print(\"ARG_PYTHON\")"}' } },
          ],
        },
        // Runner transcripts before this fix omitted result names. The loaded
        // transcript must recover each name from exact call identity only.
        { id: 'durable-result-python', role: 'tool', tool_call_id: 'call-python', content: 'RESULT_PYTHON' },
        { id: 'durable-result-bash', role: 'tool', tool_call_id: 'call-bash', content: 'RESULT_BASH' },
        { id: 'durable-result-orphan', role: 'tool', tool_call_id: 'call-orphan-1234567890', content: 'RESULT_ORPHAN' },
      ],
    });
    await page.goto(`/${threadId}`);
    await page.reload();
    const chat = page.getByTestId('chat-panel');
    const select = page.locator('select[title="Transcript display verbosity"]');

    for (const verbosity of ['max', 'medium'] as const) {
      await select.selectOption(verbosity);
      await expect(chat).toContainText('Tool Result: bash');
      await expect(chat).toContainText('Tool Result: python');
      await expect(chat).toContainText('Tool result · n-1234567890');
      await expect(chat).not.toContainText('Tool Result: tool');
    }

    await select.selectOption('min');
    const hidden = page.getByTestId('hidden-details');
    await expect(hidden).toHaveCount(1);
    await expect(hidden).toHaveAttribute('data-source-message-count', '4');
    await expect(hidden).not.toHaveAttribute('data-source-message-id');
    await expect(hidden).toHaveAttribute(
      'data-source-message-ids',
      'durable-tools-calls durable-result-python durable-result-bash durable-result-orphan',
    );
    await expect(hidden).toContainText('Executed 2 tools, got 3 tool results');
    await expect(hidden).toContainText('Tools: bash, python');
    await expect(hidden).toContainText('Inspect unmatched details (1)');
    const bashEntry = hidden.getByRole('button', { name: 'bash', exact: true });
    const pythonEntry = hidden.getByRole('button', { name: 'python', exact: true });

    await bashEntry.click();
    let dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('ARG_BASH');
    await expect(dialog).toContainText('RESULT_BASH');
    await expect(dialog).toContainText('msg_id: durable-result-bash');
    await expect(dialog).not.toContainText('RESULT_PYTHON');
    await dialog.getByRole('button', { name: 'Close hidden detail' }).click();

    await pythonEntry.click();
    dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('ARG_PYTHON');
    await expect(dialog).toContainText('RESULT_PYTHON');
    await expect(dialog).toContainText('msg_id: durable-result-python');
    await expect(dialog).not.toContainText('RESULT_BASH');
    await dialog.getByRole('button', { name: 'Close hidden detail' }).click();

    await hidden.getByText('Inspect unmatched details (1)').click();
    await hidden.getByRole('button', { name: 'Tool result · n-1234567890' }).click();
    dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('RESULT_ORPHAN');
  });

  test('keeps repeated result-only compact entries mapped to their own popup', async ({ page }) => {
    const threadId = 'min-repeated-result-popups';
    await mockThreadShell(page, threadId, {
      messages: [
        { id: 'repeated-results-user', role: 'user', content: 'Show both legacy results.' },
        { id: 'repeated-result-a', role: 'tool', name: 'bash', tool_call_id: 'reused-result-id', content: 'RESULT_ALPHA' },
        { id: 'repeated-result-b', role: 'tool', name: 'bash', tool_call_id: 'reused-result-id', content: 'RESULT_BETA' },
      ],
    });
    await page.goto(`/${threadId}`);
    await page.locator('select[title="Transcript display verbosity"]').selectOption('min');

    const resultCards = page.getByTestId('hidden-details');
    await expect(resultCards).toHaveCount(1);
    await expect(resultCards).toHaveAttribute('data-source-message-count', '2');
    await expect(resultCards).not.toHaveAttribute('data-source-message-id');
    await expect(resultCards).toContainText('got 2 tool results');
    const tools = resultCards.getByRole('button', { name: 'bash' });
    await expect(tools).toHaveCount(2);
    await tools.nth(0).click();
    let dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('RESULT_ALPHA');
    await expect(dialog).not.toContainText('RESULT_BETA');
    await dialog.getByRole('button', { name: 'Close hidden detail' }).click();
    await tools.nth(1).click();
    dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('RESULT_BETA');
    await expect(dialog).not.toContainText('RESULT_ALPHA');
  });

  test('keeps unmatched reused-ID results inspectable without polluting compact tool names', async ({ page }) => {
    const threadId = 'min-reused-id-unmatched-results';
    const reusedId = 'reused-call-id';
    await mockThreadShell(page, threadId, {
      messages: [
        { id: 'reused-user', role: 'user', content: 'Run ambiguous checks.' },
        {
          id: 'reused-calls', role: 'assistant', content: '', tool_calls: [
            { id: reusedId, name: 'bash', arguments: { script: 'echo A' } },
            { id: reusedId, name: 'python', arguments: { code: 'print("B")' } },
          ],
        },
        { id: 'reused-result-a', role: 'tool', name: 'bash', tool_call_id: reusedId, content: 'AMBIGUOUS_ALPHA' },
        { id: 'reused-result-b', role: 'tool', name: 'python', tool_call_id: reusedId, content: 'AMBIGUOUS_BETA' },
      ],
    });
    await page.goto(`/${threadId}`);
    await page.locator('select[title="Transcript display verbosity"]').selectOption('min');

    const summaries = page.getByTestId('hidden-details');
    await expect(summaries).toHaveCount(1);
    await expect(summaries).toHaveAttribute('data-source-message-count', '3');
    await expect(summaries).toContainText('Executed 2 tools, got 2 tool results');
    await expect(summaries).toContainText('Tools: bash, python');
    await expect(summaries).toContainText('Inspect unmatched details (2)');
    await summaries.getByText('Inspect unmatched details (2)').click();
    await summaries.getByTitle(/^Show Tool Result: bash/).click();
    let dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('AMBIGUOUS_ALPHA');
    await dialog.getByRole('button', { name: 'Close hidden detail' }).click();
    await summaries.getByTitle(/^Show Tool Result: python/).click();
    dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('AMBIGUOUS_BETA');
  });

  test('pairs Assistant Note tool popups only by matching tool call identity', async ({ page }) => {
    const threadId = 'min-tool-popup-identity';
    await mockThreadShell(page, threadId, {
      messages: [
        { id: 'identity-user', role: 'user', content: 'run the checks' },
        {
          id: 'identity-note-a',
          role: 'assistant',
          answer_user_preserve_turn: true,
          content: 'First check is still running.',
          tool_calls: [{ id: 'call-a', name: 'bash', arguments: { script: 'echo ARGUMENT_A' } }],
        },
        {
          id: 'identity-call-b',
          role: 'assistant',
          content: '',
          tool_calls: [{ id: 'call-b', name: 'bash', arguments: { script: 'echo ARGUMENT_B' } }],
        },
        {
          id: 'identity-result-b',
          role: 'tool',
          name: 'bash',
          tool_call_id: 'call-b',
          content: 'RESULT_B',
        },
        { id: 'identity-answer', role: 'assistant', content: 'Checks complete.' },
      ],
    });
    await page.goto(`/${threadId}`);
    await page.locator('select[title="Transcript display verbosity"]').selectOption('min');

    const summaries = page.getByTestId('hidden-details');
    await expect(summaries).toHaveCount(1);
    await expect(summaries).toHaveAttribute('data-source-message-count', '3');
    await expect(summaries).toContainText('Executed 2 tools, got 1 tool result');
    await expect(summaries).toContainText('Tools: bash, bash');
    const tools = summaries.getByRole('button', { name: 'bash' });
    await expect(tools).toHaveCount(2);

    await tools.nth(0).click();
    let dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('ARGUMENT_A');
    await expect(dialog).not.toContainText('RESULT_B');
    await dialog.getByRole('button', { name: 'Close hidden detail' }).click();

    await tools.nth(1).click();
    dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('ARGUMENT_B');
    await expect(dialog).toContainText('RESULT_B');
    await expect(dialog).not.toContainText('ARGUMENT_A');
  });

  test('preserves literal interleaved durable chronology in min', async ({ page }) => {
    const threadId = 'min-interleaved-chronology';
    const sameTimestamp = '2026-07-17T04:00:00.000Z';
    const canonicalMessages = [
      { id: 'chronology-user', role: 'user', content: 'Run both checks.', event_seq: 10, timestamp: sameTimestamp },
      {
        id: 'chronology-call-a',
        role: 'assistant',
        content: '',
        tool_calls: [{ id: 'chronology-tool-a', name: 'bash', arguments: { script: 'echo ARG_A' } }],
        event_seq: 20,
        timestamp: sameTimestamp,
      },
      {
        id: 'chronology-note-a',
        role: 'assistant',
        content: 'FIRST INTERLEAVED NOTE',
        answer_user_preserve_turn: true,
        event_seq: 30,
      },
      {
        id: 'chronology-result-a',
        role: 'tool',
        name: 'bash',
        tool_call_id: 'chronology-tool-a',
        content: 'RESULT_A',
        event_seq: 40,
        timestamp: '2020-01-01T00:00:00.000Z',
      },
      {
        id: 'chronology-call-b',
        role: 'assistant',
        content: '',
        tool_calls: [{ id: 'chronology-tool-b', name: 'python', arguments: { script: 'print("ARG_B")' } }],
        event_seq: 50,
        timestamp: sameTimestamp,
      },
      {
        id: 'chronology-note-b',
        role: 'assistant',
        content: 'SECOND INTERLEAVED NOTE',
        answer_user_preserve_turn: true,
        event_seq: 60,
        timestamp: '2030-01-01T00:00:00.000Z',
      },
      {
        id: 'chronology-result-b',
        role: 'tool',
        name: 'python',
        tool_call_id: 'chronology-tool-b',
        content: 'RESULT_B',
        event_seq: 70,
      },
      {
        id: 'chronology-recovery',
        role: 'system',
        content: 'RECOVERY NOTICE AFTER TOOLS',
        recovery_notice: true,
        event_seq: 80,
        timestamp: '2010-01-01T00:00:00.000Z',
      },
      { id: 'chronology-final', role: 'assistant', content: 'FINAL AFTER RECOVERY', event_seq: 90, timestamp: sameTimestamp },
    ];
    await mockThreadShell(page, threadId, { messages: canonicalMessages });

    await page.goto(`/${threadId}`);
    const verbosity = page.locator('select[title="Transcript display verbosity"]');
    const expectedMessageOrder = [
      'chronology-user',
      'chronology-call-a',
      'chronology-note-a',
      'chronology-result-a',
      'chronology-call-b',
      'chronology-note-b',
      'chronology-result-b',
      'chronology-recovery',
      'chronology-final',
    ];
    for (const level of ['max', 'medium'] as const) {
      await verbosity.selectOption(level);
      await expect.poll(() => page.getByTestId('static-transcript-owner').locator(':scope > .eggw-message-card').evaluateAll((cards) =>
        cards.map((card) => card.getAttribute('data-message-id')),
      )).toEqual(expectedMessageOrder);
    }

    await verbosity.selectOption('min');
    const transcript = page.getByTestId('static-transcript-owner');
    const semanticOrder = await transcript.locator(':scope > .eggw-message-card').evaluateAll((cards) => cards.map((card) => ({
      role: card.getAttribute('data-message-role') || 'hidden-details',
      messageId: card.getAttribute('data-message-id') || card.getAttribute('data-source-message-id'),
      text: (card.textContent || '').replace(/\s+/g, ' ').trim(),
    })));
    expect(canonicalMessages.map((message) => message.event_seq)).toEqual([10, 20, 30, 40, 50, 60, 70, 80, 90]);
    expect(semanticOrder).toEqual([
      expect.objectContaining({ messageId: 'chronology-user' }),
      expect.objectContaining({ messageId: 'chronology-call-a', role: 'hidden-details', text: expect.stringContaining('Tool Calls') }),
      expect.objectContaining({ messageId: 'chronology-note-a' }),
      expect.objectContaining({ messageId: null, role: 'hidden-details', text: expect.stringContaining('Executed 1 tool, got 1 tool result') }),
      expect.objectContaining({ messageId: 'chronology-note-b' }),
      expect.objectContaining({ messageId: 'chronology-result-b', role: 'hidden-details', text: expect.stringContaining('Tool Result: python') }),
      expect.objectContaining({ messageId: 'chronology-recovery' }),
      expect.objectContaining({ messageId: 'chronology-final' }),
    ]);
    expect(semanticOrder[1].text).toContain('Tools: bash');
    expect(semanticOrder[3].text).toContain('Tools: python');
    expect(semanticOrder[5].text).toContain('Tools: python');

    await page.reload();
    await page.locator('select[title="Transcript display verbosity"]').selectOption('min');
    await expect.poll(() => page.getByTestId('static-transcript-owner').locator(':scope > .eggw-message-card').evaluateAll((cards) =>
      cards.map((card) => card.getAttribute('data-message-id') || card.getAttribute('data-source-message-id')),
    )).toEqual([
      'chronology-user',
      'chronology-call-a',
      'chronology-note-a',
      null,
      'chronology-note-b',
      'chronology-result-b',
      'chronology-recovery',
      'chronology-final',
    ]);
  });

  test('keeps compact tool runs on their own sides of system and compaction boundaries', async ({ page }) => {
    const threadId = 'min-tool-run-boundaries';
    await mockThreadShell(page, threadId, {
      messages: [
        { id: 'boundary-user', role: 'user', content: 'Run the boundary checks.' },
        {
          id: 'boundary-call-a', role: 'assistant', content: '',
          tool_calls: [{ id: 'boundary-tool-a', name: 'bash', arguments: { script: 'echo A' } }],
        },
        { id: 'boundary-system', role: 'system', content: 'SYSTEM BOUNDARY' },
        { id: 'boundary-result-a', role: 'tool', name: 'bash', tool_call_id: 'boundary-tool-a', content: 'A' },
        {
          id: 'boundary-compaction', role: 'compaction_marker', kind: 'compaction_marker',
          content: 'COMPACTION BOUNDARY', marker_event_seq: 40, start_event_seq: 10,
        },
        {
          id: 'boundary-call-b', role: 'assistant', content: '',
          tool_calls: [{ id: 'boundary-tool-b', name: 'python', arguments: { code: 'print("B")' } }],
        },
        { id: 'boundary-final', role: 'assistant', content: 'Boundary checks complete.' },
      ],
    });
    await page.goto(`/${threadId}`);
    await page.locator('select[title="Transcript display verbosity"]').selectOption('min');

    const records = await page.getByTestId('static-transcript-owner').locator(':scope > *').evaluateAll((nodes) => nodes.map((node) => ({
      kind: node.getAttribute('data-message-id') || node.getAttribute('data-source-message-id') || node.getAttribute('data-testid') || '',
      text: (node.textContent || '').replace(/\s+/g, ' ').trim(),
    })).filter((record) => record.text));
    expect(records).toEqual([
      expect.objectContaining({ kind: 'boundary-user' }),
      expect.objectContaining({ kind: 'boundary-call-a', text: expect.stringContaining('Tool Calls') }),
      expect.objectContaining({ kind: 'boundary-system' }),
      expect.objectContaining({ kind: 'boundary-result-a', text: expect.stringContaining('Tool Result: bash') }),
      expect.objectContaining({ text: expect.stringContaining('Compaction boundary') }),
      expect.objectContaining({ kind: 'boundary-call-b', text: expect.stringContaining('Tool Calls') }),
      expect.objectContaining({ kind: 'boundary-final' }),
    ]);
    expect(records[1].text).toContain('Tools: bash');
    expect(records[3].text).toContain('Tools: bash');
    expect(records[5].text).toContain('Tools: python');
  });

  test('splits an Assistant Note tool call and result around the visible note in min', async ({ page }) => {
    const threadId = 'min-assistant-note-result';
    const toolCallId = 'call-answer-user-note';
    await mockThreadShell(page, threadId, {
      messages: [
        { id: 'note-result-user', role: 'user', content: 'keep me updated' },
        {
          id: 'note-result-call',
          role: 'assistant',
          content: '',
          tool_calls: [{
            id: toolCallId,
            name: 'answer_user_while_preserving_llm_turn',
            arguments: { message: 'Visible interim status.' },
          }],
        },
        {
          id: 'note-result-visible-note',
          role: 'assistant',
          content: 'Visible interim status.',
          answer_user_preserve_turn: true,
          tool_call_id: toolCallId,
          source_tool_name: 'answer_user_while_preserving_llm_turn',
        },
        {
          id: 'note-result-tool-message',
          role: 'tool',
          name: 'answer_user_while_preserving_llm_turn',
          tool_call_id: toolCallId,
          content: 'Interim answer shown to user.',
        },
        { id: 'note-result-final', role: 'assistant', content: 'Final answer.' },
      ],
    });
    await page.goto(`/${threadId}`);
    await page.locator('select[title="Transcript display verbosity"]').selectOption('min');

    await expect(page.getByText('Visible interim status.', { exact: true })).toBeVisible();
    const summaries = page.getByTestId('hidden-details');
    await expect(summaries).toHaveCount(2);
    await expect(summaries.nth(0)).toContainText('Executed 1 tool');
    await expect(summaries.nth(1)).toContainText('got 1 tool result');
    await expect(summaries.nth(0)).toContainText('Tools: answer_user_while_preserving_llm_turn');
    await expect(summaries.nth(1)).toContainText('Tools: answer_user_while_preserving_llm_turn');
    await summaries.nth(0).getByRole('button', { name: 'answer_user_while_preserving_llm_turn' }).click();
    let dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('Visible interim status.');
    await expect(dialog).toContainText('(not present in this compact run)');
    await expect(dialog).not.toContainText('Interim answer shown to user.');
    await dialog.getByRole('button', { name: 'Close hidden detail' }).click();
    await summaries.nth(1).getByRole('button', { name: 'answer_user_while_preserving_llm_turn' }).click();
    dialog = page.getByRole('dialog');
    await expect(dialog).toContainText(`tool_call_id: ${toolCallId}`);
    await expect(dialog).toContainText('Interim answer shown to user.');
    await expect(dialog).not.toContainText('Visible interim status.');
  });

  test('keeps grouped reasoning source identities in canonical order', async ({ page }) => {
    const threadId = 'min-grouped-reasoning-identities';
    await mockThreadShell(page, threadId, {
      messages: [
        { id: 'reasoning-boundary-user', role: 'user', content: 'Think twice.' },
        { id: 'reasoning-source-a', role: 'assistant', content: '', reasoning: 'PRIVATE REASONING A', event_seq: 20 },
        { id: 'reasoning-source-b', role: 'assistant', content: '', reasoning: 'PRIVATE REASONING B', event_seq: 30 },
        { id: 'reasoning-boundary-answer', role: 'assistant', content: 'Visible answer.' },
      ],
    });
    await page.goto(`/${threadId}`);
    await page.locator('select[title="Transcript display verbosity"]').selectOption('min');

    const grouped = page.getByTestId('hidden-details');
    await expect(grouped).toHaveCount(1);
    await expect(grouped).toContainText('2 reasoning blocks');
    await expect(grouped).not.toContainText('PRIVATE REASONING A');
    await expect(grouped).not.toContainText('PRIVATE REASONING B');
    await expect(grouped).toHaveAttribute('data-source-message-count', '2');
    await expect(grouped).toHaveAttribute('data-source-message-ids', 'reasoning-source-a reasoning-source-b');
    await expect(grouped).toHaveAttribute('data-source-event-seqs', '20 30');
    expect(await grouped.evaluate((element) => element.compareDocumentPosition(
      document.querySelector('[data-message-id="reasoning-boundary-answer"]')!,
    ) & Node.DOCUMENT_POSITION_FOLLOWING)).toBeTruthy();
  });
});

test.describe('Get-user lifecycle', () => {
  const getUserTool = 'get_user_message_while_preserving_llm_turn';

  test('renders pending, answered, manager, interrupted, reload, and multi-tool states by durable identity', async ({ page }) => {
    await page.addInitScript(() => {
      window.sessionStorage.setItem('eggw.apiToken', 'test-eggw-browser-token-' + 'a'.repeat(48));
    });
    const threadId = 'get-user-lifecycle-thread';
    const messages = [
      { id: 'get-user-request', role: 'user', content: 'ask for input' },
      {
        id: 'get-user-calls', role: 'assistant', content: '',
        tool_calls: [
          { id: 'call-get-user-one', name: getUserTool, arguments: { assistant_note: 'Which option?' } },
          { id: 'call-bash-sibling', name: 'bash', arguments: { script: 'echo sibling' } },
          { id: 'call-get-user-two', name: getUserTool, arguments: { assistant_note: 'Manager decision?' } },
          { id: 'call-get-user-interrupted', name: getUserTool, arguments: { assistant_note: 'Interrupt me?' } },
          { id: 'call-get-user-pending', name: getUserTool, arguments: { assistant_note: 'Still pending?' } },
        ],
      },
      { id: 'get-user-note-one', role: 'assistant', content: 'Which option?', answer_user_preserve_turn: true, tool_call_id: 'call-get-user-one' },
      {
        id: 'get-user-answer-one', role: 'user', content: 'Option A',
        consumed_by_tool_name: getUserTool, consumed_by_tool_call_id: 'call-get-user-one',
      },
      { id: 'get-user-result-one', role: 'tool', name: getUserTool, tool_call_id: 'call-get-user-one', content: 'Option A' },
      { id: 'sibling-result', role: 'tool', name: 'bash', tool_call_id: 'call-bash-sibling', content: 'SIBLING_RESULT' },
      { id: 'get-user-note-two', role: 'assistant', content: 'Manager decision?', answer_user_preserve_turn: true, tool_call_id: 'call-get-user-two' },
      {
        id: 'get-user-answer-two', role: 'user', content: 'Continue Phase 6', origin: 'manager_message', from_thread_id: 'manager-thread',
        consumed_by_tool_name: getUserTool, consumed_by_tool_call_id: 'call-get-user-two',
      },
      { id: 'get-user-result-two', role: 'tool', name: getUserTool, tool_call_id: 'call-get-user-two', content: 'Continue Phase 6' },
      { id: 'get-user-note-interrupted', role: 'assistant', content: 'Interrupt me?', answer_user_preserve_turn: true, tool_call_id: 'call-get-user-interrupted' },
      { id: 'get-user-result-interrupted', role: 'tool', name: getUserTool, tool_call_id: 'call-get-user-interrupted', content: 'User interrupted get_user_message_while_preserving_llm_turn.' },
      { id: 'get-user-note-pending', role: 'assistant', content: 'Still pending?', answer_user_preserve_turn: true, tool_call_id: 'call-get-user-pending' },
    ];
    await mockThreadShell(page, threadId, { messages });
    await page.goto(`/${threadId}`);
    await page.reload();
    const chat = page.getByTestId('chat-panel');
    const select = page.locator('select[title="Transcript display verbosity"]');

    for (const verbosity of ['max', 'medium', 'min'] as const) {
      await select.selectOption(verbosity);
      await expect(chat.getByText('Option A', { exact: true }).first()).toBeVisible();
      await expect(chat.getByText('Continue Phase 6', { exact: true }).first()).toBeVisible();
      await expect(chat.getByText('Which option?', { exact: true }).first()).toBeVisible();
      await expect(chat.getByText('Manager decision?', { exact: true }).first()).toBeVisible();
      await expect(chat.getByText('Still pending?', { exact: true }).first()).toBeVisible();
      if (verbosity !== 'min') await expect(chat).toContainText('SIBLING_RESULT');
    }

    await select.selectOption('min');
    const userCards = chat.locator('[data-message-role="user"]');
    await expect(userCards.filter({ hasText: 'Option A' })).toHaveCount(1);
    await expect(userCards.filter({ hasText: 'Continue Phase 6' })).toHaveCount(1);
    const hiddenTools = page.getByTestId('hidden-details').getByRole('button');
    await expect(hiddenTools.filter({ hasText: getUserTool })).toHaveCount(3);
    await expect(hiddenTools.filter({ hasText: 'bash' })).toHaveCount(2);
  });

  test('keeps a 24-hour get-user wait compact and stable across verbosity and reload', async ({ page }) => {
    await page.addInitScript(() => {
      window.sessionStorage.setItem('eggw.apiToken', 'test-eggw-browser-token-' + 'a'.repeat(48));
    });
    const threadId = 'compact-get-user-wait-thread';
    const waitId = 'call-get-user-86400';
    const bashId = 'call-live-bash-sibling';
    await mockThreadShell(page, threadId, {
      messages: [
        { id: 'before-compact-wait', role: 'user', content: 'start' },
        { id: 'compact-wait-note', role: 'assistant', content: 'Choose the next slice', answer_user_preserve_turn: true, tool_call_id: waitId },
      ],
    });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/state`);
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200, headers: mockApiHeaders,
      json: { state: 'waiting_user', streaming_kind: 'tool', streaming_invoke_id: 'invoke-compact-wait', live_replay_cursor: 0, active_get_user_wait: true },
    }));
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route) => {
      const oldTs = new Date(Date.now() - 2_243_000).toISOString();
      const envelope = (eventSeq: number, type: string, payload: Record<string, unknown>) => JSON.stringify({
        event_id: `compact-wait-${eventSeq}`, event_seq: eventSeq, type, ts: oldTs, msg_id: null,
        invoke_id: 'invoke-compact-wait', chunk_seq: null, payload,
      });
      const block = (eventSeq: number, type: string, payload: Record<string, unknown>) => [
        `id: ${eventSeq}`, `event: ${type}`, `data: ${envelope(eventSeq, type, payload)}`, '',
      ];
      await route.fulfill({
        status: 200, headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body: [
          ...block(1, 'stream.open', { stream_kind: 'tool' }),
          ...block(2, 'tool_call.execution_started', {
            tool_call_id: waitId, name: getUserTool,
            arguments: '{"assistant_note":"Choose the next slice","timeout":86400}', timeout: 86400,
          }),
          ...block(3, 'tool_call.execution_started', {
            tool_call_id: bashId, name: 'bash', arguments: '{"script":"sleep 60"}', timeout: 60,
          }),
          '',
        ].join('\n'),
      });
    });

    await page.goto(`/${threadId}`);
    const chat = page.getByTestId('chat-panel');
    const select = page.locator('select[title="Transcript display verbosity"]');
    for (const verbosity of ['max', 'medium', 'min'] as const) {
      await select.selectOption(verbosity);
      const waitCall = page.getByTestId('get-user-wait-call');
      const waitOutput = page.getByTestId('get-user-wait-output');
      await expect(waitCall).toContainText('waiting for reply');
      await expect(waitOutput).toContainText('waiting for reply');
      await expect(waitCall).not.toHaveAttribute('open', '');
      await expect(waitOutput).not.toHaveAttribute('open', '');
      await expect(chat).not.toContainText('86400s');
      await expect(chat).not.toContainText('running 2243s');
      await expect(chat).toContainText('Choose the next slice');
      await expect(chat).toContainText('bash');
      await expect(chat).toContainText('timeout in');
    }

    const waitBefore = await page.getByTestId('get-user-wait-output').innerText();
    await page.waitForTimeout(1200);
    expect(await page.getByTestId('get-user-wait-output').innerText()).toBe(waitBefore);
    await page.reload();
    await expect(page.getByTestId('get-user-wait-output')).toContainText('waiting for reply');
    await expect(page.getByTestId('chat-panel')).not.toContainText('86400s');
  });

  test('keeps a real wait-only tool stream timer-free', async ({ page }) => {
    await page.addInitScript(() => {
      window.sessionStorage.setItem('eggw.apiToken', 'test-eggw-browser-token-' + 'a'.repeat(48));
      const originalSetInterval = window.setInterval.bind(window);
      (window as typeof window & { __eggwIntervals?: number }).__eggwIntervals = 0;
      window.setInterval = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
        if (timeout === 1000) {
          (window as typeof window & { __eggwIntervals?: number }).__eggwIntervals =
            ((window as typeof window & { __eggwIntervals?: number }).__eggwIntervals || 0) + 1;
        }
        return originalSetInterval(handler, timeout, ...args);
      }) as typeof window.setInterval;
    });
    const threadId = 'wait-only-no-timer-thread';
    const waitId = 'call-get-user-only';
    await mockThreadShell(page, threadId, {
      messages: [
        { id: 'before-wait-only', role: 'user', content: 'start' },
        { id: 'wait-only-note', role: 'assistant', content: 'Choose one', answer_user_preserve_turn: true, tool_call_id: waitId },
      ],
    });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/state`);
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200, headers: mockApiHeaders,
      json: { state: 'waiting_user', streaming_kind: 'tool', streaming_invoke_id: 'invoke-wait-only', live_replay_cursor: 0, active_get_user_wait: true },
    }));
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route) => {
      const oldTs = new Date(Date.now() - 2_243_000).toISOString();
      const envelope = (eventSeq: number, type: string, payload: Record<string, unknown>) => JSON.stringify({
        event_id: `wait-only-${eventSeq}`, event_seq: eventSeq, type, ts: oldTs, msg_id: null,
        invoke_id: 'invoke-wait-only', chunk_seq: null, payload,
      });
      const block = (eventSeq: number, type: string, payload: Record<string, unknown>) => [
        `id: ${eventSeq}`, `event: ${type}`, `data: ${envelope(eventSeq, type, payload)}`, '',
      ];
      await route.fulfill({
        status: 200, headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body: [
          ...block(1, 'stream.open', { stream_kind: 'tool' }),
          ...block(2, 'tool_call.execution_started', {
            tool_call_id: waitId, name: getUserTool,
            arguments: '{"assistant_note":"Choose one","timeout":86400}', timeout: 86400,
          }),
          '',
        ].join('\n'),
      });
    });

    await page.goto(`/${threadId}`);
    await expect(page.getByTestId('get-user-wait-output')).toContainText('waiting for reply');
    await expect(page.getByTestId('chat-panel')).not.toContainText(/streaming \d+s/i);
    const intervalsAfterWaitMounted = await page.evaluate(() => (
      (window as typeof window & { __eggwIntervals?: number }).__eggwIntervals || 0
    ));
    await page.waitForTimeout(1200);
    expect(await page.evaluate(() => (
      (window as typeof window & { __eggwIntervals?: number }).__eggwIntervals || 0
    ))).toBe(intervalsAfterWaitMounted);
  });

  test('stops only the answered live get-user card when canonical edit arrives', async ({ page }) => {
    await page.addInitScript(() => {
      window.sessionStorage.setItem('eggw.apiToken', 'test-eggw-browser-token-' + 'a'.repeat(48));
    });
    const threadId = 'get-user-live-terminal-thread';
    await mockThreadShell(page, threadId, { messages: [{ id: 'before-get-user', role: 'user', content: 'start' }] });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/state`);
    let getUserAnswered = false;
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200, headers: mockApiHeaders,
      json: { state: 'running', streaming_kind: 'tool', streaming_invoke_id: 'invoke-get-user-live', live_replay_cursor: 0, active_get_user_wait: !getUserAnswered },
    }));
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route) => {
      const ts = new Date().toISOString();
      const envelope = (eventSeq: number, type: string, payload: Record<string, unknown>, msgId: string | null = null) => JSON.stringify({
        event_id: `get-user-live-${eventSeq}`, event_seq: eventSeq, type, ts, msg_id: msgId,
        invoke_id: 'invoke-get-user-live', chunk_seq: null, payload,
      });
      const block = (eventSeq: number, type: string, payload: Record<string, unknown>, msgId: string | null = null) => [
        `id: ${eventSeq}`, `event: ${type}`, `data: ${envelope(eventSeq, type, payload, msgId)}`, '',
      ];
      getUserAnswered = true;
      await route.fulfill({
        status: 200, headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body: [
          ...block(1, 'stream.open', { stream_kind: 'tool' }),
          ...block(2, 'tool_call.execution_started', { tool_call_id: 'call-get-user-live', name: getUserTool, arguments: '{"assistant_note":"Choose"}' }),
          ...block(3, 'tool_call.execution_started', { tool_call_id: 'call-bash-live', name: 'bash', arguments: '{"script":"sleep 30"}' }),
          ...block(4, 'msg.create', { role: 'user', content: 'Answer from manager', origin: 'manager_message' }, 'answer-live'),
          ...block(5, 'msg.edit', {
            content: 'Answer from manager', no_api: true, keep_user_turn: true,
            consumed_by_tool_name: getUserTool, consumed_by_tool_call_id: 'call-get-user-live',
          }, 'answer-live'),
          '',
        ].join('\n'),
      });
    });

    await page.goto(`/${threadId}`);
    await expect(page.getByTestId('chat-panel').locator('[data-message-role="user"]', { hasText: 'Answer from manager' })).toHaveCount(1);
    const outputs = page.getByTestId('streaming-tool-output').locator('..').locator('..');
    await expect(outputs.filter({ hasText: getUserTool })).toContainText('finished');
    await expect(outputs.filter({ hasText: getUserTool })).not.toContainText('streaming output...');
    const calls = page.getByTestId('streaming-tool-arguments').locator('..').locator('..');
    await expect(calls.filter({ hasText: getUserTool })).toContainText('finished');
    await expect(calls.filter({ hasText: getUserTool })).not.toContainText('streaming...');
    await expect(calls.filter({ hasText: 'bash' })).toContainText('streaming...');
    await expect(outputs.filter({ hasText: 'bash' })).toContainText('streaming output...');
    await expect(page.getByTestId('message-composer')).toContainText('Streaming; new messages will queue...');
  });
});

test.describe('Cross-client model synchronization', () => {
  test('terminal and web model writes converge by event order despite stale settings responses and reconnect', async ({ page }) => {
    const threadId = 'cross-client-model-sync';
    const models = ['Initial Model', 'Terminal Model', 'Web Model', 'Terminal Final'];
    let canonicalModel = models[0];
    let settingsRequests = 0;
    let releaseStaleSettings!: () => void;
    const staleSettingsReady = new Promise<void>((resolve) => { releaseStaleSettings = resolve; });
    let staleSettingsRequestSeen!: () => void;
    const staleSettingsRequested = new Promise<void>((resolve) => { staleSettingsRequestSeen = resolve; });
    let releaseEvents!: () => void;
    const eventsReady = new Promise<void>((resolve) => { releaseEvents = resolve; });
    let eventConnection = 0;

    await mockThreadShell(page, threadId);
    await page.unroute(`${TEST_API_BASE}/api/models`);
    await page.route(`${TEST_API_BASE}/api/models`, (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: {
        models: models.map((key) => ({ key, provider: 'fixture', model_id: key })),
        default_model: models[0],
      },
    }));
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/settings`);
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/settings`, async (route) => {
      settingsRequests += 1;
      const requestNumber = settingsRequests;
      const captured = canonicalModel;
      if (requestNumber === 2) {
        staleSettingsRequestSeen();
        await staleSettingsReady;
      }
      await route.fulfill({
        status: 200,
        headers: mockApiHeaders,
        json: { auto_approval: false, model_key: captured },
      });
    });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/model`);
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/model`, async (route, request) => {
      canonicalModel = String(request.postDataJSON().model_key);
      await route.fulfill({ status: 200, headers: mockApiHeaders, json: { status: 'ok', model_key: canonicalModel } });
    });
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/state`);
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: { state: 'waiting_user', streaming_invoke_id: null, live_replay_cursor: 0, active_get_user_wait: false },
    }));
    await page.unroute(`${TEST_API_BASE}/api/threads/${threadId}/events`);
    await page.unroute(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route) => {
      eventConnection += 1;
      const frame = (eventSeq: number, modelKey: string) => [
        `id: ${eventSeq}`,
        'event: model.switch',
        `data: ${JSON.stringify({
          event_id: `model-event-${eventSeq}`,
          event_seq: eventSeq,
          type: 'model.switch',
          ts: new Date().toISOString(),
          msg_id: null,
          invoke_id: null,
          chunk_seq: null,
          payload: { model_key: modelKey, reason: eventSeq === 10 ? 'ui /model' : 'web selector' },
        })}`,
        '',
      ];
      if (eventConnection === 1) await eventsReady;
      const body = eventConnection === 1
        ? [
            ...frame(10, 'Terminal Model'),
            ...frame(11, 'Web Model'),
            // Deliberately stale replay after the newer canonical write.
            ...frame(10, 'Terminal Model'),
            '',
          ].join('\n')
        : eventConnection === 2
          ? [...frame(12, 'Terminal Final'), ''].join('\n')
          : '';
      if (eventConnection === 1) canonicalModel = 'Web Model';
      if (eventConnection === 2) canonicalModel = 'Terminal Final';
      await route.fulfill({
        status: 200,
        headers: { ...mockApiHeaders, 'content-type': 'text/event-stream' },
        body,
      });
    });

    await page.goto(`/${threadId}`);
    const selector = page.getByLabel('Model');
    await staleSettingsRequested;
    releaseEvents();
    await expect.poll(() => eventConnection).toBeGreaterThanOrEqual(1);
    await expect(selector).toHaveValue('Web Model');
    releaseStaleSettings();
    await expect(selector).toHaveValue('Web Model');

    // The event stream reconnects from event 11 and observes the later terminal
    // writer's event 12 without polling or reloading the page.
    await expect.poll(() => eventConnection, { timeout: 8000 }).toBeGreaterThanOrEqual(2);
    await expect(selector).toHaveValue('Terminal Final');

    await selector.selectOption('Web Model');
    await expect.poll(() => canonicalModel).toBe('Web Model');
    await expect(selector).toHaveValue('Web Model');
    await page.reload();
    await expect(page.getByLabel('Model')).toHaveValue('Web Model');
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
    await expect(page.getByLabel('Model')).toBeVisible({ timeout: 5000 });
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

test.describe('Accessible composer and approval interactions', () => {
  test('exposes combobox/listbox semantics and preserves keyboard completion ownership', async ({ page }) => {
    await page.goto('/');
    await ensureThread(page);
    const input = page.getByTestId('message-input');
    await input.fill('/');
    await expect(input).toHaveAttribute('role', 'combobox');
    await expect(input).toHaveAttribute('aria-expanded', 'true');
    const listbox = page.getByRole('listbox', { name: 'Command suggestions' });
    await expect(listbox).toBeVisible();
    const options = listbox.getByRole('option');
    await expect(options.first()).toHaveAttribute('aria-selected', 'true');
    const count = await options.count();
    await expect(page.getByTestId('autocomplete-status')).toHaveText(`${count} suggestions available`);
    const firstId = await options.first().getAttribute('id');
    await expect(input).toHaveAttribute('aria-activedescendant', firstId || '');
    await input.press('ArrowDown');
    await expect(options.nth(1)).toHaveAttribute('aria-selected', 'true');
    const selectedText = await options.nth(1).locator('span').first().textContent();
    await input.press('Tab');
    await expect(input).toBeFocused();
    await expect(input).toHaveValue(selectedText || '');
    await expect(listbox).toBeHidden();
  });

  test('supports keyboard-only approval details and decisions', async ({ page }) => {
    await page.addInitScript(() => window.sessionStorage.setItem('eggw.apiToken', 'test-eggw-browser-token-' + 'a'.repeat(48)));
    const threadId = 'keyboard-approval-thread';
    let approval: Record<string, unknown> | undefined;
    await mockThreadShell(page, threadId, {
      tools: [{ id: 'keyboard-call', name: 'bash', arguments: { script: 'echo keyboard' }, state: 'TC4', output: 'keyboard output' }],
      onApprove: (payload) => { approval = payload; },
    });
    await page.goto(`/${threadId}`);
    const details = page.getByText(/View Output/).locator('..');
    await details.locator('summary').focus();
    await details.locator('summary').press('Enter');
    await expect(details).toHaveAttribute('open', '');
    const whole = page.getByRole('button', { name: 'Whole' });
    await whole.focus();
    await whole.press('Enter');
    await expect.poll(() => approval).toMatchObject({ output_decision: 'whole', approved: true });
  });

  test('edit-answer reuses the shared modal focus trap and returns focus to the composer', async ({ page }) => {
    const threadId = 'accessible-edit-answer-thread';
    await mockThreadShell(page, threadId, {
      messages: [{ id: 'accessible-assistant', role: 'assistant', content: 'Accessible answer', content_text: 'Accessible answer' }],
    });
    await page.route(`${TEST_API_BASE}/api/threads/${threadId}/command`, (route) => route.fulfill({
      status: 200,
      headers: mockApiHeaders,
      json: {
        success: true,
        message: 'Prepared accessible answer.',
        command_id: 'accessible-edit-command',
        command_name: 'editAnswer',
        started_at: new Date().toISOString(),
        finished_at: new Date().toISOString(),
        elapsed_sec: 0.01,
        data: {
          action: 'open_edit_answer_modal',
          draft: '> Accessible answer',
          source_msg_id: 'accessible-assistant',
          source_kind: 'assistant_answer',
          source_suffix: 'ssistant',
          source_label: 'assistant answer',
          suppress_transcript: true,
        },
      },
    }));
    await page.goto(`/${threadId}`);
    const composer = page.getByTestId('message-input');
    await composer.fill('/editAnswer');
    await composer.press('Enter');
    const dialog = page.getByRole('dialog', { name: 'Edit assistant answer' });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('button', { name: 'Close edit answer modal' })).toBeFocused();
    await expect(page.locator('header')).toHaveAttribute('inert', '');
    await page.keyboard.press('Shift+Tab');
    await expect(dialog.getByTestId('edit-answer-load')).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(dialog.getByRole('button', { name: 'Close edit answer modal' })).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
    await expect(composer).toBeFocused();
  });
});
