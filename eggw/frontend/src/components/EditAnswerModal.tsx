"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useRef, useState } from "react";
import { useAppStore } from "@/lib/store";
import { PlainDraftEditor, type DraftEditorProps } from "@/components/PlainDraftEditor";
import { OverlayPanel } from "@/components/ui/OverlayPanel";
import { Button } from "@/components/ui/primitives";
import { discardEditorFile, saveEditorFile } from "@/lib/api";

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
      aria-label="Editor draft"
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
  const [savingFile, setSavingFile] = useState(false);
  const [fileError, setFileError] = useState("");
  const saveInFlightRef = useRef(false);

  const isVisible = modal.isOpen && Boolean(modal.threadId) && modal.threadId === currentThreadId;
  const source = useMemo(() => sourceTitle(modal.sourceLabel, modal.sourceSuffix), [modal.sourceLabel, modal.sourceSuffix]);
  const isInputMessage = isInputMessageSource(modal.sourceLabel, modal.sourceKind);
  const isQuotedAssistant = isQuotedAssistantSource(modal.sourceKind);
  const replacesCommandText = Boolean(modal.replaceCommandText && composerDraft === modal.replaceCommandText);
  const hasExistingComposerDraft = Boolean(composerDraft.trim()) && !replacesCommandText;
  const canLoadDirectly = !hasExistingComposerDraft;
  const draftHasText = Boolean(modal.draft.trim());
  const isFileMode = modal.editorMode === "file" && Boolean(modal.filePath);

  useEffect(() => {
    if (!isVisible) return;
    setInitialDraft(modal.draft);
    setFileError("");
    saveInFlightRef.current = false;
    setSavingFile(false);
    // Capture the initial draft only when a modal instance opens.  Subsequent
    // edits must not reset this baseline because Escape/Cancel uses it for the
    // dirty-draft confirmation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isVisible, modal.threadId, modal.sourceMsgId, modal.filePath]);

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
    if (saveInFlightRef.current) return;
    if (modal.draft !== initialDraft) {
      const discard = window.confirm(isFileMode ? "Discard unsaved file changes?" : "Discard changes to the editor draft?");
      if (!discard) return;
    }
    if (isFileMode && modal.threadId && modal.fileHandle) {
      void discardEditorFile(modal.threadId, modal.fileHandle).catch(() => undefined);
    }
    closeEditAnswerModal();
  };

  const saveFile = async () => {
    if (!modal.threadId || !modal.filePath || !modal.fileHandle || saveInFlightRef.current) return;
    saveInFlightRef.current = true;
    setSavingFile(true);
    setFileError("");
    try {
      await saveEditorFile(modal.threadId, {
        handle: modal.fileHandle,
        content: modal.draft,
      });
      addSystemLog(`Saved ${modal.filePath}`, "success");
      closeEditAnswerModal();
    } catch (error) {
      setFileError(error instanceof Error ? error.message : "Failed to save file");
    } finally {
      saveInFlightRef.current = false;
      if (useAppStore.getState().editAnswerModal.isOpen) setSavingFile(false);
    }
  };

  useEffect(() => {
    if (!isVisible) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (
        (event.ctrlKey || event.metaKey)
        && event.key === "Enter"
        && (isFileMode || (canLoadDirectly && draftHasText))
      ) {
        const target = event.target instanceof Element ? event.target : null;
        if (target?.closest('[data-testid="edit-answer-draft"]')) return;
        event.preventDefault();
        if (isFileMode) void saveFile();
        else loadReplace();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    isVisible,
    modal.draft,
    initialDraft,
    canLoadDirectly,
    draftHasText,
    isFileMode,
    modal.filePath,
    modal.fileHandle,
    modal.threadId,
    savingFile,
  ]);

  if (!isVisible) return null;

  const title = isFileMode
    ? "Edit file"
    : isInputMessage
      ? "Edit input message"
      : isQuotedAssistant
        ? "Edit assistant answer"
        : `Edit ${modal.sourceLabel || "message"}`;
  const footer = (
    <>
      <Button variant="secondary" onClick={closeWithDirtyCheck}>Cancel</Button>
      {isFileMode ? (
        <Button variant="primary" onClick={saveFile} disabled={savingFile} data-testid="editor-file-save">
          {savingFile ? "Saving…" : "Save file"}
        </Button>
      ) : canLoadDirectly ? (
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
      description={isFileMode ? `File: ${modal.filePath}` : `Source: ${source}${modal.sourceMsgId ? ` · ${modal.sourceMsgId}` : ""}`}
      closeLabel="Close editor modal"
      testId="edit-answer-modal"
      returnFocusSelector="[data-testid='message-input']"
      panelClassName="eggw-edit-dialog"
      footerClassName="eggw-edit-footer"
      footer={footer}
      portal
    >
      <p className="eggw-ui-muted mb-3 text-sm">
        {isFileMode
          ? "Editing the host file in place. Saving uses conflict detection and will not change the composer."
          : isInputMessage
          ? "Write an input message in Monaco. This will load into the composer; it will not send automatically."
          : isQuotedAssistant
            ? "Editing raw quoted assistant markdown in Monaco. This will load into the composer; it will not send automatically."
            : "Editing raw message text in Monaco. This will load into the composer; it will not send automatically."}
      </p>
      <DraftEditor
        value={modal.draft}
        onChange={setEditAnswerDraft}
        sourceMsgId={modal.sourceMsgId}
        canSubmitShortcut={isFileMode || (canLoadDirectly && draftHasText)}
        onSubmitShortcut={isFileMode ? saveFile : loadReplace}
        filePath={isFileMode ? modal.filePath : undefined}
      />
      {fileError && <div className="eggw-edit-warning" role="alert">{fileError}</div>}
      {!isFileMode && hasExistingComposerDraft && (
        <div className="eggw-edit-warning" role="alert" data-testid="edit-answer-overwrite-warning">
          The composer already has text. Choose Replace or Append; EggW will not overwrite it silently.
        </div>
      )}
    </OverlayPanel>
  );
}
