import { describe, expect, it } from "vitest";
import {
  createThreadEventSyncState,
  reconcileThreadEventCursor,
  reduceThreadEvent,
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

  it("lets a later lease-fenced stream.open replace the completed invocation view", () => {
    const state = createThreadEventSyncState("thread-a", 4, "invoke-a");
    const replaced = reduceThreadEvent(state, event(5, "stream.open", "invoke-b"), "stream.open");
    expect(replaced.accepted).toBe(true);
    expect(replaced.state.activeInvokeId).toBe("invoke-b");
  });

  it("rejects a late frame from the invocation replaced at the reconnect watermark", () => {
    const beforeReconnect = createThreadEventSyncState("thread-a", 20, "old-invoke");
    const reconciled = reconcileThreadEventCursor(beforeReconnect, 25, "new-invoke");
    expect(reduceThreadEvent(reconciled, event(26, "stream.delta", "old-invoke"), "stream.delta")).toMatchObject({
      accepted: false,
      reason: "stale_invocation",
    });
    expect(reduceThreadEvent(reconciled, event(27, "stream.delta", "new-invoke"), "stream.delta").accepted).toBe(true);
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
