import { describe, expect, it, vi } from "vitest";
import { AnimationFrameCoalescer, IntervalCoalescer, StreamingBuffer } from "./streamingBuffer";
import { useAppStore } from "./store";

describe("StreamingBuffer tool arguments", () => {
  it("notifies live leaves when an assistant generation is cleared", () => {
    const buffer = new StreamingBuffer();
    let contentNotifications = 0;
    let reasoningNotifications = 0;
    buffer.subscribeContent(() => { contentNotifications += 1; });
    buffer.subscribeReasoning(() => { reasoningNotifications += 1; });
    buffer.appendContent("old answer");
    buffer.appendReasoning("old reasoning");
    const generation = buffer.assistantGeneration;

    buffer.clearAssistantText();

    expect(buffer.assistantGeneration).toBe(generation + 1);
    expect(buffer.getContent()).toBe("");
    expect(buffer.getReasoning()).toBe("");
    expect(contentNotifications).toBe(2);
    expect(reasoningNotifications).toBe(2);
  });

  it("retains argument chunks without growing-string concatenation", () => {
    const buffer = new StreamingBuffer();
    const chunks = Array.from({ length: 1_100 }, (_, index) => `${index},`);

    chunks.forEach((chunk) => buffer.appendToolCallArgs("call-a", "bash", chunk));

    const call = buffer.toolCalls.get("call-a");
    expect(call?.argumentChunks).toEqual(chunks);
    expect(call?.argumentLength).toBe(chunks.reduce((total, chunk) => total + chunk.length, 0));
    expect(buffer.getToolCallArguments("call-a")).toBe(chunks.join(""));
    expect(buffer.getToolCallArgumentPrefix("call-a", 40)).toBe(chunks.join("").slice(0, 40));
  });

  it("marks one live tool terminal without removing sibling tools", () => {
    useAppStore.setState({ streamingByThread: {} });
    const store = useAppStore.getState();
    store.upsertThreadStreamingToolCall("thread-a", "call-get-user", "get_user_message_while_preserving_llm_turn");
    store.upsertThreadStreamingToolCall("thread-a", "call-bash", "bash");
    store.markThreadStreamingToolStarted("thread-a", "call-get-user", "get_user_message_while_preserving_llm_turn", 1000, 30);
    store.markThreadStreamingToolStarted("thread-a", "call-bash", "bash", 1000, 30);

    store.markThreadStreamingToolFinished("thread-a", "call-get-user");

    const outputs = useAppStore.getState().streamingByThread["thread-a"].streamingToolOutputs;
    expect(useAppStore.getState().streamingByThread["thread-a"].streamingToolCalls["call-get-user"].finished).toBe(true);
    expect(outputs["call-get-user"]).toMatchObject({ finished: true });
    expect(outputs["call-get-user"].startedAtMs).toBeUndefined();
    expect(outputs["call-get-user"].timeout).toBeUndefined();
    expect(outputs["call-bash"]).toMatchObject({ name: "bash", startedAtMs: 1000 });
    expect(outputs["call-bash"].finished).toBeUndefined();
  });


  it("bounds compact preview notifications to ten per second", () => {
    let now = 0;
    let pending: { callback: () => void; dueAt: number } | null = null;
    const coalescer = new IntervalCoalescer<number>(
      100,
      () => now,
      (callback, delayMs) => {
        pending = { callback, dueAt: now + delayMs };
        return 1;
      },
      () => { pending = null; },
    );
    const flush = vi.fn();

    for (let index = 0; index < 1_100; index += 1) {
      now = index * (1000 / 1_100);
      const scheduled = pending as { callback: () => void; dueAt: number } | null;
      if (scheduled && scheduled.dueAt <= now) {
        const due = scheduled;
        pending = null;
        due.callback();
      }
      coalescer.schedule(flush);
    }

    expect(flush.mock.calls.length).toBeLessThanOrEqual(10);
    expect(flush.mock.calls.length).toBeGreaterThanOrEqual(9);
  });

  it("coalesces an argument preview burst to one flush per animation frame", () => {
    const frames = new Map<number, FrameRequestCallback>();
    let nextFrameId = 1;
    const coalescer = new AnimationFrameCoalescer(
      (callback) => {
        const id = nextFrameId++;
        frames.set(id, callback);
        return id;
      },
      (id) => { frames.delete(id); },
    );
    const flush = vi.fn();

    for (let index = 0; index < 1_100; index += 1) coalescer.schedule(flush);

    expect(frames.size).toBe(1);
    expect(flush).not.toHaveBeenCalled();
    const callback = Array.from(frames.values())[0];
    frames.clear();
    callback(0);
    expect(flush).toHaveBeenCalledTimes(1);

    coalescer.schedule(flush);
    expect(frames.size).toBe(1);
  });
});
