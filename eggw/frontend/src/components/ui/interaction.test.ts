import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(__dirname, "../..");
const css = readFileSync(resolve(root, "app/globals.css"), "utf8");
const overlay = readFileSync(resolve(root, "components/ui/OverlayPanel.tsx"), "utf8");
const shell = readFileSync(resolve(root, "app/[threadId]/page.tsx"), "utf8");

describe("global interaction foundation", () => {
  it("defines semantic focus, touch, viewport, reduced-motion, and forced-color behavior", () => {
    expect(css).toContain("height: 100dvh");
    expect(css).toContain("env(safe-area-inset-top)");
    expect(css).toMatch(/:focus-visible[\s\S]*var\(--focus-ring\)/);
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
    expect(css).toContain("@media (forced-colors: active)");
    expect(css).toMatch(/@media \(max-width: 639px\)[\s\S]*\.ui-icon-button[\s\S]*width: 2\.75rem/);
  });

  it("uses one reusable modal interaction implementation for help and drawers", () => {
    expect(overlay).toContain('role="dialog"');
    expect(overlay).toContain('aria-modal="true"');
    expect(overlay).toContain('event.key === "Escape"');
    expect(overlay).toContain('event.key !== "Tab"');
    expect(overlay).toContain("element.inert = true");
    expect(overlay).toContain("target.focus()");
    expect(shell).toContain("<HelpDialog");
    expect(shell.match(/<OverlayPanel/g)).toHaveLength(2);
  });

  it("keeps narrow header controls in a settings drawer instead of horizontal overflow", () => {
    expect(css).not.toMatch(/\.eggw-topbar-controls[^}]*overflow-x/);
    expect(css).toMatch(/@media \(max-width: 1023px\)[\s\S]*\.eggw-topbar-controls\s*\{\s*display: none/);
    expect(shell).toContain('title="Thread settings"');
    expect(shell).toContain('testId="settings-drawer"');
    expect(shell).toContain('testId="system-drawer"');
  });
});
