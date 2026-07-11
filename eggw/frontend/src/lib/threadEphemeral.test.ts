import { describe, expect, it } from "vitest";
import { canEvictThreadEphemeralState, type ThreadEphemeralState } from "./threadEphemeral";
import { evictStreamingBufferForThread, hasStreamingBufferForThread, streamingBufferForThread, streamingBufferThreadIds } from "./streamingBuffer";
import { clearLiveToolsForThread, liveToolRegistryForThread } from "./liveToolContinuity";
import { emptyThreadStreamingState, useAppStore } from "./store";

describe("inactive thread ephemeral eviction", () => {
  it("protects current, active, reconnecting, and retained-tool threads", () => {
    expect(canEvictThreadEphemeralState({ isCurrent: true, isStreaming: false, hasRetainedTools: false })).toBe(false);
    expect(canEvictThreadEphemeralState({ isCurrent: false, isStreaming: true, hasRetainedTools: false })).toBe(false);
    expect(canEvictThreadEphemeralState({ isCurrent: false, isStreaming: false, connectionStatus: "reconnecting", hasRetainedTools: false })).toBe(false);
    expect(canEvictThreadEphemeralState({ isCurrent: false, isStreaming: false, connectionStatus: "disconnected", hasRetainedTools: true })).toBe(false);
    expect(canEvictThreadEphemeralState({ isCurrent: false, isStreaming: false, connectionStatus: "disconnected", hasRetainedTools: false })).toBe(true);
  });

  it("sweeps inactive containers while retaining current, active, reconnecting, and tool owners", () => {
    const states: Record<string, ThreadEphemeralState> = {
      "inactive-a": { isCurrent: false, isStreaming: false, connectionStatus: "disconnected", hasRetainedTools: false },
      "inactive-b": { isCurrent: false, isStreaming: false, hasRetainedTools: false },
      current: { isCurrent: true, isStreaming: false, hasRetainedTools: false },
      active: { isCurrent: false, isStreaming: true, hasRetainedTools: false },
      reconnecting: { isCurrent: false, isStreaming: false, connectionStatus: "reconnecting", hasRetainedTools: false },
      retained: { isCurrent: false, isStreaming: false, connectionStatus: "disconnected", hasRetainedTools: true },
    };
    Object.keys(states).forEach(streamingBufferForThread);
    liveToolRegistryForThread("retained").registry.observe("call-retained");

    const evicted = streamingBufferThreadIds().filter((threadId) => {
      if (!states[threadId] || !canEvictThreadEphemeralState(states[threadId])) return false;
      return evictStreamingBufferForThread(threadId);
    });
    expect(evicted).toEqual(expect.arrayContaining(["inactive-a", "inactive-b"]));
    for (const protectedThread of ["current", "active", "reconnecting", "retained"]) {
      expect(hasStreamingBufferForThread(protectedThread)).toBe(true);
      evictStreamingBufferForThread(protectedThread);
    }
    clearLiveToolsForThread("retained");
  });

  it("truly removes an inactive buffer and related Zustand run/connection state", () => {
    const threadId = "inactive-thread";
    streamingBufferForThread(threadId).appendContent("stale");
    useAppStore.setState({
      streamingByThread: { [threadId]: emptyThreadStreamingState() },
      connectionByThread: { [threadId]: { status: "disconnected" } },
    });

    expect(evictStreamingBufferForThread(threadId)).toBe(true);
    useAppStore.getState().evictThreadEphemeralState(threadId);
    expect(hasStreamingBufferForThread(threadId)).toBe(false);
    expect(useAppStore.getState().streamingByThread[threadId]).toBeUndefined();
    expect(useAppStore.getState().connectionByThread[threadId]).toBeUndefined();
  });
});
