import { describe, expect, it } from "vitest";
import {
  createThreadEventSyncState,
  evictThreadEventSyncState,
  reduceThreadEvent,
  retainedThreadEventSyncState,
  retainThreadEventSyncState,
} from "./eventSync";

function event(eventSeq: number, type: string, invokeId: string | null = "invoke-a"): string {
  return JSON.stringify({
    event_id: `event-${eventSeq}`,
    event_seq: eventSeq,
    type,
    ts: "2026-07-10T00:00:00Z",
    msg_id: null,
    invoke_id: invokeId,
    chunk_seq: null,
    payload: {},
  });
}

describe("thread event reconciliation", () => {
  it("retains and explicitly evicts the last applied cursor by thread", () => {
    const state = createThreadEventSyncState("retained-thread", 42, "invoke-a");
    retainThreadEventSyncState(state);
    expect(retainedThreadEventSyncState("retained-thread")).toEqual(state);
    expect(retainedThreadEventSyncState("another-thread")).toBeNull();

    evictThreadEventSyncState("retained-thread");
    expect(retainedThreadEventSyncState("retained-thread")).toBeNull();
  });

  it("rejects duplicate and out-of-order reconnect frames", () => {
    const initial = createThreadEventSyncState("thread-a", 10, "invoke-a");
    const first = reduceThreadEvent(initial, event(11, "stream.delta"), "stream.delta");
    expect(first.accepted).toBe(true);
    const acceptedState = first.state;

    expect(reduceThreadEvent(acceptedState, event(11, "stream.delta"), "stream.delta")).toMatchObject({
      accepted: false,
      reason: "stale_sequence",
    });
    expect(reduceThreadEvent(acceptedState, event(9, "stream.delta"), "stream.delta")).toMatchObject({
      accepted: false,
      reason: "stale_sequence",
    });
  });

  it("replays active frames exactly once from the shared cursor even when snapshot is later", () => {
    const replayCursor = 4;
    const snapshotCursor = 20;
    let state = createThreadEventSyncState("thread-a", replayCursor, "invoke-a");
    for (const [sequence, type] of [[5, "stream.open"], [6, "stream.delta"], [7, "tool_call.execution_started"]] as const) {
      const reduced = reduceThreadEvent(state, event(sequence, type, "invoke-a"), type);
      expect(reduced.accepted).toBe(true);
      state = reduced.state;
    }
    expect(state.lastEventSeq).toBe(7);
    expect(state.lastEventSeq).toBeLessThan(snapshotCursor);
    expect(reduceThreadEvent(state, event(6, "stream.delta", "invoke-a"), "stream.delta")).toMatchObject({
      accepted: false,
      reason: "stale_sequence",
    });
  });

  it("lets a later lease-fenced stream.open replace the completed invocation view", () => {
    const state = createThreadEventSyncState("thread-a", 4, "invoke-a");
    const replaced = reduceThreadEvent(state, event(5, "stream.open", "invoke-b"), "stream.open");
    expect(replaced.accepted).toBe(true);
    expect(replaced.state.activeInvokeId).toBe("invoke-b");
  });

  it("adopts a disconnected replacement only from ordered stream.open and rejects old frames", () => {
    const disconnected = createThreadEventSyncState("thread-a", 20, "old-invoke");
    expect(reduceThreadEvent(disconnected, event(21, "stream.delta", "new-invoke"), "stream.delta")).toMatchObject({
      accepted: false,
      reason: "stale_invocation",
    });
    const opened = reduceThreadEvent(disconnected, event(22, "stream.open", "new-invoke"), "stream.open");
    expect(opened.accepted).toBe(true);
    expect(opened.state.activeInvokeId).toBe("new-invoke");
    expect(reduceThreadEvent(opened.state, event(23, "stream.delta", "old-invoke"), "stream.delta")).toMatchObject({
      accepted: false,
      reason: "stale_invocation",
    });
    expect(reduceThreadEvent(opened.state, event(24, "stream.delta", "new-invoke"), "stream.delta").accepted).toBe(true);
  });

  it("tracks the stream invocation independently of connection status", () => {
    const opened = reduceThreadEvent(
      createThreadEventSyncState("thread-a", 3),
      event(4, "stream.open", "invoke-b"),
      "stream.open",
    );
    expect(opened.state.activeInvokeId).toBe("invoke-b");
    const closed = reduceThreadEvent(opened.state, event(5, "stream.close", "invoke-b"), "stream.close");
    expect(closed.state.activeInvokeId).toBeNull();
  });

  it("rejects malformed or mismatched canonical envelopes", () => {
    const state = createThreadEventSyncState("thread-a", 0);
    expect(reduceThreadEvent(state, "{}", "msg.create")).toMatchObject({ accepted: false, reason: "invalid" });
    expect(reduceThreadEvent(state, event(1, "stream.open"), "msg.create")).toMatchObject({ accepted: false, reason: "invalid" });
  });
});
