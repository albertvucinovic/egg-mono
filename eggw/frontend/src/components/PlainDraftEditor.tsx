"use client";

import { useEffect, useRef } from "react";

export interface DraftEditorProps {
  value: string;
  onChange: (value: string) => void;
  sourceMsgId: string;
  canSubmitShortcut: boolean;
  onSubmitShortcut: () => void;
}

export function PlainDraftEditor({ value, onChange, canSubmitShortcut, onSubmitShortcut }: DraftEditorProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    window.setTimeout(() => textareaRef.current?.focus(), 0);
  }, []);

  return (
    <div data-testid="edit-answer-draft" data-editor="textarea-fallback">
      <textarea
        ref={textareaRef}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && canSubmitShortcut) {
            event.preventDefault();
            onSubmitShortcut();
          }
        }}
        className="min-h-[45vh] w-full resize-y rounded border p-3 font-mono text-sm outline-none"
        style={{
          background: "var(--code-bg)",
          borderColor: "var(--panel-border)",
          color: "var(--foreground)",
        }}
        spellCheck={false}
        data-testid="edit-answer-draft-textarea"
        aria-label="Quoted assistant markdown draft"
      />
      <p className="mt-2 text-xs" style={{ color: "var(--muted)" }}>
        Monaco editor did not become ready, so EggW is using a plain textarea fallback.
      </p>
    </div>
  );
}
