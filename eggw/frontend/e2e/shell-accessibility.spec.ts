import { expect, test, type Page } from "@playwright/test";

async function openShell(page: Page, width: number, height: number, theme: string) {
  await page.setViewportSize({ width, height });
  await page.addInitScript((savedTheme) => localStorage.setItem("eggw-theme", savedTheme), theme);
  await page.goto("/");
  await expect(page.getByTestId("message-input")).toBeVisible({ timeout: 15_000 });
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
}

async function expectNoDocumentOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    await page.evaluate(() => window.innerWidth),
  );
}

for (const scenario of [
  { name: "desktop dark", width: 1440, height: 1000, theme: "dark" },
  { name: "tablet light", width: 900, height: 900, theme: "light-background" },
  { name: "mobile mono", width: 390, height: 844, theme: "light-mono" },
  { name: "mobile high-chroma", width: 390, height: 844, theme: "cyberpunk-background" },
]) {
  test(`${scenario.name} keeps shell actions reachable without horizontal overflow`, async ({ page }) => {
    await openShell(page, scenario.width, scenario.height, scenario.theme);
    await expectNoDocumentOverflow(page);
    await expect(page.getByRole("button", { name: "Help" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Show system panel" })).toBeVisible();
    expect(await page.getByRole("button", { name: "Help" }).evaluate((element) => getComputedStyle(element).borderTopWidth)).toBe("1px");

    if (scenario.width < 1024) {
      await expect(page.getByRole("button", { name: "Open settings" })).toBeVisible();
      await expect(page.getByLabel("Thread settings")).toBeHidden();
      await page.getByRole("button", { name: "Open settings" }).click();
      const drawer = page.getByRole("dialog", { name: "Thread settings" });
      await expect(drawer).toBeVisible();
      await expect(drawer.getByLabel("Model")).toBeVisible();
      await expect(drawer.getByRole("switch", { name: /Auto-approval/ })).toBeVisible();
      await expect(drawer.getByRole("switch", { name: "Toggle sandboxing" })).toBeVisible();
      await expect(drawer.getByLabel("Verbosity")).toBeVisible();
      const sizes = await drawer.locator("button, select").evaluateAll((elements) => elements.map((element) => {
        const rect = element.getBoundingClientRect();
        return { width: rect.width, height: rect.height };
      }));
      expect(sizes.every(({ height }) => height >= 44)).toBe(true);
      await page.keyboard.press("Escape");
      await expect(drawer).toBeHidden();
      await expect(page.getByRole("button", { name: "Open settings" })).toBeFocused();
    } else {
      await expect(page.getByLabel("Thread settings")).toBeVisible();
      await expect(page.getByRole("button", { name: "Open settings" })).toBeHidden();
    }
  });
}

test("help dialog traps focus, makes shell inert, closes on Escape, and returns focus", async ({ page }) => {
  await openShell(page, 390, 844, "colorful-light-background");
  const trigger = page.getByRole("button", { name: "Help" });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "Keyboard Shortcuts" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Close help" })).toBeFocused();
  await expect(page.locator("header")).toHaveAttribute("inert", "");
  await page.keyboard.press("Shift+Tab");
  await expect(dialog.getByRole("button", { name: "Close", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Close help" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
});

test("System uses desktop rail and responsive modal drawer with focus return", async ({ page }) => {
  await openShell(page, 1440, 1000, "forest-background");
  const trigger = page.getByRole("button", { name: "Show system panel" });
  await trigger.click();
  await expect(page.getByRole("complementary", { name: "System panel" })).toBeVisible();
  await expect(page.getByRole("dialog", { name: "System panel" })).toHaveCount(0);

  await page.setViewportSize({ width: 900, height: 900 });
  const drawer = page.getByRole("dialog", { name: "System panel" });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole("button", { name: "Close system panel" })).toBeFocused();
  expect(await page.getByTestId("message-composer").evaluate((element) => Boolean(element.closest("[inert]")))).toBe(true);
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(page.getByRole("button", { name: "Show system panel" })).toBeFocused();
});

test("visible keyboard focus uses the semantic focus ring", async ({ page }) => {
  await openShell(page, 390, 844, "cyberpunk");
  await page.getByRole("button", { name: "Help" }).focus();
  await page.keyboard.press("Tab");
  const activeStyle = await page.evaluate(() => {
    const style = getComputedStyle(document.activeElement as Element);
    return { width: style.outlineWidth, style: style.outlineStyle, color: style.outlineColor };
  });
  expect(activeStyle.width).toBe("3px");
  expect(activeStyle.style).toBe("solid");
  expect(activeStyle.color).not.toBe("rgba(0, 0, 0, 0)");
});
