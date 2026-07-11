import { describe, expect, it } from "vitest";
import type { Message } from "./store";
import { expandedTranscriptStartId, transcriptWindow } from "./transcriptWindow";

const messages = (count: number): Message[] => Array.from({ length: count }, (_, index) => ({
  id: `message-${index}`,
  role: index % 2 ? "assistant" : "user",
  content: `message ${index}`,
}));

describe("transcript render window", () => {
  it("mounts a bounded newest tail without removing loaded messages", () => {
    const loaded = messages(300);
    const window = transcriptWindow(loaded, null, 10);
    expect(window.messages).toHaveLength(10);
    expect(window.messages[0].id).toBe("message-290");
    expect(window.hiddenCount).toBe(290);
    expect(loaded).toHaveLength(300);
  });

  it("keeps an anchored mounted start when new tail messages arrive", () => {
    const loaded = messages(300);
    const initial = transcriptWindow(loaded, null, 10);
    const appended = [...loaded, { id: "message-300", role: "assistant" }];
    const anchored = transcriptWindow(appended, initial.messages[0].id, 60);
    expect(anchored.messages[0].id).toBe("message-290");
    expect(anchored.messages.at(-1)?.id).toBe("message-300");
  });

  it("expands toward loaded history in stable chunks until every message is mounted", () => {
    const loaded = messages(180);
    const initial = transcriptWindow(loaded, null, 60);
    const firstStart = expandedTranscriptStartId(loaded, initial.startIndex, 60);
    const first = transcriptWindow(loaded, firstStart, 60);
    expect(first.messages[0].id).toBe("message-60");
    expect(first.hiddenCount).toBe(60);
    const all = transcriptWindow(loaded, expandedTranscriptStartId(loaded, first.startIndex, 60), 60);
    expect(all.messages).toHaveLength(180);
    expect(all.hiddenCount).toBe(0);
  });
});
