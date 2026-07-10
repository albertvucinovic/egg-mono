import { describe, expect, it } from "vitest";
import {
  MAX_LIVE_TOOL_THREADS,
  LiveToolRegistry,
  clearLiveToolsForThread,
  durableToolCallIds,
  liveToolRegistryForThread,
} from "./liveToolContinuity";

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

  it("bounds thread registries and supports explicit interruption cleanup", () => {
    for (let index = 0; index <= MAX_LIVE_TOOL_THREADS; index += 1) {
      liveToolRegistryForThread(`thread-${index}`).observe(`call-${index}`);
    }
    expect(clearLiveToolsForThread("thread-0")).toEqual([]);
    expect(clearLiveToolsForThread(`thread-${MAX_LIVE_TOOL_THREADS}`)).toEqual([`call-${MAX_LIVE_TOOL_THREADS}`]);
  });

  it("recognizes assistant calls and tool results by canonical IDs", () => {
    expect(durableToolCallIds(assistant("call-a"))).toEqual({ callIds: ["call-a"], resultIds: [] });
    expect(durableToolCallIds(result("call-a"))).toEqual({ callIds: [], resultIds: ["call-a"] });
  });
});
