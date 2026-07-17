import type { InfiniteData } from "@tanstack/react-query";
import type { Message } from "./store";
import type { TranscriptData, TranscriptPage } from "./transcript";
import {
  previousTranscriptStartIndex,
  TRANSCRIPT_WINDOW_MAX_MESSAGES,
  TRANSCRIPT_WINDOW_MESSAGES,
} from "./transcriptWindow";

export interface TranscriptHistoryNode {
  page: TranscriptPage;
  pageParam: string | null;
  newer: TranscriptHistoryNode | null;
  older: TranscriptHistoryNode | null;
}

export interface TranscriptHistory {
  newest: TranscriptHistoryNode | null;
  oldest: TranscriptHistoryNode | null;
  tail: TranscriptPage;
  ownerByMessageId: Map<string, TranscriptPage>;
  fallbackOwnersByMessageId: Map<string, TranscriptPage[]>;
  nodeByPage: WeakMap<TranscriptPage, TranscriptHistoryNode>;
  totalMessages: number;
  revision: number;
  renderWindowCache?: {
    startIndex: number;
    endIndex: number;
    messages: Message[];
  };
  /** Optional deterministic cost-shape probes used by regression tests. */
  counters?: { pageVisits: number; messageVisits: number };
}

type IndexedTranscriptData = InfiniteData<TranscriptPage, unknown> & {
  eggw_history?: TranscriptHistory;
  eggw_revision?: number;
};

const EMPTY_PAGE: TranscriptPage = {
  items: [],
  snapshot_cursor: -1,
  next_before: null,
};
let revision = 0;

function visitPage(history: TranscriptHistory): void {
  if (history.counters) history.counters.pageVisits += 1;
}

function visitMessage(history: TranscriptHistory): void {
  if (history.counters) history.counters.messageVisits += 1;
}

export function instrumentTranscriptCosts(
  data: InfiniteData<TranscriptPage, unknown>,
): { pageVisits: number; messageVisits: number } {
  const counters = { pageVisits: 0, messageVisits: 0 };
  transcriptHistory(data).counters = counters;
  return counters;
}

function effectiveMessages(
  page: TranscriptPage,
  history: TranscriptHistory,
): Message[] {
  return page.items.filter(
    (message) =>
      !message.id || history.ownerByMessageId.get(message.id) === page,
  );
}

function appendHistoryPage(
  history: TranscriptHistory,
  page: TranscriptPage,
  pageParam: string | null,
): TranscriptHistoryNode {
  const node: TranscriptHistoryNode = {
    page,
    pageParam,
    newer: history.oldest,
    older: null,
  };
  if (history.oldest) history.oldest.older = node;
  else history.newest = node;
  history.oldest = node;
  history.nodeByPage.set(page, node);
  return node;
}

function prependHistoryPage(
  history: TranscriptHistory,
  page: TranscriptPage,
  pageParam: string | null,
): TranscriptHistoryNode {
  const node: TranscriptHistoryNode = {
    page,
    pageParam,
    newer: null,
    older: history.newest,
  };
  if (history.newest) history.newest.newer = node;
  else history.oldest = node;
  history.newest = node;
  history.nodeByPage.set(page, node);
  return node;
}

function registerPage(
  history: TranscriptHistory,
  page: TranscriptPage,
  override: boolean,
): void {
  visitPage(history);
  for (const message of page.items) {
    visitMessage(history);
    if (!message.id) {
      history.totalMessages += 1;
      continue;
    }
    const existing = history.ownerByMessageId.get(message.id);
    if (!existing) {
      history.totalMessages += 1;
      history.ownerByMessageId.set(message.id, page);
    } else if (override && existing !== page) {
      const fallbacks = history.fallbackOwnersByMessageId.get(message.id) || [];
      if (fallbacks.at(-1) !== existing) fallbacks.push(existing);
      history.fallbackOwnersByMessageId.set(message.id, fallbacks);
      history.ownerByMessageId.set(message.id, page);
    }
  }
}

function unregisterPage(
  history: TranscriptHistory,
  page: TranscriptPage,
): void {
  visitPage(history);
  for (const message of page.items) {
    visitMessage(history);
    if (!message.id) {
      history.totalMessages -= 1;
      continue;
    }
    if (history.ownerByMessageId.get(message.id) !== page) continue;
    const fallbacks = history.fallbackOwnersByMessageId.get(message.id);
    const restored = fallbacks?.pop();
    if (fallbacks && !fallbacks.length) {
      history.fallbackOwnersByMessageId.delete(message.id);
    }
    if (restored) history.ownerByMessageId.set(message.id, restored);
    else {
      history.ownerByMessageId.delete(message.id);
      history.totalMessages -= 1;
    }
  }
}

/** Normalize old InfiniteData once; steady-state updates retain this O(1) index. */
export function transcriptHistory(
  data: InfiniteData<TranscriptPage, unknown> | undefined,
): TranscriptHistory {
  if (!data) {
    return {
      newest: null,
      oldest: null,
      tail: EMPTY_PAGE,
      ownerByMessageId: new Map(),
      fallbackOwnersByMessageId: new Map(),
      nodeByPage: new WeakMap(),
      totalMessages: 0,
      revision: 0,
    };
  }
  const indexed = data as IndexedTranscriptData;
  if (indexed.eggw_history) return indexed.eggw_history;
  const tail = data.pages[0] || EMPTY_PAGE;
  const history: TranscriptHistory = {
    newest: null,
    oldest: null,
    tail,
    ownerByMessageId: new Map(),
    fallbackOwnersByMessageId: new Map(),
    nodeByPage: new WeakMap(),
    totalMessages: 0,
    revision: ++revision,
  };
  // Imported InfiniteData pages are newest -> oldest. Register oldest first so
  // newer pages become overlap authority without any render-time dedupe scan.
  for (let pageIndex = data.pages.length - 1; pageIndex >= 1; pageIndex -= 1) {
    const page = data.pages[pageIndex];
    prependHistoryPage(
      history,
      page,
      (data.pageParams[pageIndex] as string | null | undefined) ?? null,
    );
    registerPage(history, page, true);
  }
  registerPage(history, tail, true);
  indexed.eggw_history = history;
  indexed.eggw_revision = history.revision;
  return history;
}

function publish(history: TranscriptHistory): TranscriptData {
  history.revision = ++revision;
  // TanStack stores one bounded tail page. Retained pages live in the linked
  // history authority, so hot cache commits never copy the loaded page chain.
  return {
    pages: [history.tail],
    pageParams: [null],
    eggw_history: history,
    eggw_revision: history.revision,
  } as TranscriptData;
}

export function transcriptDataFromTail(tail: TranscriptPage): TranscriptData {
  const data = { pages: [tail], pageParams: [null] } as TranscriptData;
  transcriptHistory(data);
  return data;
}

/** Replace the bounded tail and prepend bounded displaced bridge pages. */
export function replaceTranscriptTailData(
  previous: TranscriptData,
  tail: TranscriptPage,
  bridges: readonly TranscriptPage[],
): TranscriptData {
  const history = transcriptHistory(previous);
  unregisterPage(history, history.tail);
  for (const bridge of bridges) {
    prependHistoryPage(
      history,
      bridge,
      `retained-tail:${bridge.items[0]?.id || 0}`,
    );
    registerPage(history, bridge, true);
  }
  history.tail = tail;
  registerPage(history, tail, true);
  return publish(history);
}

/** Append exactly one backend page at the oldest frontier. */
export function appendTranscriptHistoryData(
  previous: TranscriptData,
  page: TranscriptPage,
): TranscriptData {
  const history = transcriptHistory(previous);
  appendHistoryPage(
    history,
    page,
    history.oldest?.page.next_before || history.tail.next_before,
  );
  registerPage(history, page, false);
  // TanStack derives hasNextPage from pages[0], so keep the current backend
  // frontier on the bounded tail while history itself lives in the linked index.
  const priorTail = history.tail;
  const tail = { ...priorTail, next_before: page.next_before };
  unregisterPage(history, priorTail);
  history.tail = tail;
  registerPage(history, tail, true);
  return publish(history);
}

function replaceIndexedPage(
  history: TranscriptHistory,
  previousPage: TranscriptPage,
  nextPage: TranscriptPage,
): boolean {
  if (history.tail === previousPage) {
    history.tail = nextPage;
    return true;
  }
  const node = history.nodeByPage.get(previousPage);
  if (!node) return false;
  node.page = nextPage;
  history.nodeByPage.delete(previousPage);
  history.nodeByPage.set(nextPage, node);
  return true;
}

/** Replace one indexed page after a bounded edit/upsert operation. */
export function replaceTranscriptPageData(
  previous: TranscriptData,
  previousPage: TranscriptPage,
  nextPage: TranscriptPage,
): TranscriptData {
  const history = transcriptHistory(previous);
  if (
    history.tail !== previousPage &&
    !history.nodeByPage.get(previousPage)
  ) return previous;
  unregisterPage(history, previousPage);
  replaceIndexedPage(history, previousPage, nextPage);
  registerPage(history, nextPage, true);
  return publish(history);
}

/**
 * Move one stable message ID into the live tail without scanning linked history.
 * Ownership lookup is O(1); only the bounded tail and the prior owning page are
 * copied/registered.
 */
export function upsertTranscriptTailData(
  previous: TranscriptData,
  message: Message,
): TranscriptData {
  if (!message.id) return previous;
  const history = transcriptHistory(previous);
  const tail = history.tail;
  const priorOwner = history.ownerByMessageId.get(message.id);
  const tailIndex = tail.items.findIndex(
    (candidate) => candidate.id === message.id,
  );
  const nextTail = {
    ...tail,
    items:
      tailIndex >= 0
        ? tail.items.map((candidate, index) =>
            index === tailIndex ? message : candidate,
          )
        : [...tail.items, message],
  };

  // Remove the prior page first, then register the new tail once. This avoids
  // transiently restoring overlap ownership or registering the same new owner
  // through two separate page-replacement operations.
  unregisterPage(history, tail);
  if (priorOwner && priorOwner !== tail) {
    const nextPriorOwner = {
      ...priorOwner,
      items: priorOwner.items.filter(
        (candidate) => candidate.id !== message.id,
      ),
    };
    if (history.nodeByPage.get(priorOwner)) {
      unregisterPage(history, priorOwner);
      replaceIndexedPage(history, priorOwner, nextPriorOwner);
      registerPage(history, nextPriorOwner, true);
    }
  }
  history.tail = nextTail;
  registerPage(history, nextTail, true);
  return publish(history);
}

export function transcriptOwnerPage(
  data: TranscriptData,
  messageId: string,
): TranscriptPage | undefined {
  const history = transcriptHistory(data);
  return history.ownerByMessageId.get(messageId);
}

export interface TranscriptIndex {
  totalMessages: number;
  revision: number;
  tailTps: number | null;
}

function tailTps(page: TranscriptPage): number | null {
  for (let index = page.items.length - 1; index >= 0; index -= 1) {
    const message = page.items[index];
    if (
      (message.role === "assistant" || message.role === "tool") &&
      typeof message.tps === "number" &&
      Number.isFinite(message.tps) &&
      message.tps > 0
    ) {
      return message.tps;
    }
  }
  return null;
}

export function transcriptIndex(
  data: InfiniteData<TranscriptPage, unknown> | undefined,
): TranscriptIndex {
  const history = transcriptHistory(data);
  return {
    totalMessages: history.totalMessages,
    revision: history.revision,
    tailTps: tailTps(history.tail),
  };
}

export interface TranscriptRenderWindow {
  messages: Message[];
  startIndex: number;
  endIndex: number;
  hiddenCount: number;
  newerHiddenCount: number;
  atLiveTail: boolean;
  startMessageId: string | null;
}

function sameRenderedMessage(left: Message, right: Message): boolean {
  if (left === right) return true;
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  if (leftKeys.length !== rightKeys.length) return false;
  return leftKeys.every((key) => {
    const leftValue = (left as unknown as Record<string, unknown>)[key];
    const rightValue = (right as unknown as Record<string, unknown>)[key];
    if (leftValue === rightValue) return true;
    if (
      leftValue === null ||
      rightValue === null ||
      typeof leftValue !== "object" ||
      typeof rightValue !== "object"
    ) return false;
    try {
      return JSON.stringify(leftValue) === JSON.stringify(rightValue);
    } catch {
      return false;
    }
  });
}

/** Visit only enough pages to extract one strict sliding DOM window. */
export function transcriptRenderWindow(
  data: InfiniteData<TranscriptPage, unknown> | undefined,
  startIndex: number | null,
  initialLimit = TRANSCRIPT_WINDOW_MESSAGES,
  maxMessages = TRANSCRIPT_WINDOW_MAX_MESSAGES,
): TranscriptRenderWindow {
  const history = transcriptHistory(data);
  const resolvedStart =
    startIndex === null
      ? Math.max(0, history.totalMessages - initialLimit)
      : Math.max(0, Math.min(startIndex, history.totalMessages));
  const endIndex =
    startIndex === null
      ? history.totalMessages
      : Math.min(history.totalMessages, resolvedStart + maxMessages);
  const wanted = Math.max(0, endIndex - resolvedStart);
  const skipNewest = history.totalMessages - endIndex;
  const reversed: Message[] = [];
  let newerSeen = 0;
  const collect = (page: TranscriptPage) => {
    visitPage(history);
    for (let index = page.items.length - 1; index >= 0; index -= 1) {
      if (reversed.length >= wanted && newerSeen >= skipNewest) return;
      const message = page.items[index];
      visitMessage(history);
      if (message.id && history.ownerByMessageId.get(message.id) !== page)
        continue;
      if (newerSeen < skipNewest) {
        newerSeen += 1;
        continue;
      }
      reversed.push(message);
      newerSeen += 1;
    }
  };
  if (wanted > 0) collect(history.tail);
  for (
    let node = history.newest;
    node && reversed.length < wanted;
    node = node.older
  )
    collect(node.page);
  const nextMessages = reversed.reverse();
  const cached = history.renderWindowCache;
  const messages =
    cached &&
    cached.startIndex === resolvedStart &&
    cached.endIndex === endIndex &&
    cached.messages.length === nextMessages.length &&
    cached.messages.every((message, index) =>
      sameRenderedMessage(message, nextMessages[index]),
    )
      ? cached.messages
      : nextMessages;
  history.renderWindowCache = {
    startIndex: resolvedStart,
    endIndex,
    messages,
  };
  return {
    messages,
    startIndex: resolvedStart,
    endIndex,
    hiddenCount: resolvedStart,
    newerHiddenCount: history.totalMessages - endIndex,
    atLiveTail: endIndex === history.totalMessages,
    startMessageId: messages[0]?.id || null,
  };
}

export function expandedTranscriptStartIndex(
  currentStartIndex: number,
  expansion = TRANSCRIPT_WINDOW_MESSAGES,
): number {
  return previousTranscriptStartIndex(currentStartIndex, expansion);
}

/** Full traversal is reserved for compatibility/tests, never the render selector. */
export function oldestTranscriptFrontier(
  data: InfiniteData<TranscriptPage, unknown> | undefined,
): string | null {
  const history = transcriptHistory(data);
  return history.oldest?.page.next_before || history.tail.next_before || null;
}

export function flattenIndexedTranscript(
  data: InfiniteData<TranscriptPage, unknown> | undefined,
): Message[] {
  const history = transcriptHistory(data);
  const out: Message[] = [];
  for (let node = history.oldest; node; node = node.newer) {
    out.push(...effectiveMessages(node.page, history));
  }
  out.push(...effectiveMessages(history.tail, history));
  return out;
}
