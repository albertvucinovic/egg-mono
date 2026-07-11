import type { QueryClient } from "@tanstack/react-query";
import type { AttachmentContentPart, EggMessageContent } from "./contentParts";
import { useAppStore, type Message } from "./store";
import {
  appendClientTranscriptMessage,
  removeClientTranscriptMessage,
  replaceClientTranscriptMessage,
} from "./transcript";

export function createClientOperationId(prefix: string): string {
  const suffix = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${suffix}`;
}

export interface SendMessageOperation {
  threadId: string;
  operationId: string;
  content: EggMessageContent;
  draft: string;
  attachments: AttachmentContentPart[];
}

export function beginOptimisticSend(
  queryClient: QueryClient,
  operation: SendMessageOperation,
  clearStoredInputs = true,
): void {
  const optimistic: Message = {
    id: operation.operationId,
    role: "user",
    content: operation.content,
    content_text: typeof operation.content === "string" ? operation.content : undefined,
    timestamp: new Date().toISOString(),
    client_only: "optimistic",
    client_operation_id: operation.operationId,
  };
  appendClientTranscriptMessage(queryClient, operation.threadId, optimistic);
  if (clearStoredInputs) {
    const store = useAppStore.getState();
    store.setComposerDraft(operation.threadId, "");
    store.setStagedAttachments(operation.threadId, []);
  }
}

export function completeOptimisticSend(
  queryClient: QueryClient,
  operation: SendMessageOperation,
  messageId: string,
): void {
  replaceClientTranscriptMessage(queryClient, operation.threadId, operation.operationId, messageId);
}

export function rollbackOptimisticSend(
  queryClient: QueryClient,
  operation: SendMessageOperation,
  restoredDraft = operation.draft,
): void {
  removeClientTranscriptMessage(queryClient, operation.threadId, operation.operationId);
  const store = useAppStore.getState();
  store.setComposerDraft(operation.threadId, restoredDraft);
  store.setStagedAttachments(operation.threadId, operation.attachments);
}
