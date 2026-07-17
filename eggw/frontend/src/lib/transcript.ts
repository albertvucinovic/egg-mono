import type { InfiniteData, QueryClient } from "@tanstack/react-query";
import { fetchMessages, type MessageSnapshot } from "./api";
import type { Message } from "./store";
import {
  appendTranscriptHistoryData,
  flattenIndexedTranscript,
  replaceTranscriptPageData,
  replaceTranscriptTailData,
  transcriptDataFromTail,
  transcriptHistory,
  transcriptOwnerPage,
  upsertTranscriptTailData,
} from "./transcriptIndex";

export const TRANSCRIPT_PAGE_SIZE = 300;
export type TranscriptPage = MessageSnapshot<Message> & {
  /** Client-only page of entries displaced from a newer authoritative tail. */
  retained_tail_bridge?: true;
};
export type TranscriptData = InfiniteData<TranscriptPage, string | null>;

export function transcriptQueryKey(threadId: string) {
  return ["messages", threadId] as const;
}

interface TranscriptRequestState {
  generation: number;
  tailController: AbortController | null;
  tailRefreshPromise: Promise<TranscriptData> | null;
  tailRefreshGeneration: number | null;
  initialController: AbortController | null;
  initialGeneration: number | null;
  olderControllers: Set<AbortController>;
}

const transcriptRequests = new Map<string, TranscriptRequestState>();

function transcriptRequestState(threadId: string): TranscriptRequestState {
  let state = transcriptRequests.get(threadId);
  if (!state) {
    state = {
      generation: 0,
      tailController: null,
      tailRefreshPromise: null,
      tailRefreshGeneration: null,
      initialController: null,
      initialGeneration: null,
      olderControllers: new Set(),
    };
    transcriptRequests.set(threadId, state);
  }
  return state;
}

function beginTailRequest(threadId: string): {
  generation: number;
  controller: AbortController;
} {
  const state = transcriptRequestState(threadId);
  state.tailController?.abort();
  const controller = new AbortController();
  state.tailController = controller;
  return { generation: state.generation, controller };
}

function beginOlderRequest(threadId: string): {
  generation: number;
  controller: AbortController;
} {
  const state = transcriptRequestState(threadId);
  const controller = new AbortController();
  state.olderControllers.add(controller);
  return { generation: state.generation, controller };
}

function requestGenerationMatches(
  threadId: string,
  generation: number,
): boolean {
  return transcriptRequestState(threadId).generation === generation;
}

/** Fence every in-flight page response before applying destructive authority. */
export function advanceTranscriptGeneration(threadId: string): number {
  const state = transcriptRequestState(threadId);
  state.generation += 1;
  state.tailController?.abort();
  state.tailController = null;
  // A post-invalidation refresh must never join the stale generation's request.
  // Its rejected promise retains its own identity and clears itself in finally.
  state.tailRefreshPromise = null;
  state.tailRefreshGeneration = null;
  state.initialController?.abort();
  state.initialController = null;
  state.initialGeneration = null;
  state.olderControllers.forEach((controller) => controller.abort());
  state.olderControllers.clear();
  return state.generation;
}

export function transcriptGeneration(threadId: string): number {
  return transcriptRequestState(threadId).generation;
}

/** Cancel pending retry waits when a route stops owning this transcript. */
export function cancelTranscriptRequests(threadId: string): void {
  advanceTranscriptGeneration(threadId);
}

function isCommandClientMessage(message: Message): boolean {
  return message.client_only === "command";
}

function messageTimestampMs(
  message: Pick<Message, "timestamp">,
): number | null {
  if (typeof message.timestamp !== "string" || !message.timestamp) return null;
  const parsed = Date.parse(message.timestamp);
  return Number.isFinite(parsed) ? parsed : null;
}

function operationDedupKey(message: Message): string | null {
  if (!message.client_operation_id) return null;
  if (message.client_only === "optimistic")
    return `optimistic:${message.client_operation_id}`;
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
export function mergeMessagesByTimestamp(
  base: Message[],
  placed: Message[],
): Message[] {
  const merged: Message[] = [];
  const seenIds = new Set<string>();
  const seenOperations = new Set<string>();
  base.forEach((message) =>
    appendIfUnique(merged, message, seenIds, seenOperations),
  );

  const uniquePlaced: Array<{
    message: Message;
    index: number;
    timestampMs: number | null;
  }> = [];
  placed.forEach((message, index) => {
    const operationKey = operationDedupKey(message);
    if (message.id && seenIds.has(message.id)) return;
    if (operationKey && seenOperations.has(operationKey)) return;
    if (message.id) seenIds.add(message.id);
    if (operationKey) seenOperations.add(operationKey);
    uniquePlaced.push({
      message,
      index,
      timestampMs: messageTimestampMs(message),
    });
  });
  uniquePlaced.sort((left, right) => {
    if (
      left.timestampMs !== null &&
      right.timestampMs !== null &&
      left.timestampMs !== right.timestampMs
    ) {
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
      .filter(
        (message) =>
          Boolean(message.id) &&
          Number.isSafeInteger(message.event_seq) &&
          message.event_seq! > fetched.snapshot_cursor,
      )
      .map((message) => [message.id, message]),
  );
  let replacedFetchedVersion = false;
  const fetchedItems = fetched.items.map((message) => {
    const newer = message.id ? newerPreviousById.get(message.id) : undefined;
    if (!newer) return message;
    replacedFetchedVersion = true;
    return newer;
  });
  const fetchedIds = new Set(
    fetchedItems.map((message) => message.id).filter(Boolean),
  );
  const fetchedOperations = new Set(
    fetchedItems
      .map(operationDedupKey)
      .filter((key): key is string => Boolean(key)),
  );
  const preserved = previous.items.filter((message) => {
    if (message.id && fetchedIds.has(message.id)) return false;
    const operationKey = operationDedupKey(message);
    if (operationKey && fetchedOperations.has(operationKey)) return false;
    if (message.client_operation_id && message.client_only === "optimistic")
      return true;
    if (isCommandClientMessage(message)) return true;
    // A delayed HTTP snapshot must not erase a msg.create already consumed
    // from the ordered live feed. Once its cursor covers the event, absence is
    // authoritative and the retained envelope can be discarded.
    return (
      Number.isSafeInteger(message.event_seq) &&
      message.event_seq! > fetched.snapshot_cursor
    );
  });
  if (!preserved.length) {
    return replacedFetchedVersion
      ? { ...fetched, items: fetchedItems }
      : fetched;
  }
  return {
    ...fetched,
    // Event-installed/edit-patched messages already carry canonical timestamps.
    // Place all preserved entries at that chronology rather than appending a
    // get-user answer behind later durable messages during a stale refetch.
    items: mergeMessagesByTimestamp(fetchedItems, preserved),
  };
}

function retainedTailPrefix(
  fetched: TranscriptPage,
  previousTail: TranscriptPage,
): Message[] {
  const previousIndexById = new Map<string, number>();
  const previousIndexByOperation = new Map<string, number>();
  previousTail.items.forEach((message, index) => {
    if (message.id) previousIndexById.set(message.id, index);
    const operationKey = operationDedupKey(message);
    if (operationKey) previousIndexByOperation.set(operationKey, index);
  });
  let firstOverlap = -1;
  for (const message of fetched.items) {
    const operationKey = operationDedupKey(message);
    const previousIndex =
      (message.id ? previousIndexById.get(message.id) : undefined) ??
      (operationKey ? previousIndexByOperation.get(operationKey) : undefined);
    if (previousIndex !== undefined) {
      firstOverlap = previousIndex;
      break;
    }
  }
  // No overlap means the complete old tail may have been displaced by a large
  // burst. Preserve it; explicit destructive events own wholesale invalidation.
  const candidates =
    firstOverlap >= 0
      ? previousTail.items.slice(0, firstOverlap)
      : previousTail.items;
  const fetchedIds = new Set(
    fetched.items.map((message) => message.id).filter(Boolean),
  );
  const fetchedOperations = new Set(
    fetched.items
      .map(operationDedupKey)
      .filter((key): key is string => Boolean(key)),
  );
  return candidates.filter((message) => {
    if (message.id && fetchedIds.has(message.id)) return false;
    const operationKey = operationDedupKey(message);
    return !operationKey || !fetchedOperations.has(operationKey);
  });
}

function retainedBridgePages(
  displaced: Message[],
  previous: TranscriptData,
  snapshotCursor: number,
): TranscriptPage[] {
  if (!displaced.length) return [];
  const bridgePages: TranscriptPage[] = [];
  for (
    let offset = 0;
    offset < displaced.length;
    offset += TRANSCRIPT_PAGE_SIZE
  ) {
    const items = displaced.slice(offset, offset + TRANSCRIPT_PAGE_SIZE);
    bridgePages.push({
      items,
      snapshot_cursor: snapshotCursor,
      next_before: null,
      retained_tail_bridge: true,
    });
  }
  // If this is the first displacement, transfer the backend frontier to the
  // oldest bridge. The linked history index retains it across later refreshes.
  const history = transcriptHistory(previous);
  if (!history.newest && bridgePages.length) {
    const continuation = history.tail.next_before;
    if (continuation) {
      const oldestIndex = bridgePages.length - 1;
      bridgePages[oldestIndex] = {
        ...bridgePages[oldestIndex],
        next_before: continuation,
      };
    }
  }
  return bridgePages;
}

export function mergeRefreshedTranscriptTail(
  fetched: TranscriptPage,
  previous: TranscriptData | undefined,
): TranscriptData {
  if (!previous) return transcriptDataFromTail(fetched);
  const previousTail = transcriptHistory(previous).tail;
  if (previousTail.snapshot_cursor > fetched.snapshot_cursor) return previous;
  const displaced =
    previousTail.snapshot_cursor === fetched.snapshot_cursor
      ? previousTail.items.filter(
          (message) =>
            message.client_only === "optimistic" ||
            isCommandClientMessage(message),
        )
      : retainedTailPrefix(fetched, previousTail);
  let tail = reconcileTranscriptTail(fetched, previousTail);
  const bridges = retainedBridgePages(
    displaced,
    previous,
    fetched.snapshot_cursor,
  );
  const history = transcriptHistory(previous);
  if (
    !tail.next_before &&
    (history.newest || bridges.length || previousTail.next_before)
  ) {
    const frontier =
      history.oldest?.page.next_before || previousTail.next_before;
    tail = { ...tail, next_before: frontier || fetched.items[0]?.id || null };
  }
  return replaceTranscriptTailData(previous, tail, bridges);
}

/**
 * Refresh only the bounded live tail. TanStack's default infinite-query refetch
 * walks every loaded page; using it on SSE/input paths would scale with loaded
 * history and can commit a shorter cursor chain. Displaced tail prefixes move
 * into bounded client bridge pages; already-loaded older pages stay immutable.
 */
export function refreshTranscriptTail(
  queryClient: QueryClient,
  threadId: string,
): Promise<TranscriptData> {
  const state = transcriptRequestState(threadId);
  // React Strict Mode and reconnect/setup callers can request the same current
  // tail in one turn. Share that generation's authority instead of aborting and
  // restarting it: duplicate requests can observe different snapshots and make
  // an aborted intermediate response look like disappeared history.
  if (
    state.tailRefreshPromise &&
    state.tailRefreshGeneration === state.generation
  )
    return state.tailRefreshPromise;

  const request = beginTailRequest(threadId);
  state.tailRefreshGeneration = request.generation;
  let pending!: Promise<TranscriptData>;
  pending = (async () => {
    try {
      const fetched = (await fetchMessages(threadId, {
        limit: TRANSCRIPT_PAGE_SIZE,
        signal: request.controller.signal,
      })) as TranscriptPage;
      if (!requestGenerationMatches(threadId, request.generation)) {
        return (
          queryClient.getQueryData<TranscriptData>(
            transcriptQueryKey(threadId),
          ) || emptyTranscriptData()
        );
      }
      queryClient.setQueryData<TranscriptData>(
        transcriptQueryKey(threadId),
        (previous) => {
          if (!requestGenerationMatches(threadId, request.generation)) {
            return previous;
          }
          const latestCursor = previous
            ? transcriptHistory(previous).tail.snapshot_cursor
            : undefined;
          if (
            Number.isSafeInteger(latestCursor) &&
            Number(latestCursor) > fetched.snapshot_cursor
          ) {
            return previous;
          }
          return mergeRefreshedTranscriptTail(fetched, previous);
        },
      );
      return (
        queryClient.getQueryData<TranscriptData>(
          transcriptQueryKey(threadId),
        ) || transcriptDataFromTail(fetched)
      );
    } finally {
      const latest = transcriptRequestState(threadId);
      if (latest.tailController === request.controller)
        latest.tailController = null;
    }
  })();
  state.tailRefreshPromise = pending;
  void pending
    .finally(() => {
      const latest = transcriptRequestState(threadId);
      if (latest.tailRefreshPromise === pending) {
        latest.tailRefreshPromise = null;
        latest.tailRefreshGeneration = null;
      }
    })
    .catch(() => {
      // The caller owns the original rejection. This cleanup branch only
      // prevents the bookkeeping promise from becoming unhandled.
    });
  return pending;
}

export function mergeOlderTranscriptPage(
  current: TranscriptData | undefined,
  requestedBefore: string,
  fetched: TranscriptPage,
): TranscriptData | undefined {
  if (!current) return current;
  const history = transcriptHistory(current);
  const frontier = history.oldest?.page.next_before || history.tail.next_before;
  if (frontier !== requestedBefore) return current;
  return appendTranscriptHistoryData(current, fetched);
}

/** Fetch one explicit history page and merge it into the latest cache atomically. */
export async function fetchOlderTranscriptPage(
  queryClient: QueryClient,
  threadId: string,
): Promise<TranscriptData | undefined> {
  const cached = queryClient.getQueryData<TranscriptData>(
    transcriptQueryKey(threadId),
  );
  const history = transcriptHistory(cached);
  const before = history.oldest?.page.next_before || history.tail.next_before;
  if (!before)
    return queryClient.getQueryData<TranscriptData>(
      transcriptQueryKey(threadId),
    );
  const request = beginOlderRequest(threadId);
  try {
    const fetched = (await fetchMessages(threadId, {
      limit: TRANSCRIPT_PAGE_SIZE,
      beforeId: before,
      signal: request.controller.signal,
    })) as TranscriptPage;
    if (!requestGenerationMatches(threadId, request.generation)) {
      return queryClient.getQueryData<TranscriptData>(
        transcriptQueryKey(threadId),
      );
    }
    queryClient.setQueryData<TranscriptData>(
      transcriptQueryKey(threadId),
      (current) =>
        requestGenerationMatches(threadId, request.generation)
          ? mergeOlderTranscriptPage(current, before, fetched)
          : current,
    );
    return queryClient.getQueryData<TranscriptData>(
      transcriptQueryKey(threadId),
    );
  } finally {
    transcriptRequestState(threadId).olderControllers.delete(
      request.controller,
    );
  }
}

export function transcriptInfiniteQueryOptions(
  threadId: string,
  queryClient: QueryClient,
) {
  return {
    queryKey: transcriptQueryKey(threadId),
    initialPageParam: null as string | null,
    staleTime: Infinity,
    retryOnMount: true,
    // The linked history/index is intentionally mutable so bounded updates do
    // not clone every retained page. TanStack's deep structural sharing would
    // recursively traverse that graph, both violating boundedness and reusing a
    // stale root after the shared index was advanced in place.
    structuralSharing: false,
    queryFn: async ({ pageParam }: { pageParam: string | null }) => {
      // Do not consume TanStack's observer-owned AbortSignal here. React Strict
      // Mode deliberately unmounts/remounts the observer once; consuming that
      // signal aborts the first durable tail read and starts a second request,
      // so two different snapshots can race as if both were initial authority.
      // Our per-thread generation owns cancellation instead: destructive events
      // abort this controller and the generation check fences a response that
      // crossed the abort boundary, while a transient observer remount reuses
      // TanStack's still-in-flight promise.
      const state = transcriptRequestState(threadId);
      const generation = state.generation;
      const controller = new AbortController();
      if (pageParam) {
        state.olderControllers.add(controller);
      } else {
        // This is TanStack's initial cache fill, not an explicit refresh. Keep it
        // distinct so a destructive invalidation can abort/fence it without an
        // overlapping explicit refresh replacing its controller ownership.
        state.initialController?.abort();
        state.initialController = controller;
        state.initialGeneration = generation;
      }
      try {
        const fetched = (await fetchMessages(threadId, {
          limit: TRANSCRIPT_PAGE_SIZE,
          ...(pageParam ? { beforeId: pageParam } : {}),
          signal: controller.signal,
        })) as TranscriptPage;
        if (!requestGenerationMatches(threadId, generation)) {
          throw new DOMException("Stale transcript request", "AbortError");
        }
        if (pageParam) return fetched;
        const previous = queryClient.getQueryData<TranscriptData>(
          transcriptQueryKey(threadId),
        );
        if (!previous) return fetched;
        const previousTail = transcriptHistory(previous).tail;
        if (
          Number.isSafeInteger(previousTail.snapshot_cursor) &&
          previousTail.snapshot_cursor >= fetched.snapshot_cursor
        ) {
          return previousTail;
        }
        return reconcileTranscriptTail(fetched, previousTail);
      } finally {
        const latest = transcriptRequestState(threadId);
        if (pageParam) {
          latest.olderControllers.delete(controller);
        } else if (
          latest.initialController === controller &&
          latest.initialGeneration === generation
        ) {
          latest.initialController = null;
          latest.initialGeneration = null;
        }
      }
    },
    getNextPageParam: (lastPage: TranscriptPage) =>
      lastPage.next_before || undefined,
  };
}

/** Explicit authority for destructive history changes (for example msg.delete). */
export function invalidateTranscriptAuthoritatively(
  queryClient: QueryClient,
  threadId: string,
): void {
  advanceTranscriptGeneration(threadId);
  void queryClient.cancelQueries({
    queryKey: transcriptQueryKey(threadId),
    exact: true,
  });
  // Reset, rather than remove, the active Query object. Removing an observed
  // query leaves its mounted observer attached to an orphan that never sees the
  // replacement cache entry; reset keeps observer identity and immediately
  // refetches the post-invalidation generation.
  void queryClient.resetQueries({
    queryKey: transcriptQueryKey(threadId),
    exact: true,
  });
}

/**
 * Rebuild from the canonical post-continue projection.
 *
 * Ordinary tail refreshes deliberately retain a disjoint old tail as reachable
 * bridge history. A continuation reverses that rule: messages after the chosen
 * boundary are explicitly skipped, so no pre-continue page or response may be
 * merged into the rebuilt chain. The fresh backend frontier remains responsible
 * for reaching legitimate messages before the boundary.
 */
export function rewindTranscriptForContinuation(
  queryClient: QueryClient,
  threadId: string,
): void {
  invalidateTranscriptAuthoritatively(queryClient, threadId);
}

/** Apply the command response's reload contract without overloading reload=true. */
export function reloadTranscriptFromCommand(
  queryClient: QueryClient,
  threadId: string,
  reloadMode: unknown,
): Promise<TranscriptData | void> {
  if (reloadMode === "continuation") {
    rewindTranscriptForContinuation(queryClient, threadId);
    return Promise.resolve();
  }
  return refreshTranscriptTail(queryClient, threadId);
}

export function flattenTranscript(
  data: InfiniteData<TranscriptPage, unknown> | undefined,
): Message[] {
  return flattenIndexedTranscript(data);
}

export function transcriptSnapshotCursor(
  data: InfiniteData<TranscriptPage, unknown> | undefined,
): number {
  if (!data) return -1;
  const cursor = transcriptHistory(data as TranscriptData).tail.snapshot_cursor;
  return Number.isSafeInteger(cursor) ? Number(cursor) : -1;
}

function emptyTranscriptData(): TranscriptData {
  return transcriptDataFromTail({
    items: [],
    snapshot_cursor: -1,
    next_before: null,
  });
}

function updateTranscriptTail(
  queryClient: QueryClient,
  threadId: string,
  update: (tail: TranscriptPage, data: TranscriptData) => TranscriptPage,
): void {
  queryClient.setQueryData<TranscriptData>(
    transcriptQueryKey(threadId),
    (current) => {
      const previous = current || emptyTranscriptData();
      const tail = transcriptHistory(previous).tail;
      const nextTail = update(tail, previous);
      return nextTail === tail
        ? previous
        : replaceTranscriptPageData(previous, tail, nextTail);
    },
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
  queryClient.setQueryData<TranscriptData>(
    transcriptQueryKey(threadId),
    (current) => {
      const previous = current || emptyTranscriptData();
      return upsertTranscriptTailData(previous, message);
    },
  );
}

/** Apply one canonical msg.edit in its indexed owning page. */
export function patchTranscriptMessage(
  queryClient: QueryClient,
  threadId: string,
  messageId: string,
  patch: Partial<Message>,
  eventSeq: number,
): void {
  if (!messageId) return;
  queryClient.setQueryData<TranscriptData>(
    transcriptQueryKey(threadId),
    (current) => {
      const previous = current || emptyTranscriptData();
      const owner = transcriptOwnerPage(previous, messageId);
      if (!owner) return previous;
      const nextPage = {
        ...owner,
        items: owner.items.map((message) =>
          message.id === messageId
            ? { ...message, ...patch, id: messageId, event_seq: eventSeq }
            : message,
        ),
      };
      return replaceTranscriptPageData(previous, owner, nextPage);
    },
  );
}

export function appendClientTranscriptMessage(
  queryClient: QueryClient,
  threadId: string,
  message: Message,
): void {
  updateTranscriptTail(queryClient, threadId, (tail) => ({
    ...tail,
    items: isCommandClientMessage(message)
      ? mergeMessagesByTimestamp(
          tail.items.filter((candidate) => !isCommandClientMessage(candidate)),
          [...tail.items.filter(isCommandClientMessage), message],
        )
      : [...tail.items, message],
  }));
}

function updateClientOperation(
  queryClient: QueryClient,
  threadId: string,
  operationId: string,
  update: (messages: Message[]) => Message[],
): void {
  queryClient.setQueryData<TranscriptData>(
    transcriptQueryKey(threadId),
    (current) => {
      const previous = current || emptyTranscriptData();
      // Client-owned operations are always installed in the bounded live tail.
      const tail = transcriptHistory(previous).tail;
      if (
        !tail.items.some(
          (message) => message.client_operation_id === operationId,
        )
      )
        return previous;
      return replaceTranscriptPageData(previous, tail, {
        ...tail,
        items: update(tail.items),
      });
    },
  );
}

export function replaceClientTranscriptMessage(
  queryClient: QueryClient,
  threadId: string,
  operationId: string,
  messageId: string,
): void {
  updateClientOperation(queryClient, threadId, operationId, (messages) =>
    messages.map((message) => {
      if (message.client_operation_id !== operationId) return message;
      const {
        client_only: _clientOnly,
        client_operation_id: _operationId,
        ...persisted
      } = message;
      return { ...persisted, id: messageId };
    }),
  );
}

export function removeClientTranscriptMessage(
  queryClient: QueryClient,
  threadId: string,
  operationId: string,
): void {
  updateClientOperation(queryClient, threadId, operationId, (messages) =>
    messages.filter((message) => message.client_operation_id !== operationId),
  );
}
