"use client";

import { OverlayPanel } from "./ui/OverlayPanel";
import { Button } from "./ui/primitives";

const shortcuts = [
  ["Cancel streaming", "Esc"], ["New thread", "Ctrl+N"], ["Spawn child thread", "Ctrl+S"],
  ["Clear input", "Ctrl+E"], ["Paste clipboard", "Ctrl+P"], ["Focus input", "i"],
  ["Start command", "/"], ["Show this help", "?"],
];

const commands = [
  "/model, /updateAllModels, /spawnChildThread, /spawnAutoApprovedChildThread",
  "/skills, /skill", "/newThread, /threads, /thread, /rename, /waitForThreads",
  "/parentThread, /listChildren, /deleteThread, /duplicateThread",
  "/context, /compact, /compactWithSummary, /setAutoCompactThreshold",
  "/toggleAutoApproval, /toolsOn, /toolsOff, /toolsStatus, /toolInfo",
  "/disableTool, /enableTool, /toolsSecrets",
  "/toggleSandboxing, /setSandboxConfiguration, /getSandboxingConfig",
  "/sessionStatus, /sessionOn, /sessionOff, /sessionStop, /sessionReset",
  "/sessionCleanup, /pythonRepl, /bashRepl",
  "/setContextLimit, /setThreadPriority, /authStatus, /login, /logout",
  "/editor, /editAnswer, /togglePanel, /toggleBorders, /enterMode, /theme, /cost, /reload, /quit",
  "/startSearxng, /stopSearxng", "$ cmd - Shell, $$ cmd - Hidden shell",
];

export function HelpDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <OverlayPanel
      open={open}
      onClose={onClose}
      title="Keyboard Shortcuts"
      description="Keyboard controls and available EggW commands."
      closeLabel="Close help"
      testId="help-dialog"
      returnFocusSelector="[aria-label='Help']"
      footer={<Button variant="primary" onClick={onClose}>Close</Button>}
    >
      <dl className="eggw-shortcut-list">
        {shortcuts.map(([label, key]) => (
          <div key={label} className="eggw-shortcut-row">
            <dt>{label}</dt><dd><kbd>{key}</kbd></dd>
          </div>
        ))}
      </dl>
      <section className="eggw-command-list" aria-labelledby="help-commands-heading">
        <h3 id="help-commands-heading">Commands</h3>
        {commands.map((command) => <p key={command}>{command}</p>)}
      </section>
    </OverlayPanel>
  );
}
