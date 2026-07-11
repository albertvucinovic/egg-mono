import { chromium } from 'playwright';
const MARKDOWN_BLOCK = [
  '## Streaming performance fixture', '',
  'A realistic paragraph with **bold**, _emphasis_, [a link](https://example.com), and `inline code`.', '',
  '```typescript', 'export function boundedWork(value: string): number {',
  '  return value.split("\\n").reduce((total, line) => total + line.length, 0);', '}', '```', '',
  '$$\\sum_{i=1}^{n} i = \\frac{n(n+1)}{2}$$', '',
  '| invariant | result |', '| --- | --- |', '| chronology | preserved |', '| continuity | atomic |',
].join('\n');
function performanceTranscript(size) {
  if (size === 0) return [];
  const filler = size === 300 ? `\n\n${'Large realistic payload text with stable wrapping. '.repeat(145)}` : '';
  return Array.from({ length: size }, (_, index) => index % 5 === 4 ? {
    id: `perf-tool-${index}`, role: 'tool', tool_call_id: `perf-call-${index}`,
    timestamp: new Date(1_700_000_000_000 + index * 1000).toISOString(),
    content: `tool output ${index}\n${'x'.repeat(size === 300 ? 6_500 : 1_200)}`,
  } : {
    id: `perf-message-${index}`, role: index % 2 ? 'assistant' : 'user',
    timestamp: new Date(1_700_000_000_000 + index * 1000).toISOString(),
    content: `${MARKDOWN_BLOCK}${filler}`,
    ...(index % 10 === 1 ? { reasoning: `Reasoning ${index}: ${'bounded reasoning '.repeat(30)}`, tool_calls: [{ id: `perf-call-${index}`, name: 'bash', arguments: JSON.stringify({ script: `echo ${index}` }) }] } : {}),
  });
}

const API_BASE = process.env.EGGW_PROFILE_API_URL || 'http://localhost:8099';
const APP_BASE = process.env.EGGW_PROFILE_URL || 'http://localhost:3099';
const cpuRate = Number(process.env.EGGW_CPU_RATE || 1);
const sizes = String(process.env.EGGW_PROFILE_SIZES || '0,100,300')
  .split(',').map(Number).filter((value) => [0, 100, 300].includes(value));

function percentile(values, fraction) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * fraction) - 1)];
}

const browser = await chromium.launch();
const context = await browser.newContext();
for (const size of sizes) {
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  if (cpuRate > 1) await cdp.send('Emulation.setCPUThrottlingRate', { rate: cpuRate });
  await context.clearCookies();
  const threadId = `profile-${size}`;
  const messages = performanceTranscript(size);
  let responseStart = 0;
  const headers = { 'access-control-allow-origin': '*', 'content-type': 'application/json' };
  await page.route(`${API_BASE}/api/**`, async (route, request) => {
    const url = new URL(request.url());
    if (url.pathname.endsWith('/events')) {
      return route.fulfill({ status: 200, headers: { 'access-control-allow-origin': '*', 'content-type': 'text/event-stream' }, body: '' });
    }
    let json = {};
    if (/\/messages$/.test(url.pathname)) json = { items: messages, snapshot_cursor: 0, next_before: null };
    else if (/\/state$/.test(url.pathname)) json = { state: 'waiting_user', live_replay_cursor: 0 };
    else if (/\/stats$/.test(url.pathname)) json = { context_tokens: 0, cost_usd: 0 };
    else if (/\/settings$/.test(url.pathname)) json = { auto_approval: false };
    else if (/\/children$/.test(url.pathname) || /\/tools$/.test(url.pathname)) json = [];
    else if (/\/open$/.test(url.pathname)) json = { status: 'opened' };
    else if (url.pathname === '/api/threads/roots' || (url.pathname === '/api/threads' && request.method() === 'GET')) json = [{ id: threadId, name: threadId, has_children: false }];
    else if (url.pathname === '/api/threads' && request.method() === 'POST') json = { id: threadId, name: threadId, has_children: false };
    else if (url.pathname === '/api/models') json = { models: [] };
    else if (url.pathname.endsWith('/sandbox')) json = { enabled: false, effective: false, available: false };
    else if (url.pathname === `/api/threads/${threadId}`) json = { id: threadId, name: threadId, has_children: false };
    return route.fulfill({ status: 200, headers, json });
  });

  await page.addInitScript(() => {
    window.sessionStorage.setItem('eggw.apiToken', 'test-eggw-browser-token-' + 'a'.repeat(48));
    window.__eggwLongTasks = [];
    window.__eggwEvents = [];
    new PerformanceObserver((list) => window.__eggwLongTasks.push(...list.getEntries().map((entry) => entry.duration)))
      .observe({ type: 'longtask', buffered: true });
    try {
      new PerformanceObserver((list) => window.__eggwEvents.push(...list.getEntries().map((entry) => entry.duration)))
        .observe({ type: 'event', buffered: true, durationThreshold: 0 });
    } catch {}
  });
  responseStart = performance.now();
  await page.goto(`${APP_BASE}/${threadId}`);
  try {
    await page.getByText(new RegExp(`Chat Messages · ${size} loaded`)).waitFor({ timeout: 60_000 });
  } catch (error) {
    console.error('profile page', await page.locator('body').innerText());
    throw error;
  }
  const readyMs = performance.now() - responseStart;
  const input = page.getByTestId('message-input');
  await input.focus();
  await input.fill('');
  if (!(await input.evaluate((element) => element === document.activeElement))) {
    throw new Error('Composer textarea did not retain focus');
  }
  const samples = [];
  for (let index = 0; index < 200; index += 1) {
    const started = performance.now();
    await input.pressSequentially('x');
    samples.push(performance.now() - started);
  }
  const finalInput = await input.inputValue();
  if (finalInput !== 'x'.repeat(200)) {
    throw new Error(`Composer verification failed: expected 200 characters, received ${finalInput.length}`);
  }
  const browserMetrics = await page.evaluate(() => ({
    domNodes: document.getElementsByTagName('*').length,
    longTasks: window.__eggwLongTasks || [],
    eventDurations: window.__eggwEvents || [],
    counters: window.__EGGW_PERFORMANCE__,
  }));
  console.log(JSON.stringify({
    size,
    payloadBytes: Buffer.byteLength(JSON.stringify(messages)),
    cpuRate,
    readyMs: Number(readyMs.toFixed(1)),
    domNodes: browserMetrics.domNodes,
    verifiedInputCharacters: finalInput.length,
    inputRoundTripP95Ms: Number(percentile(samples, 0.95).toFixed(1)),
    eventTimingP95Ms: Number(percentile(browserMetrics.eventDurations, 0.95).toFixed(1)),
    maxEventTimingMs: Number(Math.max(0, ...browserMetrics.eventDurations).toFixed(1)),
    maxLongTaskMs: Number(Math.max(0, ...browserMetrics.longTasks).toFixed(1)),
    longTaskLimitation: "includes framework hydration and browser work; compare against the 0-message floor",
    developmentCounters: browserMetrics.counters || null,
  }));
  await page.close();
}

await browser.close();
