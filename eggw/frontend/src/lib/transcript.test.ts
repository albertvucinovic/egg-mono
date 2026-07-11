import { QueryClient, type InfiniteData } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import type { Message } from "./store";
import {
  appendClientTranscriptMessage,
  flattenTranscript,
  mergeMessagesByTimestamp,
  reconcileTranscriptTail,
  removeClientTranscriptMessage,
  replaceClientTranscriptMessage,
  transcriptQueryKey,
  upsertTranscriptTailMessage,
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

  it("keeps command chronology through one and repeated authoritative refetches", () => {
    const before: Message = { id: "before", role: "user", timestamp: "2026-01-01T00:00:00Z" };
    const after: Message = { id: "after", role: "assistant", timestamp: "2026-01-01T00:00:02Z" };
    const command: Message = {
      id: "command",
      role: "system",
      timestamp: "2026-01-01T00:00:01Z",
      client_only: "command",
      client_operation_id: "command-op",
      command_name: "help",
    };
    const previous = page([], 1);
    previous.items = [before, command, after];

    const first = reconcileTranscriptTail({ ...page([], 2), items: [before, after] }, previous);
    const second = reconcileTranscriptTail({ ...page([], 3), items: [before, after] }, first);

    expect(first.items.map((message) => message.id)).toEqual(["before", "command", "after"]);
    expect(second.items.map((message) => message.id)).toEqual(["before", "command", "after"]);
  });

  it("stably places multiple commands and preserves equal timestamp ties", () => {
    const merged = mergeMessagesByTimestamp(
      [
        { id: "base-equal", role: "user", timestamp: "2026-01-01T00:00:01Z" },
        { id: "base-after", role: "assistant", timestamp: "2026-01-01T00:00:02Z" },
      ],
      [
        { id: "command-first", role: "system", timestamp: "2026-01-01T00:00:01Z", client_only: "command" },
        { id: "command-second", role: "system", timestamp: "2026-01-01T00:00:01Z", client_only: "command" },
      ],
    );

    expect(merged.map((message) => message.id)).toEqual([
      "base-equal", "command-first", "command-second", "base-after",
    ]);
  });

  it("keeps missing and invalid timestamp commands in deterministic local order", () => {
    const base: Message[] = [
      { id: "before", role: "user", timestamp: "2026-01-01T00:00:00Z" },
      { id: "after", role: "assistant", timestamp: "2026-01-01T00:00:02Z" },
    ];
    const commands: Message[] = [
      { id: "missing", role: "system", client_only: "command" },
      { id: "invalid", role: "system", timestamp: "not-a-date", client_only: "command" },
    ];

    const first = mergeMessagesByTimestamp(base, commands);
    const second = mergeMessagesByTimestamp(base, first.filter((message) => message.client_only === "command"));
    expect(first.map((message) => message.id)).toEqual(["before", "after", "missing", "invalid"]);
    expect(second.map((message) => message.id)).toEqual(["before", "after", "missing", "invalid"]);
  });

  it("places a timestamped command relative to authoritative messages with missing timestamps", () => {
    const merged = mergeMessagesByTimestamp(
      [
        { id: "missing-before", role: "user" },
        { id: "later", role: "assistant", timestamp: "2026-01-01T00:00:02Z" },
        { id: "missing-after", role: "assistant", timestamp: "invalid" },
      ],
      [{ id: "command", role: "system", timestamp: "2026-01-01T00:00:01Z", client_only: "command" }],
    );
    expect(merged.map((message) => message.id)).toEqual([
      "missing-before", "command", "later", "missing-after",
    ]);
  });

  it("deduplicates command lifecycle slots by operation without colliding with optimistic sends", () => {
    const command = (id: string, operation: string, commandName?: string): Message => ({
      id,
      role: "system",
      timestamp: "2026-01-01T00:00:01Z",
      client_only: "command",
      client_operation_id: operation,
      command_name: commandName,
    });
    const merged = mergeMessagesByTimestamp(
      [
        command("pending", "operation-a"),
        command("response", "operation-a", "help"),
        {
          id: "optimistic",
          role: "user",
          client_only: "optimistic",
          client_operation_id: "operation-a",
        },
      ],
      [
        command("duplicate-pending", "operation-a"),
        command("duplicate-response", "operation-a", "attachments"),
        command("other-operation", "operation-b"),
      ],
    );

    expect(merged.map((message) => message.id)).toEqual([
      "pending", "response", "optimistic", "other-operation",
    ]);
  });

  it("reconciles only the newest page while preserving optimistic state and page metadata", () => {
    const client = new QueryClient();
    const previous = data([
      {
        ...page(["tail-before", "tail-after"], 5, "older-cursor"),
        items: [
          { id: "tail-before", role: "user", timestamp: "2026-01-01T00:00:00Z" },
          { id: "command", role: "system", timestamp: "2026-01-01T00:00:01Z", client_only: "command", client_operation_id: "command-op", command_name: "help" },
          { id: "optimistic", role: "user", client_only: "optimistic", client_operation_id: "send-op" },
          { id: "tail-after", role: "assistant", timestamp: "2026-01-01T00:00:02Z" },
        ],
      },
      page(["older", "overlap"], 3),
    ]);
    client.setQueryData(transcriptQueryKey("thread-a"), previous);
    const fetched: TranscriptPage = {
      ...page([], 6, "older-cursor"),
      items: [
        { id: "tail-before", role: "user", timestamp: "2026-01-01T00:00:00Z" },
        { id: "tail-after", role: "assistant", timestamp: "2026-01-01T00:00:02Z" },
      ],
    };
    const reconciled = reconcileTranscriptTail(fetched, previous.pages[0]);
    const updated: TranscriptData = { ...previous, pages: [reconciled, previous.pages[1]] };

    expect(updated.pages[0].items.map((message) => message.id)).toEqual([
      "tail-before", "command", "tail-after", "optimistic",
    ]);
    expect(updated.pages[0].next_before).toBe("older-cursor");
    expect(updated.pages[1]).toBe(previous.pages[1]);
    expect(updated.pageParams).toBe(previous.pageParams);
  });

  it("upserts a canonical live message into the authoritative tail without disturbing pagination", () => {
    const client = new QueryClient();
    const transcript = data([
      page(["overlap", "newest"], 20, "older-cursor"),
      page(["oldest", "overlap"], 10),
    ]);
    transcript.pages[0].items.push({
      id: "pending",
      role: "user",
      content: "pending",
      client_only: "optimistic",
      client_operation_id: "operation-pending",
    });
    client.setQueryData(transcriptQueryKey("thread-a"), transcript);

    upsertTranscriptTailMessage(client, "thread-a", {
      id: "overlap",
      role: "assistant",
      content: "canonical event value",
      event_seq: 21,
    });

    const updated = client.getQueryData<TranscriptData>(transcriptQueryKey("thread-a"))!;
    expect(updated.pageParams).toEqual([null, "before-1"]);
    expect(updated.pages[0].next_before).toBe("older-cursor");
    expect(updated.pages[0].items.map((message) => message.id)).toEqual(["overlap", "newest", "pending"]);
    expect(updated.pages[0].items[0].content).toBe("canonical event value");
    expect(updated.pages[1].items.map((message) => message.id)).toEqual(["oldest"]);
  });

  it("installs msg.create synchronously before a delayed HTTP response resolves", async () => {
    const client = new QueryClient();
    client.setQueryData(transcriptQueryKey("thread-a"), data([page(["before"], 10)]));
    let resolveFetch!: (value: TranscriptPage) => void;
    const delayedFetch = new Promise<TranscriptPage>((resolve) => { resolveFetch = resolve; });

    upsertTranscriptTailMessage(client, "thread-a", {
      id: "assistant-tool-call",
      role: "assistant",
      tool_calls: [{ id: "call-a", name: "bash", arguments: "{}" }],
      event_seq: 11,
    });
    expect(flattenTranscript(client.getQueryData<TranscriptData>(transcriptQueryKey("thread-a")))
      .map((message) => message.id)).toContain("assistant-tool-call");

    resolveFetch(page(["before"], 10));
    const stale = await delayedFetch;
    const current = client.getQueryData<TranscriptData>(transcriptQueryKey("thread-a"))!;
    const reconciled = reconcileTranscriptTail(stale, current.pages[0]);
    expect(reconciled.items.map((message) => message.id)).toEqual(["before", "assistant-tool-call"]);
  });

  it("retains a consumed msg.create across stale refetch and deduplicates once covered", () => {
    const eventMessage: Message = {
      id: "event-message",
      role: "assistant",
      content: "durable tool call",
      event_seq: 12,
      tool_calls: [{ id: "call-a", name: "bash", arguments: "{}" }],
    };
    const previous = page(["before"], 10);
    previous.items.push(eventMessage);

    const stale = reconcileTranscriptTail(page(["before"], 11), previous);
    expect(stale.items.map((message) => message.id)).toEqual(["before", "event-message"]);

    const covered = page(["before", "event-message"], 12);
    covered.items[1].content = "normalized server value";
    const reconciled = reconcileTranscriptTail(covered, stale);
    expect(reconciled.items.map((message) => message.id)).toEqual(["before", "event-message"]);
    expect(reconciled.items[1].content).toBe("normalized server value");
  });
});
