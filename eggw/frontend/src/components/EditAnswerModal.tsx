"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useState } from "react";
import { X } from "lucide-react";
import { useAppStore } from "@/lib/store";
import clsx from "clsx";
import { PlainDraftEditor, type DraftEditorProps } from "@/components/PlainDraftEditor";

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

function isInputMessageSource(sourceLabel: string, sourceKind: string) {
  return sourceKind === "input_message" || sourceLabel === "input message";
}

function DraftEditorLoading() {
  return (
    <div
      className="flex min-h-[45vh] w-full items-center justify-center rounded border p-3 text-sm"
      style={{
        background: "var(--code-bg)",
        borderColor: "var(--panel-border)",
        color: "var(--muted)",
      }}
      data-testid="edit-answer-draft"
      aria-label="Quoted assistant markdown draft"
    >
      Loading Monaco editor…
    </div>
  );
}

const DraftEditor = dynamic<DraftEditorProps>(
  () => import("@/components/MonacoDraftEditor").then((mod) => mod.MonacoDraftEditor).catch(() => PlainDraftEditor),
  {
    ssr: false,
    loading: DraftEditorLoading,
  },
);

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
  const [initialDraft, setInitialDraft] = useState("");

  const isVisible = modal.isOpen && Boolean(modal.threadId) && modal.threadId === currentThreadId;
  const source = useMemo(() => sourceTitle(modal.sourceLabel, modal.sourceSuffix), [modal.sourceLabel, modal.sourceSuffix]);
  const isInputMessage = isInputMessageSource(modal.sourceLabel, modal.sourceKind);
  const replacesCommandText = Boolean(modal.replaceCommandText && composerDraft === modal.replaceCommandText);
  const hasExistingComposerDraft = Boolean(composerDraft.trim()) && !replacesCommandText;
  const canLoadDirectly = !hasExistingComposerDraft;
  const draftHasText = Boolean(modal.draft.trim());

  useEffect(() => {
    if (!isVisible) return;
    setInitialDraft(modal.draft);
    // Capture the initial draft only when a modal instance opens.  Subsequent
    // edits must not reset this baseline because Escape/Cancel uses it for the
    // dirty-draft confirmation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isVisible, modal.threadId, modal.sourceMsgId]);

  const finishLoad = (verb: "Loaded" | "Appended") => {
    addSystemLog(isInputMessage ? `${verb} input message draft into composer` : `${verb} quoted ${source} into composer`, "success");
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
        const target = event.target instanceof Element ? event.target : null;
        if (target?.closest('[data-testid="edit-answer-draft"]')) return;
        event.preventDefault();
        loadReplace();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isVisible, modal.draft, initialDraft, canLoadDirectly, draftHasText]);

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
        aria-label={isInputMessage ? "Edit input message" : "Edit assistant answer"}
      >
        <div className="flex items-start justify-between gap-3 border-b p-4" style={{ borderColor: "var(--panel-border)" }}>
          <div className="min-w-0">
            <h2 className="text-lg font-semibold">{isInputMessage ? "Edit input message" : "Edit assistant answer"}</h2>
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
            {isInputMessage
              ? "Write an input message in Monaco. This will load into the composer; it will not send automatically."
              : "Editing raw quoted assistant markdown in Monaco. This will load into the composer; it will not send automatically."}
          </p>
          <DraftEditor
            value={modal.draft}
            onChange={setEditAnswerDraft}
            sourceMsgId={modal.sourceMsgId}
            canSubmitShortcut={canLoadDirectly && draftHasText}
            onSubmitShortcut={loadReplace}
          />
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
