import { QueryClient } from "@tanstack/react-query";
import { beforeEach, describe, expect, it } from "vitest";
import type { AttachmentContentPart } from "./contentParts";
import {
  beginOptimisticSend,
  completeOptimisticSend,
  rollbackOptimisticSend,
  type SendMessageOperation,
} from "./messageOperations";
import { useAppStore } from "./store";
import { flattenTranscript, transcriptQueryKey, type TranscriptData } from "./transcript";

const attachment: AttachmentContentPart = {
  type: "attachment",
  input_id: "input-one",
  owner_thread_id: "thread-a",
  presentation: "file",
  mime_type: "text/plain",
  filename: "draft.txt",
  size_bytes: 5,
  sha256: "a".repeat(64),
  options: {},
};

function operation(): SendMessageOperation {
  return {
    threadId: "thread-a",
    operationId: "operation-a",
    content: [{ type: "text", text: "draft text" }, attachment],
    draft: "draft text",
    attachments: [attachment],
  };
}

function ids(client: QueryClient, threadId: string): string[] {
  return flattenTranscript(client.getQueryData<TranscriptData>(transcriptQueryKey(threadId)))
    .map((message) => message.id);
}

describe("optimistic send lifecycle", () => {
  beforeEach(() => {
    useAppStore.setState({ composerDraftByThread: {}, stagedAttachmentsByThread: {} });
  });

  it("clears the source composer and replaces only its temporary ID on success", () => {
    const client = new QueryClient();
    const send = operation();
    useAppStore.getState().setComposerDraft(send.threadId, send.draft);
    useAppStore.getState().setStagedAttachments(send.threadId, send.attachments);
    useAppStore.getState().setComposerDraft("thread-b", "keep b");

    beginOptimisticSend(client, send);
    expect(ids(client, send.threadId)).toEqual([send.operationId]);
    expect(useAppStore.getState().composerDraftByThread[send.threadId]).toBe("");
    expect(useAppStore.getState().stagedAttachmentsByThread[send.threadId]).toEqual([]);

    completeOptimisticSend(client, send, "persisted-message");
    expect(ids(client, send.threadId)).toEqual(["persisted-message"]);
    expect(useAppStore.getState().composerDraftByThread["thread-b"]).toBe("keep b");
  });

  it("removes only the failed operation and restores its source draft and attachments after navigation", () => {
    const client = new QueryClient();
    const send = operation();
    beginOptimisticSend(client, send);
    useAppStore.getState().setCurrentThreadId("thread-b");
    useAppStore.getState().setComposerDraft("thread-b", "thread b draft");

    rollbackOptimisticSend(client, send);

    expect(ids(client, send.threadId)).toEqual([]);
    expect(useAppStore.getState().composerDraftByThread[send.threadId]).toBe(send.draft);
    expect(useAppStore.getState().stagedAttachmentsByThread[send.threadId]).toEqual(send.attachments);
    expect(useAppStore.getState().composerDraftByThread["thread-b"]).toBe("thread b draft");
  });
});

describe("thread-scoped ephemeral state", () => {
  it("keeps connection status separate from run state across navigation", () => {
    useAppStore.setState({ streamingByThread: {}, connectionByThread: {}, currentThreadId: "thread-a" });
    const store = useAppStore.getState();
    store.patchThreadStreaming("thread-a", { isStreaming: true, invokeId: "invoke-a" });
    store.setThreadConnection("thread-a", { status: "reconnecting", lastEventSeq: 42 });
    store.setCurrentThreadId("thread-b");

    const state = useAppStore.getState();
    expect(state.streamingByThread["thread-a"]).toMatchObject({ isStreaming: true, invokeId: "invoke-a" });
    expect(state.connectionByThread["thread-a"]).toEqual({ status: "reconnecting", lastEventSeq: 42 });
    expect(state.currentThreadId).toBe("thread-b");
  });
});
