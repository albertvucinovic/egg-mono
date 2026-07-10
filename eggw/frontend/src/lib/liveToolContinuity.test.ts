import { describe, expect, it } from "vitest";
import {
  MAX_LIVE_TOOL_THREADS,
  LiveToolRegistry,
  LiveToolRegistryOwner,
  cleanUpEvictedLiveTools,
  clearLiveToolsForThread,
  durableToolCallIds,
  liveToolRegistryForThread,
} from "./liveToolContinuity";
import { useAppStore } from "./store";
import { streamingBufferForThread } from "./streamingBuffer";

function assistant(id: string) {
  return { id: `assistant-${id}`, role: "assistant", tool_calls: [{ id, name: "bash", arguments: "{}" }] };
}
function result(id: string) {
  return { id: `result-${id}`, role: "tool", tool_call_id: id, content: "done" };
}

describe("live tool continuity", () => {
  it("keeps a call after assistant durability and removes it only at its matching tool result", () => {
    const registry = new LiveToolRegistry();
    registry.observe("call-a");

    expect(registry.reconcileMessage(assistant("call-a"))).toEqual({ hideCalls: ["call-a"], removeTools: [] });
    expect(registry.has("call-a")).toBe(true);
    expect(registry.reconcileMessage(result("call-b"))).toEqual({ hideCalls: [], removeTools: [] });
    expect(registry.has("call-a")).toBe(true);
    expect(registry.reconcileMessage(result("call-a"))).toEqual({ hideCalls: [], removeTools: ["call-a"] });
    expect(registry.has("call-a")).toBe(false);
  });

  it("keeps live output visible after its assistant call becomes durable", () => {
    const registry = new LiveToolRegistry();
    registry.observe("call-output", true);
    expect(registry.reconcileMessage(assistant("call-output"))).toEqual({ hideCalls: [], removeTools: [] });
    expect(registry.has("call-output")).toBe(true);
  });

  it("keeps a settled tool across invocation close until a later invocation publishes its result", () => {
    const registry = new LiveToolRegistry();
    registry.observe("call-later");
    // stream.close intentionally has no registry operation.
    expect(registry.has("call-later")).toBe(true);
    expect(registry.reconcileMessage(result("call-later"))).toEqual({ hideCalls: [], removeTools: ["call-later"] });
  });

  it("supports explicit terminal cleanup only when no durable card is required", () => {
    const registry = new LiveToolRegistry();
    registry.observe("call-no-card");
    expect(registry.markTerminalWithoutDurable("other-call")).toEqual([]);
    expect(registry.markTerminalWithoutDurable("call-no-card")).toEqual(["call-no-card"]);
  });

  it("bounds retained entries deterministically", () => {
    const registry = new LiveToolRegistry(3);
    for (const id of ["one", "two", "three", "four", "five"]) registry.observe(id);
    expect(registry.size).toBe(3);
    expect(registry.has("one")).toBe(false);
    expect(registry.has("two")).toBe(false);
    expect(registry.has("three")).toBe(true);
  });

  it("clears retained entries at an authoritative reconnect/interruption boundary", () => {
    const registry = new LiveToolRegistry();
    registry.observe("call-a");
    registry.observe("call-b", true);
    expect(registry.clear()).toEqual(["call-a", "call-b"]);
    expect(registry.size).toBe(0);
  });

  it("cleans external state when more than 20 thread registries are LRU-bounded", () => {
    const owner = new LiveToolRegistryOwner(MAX_LIVE_TOOL_THREADS);
    useAppStore.setState({ streamingByThread: {} });
    const access = (threadId: string) => {
      const result = owner.forThread(threadId);
      cleanUpEvictedLiveTools(result.evicted, (evictedThreadId, toolCallId) => {
        streamingBufferForThread(evictedThreadId).removeTool(toolCallId);
        useAppStore.getState().removeThreadStreamingTool(evictedThreadId, toolCallId);
      });
      return result.registry;
    };
    const seed = (threadId: string, toolCallId: string) => {
      useAppStore.getState().patchThreadStreaming(threadId, {
        streamingToolCalls: { [toolCallId]: { name: "bash" } },
        streamingToolOutputs: {
          [toolCallId]: { id: toolCallId, name: "bash", suppressed: false, suppressedFrames: 0 },
        },
      });
      streamingBufferForThread(threadId).appendToolCallArgs(toolCallId, "bash", "{}");
      streamingBufferForThread(threadId).appendToolOutput(toolCallId, "output");
      access(threadId).observe(toolCallId, true);
    };

    for (let index = 0; index < MAX_LIVE_TOOL_THREADS; index += 1) {
      seed(`eviction-thread-${index}`, `call-${index}`);
    }
    // Normal current-thread access remains unchanged and refreshes its LRU age.
    expect(access("eviction-thread-0").has("call-0")).toBe(true);
    seed(`eviction-thread-${MAX_LIVE_TOOL_THREADS}`, `call-${MAX_LIVE_TOOL_THREADS}`);

    const evictedThreadId = "eviction-thread-1";
    expect(useAppStore.getState().streamingByThread[evictedThreadId].streamingToolCalls).toEqual({});
    expect(useAppStore.getState().streamingByThread[evictedThreadId].streamingToolOutputs).toEqual({});
    expect(streamingBufferForThread(evictedThreadId).toolCalls.has("call-1")).toBe(false);
    expect(streamingBufferForThread(evictedThreadId).toolOutputChunks.has("call-1")).toBe(false);
    expect(useAppStore.getState().streamingByThread["eviction-thread-0"].streamingToolCalls).toHaveProperty("call-0");
    expect(streamingBufferForThread("eviction-thread-0").toolCalls.has("call-0")).toBe(true);
  });

  it("keeps the singleton registry owner bounded and explicitly clearable", () => {
    for (let index = 0; index <= MAX_LIVE_TOOL_THREADS; index += 1) {
      liveToolRegistryForThread(`singleton-thread-${index}`).registry.observe(`singleton-call-${index}`);
    }
    expect(clearLiveToolsForThread(`singleton-thread-${MAX_LIVE_TOOL_THREADS}`))
      .toEqual([`singleton-call-${MAX_LIVE_TOOL_THREADS}`]);
  });

  it("recognizes assistant calls and tool results by canonical IDs", () => {
    expect(durableToolCallIds(assistant("call-a"))).toEqual({ callIds: ["call-a"], resultIds: [] });
    expect(durableToolCallIds(result("call-a"))).toEqual({ callIds: [], resultIds: ["call-a"] });
  });
});
