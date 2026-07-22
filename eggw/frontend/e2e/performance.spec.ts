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
    // The 60-message render window contains 48 visible conversation records
    // and 12 hidden tool-result records summarized in compact cards.
    const mountedConversationMessages = page.locator('[data-message-id]');
    await expect(mountedConversationMessages).toHaveCount(48);

    const before = await counters(page);
    await page.getByTestId('message-input').pressSequentially('x'.repeat(200), { delay: 0 });
    const after = await counters(page);
    expect(after.transcriptCommits - before.transcriptCommits).toBe(0);
    // Live timing is isolated in memoized leaves; typing never commits the page.
    expect(after.chatPanelCommits - before.chatPanelCommits).toBe(0);

    await page.getByTitle('Transcript display verbosity').selectOption('min');
    // Minimum verbosity must honor the same mounted window. It must not turn
    // all 240 unmounted entries into one synthetic prefix tool group.
    // Compact run summaries are computed only from the mounted 60-message
    // window and stay bounded rather than aggregating the loaded prefix.
    const minCardCount = await page.locator('.eggw-message-card').count();
    expect(minCardCount).toBeGreaterThanOrEqual(60);
    expect(minCardCount).toBeLessThanOrEqual(90);
    const initialToolButtons = await page.getByTestId('hidden-details').getByRole('button').count();
    expect(initialToolButtons).toBeGreaterThan(1);
    // Compact tool entries remain bounded by the render window rather than
    // loaded history.
    expect(initialToolButtons).toBeLessThan(90);

    await page.getByTestId('show-more-loaded-messages').click();
    await expect(page.getByTestId('show-more-loaded-messages')).toContainText('180 earlier');
    await expect(mountedConversationMessages).toHaveCount(96);
    const revealedToolButtons = await page.getByTestId('hidden-details').getByRole('button').count();
    expect(revealedToolButtons).toBeGreaterThan(initialToolButtons);
    expect(revealedToolButtons).toBeLessThan(180);
    await expect(page.getByTestId('hidden-details').first()).toBeVisible();
    await expect(page.getByText('Streaming performance fixture').first()).toBeVisible();
  });

  test('5M-token-equivalent history reaches oldest with a bounded initial window and retained growth', async ({ page }) => {
    test.setTimeout(240_000);
    const threadId = 'performance-five-million';
    const messagesPerPage = 300;
    const pageCount = 24;
    const tokenUnitsPerMessage = 700;
    const tokenUnits = Array.from({ length: tokenUnitsPerMessage }, (_, index) => `u${index % 10}`).join(' ');
    expect(tokenUnits.trim().split(/\s+/)).toHaveLength(tokenUnitsPerMessage);
    const pageMessages = (pageIndex: number) => Array.from({ length: messagesPerPage }, (_, index) => ({
      id: `five-million-${pageIndex}-${index}`,
      role: index % 2 ? 'assistant' : 'user',
      content: `page ${pageIndex} message ${index} ${tokenUnits}`,
    }));
    const actualTokenUnits = messagesPerPage * pageCount * tokenUnitsPerMessage;
    expect(actualTokenUnits).toBeGreaterThanOrEqual(5_040_000);
    expect(Buffer.byteLength(JSON.stringify(pageMessages(0))) * pageCount).toBeGreaterThan(10 * 1024 * 1024);

    let tailRequests = 0;
    const requestedPages: number[] = [];
    let eventConnections = 0;
    let releaseLiveUpdate!: () => void;
    const liveUpdateReady = new Promise<void>((resolve) => { releaseLiveUpdate = resolve; });
    await mockPerformanceThread(page, threadId, 0);
    await page.unroute(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`));
    await page.unroute(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`));
    await page.unroute(`${API_BASE}/api/threads/${threadId}/events`);
    await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), async (route, request) => {
      const before = new URL(request.url()).searchParams.get('before_id');
      const pageIndex = before ? Number(before.replace('cursor-', '')) : 0;
      if (before) requestedPages.push(pageIndex);
      else tailRequests += 1;
      await route.fulfill({
        status: 200,
        headers,
        json: {
          items: pageMessages(pageIndex),
          snapshot_cursor: 20,
          next_before: pageIndex + 1 < pageCount ? `cursor-${pageIndex + 1}` : null,
        },
      });
    });
    await page.route(new RegExp(`/api/threads/${threadId}/state(?:\\?.*)?$`), (route) => route.fulfill({
      status: 200,
      headers,
      json: { state: 'running', streaming_kind: 'llm', streaming_invoke_id: 'five-million-live', live_replay_cursor: 20 },
    }));
    await page.route(new RegExp(`/api/threads/${threadId}/events(?:\\?.*)?$`), async (route) => {
      eventConnections += 1;
      const ts = new Date().toISOString();
      const envelope = (eventSeq: number, type: string, payload: Record<string, unknown>) => JSON.stringify({
        event_id: `five-million-event-${eventSeq}`,
        event_seq: eventSeq,
        type,
        ts,
        msg_id: null,
        invoke_id: 'five-million-live',
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
          body: [...block(21, 'stream.open', { stream_kind: 'llm' }), ...block(22, 'stream.delta', { text: 'live-start' })].join('\n'),
        });
      }
      if (eventConnections === 2) {
        await liveUpdateReady;
        return route.fulfill({
          status: 200,
          headers: { ...headers, 'content-type': 'text/event-stream' },
          body: Array.from({ length: 100 }, (_, index) => block(index + 23, 'stream.delta', { text: ' live-unit' })).flat().join('\n'),
        });
      }
      return route.fulfill({ status: 200, headers: { ...headers, 'content-type': 'text/event-stream' }, body: '' });
    });

    await page.goto(`/${threadId}`);
    await expect(page.getByText(/Chat Messages · 300 loaded/)).toBeVisible({ timeout: 20_000 });
    const chat = page.getByTestId('chat-panel');
    const input = page.getByTestId('message-input');
    const assertInitiallyBounded = async () => {
      expect(await page.locator('[data-message-id]').count()).toBeLessThanOrEqual(120);
    };
    await assertInitiallyBounded();
    await page.waitForTimeout(150);

    const beforeInput = await counters(page);
    await input.pressSequentially('bounded input', { delay: 0 });
    const afterInput = await counters(page);
    expect(afterInput.transcriptCommits - beforeInput.transcriptCommits).toBe(0);
    expect(afterInput.chatPanelCommits - beforeInput.chatPanelCommits).toBe(0);

    // Each Home prepends another 60-message chunk without removing the already
    // rendered suffix. Once the loaded cache is exhausted, one page is fetched.
    for (let pageIndex = 1; pageIndex < pageCount; pageIndex += 1) {
      while (await page.getByTestId('show-more-loaded-messages').isVisible()) {
        await page.getByTestId('show-more-loaded-messages').click();
      }
      await chat.focus();
      await page.keyboard.press('Home');
      await expect.poll(() => requestedPages.at(-1)).toBe(pageIndex);
      await expect(page.getByText(new RegExp(`Chat Messages · ${((pageIndex + 1) * messagesPerPage).toLocaleString()} loaded`))).toBeVisible({ timeout: 20_000 });
    }
    while (await page.getByTestId('show-more-loaded-messages').isVisible()) {
      await page.getByTestId('show-more-loaded-messages').click();
    }
    await expect(page.locator('[data-message-id="five-million-23-0"]')).toBeVisible();
    await expect(page.getByTestId('return-to-live-tail')).toBeVisible();
    expect(requestedPages).toEqual(Array.from({ length: pageCount - 1 }, (_, index) => index + 1));
    expect(tailRequests).toBeLessThanOrEqual(2);

    // The live card remains mounted below retained history, but detached intent
    // prevents automatic following until End explicitly returns to the tail.
    // End scrolls only; it must not reclaim the monotonically grown transcript.
    const mountedBeforeEnd = await page.locator('[data-message-id]').count();
    const oldestMountedBeforeEnd = await page.locator('[data-message-id]').first().getAttribute('data-message-id');
    await chat.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    await expect(page.getByTestId('streaming-content')).toContainText('live-start');
    await chat.focus();
    await page.keyboard.press('End');
    await expect(page.locator('[data-message-id="five-million-0-299"]')).toBeVisible();
    await expect(page.getByTestId('streaming-content')).toContainText('live-start');
    expect(await page.locator('[data-message-id]').count()).toBe(mountedBeforeEnd);
    expect(await page.locator('[data-message-id]').first().getAttribute('data-message-id')).toBe(oldestMountedBeforeEnd);

    await expect.poll(() => eventConnections).toBeGreaterThanOrEqual(2);
    await page.waitForTimeout(150);
    const beforeLive = await counters(page);
    releaseLiveUpdate();
    await expect(page.getByTestId('streaming-content')).toContainText('live-unit');
    const afterLive = await counters(page);
    expect(afterLive.streamingTextFlushes).toBeGreaterThan(beforeLive.streamingTextFlushes);
    // The reconnect can publish one semantic query/stream metadata commit; the
    // 100 body chunks themselves remain isolated in the imperative streaming leaf.
    expect(afterLive.transcriptCommits - beforeLive.transcriptCommits).toBeLessThanOrEqual(1);
    await input.pressSequentially(' after live', { delay: 0 });
    expect(await page.locator('[data-message-id]').count()).toBe(mountedBeforeEnd);
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
    await page.waitForTimeout(250);
    const before = await counters(page);
    releaseBurst();
    await expect.poll(async () => (await counters(page)).streamingToolArgumentFlushes)
      .toBeGreaterThan(before.streamingToolArgumentFlushes);
    await page.waitForTimeout(150);
    const after = await counters(page);
    // One semantic stream-metadata publication may re-evaluate the memoized
    // static transcript; the 1,100 body deltas must not add further commits.
    expect(after.transcriptCommits - before.transcriptCommits).toBeLessThanOrEqual(1);
    // Stream metadata may publish one semantic panel transition; body chunks
    // and timing leaves do not add page-owner commits.
    expect(after.chatPanelCommits - before.chatPanelCommits).toBeLessThanOrEqual(1);
    expect(after.streamingTextFlushes - before.streamingTextFlushes).toBeLessThanOrEqual(2);
    expect(after.streamingToolOutputFlushes - before.streamingToolOutputFlushes).toBeLessThanOrEqual(2);
    expect(after.streamingToolArgumentFlushes - before.streamingToolArgumentFlushes).toBeLessThanOrEqual(2);
    expect(after.streamingToolPreviewFlushes - before.streamingToolPreviewFlushes).toBeLessThanOrEqual(2);
  });
});
