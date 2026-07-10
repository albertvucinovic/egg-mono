import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "./store";
import { StreamingBuffer } from "./streamingBuffer";
import { applyStreamingDelta } from "./streamingDelta";

describe("high-rate streaming delta path", () => {
  beforeEach(() => {
    useAppStore.setState({ streamingByThread: {}, connectionByThread: {} });
  });

  it("keeps text, reasoning, and tool-output chunks out of Zustand", () => {
    const buffer = new StreamingBuffer();
    // Seed the output block; creating its header is a semantic transition, not
    // part of the high-rate body path measured below.
    applyStreamingDelta(buffer, { tool: { id: "tool-a", name: "bash", text: "start" } });
    const storePublication = vi.fn();
    const unsubscribe = useAppStore.subscribe(storePublication);

    for (let index = 0; index < 1_100; index += 1) {
      const notification = applyStreamingDelta(buffer, {
        text: "t",
        reason: "r",
        reasoning_summary: "s",
        tool: { id: "tool-a", name: "bash", text: "o" },
      });
      expect(notification).toEqual({ toolCall: undefined, toolOutput: undefined });
    }

    expect(storePublication).not.toHaveBeenCalled();
    expect(buffer.contentChunks).toHaveLength(1_100);
    expect(buffer.reasoningChunks).toHaveLength(1_100);
    expect(buffer.reasoningSummaryChunks).toHaveLength(1_100);
    expect(buffer.toolOutputChunks.get("tool-a")).toHaveLength(1_101);
    unsubscribe();
  });

  it("notifies tool-output headers and suppression only once", () => {
    const buffer = new StreamingBuffer();

    expect(applyStreamingDelta(buffer, { tool: { id: "tool-a", name: "bash", text: "one" } }).toolOutput)
      .toEqual({ id: "tool-a", name: "bash", suppressed: false });
    expect(applyStreamingDelta(buffer, { tool: { id: "tool-a", name: "bash", text: "two" } }).toolOutput)
      .toBeUndefined();
    expect(applyStreamingDelta(buffer, { tool: { id: "tool-a", name: "bash", suppressed: true } }).toolOutput)
      .toEqual({ id: "tool-a", name: "bash", suppressed: true });
    expect(applyStreamingDelta(buffer, { tool: { id: "tool-a", name: "bash", suppressed: true } }).toolOutput)
      .toBeUndefined();
  });

  it("publishes only tool-call identity while preserving every argument delta", () => {
    const buffer = new StreamingBuffer();
    const first = applyStreamingDelta(buffer, {
      tool_call: { id: "call-a", name: "", arguments_delta: '{"script":"' },
    });
    const named = applyStreamingDelta(buffer, {
      tool_call: { id: "call-a", name: "bash", arguments_delta: "x" },
    });
    const later = Array.from({ length: 1_098 }, () => "x");
    const notifications = later.map((chunk) => applyStreamingDelta(buffer, {
      tool_call: { id: "call-a", name: "bash", arguments_delta: chunk },
    }));
    applyStreamingDelta(buffer, {
      tool_call: { id: "call-a", name: "bash", arguments_delta: '"}' },
    });

    expect(first.toolCall).toEqual({ id: "call-a", name: "" });
    expect(named.toolCall).toEqual({ id: "call-a", name: "bash" });
    expect(notifications.every((notification) => notification.toolCall === undefined)).toBe(true);
    expect(buffer.toolCalls.get("call-a")?.argumentChunks).toHaveLength(1_101);
    expect(buffer.getToolCallArguments("call-a")).toBe(`{"script":"x${later.join("")}"}`);
  });

  it("does not republish unchanged connection or invocation state", () => {
    const store = useAppStore.getState();
    store.setThreadConnection("thread-a", "connected");
    store.patchThreadStreaming("thread-a", { invokeId: "invoke-a" });
    const publication = vi.fn();
    const unsubscribe = useAppStore.subscribe(publication);

    for (let index = 0; index < 1_100; index += 1) {
      store.setThreadConnection("thread-a", "connected");
      store.patchThreadStreaming("thread-a", { invokeId: "invoke-a" });
    }

    expect(publication).not.toHaveBeenCalled();
    unsubscribe();
  });
});
