"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { X } from "lucide-react";
import { useAppStore } from "@/lib/store";
import clsx from "clsx";

function focusComposerSoon() {
  window.setTimeout(() => {
    const composer = document.querySelector<HTMLTextAreaElement>('[data-testid="message-input"]');
    composer?.focus();
  }, 0);
}

function sourceTitle(sourceLabel: string, sourceSuffix: string) {
  const label = sourceLabel || "assistant answer";
  return `${label}${sourceSuffix ? ` ${sourceSuffix}` : ""}`;
}

interface DraftEditorProps {
  value: string;
  onChange: (value: string) => void;
  textareaRef: React.RefObject<HTMLTextAreaElement>;
}

function DraftEditor({ value, onChange, textareaRef }: DraftEditorProps) {
  // Monaco can replace this small widget later without changing modal state or
  // composer-loading policy.  Plain textarea keeps this slice dependency-free.
  return (
    <textarea
      ref={textareaRef}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className="min-h-[45vh] w-full resize-y rounded border p-3 font-mono text-sm outline-none"
      style={{
        background: "var(--code-bg)",
        borderColor: "var(--panel-border)",
        color: "var(--foreground)",
      }}
      spellCheck={false}
      data-testid="edit-answer-draft"
      aria-label="Quoted assistant markdown draft"
    />
  );
}

export function EditAnswerModal() {
  const currentThreadId = useAppStore((state) => state.currentThreadId);
  const modal = useAppStore((state) => state.editAnswerModal);
  const composerDraft = useAppStore((state) => (
    modal.threadId ? state.composerDraftByThread[modal.threadId] || "" : ""
  ));
  const setComposerDraft = useAppStore((state) => state.setComposerDraft);
  const appendComposerDraft = useAppStore((state) => state.appendComposerDraft);
  const closeEditAnswerModal = useAppStore((state) => state.closeEditAnswerModal);
  const setEditAnswerDraft = useAppStore((state) => state.setEditAnswerDraft);
  const addSystemLog = useAppStore((state) => state.addSystemLog);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [initialDraft, setInitialDraft] = useState("");

  const isVisible = modal.isOpen && Boolean(modal.threadId) && modal.threadId === currentThreadId;
  const source = useMemo(() => sourceTitle(modal.sourceLabel, modal.sourceSuffix), [modal.sourceLabel, modal.sourceSuffix]);
  const replacesCommandText = Boolean(modal.replaceCommandText && composerDraft === modal.replaceCommandText);
  const hasExistingComposerDraft = Boolean(composerDraft.trim()) && !replacesCommandText;
  const canLoadDirectly = !hasExistingComposerDraft;
  const draftHasText = Boolean(modal.draft.trim());

  useEffect(() => {
    if (!isVisible) return;
    setInitialDraft(modal.draft);
    window.setTimeout(() => textareaRef.current?.focus(), 0);
    // Capture the initial draft only when a modal instance opens.  Subsequent
    // edits must not reset this baseline because Escape/Cancel uses it for the
    // dirty-draft confirmation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isVisible, modal.threadId, modal.sourceMsgId]);

  const closeWithDirtyCheck = () => {
    if (modal.draft !== initialDraft) {
      const discard = window.confirm("Discard changes to the edit-answer draft?");
      if (!discard) return;
    }
    closeEditAnswerModal();
    focusComposerSoon();
  };

  useEffect(() => {
    if (!isVisible) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeWithDirtyCheck();
      }
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && canLoadDirectly && draftHasText) {
        event.preventDefault();
        loadReplace();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isVisible, modal.draft, initialDraft, canLoadDirectly, draftHasText]);

  const finishLoad = (verb: "Loaded" | "Appended") => {
    addSystemLog(`${verb} quoted ${source} into composer`, "success");
    closeEditAnswerModal();
    focusComposerSoon();
  };

  const loadReplace = () => {
    if (!modal.threadId || !draftHasText) return;
    setComposerDraft(modal.threadId, modal.draft);
    finishLoad("Loaded");
  };

  const loadAppend = () => {
    if (!modal.threadId || !draftHasText) return;
    appendComposerDraft(modal.threadId, modal.draft);
    finishLoad("Appended");
  };

  if (!isVisible) return null;

  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 p-4"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) closeWithDirtyCheck();
      }}
      data-testid="edit-answer-modal"
    >
      <div
        className="flex max-h-[92vh] w-full max-w-5xl flex-col rounded-lg border shadow-2xl"
        style={{ background: "var(--panel-bg)", borderColor: "var(--panel-border)", color: "var(--foreground)" }}
        role="dialog"
        aria-modal="true"
        aria-label="Edit assistant answer"
      >
        <div className="flex items-start justify-between gap-3 border-b p-4" style={{ borderColor: "var(--panel-border)" }}>
          <div className="min-w-0">
            <h2 className="text-lg font-semibold">Edit assistant answer</h2>
            <div className="mt-1 text-xs font-mono" style={{ color: "var(--muted)" }}>
              Source: {source}{modal.sourceMsgId ? ` · ${modal.sourceMsgId}` : ""}
            </div>
          </div>
          <button
            type="button"
            onClick={closeWithDirtyCheck}
            className="rounded p-1 hover:bg-slate-700/60"
            aria-label="Close edit answer modal"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-4">
          <p className="mb-3 text-sm" style={{ color: "var(--muted)" }}>
            Editing raw quoted assistant markdown. This will load into the composer; it will not send automatically.
          </p>
          <DraftEditor value={modal.draft} onChange={setEditAnswerDraft} textareaRef={textareaRef} />
          {hasExistingComposerDraft && (
            <div
              className="mt-3 rounded border p-3 text-sm"
              style={{ borderColor: "#f59e0b", background: "rgba(245, 158, 11, 0.12)", color: "var(--foreground)" }}
              data-testid="edit-answer-overwrite-warning"
            >
              The composer already has text. Choose Replace or Append; EggW will not overwrite it silently.
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-center justify-end gap-2 border-t p-4" style={{ borderColor: "var(--panel-border)" }}>
          <button
            type="button"
            onClick={closeWithDirtyCheck}
            className="rounded border px-3 py-2 text-sm"
            style={{ borderColor: "var(--panel-border)", color: "var(--foreground)" }}
          >
            Cancel
          </button>
          {canLoadDirectly ? (
            <button
              type="button"
              onClick={loadReplace}
              disabled={!draftHasText}
              className={clsx("rounded px-3 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50")}
              style={{ background: "var(--accent)", color: "var(--background)" }}
              data-testid="edit-answer-load"
            >
              Load into composer
            </button>
          ) : (
            <>
              <button
                type="button"
                onClick={loadAppend}
                disabled={!draftHasText}
                className="rounded border px-3 py-2 text-sm disabled:cursor-not-allowed disabled:opacity-50"
                style={{ borderColor: "var(--panel-border)", color: "var(--foreground)" }}
                data-testid="edit-answer-append"
              >
                Append to composer
              </button>
              <button
                type="button"
                onClick={loadReplace}
                disabled={!draftHasText}
                className="rounded px-3 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50"
                style={{ background: "#f59e0b", color: "#111827" }}
                data-testid="edit-answer-replace"
              >
                Replace existing draft
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
