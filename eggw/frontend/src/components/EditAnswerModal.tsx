"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useState } from "react";
import { useAppStore } from "@/lib/store";
import { PlainDraftEditor, type DraftEditorProps } from "@/components/PlainDraftEditor";
import { OverlayPanel } from "@/components/ui/OverlayPanel";
import { Button } from "@/components/ui/primitives";

function sourceTitle(sourceLabel: string, sourceSuffix: string) {
  const label = sourceLabel || "assistant answer";
  return `${label}${sourceSuffix ? ` ${sourceSuffix}` : ""}`;
}

function isInputMessageSource(sourceLabel: string, sourceKind: string) {
  return sourceKind === "input_message" || sourceLabel === "input message";
}

function isQuotedAssistantSource(sourceKind: string) {
  return sourceKind === "assistant_answer" || sourceKind === "assistant_note";
}

function DraftEditorLoading() {
  return (
    <div
      className="eggw-editor-state min-h-[45vh]"
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
  const isQuotedAssistant = isQuotedAssistantSource(modal.sourceKind);
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
    addSystemLog(
      isInputMessage
        ? `${verb} input message draft into composer`
        : isQuotedAssistant
          ? `${verb} quoted ${source} into composer`
          : `${verb} edited ${source} into composer`,
      "success",
    );
    closeEditAnswerModal();
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
  };

  useEffect(() => {
    if (!isVisible) return;
    const handleKeyDown = (event: KeyboardEvent) => {
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

  const title = isInputMessage ? "Edit input message" : "Edit assistant answer";
  const footer = (
    <>
      <Button variant="secondary" onClick={closeWithDirtyCheck}>Cancel</Button>
      {canLoadDirectly ? (
        <Button variant="primary" onClick={loadReplace} disabled={!draftHasText} data-testid="edit-answer-load">
          Load into composer
        </Button>
      ) : (
        <>
          <Button variant="secondary" onClick={loadAppend} disabled={!draftHasText} data-testid="edit-answer-append">
            Append to composer
          </Button>
          <Button variant="warning" onClick={loadReplace} disabled={!draftHasText} data-testid="edit-answer-replace">
            Replace existing draft
          </Button>
        </>
      )}
    </>
  );

  return (
    <OverlayPanel
      open
      onClose={closeWithDirtyCheck}
      title={title}
      description={`Source: ${source}${modal.sourceMsgId ? ` · ${modal.sourceMsgId}` : ""}`}
      closeLabel="Close edit answer modal"
      testId="edit-answer-modal"
      returnFocusSelector="[data-testid='message-input']"
      panelClassName="eggw-edit-dialog"
      footerClassName="eggw-edit-footer"
      footer={footer}
      portal
    >
      <p className="eggw-ui-muted mb-3 text-sm">
        {isInputMessage
          ? "Write an input message in Monaco. This will load into the composer; it will not send automatically."
          : isQuotedAssistant
            ? "Editing raw quoted assistant markdown in Monaco. This will load into the composer; it will not send automatically."
            : "Editing raw message text in Monaco. This will load into the composer; it will not send automatically."}
      </p>
      <DraftEditor
        value={modal.draft}
        onChange={setEditAnswerDraft}
        sourceMsgId={modal.sourceMsgId}
        canSubmitShortcut={canLoadDirectly && draftHasText}
        onSubmitShortcut={loadReplace}
      />
      {hasExistingComposerDraft && (
        <div className="eggw-edit-warning" role="alert" data-testid="edit-answer-overwrite-warning">
          The composer already has text. Choose Replace or Append; EggW will not overwrite it silently.
        </div>
      )}
    </OverlayPanel>
  );
}
