"use client";

import { OverlayPanel } from "./ui/OverlayPanel";
import { Button } from "./ui/primitives";
import { EGGW_SHORTCUT_GROUPS } from "@/lib/keyboardShortcuts";

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
      {EGGW_SHORTCUT_GROUPS.map((group, index) => {
        const headingId = `help-shortcuts-${index}`;
        return (
          <section key={group.title} className="eggw-command-list" aria-labelledby={headingId}>
            <h3 id={headingId}>{group.title}</h3>
            <dl className="eggw-shortcut-list">
              {group.items.map(({ label, keys }) => (
                <div key={label} className="eggw-shortcut-row">
                  <dt>{label}</dt><dd><kbd>{keys}</kbd></dd>
                </div>
              ))}
            </dl>
          </section>
        );
      })}
      <section className="eggw-command-list" aria-labelledby="help-commands-heading">
        <h3 id="help-commands-heading">Commands</h3>
        {commands.map((command) => <p key={command}>{command}</p>)}
      </section>
    </OverlayPanel>
  );
}
