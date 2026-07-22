import type { Message } from "./store";

// Sixty messages is an initial rendering budget, not a sliding window. Once a
// transcript has mounted, its oldest mounted record is pinned and later records
// grow the mounted suffix instead of evicting content from its top.
export const TRANSCRIPT_WINDOW_MESSAGES = 60;

export function initialTranscriptStartIndex(
  totalMessages: number,
  initialLimit = TRANSCRIPT_WINDOW_MESSAGES,
): number {
  return Math.max(0, totalMessages - initialLimit);
}

export interface TranscriptWindow {
  messages: Message[];
  startIndex: number;
  endIndex: number;
  hiddenCount: number;
  newerHiddenCount: number;
  atLiveTail: boolean;
}

/** Mount a bounded tail initially, then grow it monotonically toward history. */
export function transcriptWindow(
  messages: Message[],
  startMessageId: string | null,
  initialLimit = TRANSCRIPT_WINDOW_MESSAGES,
): TranscriptWindow {
  const anchoredIndex = startMessageId
    ? messages.findIndex((message) => message.id === startMessageId)
    : -1;
  const startIndex = anchoredIndex >= 0
    ? anchoredIndex
    : initialTranscriptStartIndex(messages.length, initialLimit);
  const endIndex = messages.length;
  return {
    messages: messages.slice(startIndex, endIndex),
    startIndex,
    endIndex,
    hiddenCount: startIndex,
    newerHiddenCount: messages.length - endIndex,
    atLiveTail: endIndex === messages.length,
  };
}

export function previousTranscriptStartIndex(
  currentStartIndex: number,
  step = TRANSCRIPT_WINDOW_MESSAGES,
): number {
  return Math.max(0, currentStartIndex - step);
}

// Compatibility helpers for callers that still anchor by stable message ID.
export function expandedTranscriptStartId(
  messages: Message[],
  currentStartIndex: number,
  expansion = TRANSCRIPT_WINDOW_MESSAGES,
): string | null {
  if (currentStartIndex <= 0) return messages[0]?.id || null;
  return messages[previousTranscriptStartIndex(currentStartIndex, expansion)]?.id || null;
}
