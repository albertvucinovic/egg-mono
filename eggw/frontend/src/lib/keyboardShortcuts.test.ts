import { describe, expect, it } from "vitest";
import {
  EGGW_SHORTCUT_GROUPS,
  isToggleAutoApprovalShortcut,
  isToggleSandboxingShortcut,
} from "./keyboardShortcuts";

function keyEvent(
  code: "KeyA" | "KeyX",
  overrides: Partial<Pick<KeyboardEvent, "altKey" | "ctrlKey" | "metaKey" | "shiftKey" | "getModifierState">> = {},
) {
  return {
    code,
    altKey: true,
    ctrlKey: true,
    metaKey: false,
    shiftKey: false,
    getModifierState: () => false,
    ...overrides,
  };
}

describe("EggW keyboard shortcuts", () => {
  it("recognizes only unmodified Ctrl+Alt+A and Ctrl+Alt+X for safety toggles", () => {
    expect(isToggleAutoApprovalShortcut(keyEvent("KeyA"))).toBe(true);
    expect(isToggleSandboxingShortcut(keyEvent("KeyX"))).toBe(true);
    expect(isToggleAutoApprovalShortcut(keyEvent("KeyX"))).toBe(false);
    expect(isToggleSandboxingShortcut(keyEvent("KeyA"))).toBe(false);
    expect(isToggleAutoApprovalShortcut(keyEvent("KeyA", { ctrlKey: false }))).toBe(false);
    expect(isToggleSandboxingShortcut(keyEvent("KeyX", { shiftKey: true }))).toBe(false);
    expect(isToggleAutoApprovalShortcut(keyEvent("KeyA", { altKey: false }))).toBe(false);
    expect(isToggleAutoApprovalShortcut(keyEvent("KeyA", { getModifierState: (key) => key === "AltGraph" }))).toBe(false);
    expect(isToggleSandboxingShortcut(keyEvent("KeyX", { getModifierState: (key) => key === "AltGraph" }))).toBe(false);
  });

  it("keeps every implemented shortcut family visible in Help", () => {
    const rendered = EGGW_SHORTCUT_GROUPS.flatMap((group) => group.items)
      .map(({ label, keys }) => `${label}: ${keys}`)
      .join("\n");

    for (const expected of [
      "Ctrl+Alt+A", "Ctrl+Alt+X", "Esc", "Ctrl/Cmd+N", "Ctrl/Cmd+S", "Ctrl/Cmd+E", "Ctrl/Cmd+P",
      "i or any printable key", "?", "Shift+Enter", "Ctrl/Cmd+Enter", "Up/Down", "Tab",
      "PageUp/PageDown", "Home/End", "Right/Left", "Enter/Space", "Tab/Shift+Tab",
    ]) {
      expect(rendered).toContain(expected);
    }
  });
});
