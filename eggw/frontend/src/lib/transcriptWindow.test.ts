import { describe, expect, it } from "vitest";
import type { Message } from "./store";
import {
  expandedTranscriptStartId,
  previousTranscriptStartIndex,
  transcriptWindow,
  TRANSCRIPT_WINDOW_MESSAGES,
} from "./transcriptWindow";

const messages = (count: number): Message[] => Array.from({ length: count }, (_, index) => ({
  id: `message-${index}`,
  role: index % 2 ? "assistant" : "user",
  content: `message ${index}`,
}));

describe("transcript render window", () => {
  it("mounts a bounded newest tail without removing loaded messages", () => {
    const loaded = messages(300);
    const window = transcriptWindow(loaded, null);
    expect(TRANSCRIPT_WINDOW_MESSAGES).toBe(60);
    expect(window.messages).toHaveLength(60);
    expect(window.messages[0].id).toBe("message-240");
    expect(window.hiddenCount).toBe(240);
    expect(window.newerHiddenCount).toBe(0);
    expect(window.atLiveTail).toBe(true);
    expect(loaded).toHaveLength(300);
  });

  it("falls back to a full initial window when route navigation carries a stale parent anchor", () => {
    const childMessages = messages(140);
    const childWindow = transcriptWindow(childMessages, "parent-message-anchor");
    expect(childWindow.messages).toHaveLength(60);
    expect(childWindow.messages[0].id).toBe("message-80");
    expect(childWindow.messages.at(-1)?.id).toBe("message-139");
  });

  it("prepends older messages without unmounting the rendered tail", () => {
    const loaded = messages(300);
    let start = 240;
    const initialTailIds = transcriptWindow(loaded, null).messages.map((message) => message.id);

    start = previousTranscriptStartIndex(start);
    let window = transcriptWindow(loaded, loaded[start].id);
    expect(window.messages).toHaveLength(120);
    expect(window.messages[0].id).toBe("message-180");
    expect(window.messages.at(-1)?.id).toBe("message-299");

    start = previousTranscriptStartIndex(start);
    window = transcriptWindow(loaded, loaded[start].id);
    expect(window.messages).toHaveLength(180);
    expect(window.messages[0].id).toBe("message-120");
    expect(window.messages.at(-1)?.id).toBe("message-299");
    expect(window.newerHiddenCount).toBe(0);
    expect(window.atLiveTail).toBe(true);
    expect(initialTailIds.every((id) => window.messages.some((message) => message.id === id))).toBe(true);
  });

  it("reaches the oldest message without dropping the bottom", () => {
    const loaded = messages(300);
    const oldest = transcriptWindow(loaded, loaded[0].id);
    expect(oldest.messages).toHaveLength(300);
    expect(oldest.messages[0].id).toBe("message-0");
    expect(oldest.messages.at(-1)?.id).toBe("message-299");
    expect(oldest.hiddenCount).toBe(0);
    expect(oldest.newerHiddenCount).toBe(0);
    expect(oldest.atLiveTail).toBe(true);
  });

  it("retains stable ID compatibility for one older step", () => {
    const loaded = messages(180);
    const initial = transcriptWindow(loaded, null);
    expect(expandedTranscriptStartId(loaded, initial.startIndex)).toBe("message-60");
  });
});
