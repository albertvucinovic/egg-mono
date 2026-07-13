import { expect, test, type Page, type TestInfo } from "@playwright/test";
import contract from "../../eggw/theme-contract.json";

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

type RGB = [number, number, number];

function parseRgb(value: string): RGB {
  const match = value.match(/rgba?\((\d+(?:\.\d+)?)[, ]+(\d+(?:\.\d+)?)[, ]+(\d+(?:\.\d+)?)/);
  if (!match) throw new Error(`Expected computed RGB color, received ${value}`);
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

function relativeLuminance([red, green, blue]: RGB): number {
  const linear = [red, green, blue].map((value) => {
    const channel = value / 255;
    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

function contrast(foreground: string, background: string): number {
  const first = relativeLuminance(parseRgb(foreground));
  const second = relativeLuminance(parseRgb(background));
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
}

const renderedTextPairs = [
  { name: "assistant content", foreground: ".eggw-role-assistant", background: ".eggw-role-assistant" },
  { name: "assistant label", foreground: ".eggw-role-assistant .eggw-role-label", background: ".eggw-role-assistant" },
  { name: "approval heading", foreground: ".eggw-approval-heading", background: ".eggw-approval-panel" },
  { name: "approval card content", foreground: ".eggw-approval-card", background: ".eggw-approval-card" },
  { name: "approval tool label", foreground: ".eggw-approval-tool-name", background: ".eggw-approval-card" },
  { name: "approval summary", foreground: ".eggw-approval-summary", background: ".eggw-approval-summary" },
  { name: "approval code", foreground: ".eggw-approval-card .eggw-code-block", background: ".eggw-approval-card .eggw-code-block" },
  { name: "branch link", foreground: ".eggw-thread-link", background: ".eggw-children-panel" },
  { name: "selected autocomplete option", foreground: '.eggw-autocomplete-option[aria-selected="true"]', background: '.eggw-autocomplete-option[aria-selected="true"]' },
  { name: "selected autocomplete metadata", foreground: '.eggw-autocomplete-option[aria-selected="true"] .eggw-autocomplete-meta', background: '.eggw-autocomplete-option[aria-selected="true"]' },
  { name: "composer input", foreground: ".eggw-composer-input", background: ".eggw-composer-input" },
  { name: "primary action", foreground: ".eggw-approval-card .ui-button-primary", background: ".eggw-approval-card .ui-button-primary" },
  { name: "warning action", foreground: ".eggw-approval-card .ui-button-warning", background: ".eggw-approval-card .ui-button-warning" },
  { name: "danger action", foreground: ".eggw-approval-card .ui-button-danger", background: ".eggw-approval-card .ui-button-danger" },
] as const;

const renderedBoundaryPairs = [
  { name: "approval card boundary", selector: ".eggw-approval-card" },
  { name: "approval summary boundary", selector: ".eggw-approval-summary" },
  { name: "approval code boundary", selector: ".eggw-approval-card .eggw-code-block" },
  { name: "autocomplete boundary", selector: ".eggw-autocomplete-listbox" },
  { name: "composer input boundary", selector: ".eggw-composer-input" },
] as const;

test("all 31 themes meet contrast on rendered application states", async ({ page }) => {
  await openApplicationUI(page, "dark", { width: 1440, height: 1000 });
  const failures: string[] = [];
  for (const theme of contract.themes.map((item) => item.name)) {
    await page.evaluate((name) => { document.documentElement.dataset.theme = name; }, theme);
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await page.waitForTimeout(180);
    for (const pair of renderedTextPairs) {
      const colors = await page.evaluate(({ foreground, background }) => {
        const foregroundElement = document.querySelector(foreground);
        const backgroundElement = document.querySelector(background);
        if (!foregroundElement || !backgroundElement) return null;
        return {
          foreground: getComputedStyle(foregroundElement).color,
          background: getComputedStyle(backgroundElement).backgroundColor,
        };
      }, pair);
      if (!colors) {
        failures.push(`${theme}: ${pair.name} is missing`);
        continue;
      }
      const ratio = contrast(colors.foreground, colors.background);
      if (ratio < 4.5) failures.push(`${theme}: ${pair.name} ${ratio.toFixed(2)}:1 (${colors.foreground} on ${colors.background})`);
    }
    for (const pair of renderedBoundaryPairs) {
      const colors = await page.locator(pair.selector).first().evaluate((element) => {
        const style = getComputedStyle(element);
        return { foreground: style.borderTopColor, background: style.backgroundColor };
      });
      const ratio = contrast(colors.foreground, colors.background);
      if (ratio < 3) failures.push(`${theme}: ${pair.name} ${ratio.toFixed(2)}:1 (${colors.foreground} on ${colors.background})`);
    }
    const markerColors = await page.locator(".eggw-role-assistant").evaluate((card) => ({
      foreground: getComputedStyle(card.querySelector(".eggw-role-marker")!).backgroundColor,
      background: getComputedStyle(card).backgroundColor,
    }));
    const markerRatio = contrast(markerColors.foreground, markerColors.background);
    if (markerRatio < 3) failures.push(`${theme}: assistant marker ${markerRatio.toFixed(2)}:1 (${markerColors.foreground} on ${markerColors.background})`);
  }
  expect(failures, failures.join("\n")).toEqual([]);
});

test("all 31 themes render the deterministic application state", async ({ page }, testInfo: TestInfo) => {
  await openApplicationUI(page, "dark", { width: 1440, height: 1000 });
  for (const theme of contract.themes.map((item) => item.name)) {
    await page.evaluate((name) => { document.documentElement.dataset.theme = name; }, theme);
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await page.waitForTimeout(180);
    const screenshot = await page.locator(".eggw-chat-card").screenshot({ animations: "disabled", caret: "hide" });
    await testInfo.attach(`application-${theme}`, { body: screenshot, contentType: "image/png" });
  }
});

test("all 31 themes expose safe application geometry and focus", async ({ page }) => {
  await openApplicationUI(page, "dark", { width: 1440, height: 1000 });
  const failures: string[] = [];
  for (const theme of contract.themes.map((item) => item.name)) {
    await page.evaluate((name) => { document.documentElement.dataset.theme = name; }, theme);
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await page.waitForTimeout(180);
    if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) failures.push(`${theme}: document overflow`);
    const composer = page.getByTestId("message-input");
    const help = page.getByRole("button", { name: "Help" });
    await help.focus();
    const focus = await help.evaluate((element) => {
      const style = getComputedStyle(element);
      return { width: style.outlineWidth, style: style.outlineStyle, color: style.outlineColor };
    });
    if (focus.width !== "3px" || focus.style !== "solid" || focus.color === "rgba(0, 0, 0, 0)") {
      failures.push(`${theme}: visible focus ${JSON.stringify(focus)}`);
    }
    await composer.fill("/");
  }
  expect(failures, failures.join("\n")).toEqual([]);
});

for (const scenario of [
  { name: "desktop-application-dark", theme: "dark", width: 1440, height: 1000 },
  { name: "tablet-application-cyberpunk", theme: "cyberpunk-background", width: 900, height: 900 },
  { name: "mobile-application-light", theme: "light-background", width: 390, height: 844 },
  { name: "mobile-application-mono", theme: "light-mono", width: 390, height: 844 },
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

test("forced colors and reduced motion preserve application states", async ({ page }) => {
  await page.emulateMedia({ forcedColors: "active", reducedMotion: "reduce" });
  await openApplicationUI(page, "cyberpunk-background", { width: 390, height: 844 });
  const selected = page.locator('.eggw-autocomplete-option[aria-selected="true"]');
  await expect(selected).toBeVisible();
  await page.locator(".eggw-approval-heading").evaluate((element) => element.classList.add("animate-pulse"));
  const styles = await selected.evaluate((element) => {
    const selectedStyle = getComputedStyle(element);
    const animated = document.querySelector(".eggw-approval-heading");
    return {
      color: selectedStyle.color,
      background: selectedStyle.backgroundColor,
      animationDuration: animated ? getComputedStyle(animated).animationDuration : "",
      cardBorder: getComputedStyle(document.querySelector(".eggw-approval-card")!).borderTopColor,
    };
  });
  expect(styles.color).not.toBe(styles.background);
  expect(styles.cardBorder).not.toBe("rgba(0, 0, 0, 0)");
  expect(Number.parseFloat(styles.animationDuration || "0")).toBeLessThanOrEqual(0.01);
});
