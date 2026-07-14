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

function operationDedupKey(message: Message): string | null {
  if (!message.client_operation_id) return null;
  if (message.client_only === "optimistic") return `optimistic:${message.client_operation_id}`;
  if (isCommandClientMessage(message)) {
    // A shell/image command can intentionally render one pending card and one
    // response card for the same operation. Deduplicate each lifecycle slot,
    // while keeping command operations distinct from optimistic message sends.
    const lifecycle = message.command_name ? "response" : "pending";
    return `command:${lifecycle}:${message.client_operation_id}`;
  }
  return null;
}

function appendIfUnique(
  messages: Message[],
  message: Message,
  seenIds: Set<string>,
  seenOperations: Set<string>,
): void {
  const operationKey = operationDedupKey(message);
  if (message.id && seenIds.has(message.id)) return;
  if (operationKey && seenOperations.has(operationKey)) return;
  messages.push(message);
  if (message.id) seenIds.add(message.id);
  if (operationKey) seenOperations.add(operationKey);
}

/**
 * Stably place local timestamped entries into an authoritative newest page.
 * Authoritative/base order wins equal-timestamp ties; placed entries preserve
 * their input order for equal or missing/invalid timestamps. Only these two
 * bounded arrays are examined -- older transcript pages are never sorted.
 */
export function mergeMessagesByTimestamp(base: Message[], placed: Message[]): Message[] {
  const merged: Message[] = [];
  const seenIds = new Set<string>();
  const seenOperations = new Set<string>();
  base.forEach((message) => appendIfUnique(merged, message, seenIds, seenOperations));

  const uniquePlaced: Array<{ message: Message; index: number; timestampMs: number | null }> = [];
  placed.forEach((message, index) => {
    const operationKey = operationDedupKey(message);
    if (message.id && seenIds.has(message.id)) return;
    if (operationKey && seenOperations.has(operationKey)) return;
    if (message.id) seenIds.add(message.id);
    if (operationKey) seenOperations.add(operationKey);
    uniquePlaced.push({ message, index, timestampMs: messageTimestampMs(message) });
  });
  uniquePlaced.sort((left, right) => {
    if (left.timestampMs !== null && right.timestampMs !== null && left.timestampMs !== right.timestampMs) {
      return left.timestampMs - right.timestampMs;
    }
    if (left.timestampMs !== null && right.timestampMs === null) return -1;
    if (left.timestampMs === null && right.timestampMs !== null) return 1;
    return left.index - right.index;
  });

  for (const entry of uniquePlaced) {
    if (entry.timestampMs === null) {
      merged.push(entry.message);
      continue;
    }
    const insertAt = merged.findIndex((candidate) => {
      const candidateMs = messageTimestampMs(candidate);
      return candidateMs !== null && candidateMs > entry.timestampMs!;
    });
    if (insertAt === -1) merged.push(entry.message);
    else merged.splice(insertAt, 0, entry.message);
  }
  return merged;
}

/** Keep only explicit client-owned entries when an authoritative tail arrives. */
export function reconcileTranscriptTail(
  fetched: TranscriptPage,
  previous: TranscriptPage | undefined,
): TranscriptPage {
  if (!previous) return fetched;
  // A stale fetch may already contain a message's msg.create projection while
  // still predating a live msg.edit for the same stable ID. Preserve the
  // event-installed value in the fetched position until the snapshot cursor
  // covers that newer event; ID deduplication alone cannot distinguish these
  // two versions of one message.
  const newerPreviousById = new Map(
    previous.items
      .filter((message) => (
        Boolean(message.id)
        && Number.isSafeInteger(message.event_seq)
        && message.event_seq! > fetched.snapshot_cursor
      ))
      .map((message) => [message.id, message]),
  );
  let replacedFetchedVersion = false;
  const fetchedItems = fetched.items.map((message) => {
    const newer = message.id ? newerPreviousById.get(message.id) : undefined;
    if (!newer) return message;
    replacedFetchedVersion = true;
    return newer;
  });
  const fetchedIds = new Set(fetchedItems.map((message) => message.id).filter(Boolean));
  const fetchedOperations = new Set(
    fetchedItems.map(operationDedupKey).filter((key): key is string => Boolean(key)),
  );
  const preserved = previous.items.filter((message) => {
    if (message.id && fetchedIds.has(message.id)) return false;
    const operationKey = operationDedupKey(message);
    if (operationKey && fetchedOperations.has(operationKey)) return false;
    if (message.client_operation_id && message.client_only === "optimistic") return true;
    if (isCommandClientMessage(message)) return true;
    // A delayed HTTP snapshot must not erase a msg.create already consumed
    // from the ordered live feed. Once its cursor covers the event, absence is
    // authoritative and the retained envelope can be discarded.
    return Number.isSafeInteger(message.event_seq) && message.event_seq! > fetched.snapshot_cursor;
  });
  if (!preserved.length) {
    return replacedFetchedVersion ? { ...fetched, items: fetchedItems } : fetched;
  }
  return {
    ...fetched,
    // Event-installed/edit-patched messages already carry canonical timestamps.
    // Place all preserved entries at that chronology rather than appending a
    // get-user answer behind later durable messages during a stale refetch.
    items: mergeMessagesByTimestamp(fetchedItems, preserved),
  };
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

/** Apply one canonical msg.edit in place without moving transcript chronology. */
export function patchTranscriptMessage(
  queryClient: QueryClient,
  threadId: string,
  messageId: string,
  patch: Partial<Message>,
  eventSeq: number,
): void {
  if (!messageId) return;
  updateTranscript(queryClient, threadId, (data) => ({
    ...data,
    pages: data.pages.map((page) => ({
      ...page,
      items: page.items.map((message) => message.id === messageId
        ? { ...message, ...patch, id: messageId, event_seq: eventSeq }
        : message),
    })),
  }));
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
        ? mergeMessagesByTimestamp(
            tail.items.filter((candidate) => !isCommandClientMessage(candidate)),
            [...tail.items.filter(isCommandClientMessage), message],
          )
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
