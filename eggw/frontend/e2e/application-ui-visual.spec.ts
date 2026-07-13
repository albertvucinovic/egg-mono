import { expect, test, type Page } from "@playwright/test";

const API_BASE = "http://localhost:8099";
const headers = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, POST, OPTIONS",
  "access-control-allow-headers": "authorization, content-type",
};
const threadId = "application-ui-visual-fixture";
const childId = "application-child-fixture";

async function mockApplicationUI(page: Page) {
  await page.route(`${API_BASE}/api/threads/${threadId}/events`, (route) => route.fulfill({
    status: 200, headers: { ...headers, "content-type": "text/event-stream" }, body: "",
  }));
  await page.route(`${API_BASE}/api/threads/${threadId}/open`, (route) => route.fulfill({ status: 200, headers, json: { status: "opened" } }));
  await page.route(new RegExp(`/api/threads/${threadId}/messages(?:\\?.*)?$`), (route) => route.fulfill({
    status: 200,
    headers,
    json: {
      items: [{ id: "application-message", role: "assistant", content: "Application controls use the shared semantic surface system." }],
      snapshot_cursor: 0,
      next_before: null,
    },
  }));
  await page.route(`${API_BASE}/api/threads/${threadId}/stats`, (route) => route.fulfill({
    status: 200, headers, json: { input_tokens: 12, output_tokens: 24, reasoning_tokens: 4, cached_tokens: 2, context_tokens: 42, full_thread_tokens: 42, total_tokens: 42, cost_usd: 0.001 },
  }));
  await page.route(`${API_BASE}/api/threads/${threadId}/tools`, (route) => route.fulfill({
    status: 200,
    headers,
    json: [{
      id: "application-tool-call", name: "bash", state: "TC4",
      arguments: { script: "printf 'semantic approval output\\n'" },
      summary: "Review generated output before it enters the conversation.",
      output: "semantic approval output\nline two",
    }],
  }));
  await page.route(`${API_BASE}/api/threads/${threadId}/tools/approve`, (route) => route.fulfill({ status: 200, headers, json: { status: "ok" } }));
  await page.route(`${API_BASE}/api/threads/${threadId}/sandbox`, (route) => route.fulfill({ status: 200, headers, json: { enabled: true, effective: true, available: true, user_control_enabled: true } }));
  await page.route(`${API_BASE}/api/threads/${threadId}/state`, (route) => route.fulfill({ status: 200, headers, json: { state: "waiting_output_approval", active_get_user_wait: false } }));
  await page.route(`${API_BASE}/api/threads/${threadId}/settings`, (route) => route.fulfill({ status: 200, headers, json: { auto_approval: false, model_key: "fixture:model" } }));
  await page.route(`${API_BASE}/api/threads/${threadId}/children`, (route) => route.fulfill({
    status: 200, headers, json: [{ id: childId, name: "Contrast review branch", model_key: "fixture:model", has_children: true }],
  }));
  await page.route(`${API_BASE}/api/threads/${threadId}`, (route) => route.fulfill({
    status: 200, headers, json: { id: threadId, name: "Application UI Fixture", has_children: true, model_key: "fixture:model" },
  }));
  await page.route(`${API_BASE}/api/threads/roots`, (route) => route.fulfill({ status: 200, headers, json: [{ id: threadId, name: "Application UI Fixture", has_children: true, model_key: "fixture:model" }] }));
  await page.route(`${API_BASE}/api/models`, (route) => route.fulfill({ status: 200, headers, json: { models: [{ key: "fixture:model" }], default_model: "fixture:model" } }));
  await page.route(`${API_BASE}/api/threads`, (route) => route.fulfill({ status: 200, headers, json: [{ id: threadId, name: "Application UI Fixture", has_children: true }] }));
  await page.route(`${API_BASE}/api/autocomplete**`, (route) => route.fulfill({
    status: 200,
    headers,
    json: { suggestions: [
      { display: "/help", insert: "/help", meta: "Show commands" },
      { display: "/theme", insert: "/theme ", meta: "Change appearance" },
      { display: "/toggleAutoApproval", insert: "/toggleAutoApproval", meta: "Approval policy" },
    ] },
  }));
}

async function openApplicationUI(page: Page, theme: string, viewport: { width: number; height: number }) {
  await page.setViewportSize(viewport);
  await page.addInitScript((value) => localStorage.setItem("eggw-theme", value), theme);
  await mockApplicationUI(page);
  await page.goto(`/${threadId}`);
  await expect(page.getByTestId("message-input")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("Pending Approvals")).toBeVisible();
  await page.getByTestId("message-input").fill("/");
  await expect(page.getByRole("listbox", { name: "Command suggestions" })).toBeVisible();
}

for (const scenario of [
  { name: "desktop-application-dark", theme: "dark", width: 1440, height: 1000 },
  { name: "tablet-application-cyberpunk", theme: "cyberpunk-background", width: 900, height: 900 },
  { name: "mobile-application-light", theme: "light-background", width: 390, height: 844 },
]) {
  test(`${scenario.name} visual`, async ({ page }) => {
    await openApplicationUI(page, scenario.theme, { width: scenario.width, height: scenario.height });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(scenario.width);
    await expect(page.locator(".eggw-chat-card")).toHaveScreenshot(`${scenario.name}.png`, {
      animations: "disabled",
      caret: "hide",
      maxDiffPixelRatio: 0.015,
    });
  });
}

test("mobile composer and approval controls remain reachable and touch sized", async ({ page }) => {
  await openApplicationUI(page, "light-mono", { width: 390, height: 844 });
  const controls = page.locator(".eggw-composer button:visible, .eggw-approval-panel button:visible");
  const sizes = await controls.evaluateAll((elements) => elements.map((element) => {
    const rect = element.getBoundingClientRect();
    return { width: rect.width, height: rect.height, text: element.textContent?.trim() };
  }));
  expect(sizes.length).toBeGreaterThan(4);
  expect(sizes.every(({ height }) => height >= 44)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});
