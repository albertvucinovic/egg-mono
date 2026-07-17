import { describe, expect, it } from "vitest";
import type { Message } from "./store";
import {
  expandedTranscriptStartId,
  nextTranscriptStartIndex,
  previousTranscriptStartIndex,
  transcriptWindow,
  TRANSCRIPT_WINDOW_MAX_MESSAGES,
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
    expect(TRANSCRIPT_WINDOW_MAX_MESSAGES).toBe(120);
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

  it("slides toward oldest with overlap while enforcing the strict maximum", () => {
    const loaded = messages(300);
    let start = 240;
    start = previousTranscriptStartIndex(start);
    let window = transcriptWindow(loaded, loaded[start].id);
    expect(window.messages).toHaveLength(120);
    expect(window.messages[0].id).toBe("message-180");
    expect(window.messages.at(-1)?.id).toBe("message-299");
    expect(window.newerHiddenCount).toBe(0);

    start = previousTranscriptStartIndex(start);
    window = transcriptWindow(loaded, loaded[start].id);
    expect(window.messages).toHaveLength(120);
    expect(window.messages[0].id).toBe("message-120");
    expect(window.messages.at(-1)?.id).toBe("message-239");
    expect(window.newerHiddenCount).toBe(60);
  });

  it("reaches oldest then moves newer and returns to the live window", () => {
    const loaded = messages(300);
    const oldest = transcriptWindow(loaded, loaded[0].id);
    expect(oldest.messages).toHaveLength(120);
    expect(oldest.hiddenCount).toBe(0);
    expect(oldest.newerHiddenCount).toBe(180);
    expect(oldest.atLiveTail).toBe(false);

    const newerStart = nextTranscriptStartIndex(oldest.startIndex, loaded.length);
    expect(newerStart).toBe(60);
    const newer = transcriptWindow(loaded, loaded[newerStart!].id);
    expect(newer.messages[0].id).toBe("message-60");
    expect(newer.messages).toHaveLength(120);

    const liveStart = nextTranscriptStartIndex(120, loaded.length);
    expect(liveStart).toBe(180);
    const live = transcriptWindow(loaded, loaded[liveStart!].id);
    expect(live.messages.at(-1)?.id).toBe("message-299");
    expect(live.atLiveTail).toBe(true);
  });

  it("retains stable ID compatibility for one older step", () => {
    const loaded = messages(180);
    const initial = transcriptWindow(loaded, null);
    expect(expandedTranscriptStartId(loaded, initial.startIndex)).toBe("message-60");
  });
});
