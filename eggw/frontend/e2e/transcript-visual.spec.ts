import { expect, test, type Page, type TestInfo } from "@playwright/test";
import contract from "../../eggw/theme-contract.json";

const API_BASE = "http://localhost:8099";
const headers = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, POST, OPTIONS",
  "access-control-allow-headers": "authorization, content-type",
};

const threadId = "transcript-visual-fixture";
const timestamp = "2025-01-02T03:04:05.000Z";
const markdown = [
  "# Semantic transcript hierarchy",
  "",
  "Readable body copy with **strong emphasis**, _quiet emphasis_, [an accessible link](https://example.com), and `inline code`.",
  "",
  "> A blockquote should be secondary, structured, and unmistakable.",
  "",
  "- First list item",
  "- Second list item",
  "",
  "| element | treatment |",
  "| --- | --- |",
  "| role | semantic |",
  "| contrast | verified |",
  "",
  "```typescript",
  "export function themeName(value: string): string {",
  "  return value || \"dark\"; // semantic syntax",
  "}",
  "```",
].join("\n");

const messages = [
  {
    id: "visual-system-0001", role: "system", timestamp, tokens: 14,
    content: "Workspace initialized with deterministic transcript content.",
  },
  {
    id: "visual-user-0002", role: "user", timestamp, tokens: 18,
    content: [
      { type: "text", text: "Please audit this attachment and present a structured answer." },
      {
        type: "attachment", input_id: "visual-input", owner_thread_id: threadId,
        presentation: "file", mime_type: "text/plain", filename: "contrast-notes.txt",
        size_bytes: 2048, sha256: "a".repeat(64), options: {},
      },
    ],
  },
  {
    id: "visual-assistant-0003", role: "assistant", timestamp, tokens: 180, model_key: "fixture:model",
    content: markdown,
    reasoning: "Checked hierarchy, semantic roles, focus visibility, and composited contrast before answering.",
    tool_calls: [{ id: "visual-call", name: "bash", arguments: { script: "printf 'semantic output\\n'" } }],
  },
  {
    id: "visual-tool-0004", role: "tool", timestamp, tokens: 12, tool_call_id: "visual-call", name: "bash",
    content: "semantic output\nall transcript invariants preserved",
  },
  {
    id: "visual-compaction-0005", role: "compaction_marker", kind: "compaction_marker", timestamp,
    start_msg_id: "visual-assistant-0003", marker_event_seq: 10, start_event_seq: 4,
    selector: "visual", created_by: "fixture", content: "Compaction boundary fixture",
  },
];



const toolHeavyThreadId = "tool-heavy-min-visual-fixture";
const toolHeavyMessages = Array.from({ length: 20 }, (_, turn) => {
  const turnNumber = turn + 1;
  const suffix = String(turnNumber).padStart(2, "0");
  const toolName = ["bash", "python_repl", "read_long_tool_output"][turn % 3];
  const toolCallId = `tool-heavy-call-${suffix}`;
  return [
    {
      id: `tool-heavy-call-message-${suffix}`,
      role: "assistant",
      content: "",
      timestamp,
      tokens: 20 + turnNumber,
      tool_calls: [{
        id: toolCallId,
        name: toolName,
        arguments: toolName === "bash"
          ? { script: `printf 'tool-heavy-${suffix}\n'` }
          : { code: `print("tool-heavy-${suffix}")` },
      }],
    },
    {
      id: `tool-heavy-result-message-${suffix}`,
      role: "tool",
      name: toolName,
      tool_call_id: toolCallId,
      content: `Completed tool-heavy operation ${suffix}.`,
      timestamp,
      tokens: 8 + turnNumber,
    },
    turn % 2 === 0
      ? {
          id: `tool-heavy-note-${suffix}`,
          role: "assistant",
          answer_user_preserve_turn: true,
          content: `Checkpoint ${suffix}: verified grouped-run transcript ordering.`,
          timestamp,
          tokens: 12,
        }
      : {
          id: `tool-heavy-user-${suffix}`,
          role: "user",
          content: `Continue with operation ${suffix}.`,
          timestamp,
          tokens: 7,
        },
  ];
}).flat();

async function mockToolHeavyTranscript(page: Page) {
  await page.route(`${API_BASE}/api/threads/${toolHeavyThreadId}/events`, (route) => route.fulfill({
    status: 200, headers: { ...headers, "content-type": "text/event-stream" }, body: "",
  }));
  await page.route(`${API_BASE}/api/threads/${toolHeavyThreadId}/open`, (route) => route.fulfill({ status: 200, headers, json: { status: "opened" } }));
  await page.route(new RegExp(`/api/threads/${toolHeavyThreadId}/messages(?:\\?.*)?$`), (route) => route.fulfill({
    status: 200, headers, json: { items: toolHeavyMessages, snapshot_cursor: 120, next_before: null },
  }));
  await page.route(`${API_BASE}/api/threads/${toolHeavyThreadId}/stats`, (route) => route.fulfill({
    status: 200, headers, json: { input_tokens: 240, output_tokens: 880, reasoning_tokens: 0, cached_tokens: 0, context_tokens: 1120, full_thread_tokens: 1120, total_tokens: 1120, cost_usd: 0.01 },
  }));
  await page.route(`${API_BASE}/api/threads/${toolHeavyThreadId}/tools`, (route) => route.fulfill({ status: 200, headers, json: [] }));
  await page.route(`${API_BASE}/api/threads/${toolHeavyThreadId}/sandbox`, (route) => route.fulfill({ status: 200, headers, json: { enabled: false, effective: false, available: false, user_control_enabled: true } }));
  await page.route(`${API_BASE}/api/threads/${toolHeavyThreadId}/state`, (route) => route.fulfill({ status: 200, headers, json: { state: "waiting_user", active_get_user_wait: false } }));
  await page.route(`${API_BASE}/api/threads/${toolHeavyThreadId}/settings`, (route) => route.fulfill({ status: 200, headers, json: { auto_approval: false, model_key: "fixture:model" } }));
  await page.route(`${API_BASE}/api/threads/${toolHeavyThreadId}/children`, (route) => route.fulfill({ status: 200, headers, json: [] }));
  await page.route(`${API_BASE}/api/threads/${toolHeavyThreadId}`, (route) => route.fulfill({ status: 200, headers, json: { id: toolHeavyThreadId, name: "Tool-heavy minimum transcript", has_children: false, model_key: "fixture:model" } }));
  await page.route(`${API_BASE}/api/threads/roots`, (route) => route.fulfill({ status: 200, headers, json: [{ id: toolHeavyThreadId, name: "Tool-heavy minimum transcript", has_children: false }] }));
  await page.route(`${API_BASE}/api/models`, (route) => route.fulfill({ status: 200, headers, json: { models: [{ key: "fixture:model" }], default_model: "fixture:model" } }));
  await page.route(`${API_BASE}/api/threads`, (route) => route.fulfill({ status: 200, headers, json: [{ id: toolHeavyThreadId, name: "Tool-heavy minimum transcript", has_children: false }] }));
}

async function mockTranscript(page: Page) {
  await page.route(`${API_BASE}/api/threads/${threadId}/events`, (route) => route.fulfill({
    status: 200, headers: { ...headers, "content-type": "text/event-stream" }, body: "",
  }));
  await page.route(`${API_BASE}/api/threads/${threadId}/open`, (route) => route.fulfill({ status: 200, headers, json: { status: "opened" } }));
  await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), (route) => route.fulfill({
    status: 200, headers, json: { items: messages, snapshot_cursor: 0, next_before: null },
  }));
  await page.route(`${API_BASE}/api/threads/${threadId}/stats`, (route) => route.fulfill({
    status: 200, headers, json: { input_tokens: 32, output_tokens: 192, reasoning_tokens: 20, cached_tokens: 0, context_tokens: 244, full_thread_tokens: 244, total_tokens: 244, cost_usd: 0.0012 },
  }));
  await page.route(`${API_BASE}/api/threads/${threadId}/tools`, (route) => route.fulfill({ status: 200, headers, json: [] }));
  await page.route(`${API_BASE}/api/threads/${threadId}/sandbox`, (route) => route.fulfill({ status: 200, headers, json: { enabled: false, effective: false, available: false, user_control_enabled: true } }));
  await page.route(`${API_BASE}/api/threads/${threadId}/state`, (route) => route.fulfill({ status: 200, headers, json: { state: "waiting_user", active_get_user_wait: false } }));
  await page.route(`${API_BASE}/api/threads/${threadId}/settings`, (route) => route.fulfill({ status: 200, headers, json: { auto_approval: false, model_key: "fixture:model" } }));
  await page.route(`${API_BASE}/api/threads/${threadId}/children`, (route) => route.fulfill({ status: 200, headers, json: [] }));
  await page.route(`${API_BASE}/api/threads/${threadId}`, (route) => route.fulfill({ status: 200, headers, json: { id: threadId, name: "Transcript Visual Fixture", has_children: false, model_key: "fixture:model" } }));
  await page.route(`${API_BASE}/api/threads/roots`, (route) => route.fulfill({ status: 200, headers, json: [{ id: threadId, name: "Transcript Visual Fixture", has_children: false }] }));
  await page.route(`${API_BASE}/api/models`, (route) => route.fulfill({ status: 200, headers, json: { models: [{ key: "fixture:model" }], default_model: "fixture:model" } }));
  await page.route(`${API_BASE}/api/threads`, (route) => route.fulfill({ status: 200, headers, json: [{ id: threadId, name: "Transcript Visual Fixture", has_children: false }] }));
}

async function openFixture(page: Page, theme: string, viewport = { width: 1440, height: 1100 }) {
  await page.setViewportSize(viewport);
  await page.addInitScript((value) => localStorage.setItem("eggw-theme", value), theme);
  await mockTranscript(page);
  await page.goto(`/${threadId}`);
  const desktopVerbosity = page.getByTitle("Transcript display verbosity");
  if (await desktopVerbosity.isVisible()) {
    await desktopVerbosity.selectOption("max");
  } else {
    await page.getByRole("button", { name: "Open settings" }).click();
    await page.locator("#drawer-verbosity").selectOption("max");
    await page.getByRole("button", { name: "Close settings" }).click();
  }
  await expect(page.locator(".eggw-message-card")).toHaveCount(4, { timeout: 15_000 });
  await page.getByTestId("chat-panel").evaluate((element) => { element.scrollTop = 0; });
}

async function assertTranscriptSemantics(page: Page) {
  await expect(page.locator('[data-message-role="system"]')).toContainText("System");
  await expect(page.locator('[data-message-role="user"]')).toContainText("Attachment");
  await expect(page.locator('[data-message-role="assistant"] h1')).toHaveText("Semantic transcript hierarchy");
  await expect(page.locator('[data-message-role="assistant"] strong')).toHaveText("strong emphasis");
  await expect(page.locator('[data-message-role="assistant"] blockquote')).toContainText("blockquote");
  await expect(page.locator('[data-message-role="assistant"] table')).toBeVisible();
  await expect(page.locator('[data-message-role="assistant"] [class*="language-typescript"]')).toContainText("themeName");
  await expect(page.locator('[data-message-role="tool"]')).toContainText("all transcript invariants preserved");
  await expect(page.getByTestId("compaction-marker")).toBeVisible();
  await expect(page.locator(".eggw-role-marker")).toHaveCount(4);
  const syntaxBackground = await page.locator('[data-message-role="assistant"] [class*="language-typescript"]').first().evaluate((element) => getComputedStyle(element).backgroundColor);
  const codeBackground = await page.locator("html").evaluate((element) => getComputedStyle(element).getPropertyValue("--code-surface").trim());
  expect(syntaxBackground).not.toBe("rgb(43, 43, 43)");
  expect(codeBackground).toMatch(/^#/);
}

const allThemes = contract.themes.map((theme) => theme.name);

test("all 31 themes render the deterministic transcript fixture", async ({ page }, testInfo: TestInfo) => {
  await mockTranscript(page);
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto(`/${threadId}`);
  await page.getByTitle("Transcript display verbosity").selectOption("max");
  await expect(page.locator(".eggw-message-card")).toHaveCount(4, { timeout: 15_000 });
  for (const theme of allThemes) {
    await page.evaluate((name) => {
      document.documentElement.dataset.theme = name;
      localStorage.setItem("eggw-theme", name);
    }, theme);
    await page.getByTestId("chat-panel").evaluate((element) => { element.scrollTop = 0; });
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await assertTranscriptSemantics(page);
    const screenshot = await page.getByTestId("chat-panel").screenshot();
    await testInfo.attach(`desktop-${theme}`, { body: screenshot, contentType: "image/png" });
  }
});

for (const scenario of [
  { name: "mobile-dark", theme: "dark", width: 390, height: 844 },
  { name: "mobile-light", theme: "light-background", width: 390, height: 844 },
  { name: "mobile-mono", theme: "light-mono", width: 390, height: 844 },
  { name: "tablet-cyberpunk", theme: "cyberpunk-background", width: 900, height: 900 },
  { name: "tablet-forest", theme: "forest-background", width: 900, height: 900 },
]) {
  test(`${scenario.name} transcript visual`, async ({ page }) => {
    await openFixture(page, scenario.theme, { width: scenario.width, height: scenario.height });
    await assertTranscriptSemantics(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(scenario.width);
    await page.locator('[data-message-role="assistant"]').scrollIntoViewIfNeeded();
    await expect(page.getByTestId("chat-panel")).toHaveScreenshot(`${scenario.name}.png`, {
      animations: "disabled",
      caret: "hide",
      maxDiffPixelRatio: 0.015,
    });
  });
}

test("details and message actions are keyboard reachable", async ({ page }) => {
  await openFixture(page, "ocean-background", { width: 900, height: 900 });
  const reasoning = page.locator('[data-message-role="assistant"] summary').filter({ hasText: "Reasoning" }).first();
  await reasoning.focus();
  await expect(reasoning).toBeFocused();
  await reasoning.press("Enter");
  await expect(reasoning.locator("..")).not.toHaveAttribute("open", "");
  const id = page.locator('[data-message-role="assistant"] [data-testid="message-id"]');
  await id.focus();
  await expect(id).toBeFocused();
});

test("visible mobile transcript interactions meet the WCAG minimum target size", async ({ page }) => {
  await openFixture(page, "light-mono", { width: 390, height: 844 });
  const targets = page.getByTestId("chat-panel").locator("button:visible, a[href]:visible, summary:visible");
  const undersized = await targets.evaluateAll((elements) => elements.map((element) => {
    const rect = element.getBoundingClientRect();
    return { label: element.getAttribute("aria-label") || element.textContent?.trim() || element.tagName, width: rect.width, height: rect.height };
  }).filter(({ width, height }) => width < 24 || height < 24));
  expect(undersized, JSON.stringify(undersized, null, 2)).toEqual([]);
});


test("tool-heavy minimum transcript groups runs without crossing visible records", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.addInitScript(() => localStorage.setItem("eggw-theme", "dark"));
  await mockToolHeavyTranscript(page);
  await page.goto(`/${toolHeavyThreadId}`);
  await expect(page.getByTitle("Transcript display verbosity")).toHaveValue("min");

  const transcript = page.getByTestId("static-transcript-owner");
  const hidden = transcript.getByTestId("hidden-details");
  await expect(hidden).toHaveCount(20, { timeout: 15_000 });
  await expect(transcript.locator('[data-message-role="assistant_note"]')).toHaveCount(10);
  await expect(transcript.locator('[data-message-role="user"]')).toHaveCount(10);
  await expect(transcript.locator(':scope > *')).toHaveCount(40);

  expect(await hidden.evaluateAll((cards) => cards.every((card) => !card.hasAttribute("data-source-message-id")))).toBe(true);
  await expect(hidden.first()).toHaveAttribute("data-source-message-count", "2");
  await expect(hidden.first()).toContainText("Executed 1 tool, got 1 tool result");
  await expect(hidden.first()).toContainText("Tools: bash");
  await expect(hidden.last()).toContainText("Executed 1 tool, got 1 tool result");
  await expect(hidden.last()).toContainText("Tools: python_repl");

  const chat = page.getByTestId("chat-panel");
  await chat.evaluate((element) => { element.scrollTop = 0; });
  await expect(chat).toHaveScreenshot("tool-heavy-min-grouped-runs.png", {
    animations: "disabled",
    caret: "hide",
    maxDiffPixelRatio: 0.015,
  });
});
