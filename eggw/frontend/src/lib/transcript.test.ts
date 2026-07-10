import { QueryClient, type InfiniteData } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import type { Message } from "./store";
import {
  appendClientTranscriptMessage,
  flattenTranscript,
  reconcileTranscriptTail,
  removeClientTranscriptMessage,
  replaceClientTranscriptMessage,
  transcriptQueryKey,
  type TranscriptData,
  type TranscriptPage,
} from "./transcript";

function page(ids: string[], cursor: number, nextBefore: string | null = null): TranscriptPage {
  return {
    items: ids.map((id) => ({ id, role: "user", content: id })),
    snapshot_cursor: cursor,
    next_before: nextBefore,
  };
}

function data(pages: TranscriptPage[]): TranscriptData {
  return { pages, pageParams: pages.map((_page, index) => index === 0 ? null : `before-${index}`) };
}

describe("thread-keyed transcript cache", () => {
  it("deduplicates overlap while preserving chronological page order", () => {
    const newest = page(["overlap", "newest"], 20);
    newest.items[0].content = "authoritative overlap";
    const transcript = data([newest, page(["oldest", "overlap"], 10)]);
    const flattened = flattenTranscript(transcript);
    expect(flattened.map((message) => message.id)).toEqual(["oldest", "overlap", "newest"]);
    expect(flattened[1].content).toBe("authoritative overlap");
  });

  it("keeps independently paginated thread histories isolated", () => {
    const client = new QueryClient();
    client.setQueryData(transcriptQueryKey("thread-a"), data([page(["a-new"], 20, "a-old"), page(["a-old"], 10)]));
    client.setQueryData(transcriptQueryKey("thread-b"), data([page(["b-new"], 30)]));

    appendClientTranscriptMessage(client, "thread-a", {
      id: "a-temp",
      role: "user",
      content: "optimistic a",
      client_only: "optimistic",
      client_operation_id: "a-temp",
    });

    expect(flattenTranscript(client.getQueryData<InfiniteData<TranscriptPage>>(transcriptQueryKey("thread-a"))).map((message) => message.id))
      .toEqual(["a-old", "a-new", "a-temp"]);
    expect(flattenTranscript(client.getQueryData<InfiniteData<TranscriptPage>>(transcriptQueryKey("thread-b"))).map((message) => message.id))
      .toEqual(["b-new"]);
  });

  it("settles a navigation-racing send only in its originating thread", () => {
    const client = new QueryClient();
    client.setQueryData(transcriptQueryKey("thread-a"), data([page(["a-existing"], 1)]));
    client.setQueryData(transcriptQueryKey("thread-b"), data([page(["b-existing"], 1)]));

    appendClientTranscriptMessage(client, "thread-a", {
      id: "op-a",
      role: "user",
      content: "send from a",
      client_only: "optimistic",
      client_operation_id: "op-a",
    });
    replaceClientTranscriptMessage(client, "thread-a", "op-a", "persisted-a");

    expect(flattenTranscript(client.getQueryData<TranscriptData>(transcriptQueryKey("thread-a"))).map((message) => message.id))
      .toEqual(["a-existing", "persisted-a"]);
    expect(flattenTranscript(client.getQueryData<TranscriptData>(transcriptQueryKey("thread-b"))).map((message) => message.id))
      .toEqual(["b-existing"]);
  });

  it("rolls back the matching optimistic operation without removing another send", () => {
    const client = new QueryClient();
    client.setQueryData(transcriptQueryKey("thread-a"), data([page([], 0)]));
    const optimistic = (id: string): Message => ({
      id,
      role: "user",
      content: id,
      client_only: "optimistic",
      client_operation_id: id,
    });
    appendClientTranscriptMessage(client, "thread-a", optimistic("op-one"));
    appendClientTranscriptMessage(client, "thread-a", optimistic("op-two"));

    removeClientTranscriptMessage(client, "thread-a", "op-one");

    expect(flattenTranscript(client.getQueryData<TranscriptData>(transcriptQueryKey("thread-a"))).map((message) => message.id))
      .toEqual(["op-two"]);
  });

  it("preserves pending operations across an authoritative tail refresh", () => {
    const previous = page([], 4);
    previous.items.push({
      id: "pending",
      role: "user",
      content: "pending",
      client_only: "optimistic",
      client_operation_id: "pending",
    });
    const reconciled = reconcileTranscriptTail(page(["persisted"], 5), previous);
    expect(reconciled.items.map((message) => message.id)).toEqual(["persisted", "pending"]);
  });

  it("inserts local command output at its response timestamp", () => {
    const client = new QueryClient();
    const tail = page(["before", "after"], 2);
    tail.items[0].timestamp = "2026-01-01T00:00:00.000Z";
    tail.items[1].timestamp = "2026-01-01T00:00:02.000Z";
    client.setQueryData(transcriptQueryKey("thread-a"), data([tail]));

    appendClientTranscriptMessage(client, "thread-a", {
      id: "command",
      role: "system",
      content: "command",
      timestamp: "2026-01-01T00:00:01.000Z",
      client_only: "command",
      client_operation_id: "command-operation",
    });

    expect(flattenTranscript(client.getQueryData<TranscriptData>(transcriptQueryKey("thread-a"))).map((message) => message.id))
      .toEqual(["before", "command", "after"]);
  });
});
