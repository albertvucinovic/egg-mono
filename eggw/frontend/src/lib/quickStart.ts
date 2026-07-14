import type { AttachmentContentPart } from "./contentParts";

export type QuickStartThread = Readonly<{
  id: string;
  initial_draft?: unknown;
  initial_attachment?: unknown;
  initial_error?: unknown;
}>;

type QuickStartStore = Readonly<{
  setComposerDraft: (threadId: string, text: string) => void;
  appendStagedAttachments: (threadId: string, attachments: AttachmentContentPart[]) => void;
  addSystemLog: (message: string, type?: "info" | "error" | "success") => void;
}>;

/** Apply backend launch metadata to the existing thread-scoped composer owners. */
export function applyQuickStartThread(thread: QuickStartThread, store: QuickStartStore): void {
  if (typeof thread.initial_draft === "string") {
    store.setComposerDraft(thread.id, thread.initial_draft);
  }
  if (
    thread.initial_attachment &&
    typeof thread.initial_attachment === "object" &&
    (thread.initial_attachment as { type?: unknown }).type === "attachment"
  ) {
    store.appendStagedAttachments(thread.id, [thread.initial_attachment as AttachmentContentPart]);
  }
  if (typeof thread.initial_error === "string" && thread.initial_error) {
    store.addSystemLog(thread.initial_error, "error");
  }
}
