import type { InfiniteData, QueryClient } from "@tanstack/react-query";
import { fetchMessages, type MessageSnapshot } from "./api";
import type { Message } from "./store";

export const TRANSCRIPT_PAGE_SIZE = 300;
export type TranscriptPage = MessageSnapshot<Message>;
export type TranscriptData = InfiniteData<TranscriptPage, string | null>;

export function transcriptQueryKey(threadId: string) {
  return ["messages", threadId] as const;
}

function isCommandClientMessage(message: Message): boolean {
  return message.client_only === "command";
}

function messageTimestampMs(message: Pick<Message, "timestamp">): number | null {
  if (typeof message.timestamp !== "string" || !message.timestamp) return null;
  const parsed = Date.parse(message.timestamp);
  return Number.isFinite(parsed) ? parsed : null;
}

function insertMessageByTimestamp(messages: Message[], message: Message): Message[] {
  const messageMs = messageTimestampMs(message);
  if (messageMs === null) return [...messages, message];
  const insertAt = messages.findIndex((candidate) => {
    const candidateMs = messageTimestampMs(candidate);
    return candidateMs !== null && candidateMs > messageMs;
  });
  return insertAt === -1
    ? [...messages, message]
    : [...messages.slice(0, insertAt), message, ...messages.slice(insertAt)];
}

/** Keep only explicit client-owned entries when an authoritative tail arrives. */
export function reconcileTranscriptTail(
  fetched: TranscriptPage,
  previous: TranscriptPage | undefined,
): TranscriptPage {
  if (!previous) return fetched;
  const fetchedIds = new Set(fetched.items.map((message) => message.id).filter(Boolean));
  const preserved = previous.items.filter((message) => {
    if (fetchedIds.has(message.id)) return false;
    if (message.client_operation_id && message.client_only === "optimistic") return true;
    if (isCommandClientMessage(message)) return true;
    // A delayed HTTP snapshot must not erase a msg.create already consumed
    // from the ordered live feed. Once its cursor covers the event, absence is
    // authoritative and the retained envelope can be discarded.
    return Number.isSafeInteger(message.event_seq) && message.event_seq! > fetched.snapshot_cursor;
  });
  if (!preserved.length) return fetched;
  return { ...fetched, items: [...fetched.items, ...preserved] };
}

export function transcriptInfiniteQueryOptions(threadId: string, queryClient: QueryClient) {
  return {
    queryKey: transcriptQueryKey(threadId),
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }: { pageParam: string | null }) => {
      const fetched = (await fetchMessages(threadId, {
        limit: TRANSCRIPT_PAGE_SIZE,
        ...(pageParam ? { beforeId: pageParam } : {}),
      })) as TranscriptPage;
      if (pageParam) return fetched;
      const previous = queryClient.getQueryData<TranscriptData>(transcriptQueryKey(threadId));
      return reconcileTranscriptTail(fetched, previous?.pages[0]);
    },
    getNextPageParam: (lastPage: TranscriptPage) => lastPage.next_before || undefined,
  };
}

export function flattenTranscript(data: InfiniteData<TranscriptPage, unknown> | undefined): Message[] {
  if (!data) return [];
  const authoritativeById = new Map<string, Message>();
  for (const page of data.pages) {
    for (const message of page.items) {
      if (message.id && !authoritativeById.has(message.id)) {
        authoritativeById.set(message.id, message);
      }
    }
  }

  const messages: Message[] = [];
  const seenIds = new Set<string>();
  // Infinite-query pages are [newest tail, progressively older pages]. Iterate
  // oldest-to-newest for chronology, but render the newest value on overlap.
  for (const page of [...data.pages].reverse()) {
    for (const message of page.items) {
      const id = message.id;
      if (id && seenIds.has(id)) continue;
      if (id) seenIds.add(id);
      messages.push(id ? authoritativeById.get(id) || message : message);
    }
  }
  return messages;
}

export function transcriptSnapshotCursor(data: InfiniteData<TranscriptPage, unknown> | undefined): number {
  const cursor = data?.pages[0]?.snapshot_cursor;
  return Number.isSafeInteger(cursor) ? Number(cursor) : -1;
}

function emptyTranscriptData(): TranscriptData {
  return {
    pages: [{ items: [], snapshot_cursor: -1, next_before: null }],
    pageParams: [null],
  };
}

function updateTranscript(
  queryClient: QueryClient,
  threadId: string,
  update: (data: TranscriptData) => TranscriptData,
): void {
  queryClient.setQueryData<TranscriptData>(
    transcriptQueryKey(threadId),
    (current) => update(current || emptyTranscriptData()),
  );
}

/**
 * Install one canonical msg.create in the newest transcript page. The newest
 * page owns authoritative overlap, while older pages, pagination cursors,
 * pageParams, and unrelated optimistic/client entries remain intact.
 */
export function upsertTranscriptTailMessage(
  queryClient: QueryClient,
  threadId: string,
  message: Message,
): void {
  if (!message.id) return;
  updateTranscript(queryClient, threadId, (data) => {
    const pages = data.pages.length ? [...data.pages] : emptyTranscriptData().pages;
    const tail = pages[0];
    const tailIndex = tail.items.findIndex((candidate) => candidate.id === message.id);
    pages[0] = {
      ...tail,
      items: tailIndex >= 0
        ? tail.items.map((candidate, index) => index === tailIndex ? message : candidate)
        : [...tail.items, message],
    };
    for (let index = 1; index < pages.length; index += 1) {
      const page = pages[index];
      if (!page.items.some((candidate) => candidate.id === message.id)) continue;
      pages[index] = {
        ...page,
        items: page.items.filter((candidate) => candidate.id !== message.id),
      };
    }
    return { ...data, pages };
  });
}

export function appendClientTranscriptMessage(
  queryClient: QueryClient,
  threadId: string,
  message: Message,
): void {
  updateTranscript(queryClient, threadId, (data) => {
    const pages = [...data.pages];
    const tail = pages[0] || emptyTranscriptData().pages[0];
    pages[0] = {
      ...tail,
      items: isCommandClientMessage(message)
        ? insertMessageByTimestamp(tail.items, message)
        : [...tail.items, message],
    };
    return { ...data, pages };
  });
}

export function replaceClientTranscriptMessage(
  queryClient: QueryClient,
  threadId: string,
  operationId: string,
  messageId: string,
): void {
  updateTranscript(queryClient, threadId, (data) => ({
    ...data,
    pages: data.pages.map((page) => ({
      ...page,
      items: page.items.map((message) => {
        if (message.client_operation_id !== operationId) return message;
        const { client_only: _clientOnly, client_operation_id: _operationId, ...persisted } = message;
        return { ...persisted, id: messageId };
      }),
    })),
  }));
}

export function removeClientTranscriptMessage(
  queryClient: QueryClient,
  threadId: string,
  operationId: string,
): void {
  updateTranscript(queryClient, threadId, (data) => ({
    ...data,
    pages: data.pages.map((page) => ({
      ...page,
      items: page.items.filter((message) => message.client_operation_id !== operationId),
    })),
  }));
}
