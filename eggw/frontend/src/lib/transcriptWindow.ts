import type { Message } from "./store";

export const INITIAL_RENDERED_TRANSCRIPT_MESSAGES = 5;
export const TRANSCRIPT_WINDOW_EXPANSION_MESSAGES = 60;

export interface TranscriptWindow {
  messages: Message[];
  startIndex: number;
  hiddenCount: number;
}

/** Keep all loaded data authoritative while mounting only a bounded tail. */
export function transcriptWindow(
  messages: Message[],
  startMessageId: string | null,
  initialLimit = INITIAL_RENDERED_TRANSCRIPT_MESSAGES,
): TranscriptWindow {
  const anchoredIndex = startMessageId
    ? messages.findIndex((message) => message.id === startMessageId)
    : -1;
  const startIndex = anchoredIndex >= 0
    ? anchoredIndex
    : Math.max(0, messages.length - initialLimit);
  return {
    messages: messages.slice(startIndex),
    startIndex,
    hiddenCount: startIndex,
  };
}

export function expandedTranscriptStartId(
  messages: Message[],
  currentStartIndex: number,
  expansion = TRANSCRIPT_WINDOW_EXPANSION_MESSAGES,
): string | null {
  if (currentStartIndex <= 0) return messages[0]?.id || null;
  return messages[Math.max(0, currentStartIndex - expansion)]?.id || null;
}
