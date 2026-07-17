import type { Message } from "./store";

// Sixty messages is the smallest useful launch/navigation context promised by
// EggW. Browsing adds one overlapping chunk for stable anchor restoration while
// retaining a strict 120-message DOM ceiling.
export const TRANSCRIPT_WINDOW_MESSAGES = 60;
export const TRANSCRIPT_WINDOW_OVERLAP = 60;
export const TRANSCRIPT_WINDOW_MAX_MESSAGES =
  TRANSCRIPT_WINDOW_MESSAGES + TRANSCRIPT_WINDOW_OVERLAP;

export interface TranscriptWindow {
  messages: Message[];
  startIndex: number;
  endIndex: number;
  hiddenCount: number;
  newerHiddenCount: number;
  atLiveTail: boolean;
}

/** Keep all loaded data authoritative while mounting one strict sliding window. */
export function transcriptWindow(
  messages: Message[],
  startMessageId: string | null,
  initialLimit = TRANSCRIPT_WINDOW_MESSAGES,
  maxMessages = TRANSCRIPT_WINDOW_MAX_MESSAGES,
): TranscriptWindow {
  const anchoredIndex = startMessageId
    ? messages.findIndex((message) => message.id === startMessageId)
    : -1;
  const requestedStart = anchoredIndex >= 0
    ? anchoredIndex
    : Math.max(0, messages.length - initialLimit);
  const endIndex = anchoredIndex >= 0
    ? Math.min(messages.length, requestedStart + maxMessages)
    : messages.length;
  const startIndex = Math.max(0, Math.min(requestedStart, endIndex));
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

export function nextTranscriptStartIndex(
  currentStartIndex: number,
  totalMessages: number,
  step = TRANSCRIPT_WINDOW_MESSAGES,
  maxMessages = TRANSCRIPT_WINDOW_MAX_MESSAGES,
): number | null {
  const liveStart = Math.max(0, totalMessages - maxMessages);
  if (currentStartIndex >= liveStart) return null;
  return Math.min(liveStart, currentStartIndex + step);
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
