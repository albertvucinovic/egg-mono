export type ShortcutHelpItem = Readonly<{
  label: string;
  keys: string;
}>;

export type ShortcutHelpGroup = Readonly<{
  title: string;
  items: readonly ShortcutHelpItem[];
}>;

/**
 * User-visible EggW keyboard controls. Keep this catalog as the source for the
 * Help dialog so adding a handler also requires choosing where it is explained.
 */
export const EGGW_SHORTCUT_GROUPS: readonly ShortcutHelpGroup[] = [
  {
    title: "Global actions",
    items: [
      { label: "Toggle tool auto-approval", keys: "Ctrl+Alt+A" },
      { label: "Toggle sandboxing (X for sandboX)", keys: "Ctrl+Alt+X" },
      {
        label: "Cancel streaming, blur the composer, or close a dialog",
        keys: "Esc",
      },
      { label: "New thread (outside form fields)", keys: "Ctrl/Cmd+N" },
      { label: "Spawn a child thread (outside form fields)", keys: "Ctrl/Cmd+S" },
      { label: "Clear the composer draft (outside form fields)", keys: "Ctrl/Cmd+E" },
      { label: "Paste clipboard text into the composer (outside form fields)", keys: "Ctrl/Cmd+P" },
      { label: "Focus the composer (outside form fields)", keys: "i or any printable key" },
      { label: "Focus the composer and start a command (outside form fields)", keys: "/" },
      { label: "Show this help (outside form fields)", keys: "?" },
    ],
  },
  {
    title: "Composer and autocomplete",
    items: [
      { label: "Send in send mode", keys: "Enter" },
      { label: "Insert a newline in send mode", keys: "Shift+Enter" },
      { label: "Send in newline mode", keys: "Ctrl/Cmd+Enter" },
      { label: "Select an autocomplete item", keys: "Up/Down" },
      { label: "Accept the selected autocomplete item", keys: "Tab" },
      { label: "Dismiss autocomplete", keys: "Esc" },
    ],
  },
  {
    title: "Focused transcript and thread tree",
    items: [
      {
        label: "Scroll the transcript",
        keys: "Arrows, PageUp/PageDown, Space/Shift+Space",
      },
      { label: "Jump to transcript start/end", keys: "Home/End" },
      { label: "Move through thread-tree items", keys: "Up/Down, Home/End" },
      { label: "Expand/collapse a thread", keys: "Right/Left" },
      { label: "Open a thread", keys: "Enter/Space" },
    ],
  },
  {
    title: "Dialogs and editing",
    items: [
      { label: "Move focus within a dialog", keys: "Tab/Shift+Tab" },
      {
        label: "Load an edit-answer draft into the composer",
        keys: "Ctrl/Cmd+Enter",
      },
      { label: "Save/cancel a thread rename", keys: "Enter/Esc" },
    ],
  },
];

type ModifierKeyEvent = Pick<
  KeyboardEvent,
  "altKey" | "ctrlKey" | "metaKey" | "shiftKey" | "code"
>;

function isCtrlAltCode(
  event: ModifierKeyEvent,
  code: "KeyA" | "KeyX",
): boolean {
  return (
    event.altKey &&
    event.ctrlKey &&
    !event.metaKey &&
    !event.shiftKey &&
    event.code === code
  );
}

export function isToggleAutoApprovalShortcut(event: ModifierKeyEvent): boolean {
  return isCtrlAltCode(event, "KeyA");
}

export function isToggleSandboxingShortcut(event: ModifierKeyEvent): boolean {
  return isCtrlAltCode(event, "KeyX");
}
