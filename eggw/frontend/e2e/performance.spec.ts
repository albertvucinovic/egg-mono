import { test, expect, Page } from '@playwright/test';
import { performanceTranscript, type PerformanceFixtureSize } from './fixtures/performance';

const API_BASE = 'http://localhost:8099';
const headers = {
  'access-control-allow-origin': '*',
  'access-control-allow-methods': 'GET, POST, OPTIONS',
  'access-control-allow-headers': 'authorization, content-type',
};

type Counters = {
  chatPanelCommits: number;
  transcriptCommits: number;
  streamingTextFlushes: number;
  streamingToolOutputFlushes: number;
  streamingToolArgumentFlushes: number;
  streamingToolPreviewFlushes: number;
};

async function mockPerformanceThread(page: Page, threadId: string, size: PerformanceFixtureSize) {
  const messages = performanceTranscript(size);
  await page.route(`${API_BASE}/api/threads/${threadId}/events`, (route) => route.fulfill({
    status: 200,
    headers: { ...headers, 'content-type': 'text/event-stream' },
    body: '',
  }));
  await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), (route) => route.fulfill({
    status: 200,
    headers,
    json: { items: messages, snapshot_cursor: 0, next_before: null },
  }));
  await page.route(`${API_BASE}/api/threads/${threadId}/state`, (route) => route.fulfill({
    status: 200, headers, json: { state: 'waiting_user', active_get_user_wait: false, live_replay_cursor: 0 },
  }));
  await page.route(`${API_BASE}/api/threads/${threadId}/open`, (route) => route.fulfill({ status: 200, headers, json: { status: 'opened' } }));
  await page.route(`${API_BASE}/api/threads/${threadId}/stats`, (route) => route.fulfill({ status: 200, headers, json: { context_tokens: 0, cost_usd: 0 } }));
  await page.route(`${API_BASE}/api/threads/${threadId}/tools`, (route) => route.fulfill({ status: 200, headers, json: [] }));
  await page.route(`${API_BASE}/api/threads/${threadId}/sandbox`, (route) => route.fulfill({ status: 200, headers, json: { enabled: false, effective: false, available: false } }));
  await page.route(`${API_BASE}/api/threads/${threadId}/settings`, (route) => route.fulfill({ status: 200, headers, json: { auto_approval: false } }));
  await page.route(`${API_BASE}/api/threads/${threadId}/children`, (route) => route.fulfill({ status: 200, headers, json: [] }));
  await page.route(`${API_BASE}/api/threads/${threadId}`, (route) => route.fulfill({ status: 200, headers, json: { id: threadId, name: threadId, has_children: false } }));
  await page.route(`${API_BASE}/api/threads/roots`, (route) => route.fulfill({ status: 200, headers, json: [{ id: threadId, name: threadId, has_children: false }] }));
  await page.route(`${API_BASE}/api/threads`, (route) => route.fulfill({ status: 200, headers, json: [{ id: threadId, name: threadId, has_children: false }] }));
  await page.route(`${API_BASE}/api/models`, (route) => route.fulfill({ status: 200, headers, json: { models: [] } }));
  return messages;
}

async function counters(page: Page): Promise<Counters> {
  return page.evaluate(() => ({
    chatPanelCommits: window.__EGGW_PERFORMANCE__?.chatPanelCommits || 0,
    transcriptCommits: window.__EGGW_PERFORMANCE__?.transcriptCommits || 0,
    streamingTextFlushes: window.__EGGW_PERFORMANCE__?.streamingTextFlushes || 0,
    streamingToolOutputFlushes: window.__EGGW_PERFORMANCE__?.streamingToolOutputFlushes || 0,
    streamingToolArgumentFlushes: window.__EGGW_PERFORMANCE__?.streamingToolArgumentFlushes || 0,
    streamingToolPreviewFlushes: window.__EGGW_PERFORMANCE__?.streamingToolPreviewFlushes || 0,
  }));
}

test.describe('Deterministic performance gates', () => {
  test('300 loaded mixed messages mount a bounded window and typing does not commit transcript/page', async ({ page }) => {
    const threadId = 'performance-300';
    const messages = await mockPerformanceThread(page, threadId, 300);
    expect(Buffer.byteLength(JSON.stringify(messages))).toBeGreaterThan(1_800_000);
    await page.goto(`/${threadId}`);
    await expect(page.getByText(/Chat Messages · 300 loaded/)).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('.eggw-message-card')).toHaveCount(60);

    const before = await counters(page);
    await page.getByTestId('message-input').pressSequentially('x'.repeat(200), { delay: 0 });
    const after = await counters(page);
    expect(after.transcriptCommits - before.transcriptCommits).toBe(0);
    // A one-second elapsed-time label may commit independently of body chunks.
    expect(after.chatPanelCommits - before.chatPanelCommits).toBeLessThanOrEqual(1);

    await page.getByTitle('Transcript display verbosity').selectOption('min');
    // Minimum verbosity must honor the same mounted window. It must not turn
    // all 240 unmounted entries into one synthetic prefix tool group.
    // Min renders the same 60 source messages; synthetic hidden-detail groups
    // can add cards but must remain bounded rather than aggregating the prefix.
    const minCardCount = await page.locator('.eggw-message-card').count();
    expect(minCardCount).toBeGreaterThanOrEqual(60);
    expect(minCardCount).toBeLessThanOrEqual(90);
    const initialToolButtons = await page.getByTestId('hidden-details').getByRole('button').count();
    expect(initialToolButtons).toBeGreaterThan(1);
    expect(initialToolButtons).toBeLessThan(30);

    await page.getByTestId('show-more-loaded-messages').click();
    await expect(page.getByTestId('show-more-loaded-messages')).toContainText('180 earlier');
    const revealedToolButtons = await page.getByTestId('hidden-details').getByRole('button').count();
    expect(revealedToolButtons).toBeGreaterThan(initialToolButtons);
    expect(revealedToolButtons).toBeLessThan(60);
    await expect(page.getByTestId('hidden-details').first()).toBeVisible();
    await expect(page.getByText('Streaming performance fixture').first()).toBeVisible();
  });

  test('1,100 delta burst bypasses React commits and bounds tool previews', async ({ page }) => {
    const threadId = 'performance-burst';
    await mockPerformanceThread(page, threadId, 0);
    await page.unroute(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`));
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200,
      headers,
      json: { items: [{ id: 'burst-user', role: 'user', content: 'run burst' }], snapshot_cursor: 0, next_before: null },
    }));
    await page.unroute(`${API_BASE}/api/threads/${threadId}/events`);
    await page.unroute(`${API_BASE}/api/threads/${threadId}/state`);
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200,
      headers,
      json: { state: 'running', streaming_kind: 'tool', streaming_invoke_id: 'perf-invoke', live_replay_cursor: 0 },
    }));
    let eventConnections = 0;
    let releaseBurst!: () => void;
    const burstReady = new Promise<void>((resolve) => { releaseBurst = resolve; });
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route) => {
      eventConnections += 1;
      const ts = new Date().toISOString();
      const envelope = (eventSeq: number, type: string, payload: Record<string, unknown>) => JSON.stringify({
        event_id: `perf-${eventSeq}`,
        event_seq: eventSeq,
        type,
        ts,
        msg_id: null,
        invoke_id: 'perf-invoke',
        chunk_seq: type === 'stream.delta' ? eventSeq : null,
        payload,
      });
      const block = (eventSeq: number, type: string, payload: Record<string, unknown>) => [
        `id: ${eventSeq}`, `event: ${type}`, `data: ${envelope(eventSeq, type, payload)}`, '', '',
      ];
      if (eventConnections === 1) {
        return route.fulfill({
          status: 200,
          headers: { ...headers, 'content-type': 'text/event-stream' },
          body: [
            ...block(1, 'stream.open', { stream_kind: 'tool' }),
            ...block(2, 'stream.delta', {
              tool: { id: 'perf-output', name: 'bash', text: 'start' },
              tool_call: { id: 'perf-call', name: 'bash', arguments_delta: 'start' },
            }),
          ].join('\n'),
        });
      }
      if (eventConnections === 2) {
        await burstReady;
        return route.fulfill({
          status: 200,
          headers: { ...headers, 'content-type': 'text/event-stream' },
          body: Array.from({ length: 1_100 }, (_, index) => block(index + 3, 'stream.delta', {
            text: 't',
            reason: 'r',
            tool: { id: 'perf-output', name: 'bash', text: 'o' },
            tool_call: { id: 'perf-call', name: 'bash', arguments_delta: 'x' },
          })).flat().join('\n'),
        });
      }
      return route.fulfill({ status: 200, headers: { ...headers, 'content-type': 'text/event-stream' }, body: '' });
    });

    await page.goto(`/${threadId}`);
    await expect(page.getByTestId('chat-panel')).toContainText('Tool', { timeout: 15_000 });
    await expect.poll(() => eventConnections, { timeout: 15_000 }).toBeGreaterThanOrEqual(2);
    await page.waitForTimeout(100);
    const before = await counters(page);
    releaseBurst();
    await expect.poll(async () => (await counters(page)).streamingToolArgumentFlushes)
      .toBeGreaterThan(before.streamingToolArgumentFlushes);
    await page.waitForTimeout(150);
    const after = await counters(page);
    expect(after.transcriptCommits - before.transcriptCommits).toBe(0);
    // A one-second elapsed-time label may commit independently of body chunks.
    expect(after.chatPanelCommits - before.chatPanelCommits).toBeLessThanOrEqual(1);
    expect(after.streamingTextFlushes - before.streamingTextFlushes).toBeLessThanOrEqual(2);
    expect(after.streamingToolOutputFlushes - before.streamingToolOutputFlushes).toBeLessThanOrEqual(2);
    expect(after.streamingToolArgumentFlushes - before.streamingToolArgumentFlushes).toBeLessThanOrEqual(2);
    expect(after.streamingToolPreviewFlushes - before.streamingToolPreviewFlushes).toBeLessThanOrEqual(2);
  });
});
