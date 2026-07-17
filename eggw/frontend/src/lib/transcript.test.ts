import { QueryClient, type InfiniteData } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import type { Message } from "./store";
import {
  appendClientTranscriptMessage,
  cancelTranscriptRequests,
  flattenTranscript,
  mergeMessagesByTimestamp,
  reconcileTranscriptTail,
  invalidateTranscriptAuthoritatively,
  mergeOlderTranscriptPage,
  mergeRefreshedTranscriptTail,
  removeClientTranscriptMessage,
  replaceClientTranscriptMessage,
  transcriptQueryKey,
  patchTranscriptMessage,
  upsertTranscriptTailMessage,
  fetchOlderTranscriptPage,
  refreshTranscriptTail,
  reloadTranscriptFromCommand,
  rewindTranscriptForContinuation,
  transcriptGeneration,
  transcriptInfiniteQueryOptions,
  type TranscriptData,
  type TranscriptPage,
} from "./transcript";
import {
  instrumentTranscriptCosts,
  replaceTranscriptPageData,
  transcriptIndex,
  transcriptRenderWindow,
} from "./transcriptIndex";

function page(
  ids: string[],
  cursor: number,
  nextBefore: string | null = null,
): TranscriptPage {
  return {
    items: ids.map((id) => ({ id, role: "user", content: id })),
    snapshot_cursor: cursor,
    next_before: nextBefore,
  };
}

function data(pages: TranscriptPage[]): TranscriptData {
  return {
    pages,
    pageParams: pages.map((_page, index) =>
      index === 0 ? null : `before-${index}`,
    ),
  };
}

describe("thread-keyed transcript cache", () => {
  it("deduplicates overlap while preserving chronological page order", () => {
    const newest = page(["overlap", "newest"], 20);
    newest.items[0].content = "authoritative overlap";
    const transcript = data([newest, page(["oldest", "overlap"], 10)]);
    const flattened = flattenTranscript(transcript);
    expect(flattened.map((message) => message.id)).toEqual([
      "oldest",
      "overlap",
      "newest",
    ]);
    expect(flattened[1].content).toBe("authoritative overlap");
  });

  it("retains every loaded page when an ordinary refresh returns a shorter chain", () => {
    const previous = data([
      page(["tail-old"], 10, "cursor-1"),
      page(["middle"], 8, "cursor-2"),
      page(["oldest"], 6),
    ]);
    const fetched = page(["tail-fresh"], 12, null);

    const retained = mergeRefreshedTranscriptTail(fetched, previous);

    expect(retained.pages.map((entry) => entry.items[0]?.id)).toEqual([
      "tail-fresh",
    ]);
    expect(flattenTranscript(retained).map((message) => message.id)).toEqual([
      "oldest",
      "middle",
      "tail-old",
      "tail-fresh",
    ]);
    expect(retained.pages[0].next_before).toBe("cursor-1");
  });

  it("changes render ownership for same-id same-create-seq projection payload updates", () => {
    const initial = page(["projection-message"], 10);
    initial.items[0] = {
      id: "projection-message",
      role: "user",
      content: "before refresh",
      event_seq: 7,
    };
    const transcript = data([initial]);
    const firstWindow = transcriptRenderWindow(transcript, null);

    const fetched = page(["projection-message"], 10);
    fetched.items[0] = {
      id: "projection-message",
      role: "user",
      content: "after refresh",
      event_seq: 7,
      output_optimizer: {
        optimized: true,
        summary: "optimized on refresh",
      },
      consumed_by_tool_name: "get_user_message_while_preserving_llm_turn",
      consumed_by_tool_call_id: "call-refresh",
    };
    const refreshed = mergeRefreshedTranscriptTail(fetched, transcript);
    const refreshedWindow = transcriptRenderWindow(refreshed, null);

    expect(refreshedWindow.messages).not.toBe(firstWindow.messages);
    expect(refreshedWindow.messages[0]).toMatchObject({
      content: "after refresh",
      event_seq: 7,
      output_optimizer: {
        optimized: true,
        summary: "optimized on refresh",
      },
      consumed_by_tool_call_id: "call-refresh",
    });

    const equivalentFetched = {
      ...fetched,
      items: [{
        ...fetched.items[0],
        output_optimizer: { ...fetched.items[0].output_optimizer },
      }],
    };
    const equivalent = mergeRefreshedTranscriptTail(equivalentFetched, refreshed);
    expect(transcriptRenderWindow(equivalent, null).messages).toBe(
      refreshedWindow.messages,
    );
  });

  it("treats equal-cursor snapshots as one authority instead of retaining phantom history", () => {
    const previous = data([page(["old-projection"], 10, null)]);
    const fetched = page(["new-projection"], 10, null);

    const retained = mergeRefreshedTranscriptTail(fetched, previous);

    expect(retained.pages).toHaveLength(1);
    expect(flattenTranscript(retained).map((message) => message.id)).toEqual([
      "new-projection",
    ]);
  });

  it("moves only the displaced prefix into retained bridge pages on overlap", () => {
    const previous = data([
      page(["old-0", "old-1", "overlap", "old-tail"], 10, "old-0"),
      page(["older-page"], 8),
    ]);
    const fetched = page(["overlap", "old-tail", "new-tail"], 11, "old-0");

    const retained = mergeRefreshedTranscriptTail(fetched, previous);

    expect(retained.pages[0].items.map((message) => message.id)).toEqual([
      "overlap",
      "old-tail",
      "new-tail",
    ]);
    expect(retained.pages).toHaveLength(1);
    expect(flattenTranscript(retained).map((message) => message.id)).toEqual([
      "older-page",
      "old-0",
      "old-1",
      "overlap",
      "old-tail",
      "new-tail",
    ]);
  });

  it("bounds retained displaced tail pages while preserving all history", () => {
    const previousTail = page(
      Array.from({ length: 300 }, (_, index) => `old-${index}`),
      10,
      "old-0",
    );
    const first = mergeRefreshedTranscriptTail(
      page(["new-0"], 11, "old-0"),
      data([previousTail]),
    );
    const second = mergeRefreshedTranscriptTail(
      page(["new-1"], 12, "old-0"),
      first,
    );

    expect(second.pages.every((entry) => entry.items.length <= 300)).toBe(true);
    expect(flattenTranscript(second)).toHaveLength(302);
    expect(flattenTranscript(second).map((message) => message.id)).toEqual([
      ...previousTail.items.map((message) => message.id),
      "new-0",
      "new-1",
    ]);
  });

  it("uses optimistic operation identity as refresh overlap without duplicating it", () => {
    const optimistic: Message = {
      id: "temp-send",
      role: "user",
      content: "hello",
      client_only: "optimistic",
      client_operation_id: "send-op",
    };
    const previous = page(["before"], 10, "before");
    previous.items.push(optimistic, {
      id: "event-after",
      role: "assistant",
      event_seq: 11,
    });
    const fetched = page(["persisted-send", "event-after"], 12, "before");
    fetched.items[0].client_only = "optimistic";
    fetched.items[0].client_operation_id = "send-op";

    const retained = mergeRefreshedTranscriptTail(fetched, data([previous]));

    expect(flattenTranscript(retained).map((message) => message.id)).toEqual([
      "before",
      "persisted-send",
      "event-after",
    ]);
  });

  it("discards loaded pages only through explicit destructive authority", () => {
    const client = new QueryClient();
    client.setQueryData(
      transcriptQueryKey("thread-a"),
      data([page(["tail"], 2, "cursor"), page(["old"], 1)]),
    );

    invalidateTranscriptAuthoritatively(client, "thread-a");

    expect(client.getQueryData(transcriptQueryKey("thread-a"))).toBeUndefined();
  });

  it("rejects a stale tail cursor without changing any loaded page", () => {
    const previous = data([
      page(["new-tail"], 20, "cursor-1"),
      page(["old"], 10),
    ]);
    expect(
      mergeRefreshedTranscriptTail(page(["stale-tail"], 19), previous),
    ).toBe(previous);
  });

  it("does bounded page-metadata work without reading 5M-token-equivalent message bodies", () => {
    const bodyReads = { count: 0 };
    const guardedMessage = (id: string): Message => {
      const message: Message = { id, role: "user" };
      Object.defineProperty(message, "content", {
        enumerable: true,
        get: () => {
          bodyReads.count += 1;
          throw new Error("retention must not inspect message content");
        },
      });
      return message;
    };
    const previous: TranscriptData = {
      pages: Array.from({ length: 128 }, (_, index) => ({
        items: [guardedMessage(`page-${index}`)],
        snapshot_cursor: 1,
        next_before: index < 127 ? `cursor-${index + 1}` : null,
      })),
      pageParams: Array.from({ length: 128 }, (_, index) =>
        index ? `cursor-${index}` : null,
      ),
    };
    const fetched = data([page(["fresh-tail"], 2, "cursor-1")]);

    const retained = mergeRefreshedTranscriptTail(fetched.pages[0], previous);

    expect(retained.pages).toHaveLength(1);
    expect(bodyReads.count).toBe(0);
    expect(flattenTranscript(retained).at(0)?.id).toBe("page-127");
  });

  it("preserves backend pagination beyond retained bridge pages", () => {
    const previous = data([page(["old-tail"], 10, "backend-cursor")]);

    const retained = mergeRefreshedTranscriptTail(
      page(["fresh-tail"], 11, null),
      previous,
    );

    expect(retained.pages).toHaveLength(1);
    expect(retained.pages[0].next_before).toBe("backend-cursor");
  });

  it("appends one older page only at the current authoritative frontier", () => {
    const current = data([
      page(["tail"], 10, "cursor-1"),
      { ...page(["bridge"], 10, "cursor-2"), retained_tail_bridge: true },
    ]);
    const fetched = page(["older"], 10, null);

    const merged = mergeOlderTranscriptPage(current, "cursor-2", fetched)!;

    expect(merged.pages.map((entry) => entry.items[0]?.id)).toEqual(["tail"]);
    expect(flattenTranscript(merged).map((message) => message.id)).toEqual([
      "older",
      "bridge",
      "tail",
    ]);
    expect(mergeOlderTranscriptPage(current, "stale-cursor", fetched)).toBe(
      current,
    );
  });

  it("keeps independently paginated thread histories isolated", () => {
    const client = new QueryClient();
    client.setQueryData(
      transcriptQueryKey("thread-a"),
      data([page(["a-new"], 20, "a-old"), page(["a-old"], 10)]),
    );
    client.setQueryData(
      transcriptQueryKey("thread-b"),
      data([page(["b-new"], 30)]),
    );

    appendClientTranscriptMessage(client, "thread-a", {
      id: "a-temp",
      role: "user",
      content: "optimistic a",
      client_only: "optimistic",
      client_operation_id: "a-temp",
    });

    expect(
      flattenTranscript(
        client.getQueryData<InfiniteData<TranscriptPage>>(
          transcriptQueryKey("thread-a"),
        ),
      ).map((message) => message.id),
    ).toEqual(["a-old", "a-new", "a-temp"]);
    expect(
      flattenTranscript(
        client.getQueryData<InfiniteData<TranscriptPage>>(
          transcriptQueryKey("thread-b"),
        ),
      ).map((message) => message.id),
    ).toEqual(["b-new"]);
  });

  it("settles a navigation-racing send only in its originating thread", () => {
    const client = new QueryClient();
    client.setQueryData(
      transcriptQueryKey("thread-a"),
      data([page(["a-existing"], 1)]),
    );
    client.setQueryData(
      transcriptQueryKey("thread-b"),
      data([page(["b-existing"], 1)]),
    );

    appendClientTranscriptMessage(client, "thread-a", {
      id: "op-a",
      role: "user",
      content: "send from a",
      client_only: "optimistic",
      client_operation_id: "op-a",
    });
    replaceClientTranscriptMessage(client, "thread-a", "op-a", "persisted-a");

    expect(
      flattenTranscript(
        client.getQueryData<TranscriptData>(transcriptQueryKey("thread-a")),
      ).map((message) => message.id),
    ).toEqual(["a-existing", "persisted-a"]);
    expect(
      flattenTranscript(
        client.getQueryData<TranscriptData>(transcriptQueryKey("thread-b")),
      ).map((message) => message.id),
    ).toEqual(["b-existing"]);
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

    expect(
      flattenTranscript(
        client.getQueryData<TranscriptData>(transcriptQueryKey("thread-a")),
      ).map((message) => message.id),
    ).toEqual(["op-two"]);
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
    const reconciled = reconcileTranscriptTail(
      page(["persisted"], 5),
      previous,
    );
    expect(reconciled.items.map((message) => message.id)).toEqual([
      "persisted",
      "pending",
    ]);
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

    expect(
      flattenTranscript(
        client.getQueryData<TranscriptData>(transcriptQueryKey("thread-a")),
      ).map((message) => message.id),
    ).toEqual(["before", "command", "after"]);
  });

  it("keeps command chronology through one and repeated authoritative refetches", () => {
    const before: Message = {
      id: "before",
      role: "user",
      timestamp: "2026-01-01T00:00:00Z",
    };
    const after: Message = {
      id: "after",
      role: "assistant",
      timestamp: "2026-01-01T00:00:02Z",
    };
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

    const first = reconcileTranscriptTail(
      { ...page([], 2), items: [before, after] },
      previous,
    );
    const second = reconcileTranscriptTail(
      { ...page([], 3), items: [before, after] },
      first,
    );

    expect(first.items.map((message) => message.id)).toEqual([
      "before",
      "command",
      "after",
    ]);
    expect(second.items.map((message) => message.id)).toEqual([
      "before",
      "command",
      "after",
    ]);
  });

  it("stably places multiple commands and preserves equal timestamp ties", () => {
    const merged = mergeMessagesByTimestamp(
      [
        { id: "base-equal", role: "user", timestamp: "2026-01-01T00:00:01Z" },
        {
          id: "base-after",
          role: "assistant",
          timestamp: "2026-01-01T00:00:02Z",
        },
      ],
      [
        {
          id: "command-first",
          role: "system",
          timestamp: "2026-01-01T00:00:01Z",
          client_only: "command",
        },
        {
          id: "command-second",
          role: "system",
          timestamp: "2026-01-01T00:00:01Z",
          client_only: "command",
        },
      ],
    );

    expect(merged.map((message) => message.id)).toEqual([
      "base-equal",
      "command-first",
      "command-second",
      "base-after",
    ]);
  });

  it("keeps missing and invalid timestamp commands in deterministic local order", () => {
    const base: Message[] = [
      { id: "before", role: "user", timestamp: "2026-01-01T00:00:00Z" },
      { id: "after", role: "assistant", timestamp: "2026-01-01T00:00:02Z" },
    ];
    const commands: Message[] = [
      { id: "missing", role: "system", client_only: "command" },
      {
        id: "invalid",
        role: "system",
        timestamp: "not-a-date",
        client_only: "command",
      },
    ];

    const first = mergeMessagesByTimestamp(base, commands);
    const second = mergeMessagesByTimestamp(
      base,
      first.filter((message) => message.client_only === "command"),
    );
    expect(first.map((message) => message.id)).toEqual([
      "before",
      "after",
      "missing",
      "invalid",
    ]);
    expect(second.map((message) => message.id)).toEqual([
      "before",
      "after",
      "missing",
      "invalid",
    ]);
  });

  it("places a timestamped command relative to authoritative messages with missing timestamps", () => {
    const merged = mergeMessagesByTimestamp(
      [
        { id: "missing-before", role: "user" },
        { id: "later", role: "assistant", timestamp: "2026-01-01T00:00:02Z" },
        { id: "missing-after", role: "assistant", timestamp: "invalid" },
      ],
      [
        {
          id: "command",
          role: "system",
          timestamp: "2026-01-01T00:00:01Z",
          client_only: "command",
        },
      ],
    );
    expect(merged.map((message) => message.id)).toEqual([
      "missing-before",
      "command",
      "later",
      "missing-after",
    ]);
  });

  it("deduplicates command lifecycle slots by operation without colliding with optimistic sends", () => {
    const command = (
      id: string,
      operation: string,
      commandName?: string,
    ): Message => ({
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
      "pending",
      "response",
      "optimistic",
      "other-operation",
    ]);
  });

  it("reconciles only the newest page while preserving optimistic state and page metadata", () => {
    const client = new QueryClient();
    const previous = data([
      {
        ...page(["tail-before", "tail-after"], 5, "older-cursor"),
        items: [
          {
            id: "tail-before",
            role: "user",
            timestamp: "2026-01-01T00:00:00Z",
          },
          {
            id: "command",
            role: "system",
            timestamp: "2026-01-01T00:00:01Z",
            client_only: "command",
            client_operation_id: "command-op",
            command_name: "help",
          },
          {
            id: "optimistic",
            role: "user",
            client_only: "optimistic",
            client_operation_id: "send-op",
          },
          {
            id: "tail-after",
            role: "assistant",
            timestamp: "2026-01-01T00:00:02Z",
          },
        ],
      },
      page(["older", "overlap"], 3),
    ]);
    client.setQueryData(transcriptQueryKey("thread-a"), previous);
    const fetched: TranscriptPage = {
      ...page([], 6, "older-cursor"),
      items: [
        { id: "tail-before", role: "user", timestamp: "2026-01-01T00:00:00Z" },
        {
          id: "tail-after",
          role: "assistant",
          timestamp: "2026-01-01T00:00:02Z",
        },
      ],
    };
    const reconciled = reconcileTranscriptTail(fetched, previous.pages[0]);
    const updated: TranscriptData = {
      ...previous,
      pages: [reconciled, previous.pages[1]],
    };

    expect(updated.pages[0].items.map((message) => message.id)).toEqual([
      "tail-before",
      "command",
      "tail-after",
      "optimistic",
    ]);
    expect(updated.pages[0].next_before).toBe("older-cursor");
    expect(updated.pages[1]).toBe(previous.pages[1]);
    expect(updated.pageParams).toBe(previous.pageParams);
  });

  it("keeps no-timestamp optimistic entries at the local tail on reconciliation", () => {
    const previous = page(["before"], 5);
    previous.items.push({
      id: "optimistic",
      role: "user",
      client_only: "optimistic",
      client_operation_id: "send-op",
    });

    const reconciled = reconcileTranscriptTail(
      page(["before", "after"], 6),
      previous,
    );

    expect(reconciled.items.map((message) => message.id)).toEqual([
      "before",
      "after",
      "optimistic",
    ]);
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

    const updated = client.getQueryData<TranscriptData>(
      transcriptQueryKey("thread-a"),
    )!;
    expect(updated.pageParams).toEqual([null]);
    expect(updated.pages[0].next_before).toBe("older-cursor");
    expect(updated.pages[0].items.map((message) => message.id)).toEqual([
      "overlap",
      "newest",
      "pending",
    ]);
    expect(updated.pages[0].items[0].content).toBe("canonical event value");
    expect(flattenTranscript(updated).map((message) => message.id)).toContain(
      "oldest",
    );
  });

  it("installs msg.create synchronously before a delayed HTTP response resolves", async () => {
    const client = new QueryClient();
    client.setQueryData(
      transcriptQueryKey("thread-a"),
      data([page(["before"], 10)]),
    );
    let resolveFetch!: (value: TranscriptPage) => void;
    const delayedFetch = new Promise<TranscriptPage>((resolve) => {
      resolveFetch = resolve;
    });

    upsertTranscriptTailMessage(client, "thread-a", {
      id: "assistant-tool-call",
      role: "assistant",
      tool_calls: [{ id: "call-a", name: "bash", arguments: "{}" }],
      event_seq: 11,
    });
    expect(
      flattenTranscript(
        client.getQueryData<TranscriptData>(transcriptQueryKey("thread-a")),
      ).map((message) => message.id),
    ).toContain("assistant-tool-call");

    resolveFetch(page(["before"], 10));
    const stale = await delayedFetch;
    const current = client.getQueryData<TranscriptData>(
      transcriptQueryKey("thread-a"),
    )!;
    const reconciled = reconcileTranscriptTail(stale, current.pages[0]);
    expect(reconciled.items.map((message) => message.id)).toEqual([
      "before",
      "assistant-tool-call",
    ]);
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
    expect(stale.items.map((message) => message.id)).toEqual([
      "before",
      "event-message",
    ]);

    const covered = page(["before", "event-message"], 12);
    covered.items[1].content = "normalized server value";
    const reconciled = reconcileTranscriptTail(covered, stale);
    expect(reconciled.items.map((message) => message.id)).toEqual([
      "before",
      "event-message",
    ]);
    expect(reconciled.items[1].content).toBe("normalized server value");
  });
  it("applies consumed get-user edit metadata in place across transcript pages", () => {
    const client = new QueryClient();
    const previous = page(["before", "answer", "after"], 20, "older-cursor");
    previous.items[0].timestamp = "2026-07-14T00:00:00Z";
    previous.items[1].timestamp = "2026-07-14T00:00:01Z";
    previous.items[2].timestamp = "2026-07-14T00:00:02Z";
    client.setQueryData(
      transcriptQueryKey("thread-a"),
      data([previous, page(["oldest"], 10)]),
    );

    patchTranscriptMessage(
      client,
      "thread-a",
      "answer",
      {
        content: "Continue",
        consumed_by_tool_name: "get_user_message_while_preserving_llm_turn",
        consumed_by_tool_call_id: "call-get-user",
      },
      21,
    );

    const updated = client.getQueryData<TranscriptData>(
      transcriptQueryKey("thread-a"),
    )!;
    expect(updated.pages[0].items.map((message) => message.id)).toEqual([
      "before",
      "answer",
      "after",
    ]);
    expect(updated.pages[0].items[1]).toMatchObject({
      role: "user",
      content: "Continue",
      consumed_by_tool_call_id: "call-get-user",
      event_seq: 21,
    });
    expect(flattenTranscript(updated).map((message) => message.id)).toContain(
      "oldest",
    );

    const staleFetched = page(
      ["before", "answer", "after"],
      20,
      "older-cursor",
    );
    staleFetched.items[0].timestamp = "2026-07-14T00:00:00Z";
    staleFetched.items[1].timestamp = "2026-07-14T00:00:01Z";
    staleFetched.items[1].content = "stale create-only answer";
    staleFetched.items[2].timestamp = "2026-07-14T00:00:02Z";
    const stale = reconcileTranscriptTail(staleFetched, updated.pages[0]);
    expect(stale.items.map((message) => message.id)).toEqual([
      "before",
      "answer",
      "after",
    ]);
    expect(stale.items[1]).toMatchObject({
      content: "Continue",
      consumed_by_tool_call_id: "call-get-user",
      event_seq: 21,
    });

    const coveredFetched = page(
      ["before", "answer", "after"],
      21,
      "older-cursor",
    );
    coveredFetched.items[1].content = "canonical covered answer";
    const covered = reconcileTranscriptTail(coveredFetched, stale);
    expect(covered.items[1].content).toBe("canonical covered answer");
    expect(covered.items[1].event_seq).toBeUndefined();
    expect(covered.items[1].consumed_by_tool_call_id).toBeUndefined();
  });

  it("restores the exact older overlap owner after replacing a newer page", () => {
    const transcript = data([
      page(["tail", "overlap"], 3),
      page(["middle", "overlap"], 2),
      page(["oldest", "overlap"], 1),
    ]);
    // Build the ownership stack, then replace the newest owner without the ID.
    transcriptIndex(transcript);
    const costs = instrumentTranscriptCosts(transcript);
    const tail = transcript.pages[0];
    const updated = replaceTranscriptPageData(transcript, tail, {
      ...tail,
      items: tail.items.filter((message) => message.id !== "overlap"),
    });

    const matches = flattenTranscript(updated).filter(
      (message) => message.id === "overlap",
    );
    expect(matches).toHaveLength(1);
    expect(matches[0].content).toBe("overlap");
    expect(flattenTranscript(updated).map((message) => message.id)).toEqual([
      "oldest",
      "middle",
      "overlap",
      "tail",
    ]);
    expect(transcriptIndex(updated).totalMessages).toBe(4);
    expect(costs).toEqual({ pageVisits: 2, messageVisits: 3 });
  });

  it("bounds new SSE upsert work on 24 multi-megabyte pages", () => {
    const client = new QueryClient();
    const transcript = data(
      Array.from({ length: 24 }, (_, pageIndex) => ({
        ...page(
          Array.from(
            { length: 300 },
            (_, itemIndex) => `upsert-new-${pageIndex}-${itemIndex}`,
          ),
          pageIndex,
        ),
        items: Array.from({ length: 300 }, (_, itemIndex) => ({
          id: `upsert-new-${pageIndex}-${itemIndex}`,
          role: itemIndex % 2 ? "assistant" : "user",
          content: `payload-${pageIndex}-${itemIndex}-${"x".repeat(2_000)}`,
        })),
      })),
    );
    transcript.pages[0].items.push({
      id: "upsert-new-23-150",
      role: "assistant",
      content: "tail overlap authority",
    });
    client.setQueryData(transcriptQueryKey("large-new-upsert"), transcript);
    const costs = instrumentTranscriptCosts(transcript);

    upsertTranscriptTailMessage(client, "large-new-upsert", {
      id: "genuinely-new-sse-id",
      role: "assistant",
      content: "live event",
      event_seq: 50,
    });

    const updated = client.getQueryData<TranscriptData>(
      transcriptQueryKey("large-new-upsert"),
    )!;
    expect(transcriptIndex(updated).totalMessages).toBe(7_201);
    expect(costs.pageVisits).toBe(2);
    expect(costs.messageVisits).toBeLessThanOrEqual(603);
    const matches = flattenTranscript(updated).filter(
      (message) => message.id === "genuinely-new-sse-id",
    );
    expect(matches).toHaveLength(1);
    expect(matches[0].content).toBe("live event");
    expect(flattenTranscript(updated)[0].id).toBe("upsert-new-23-0");
    const overlapMatches = flattenTranscript(updated).filter(
      (message) => message.id === "upsert-new-23-150",
    );
    expect(overlapMatches).toHaveLength(1);
    expect(overlapMatches[0].content).toBe("tail overlap authority");
  });

  it("moves an old-page SSE ID to the tail with bounded exact ownership", () => {
    const client = new QueryClient();
    const transcript = data(
      Array.from({ length: 24 }, (_, pageIndex) => ({
        ...page(
          Array.from(
            { length: 300 },
            (_, itemIndex) => `upsert-move-${pageIndex}-${itemIndex}`,
          ),
          pageIndex,
        ),
        items: Array.from({ length: 300 }, (_, itemIndex) => ({
          id: `upsert-move-${pageIndex}-${itemIndex}`,
          role: itemIndex % 2 ? "assistant" : "user",
          content: `payload-${pageIndex}-${itemIndex}-${"x".repeat(2_000)}`,
        })),
      })),
    );
    client.setQueryData(transcriptQueryKey("large-move-upsert"), transcript);
    const costs = instrumentTranscriptCosts(transcript);
    const movedId = "upsert-move-23-150";

    upsertTranscriptTailMessage(client, "large-move-upsert", {
      id: movedId,
      role: "assistant",
      content: "authoritative live value",
      event_seq: 51,
    });

    const updated = client.getQueryData<TranscriptData>(
      transcriptQueryKey("large-move-upsert"),
    )!;
    expect(transcriptIndex(updated).totalMessages).toBe(7_200);
    expect(costs.pageVisits).toBe(4);
    expect(costs.messageVisits).toBeLessThanOrEqual(1_200);
    const flattened = flattenTranscript(updated);
    const matches = flattened.filter((message) => message.id === movedId);
    expect(matches).toHaveLength(1);
    expect(matches[0]).toMatchObject({
      content: "authoritative live value",
      event_seq: 51,
    });
    expect(flattened.at(-1)?.id).toBe(movedId);
    expect(flattened[0].id).toBe("upsert-move-23-0");
  });

  it("renders a bounded tail from 24 multi-megabyte pages without visiting old pages", () => {
    const transcript = data(
      Array.from({ length: 24 }, (_, pageIndex) => ({
        ...page(
          Array.from(
            { length: 300 },
            (_, itemIndex) => `large-${pageIndex}-${itemIndex}`,
          ),
          pageIndex,
        ),
        items: Array.from({ length: 300 }, (_, itemIndex) => ({
          id: `large-${pageIndex}-${itemIndex}`,
          role: itemIndex % 2 ? "assistant" : "user",
          content: `payload-${pageIndex}-${itemIndex}-${"x".repeat(2_000)}`,
        })),
      })),
    );
    const costs = instrumentTranscriptCosts(transcript);

    const window = transcriptRenderWindow(transcript, null);

    expect(transcriptIndex(transcript).totalMessages).toBe(7_200);
    expect(window.messages).toHaveLength(60);
    expect(costs.pageVisits).toBe(1);
    expect(costs.messageVisits).toBe(60);
  });

  it("shares one initial tail authority across a strict-mode observer remount", async () => {
    const client = new QueryClient();
    let resolveFetch!: (value: Response) => void;
    const fetchMock = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const options = transcriptInfiniteQueryOptions("strict-remount", client);

    // A no-observer ensure and the remounted observer must join one TanStack
    // request. Consuming its observer signal would cancel/restart this boundary.
    const first = client.ensureInfiniteQueryData(options);
    const second = client.ensureInfiniteQueryData(options);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    resolveFetch(
      new Response(
        JSON.stringify({
          items: [{ id: "initial", role: "user" }],
          snapshot_cursor: 1,
          next_before: null,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    await expect(first).resolves.toMatchObject({
      pages: [{ snapshot_cursor: 1 }],
    });
    await expect(second).resolves.toMatchObject({
      pages: [{ snapshot_cursor: 1 }],
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });

  it("keeps the cache-owned tail when initial observer setup overlaps it", async () => {
    const client = new QueryClient();
    client.setQueryData(
      transcriptQueryKey("initial-cache-owner"),
      data([page(["cache-owned"], 2)]),
    );
    const options = transcriptInfiniteQueryOptions(
      "initial-cache-owner",
      client,
    );
    const queryFn = options.queryFn as (context: {
      pageParam: string | null;
    }) => Promise<TranscriptPage>;
    let resolveFetch!: (value: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveFetch = resolve;
          }),
      ),
    );

    try {
      const pending = queryFn({ pageParam: null });
      resolveFetch(
        new Response(
          JSON.stringify({
            items: [{ id: "duplicate-initial", role: "user" }],
            snapshot_cursor: 1,
            next_before: null,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );

      await expect(pending).resolves.toMatchObject({
        items: [{ id: "cache-owned" }],
        snapshot_cursor: 2,
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("coalesces concurrent current-generation tail refreshes", async () => {
    const client = new QueryClient();
    client.setQueryData(
      transcriptQueryKey("coalesced-tail"),
      data([page(["old"], 1)]),
    );
    let resolveFetch!: (value: Response) => void;
    const fetchMock = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const first = refreshTranscriptTail(client, "coalesced-tail");
    const second = refreshTranscriptTail(client, "coalesced-tail");
    expect(second).toBe(first);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    resolveFetch(
      new Response(
        JSON.stringify({
          items: [{ id: "new", role: "assistant" }],
          snapshot_cursor: 2,
          next_before: null,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    await Promise.all([first, second]);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(
      flattenTranscript(
        client.getQueryData<TranscriptData>(
          transcriptQueryKey("coalesced-tail"),
        ),
      ).map((message) => message.id),
    ).toEqual(["old", "new"]);
    vi.unstubAllGlobals();
  });

  it("starts a new refresh after the current-generation request settles", async () => {
    const client = new QueryClient();
    client.setQueryData(
      transcriptQueryKey("sequential-tail"),
      data([page(["old"], 1)]),
    );
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [{ id: "new-1", role: "assistant" }],
            snapshot_cursor: 2,
            next_before: null,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [{ id: "new-2", role: "assistant" }],
            snapshot_cursor: 3,
            next_before: null,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    try {
      await refreshTranscriptTail(client, "sequential-tail");
      await refreshTranscriptTail(client, "sequential-tail");

      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(
        flattenTranscript(
          client.getQueryData<TranscriptData>(
            transcriptQueryKey("sequential-tail"),
          ),
        ).map((message) => message.id),
      ).toEqual(["old", "new-1", "new-2"]);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("fences a same-frontier older response after destructive invalidation", async () => {
    const client = new QueryClient();
    client.setQueryData(
      transcriptQueryKey("race-thread"),
      data([page(["tail"], 10, "older-cursor")]),
    );
    let resolveFetch!: (value: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveFetch = resolve;
          }),
      ),
    );
    const pending = fetchOlderTranscriptPage(client, "race-thread");
    const generation = transcriptGeneration("race-thread");

    invalidateTranscriptAuthoritatively(client, "race-thread");
    resolveFetch(
      new Response(
        JSON.stringify({
          items: [{ id: "stale-older", role: "user" }],
          snapshot_cursor: 10,
          next_before: null,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    await expect(pending).resolves.toBeUndefined();

    expect(transcriptGeneration("race-thread")).toBe(generation + 1);
    expect(
      client.getQueryData(transcriptQueryKey("race-thread")),
    ).toBeUndefined();
    vi.unstubAllGlobals();
  });

  it("rewinds disjoint bridge history and reaches legitimate pre-boundary history", async () => {
    const client = new QueryClient();
    client.setQueryData(
      transcriptQueryKey("continue-rewind"),
      data([
        page(["skipped-tail-1", "skipped-tail-2"], 20, "pre-boundary"),
        page(["skipped-bridge"], 18, "pre-boundary"),
      ]),
    );
    const generation = transcriptGeneration("continue-rewind");

    rewindTranscriptForContinuation(client, "continue-rewind");

    expect(transcriptGeneration("continue-rewind")).toBe(generation + 1);
    expect(
      client.getQueryData(transcriptQueryKey("continue-rewind")),
    ).toBeUndefined();

    client.setQueryData(
      transcriptQueryKey("continue-rewind"),
      data([page(["continue-boundary"], 23, "pre-boundary")]),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            items: [{ id: "legitimate-before", role: "user" }],
            snapshot_cursor: 23,
            next_before: null,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );

    try {
      await fetchOlderTranscriptPage(client, "continue-rewind");
      expect(
        flattenTranscript(
          client.getQueryData<TranscriptData>(
            transcriptQueryKey("continue-rewind"),
          ),
        ).map((message) => message.id),
      ).toEqual(["legitimate-before", "continue-boundary"]);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("routes only continuation command reloads through destructive rewind", async () => {
    const client = new QueryClient();
    client.setQueryData(
      transcriptQueryKey("command-continuation"),
      data([page(["skipped-command-tail"], 30)]),
    );
    let resolveStale!: (value: Response) => void;
    const fetchMock = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          resolveStale = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    try {
      const stale = refreshTranscriptTail(client, "command-continuation");
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await reloadTranscriptFromCommand(
        client,
        "command-continuation",
        "continuation",
      );
      expect(transcriptGeneration("command-continuation")).toBeGreaterThan(0);
      expect(
        client.getQueryData(transcriptQueryKey("command-continuation")),
      ).toBeUndefined();

      resolveStale(
        new Response(
          JSON.stringify({
            items: [{ id: "stale-command-tail", role: "assistant" }],
            snapshot_cursor: 31,
            next_before: null,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
      await stale;
      expect(
        client.getQueryData(transcriptQueryKey("command-continuation")),
      ).toBeUndefined();
    } finally {
      vi.unstubAllGlobals();
    }

    const ordinaryClient = new QueryClient();
    ordinaryClient.setQueryData(
      transcriptQueryKey("ordinary-reload"),
      data([page(["ordinary-loaded"], 5)]),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            items: [{ id: "ordinary-fresh", role: "assistant" }],
            snapshot_cursor: 6,
            next_before: null,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );
    try {
      await reloadTranscriptFromCommand(
        ordinaryClient,
        "ordinary-reload",
        undefined,
      );
      expect(
        flattenTranscript(
          ordinaryClient.getQueryData<TranscriptData>(
            transcriptQueryKey("ordinary-reload"),
          ),
        ).map((message) => message.id),
      ).toEqual(["ordinary-loaded", "ordinary-fresh"]);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("fences stale tail and older responses across a continuation rewind", async () => {
    const client = new QueryClient();
    client.setQueryData(
      transcriptQueryKey("continue-race"),
      data([page(["skipped-loaded-tail"], 30, "pre-boundary")]),
    );
    const resolvers: Array<(value: Response) => void> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolvers.push(resolve);
          }),
      ),
    );
    const staleTail = refreshTranscriptTail(client, "continue-race");
    const staleOlder = fetchOlderTranscriptPage(client, "continue-race");
    expect(resolvers).toHaveLength(2);

    rewindTranscriptForContinuation(client, "continue-race");
    client.setQueryData(
      transcriptQueryKey("continue-race"),
      data([page(["continue-boundary"], 33, "pre-boundary")]),
    );
    resolvers[0](
      new Response(
        JSON.stringify({
          items: [{ id: "stale-skipped-tail", role: "assistant" }],
          snapshot_cursor: 31,
          next_before: null,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    resolvers[1](
      new Response(
        JSON.stringify({
          items: [{ id: "stale-skipped-older", role: "user" }],
          snapshot_cursor: 30,
          next_before: null,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    await Promise.all([staleTail, staleOlder]);

    expect(
      flattenTranscript(
        client.getQueryData<TranscriptData>(
          transcriptQueryKey("continue-race"),
        ),
      ).map((message) => message.id),
    ).toEqual(["continue-boundary"]);
    vi.unstubAllGlobals();
  });

  it("fences a lower-cursor tail response after destructive invalidation", async () => {
    const client = new QueryClient();
    client.setQueryData(
      transcriptQueryKey("tail-race"),
      data([page(["new"], 20)]),
    );
    let resolveFetch!: (value: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveFetch = resolve;
          }),
      ),
    );
    const pending = refreshTranscriptTail(client, "tail-race");

    invalidateTranscriptAuthoritatively(client, "tail-race");
    resolveFetch(
      new Response(
        JSON.stringify({
          items: [{ id: "stale-tail", role: "assistant" }],
          snapshot_cursor: 19,
          next_before: null,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    await expect(pending).resolves.toMatchObject({ pages: [{ items: [] }] });

    expect(
      client.getQueryData(transcriptQueryKey("tail-race")),
    ).toBeUndefined();
    vi.unstubAllGlobals();
  });
});
