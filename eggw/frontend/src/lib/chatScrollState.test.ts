import { describe, expect, it } from "vitest";
import {
  IDLE_HISTORY_DEMAND,
  reduceHistoryDemand,
  reduceLiveEdgeState,
  type HistoryDemandState,
} from "./chatScrollState";

describe("live-edge intent state", () => {
  it("detaches only for user intent toward history", () => {
    expect(reduceLiveEdgeState("following", { type: "content_mutated" })).toBe("following");
    expect(reduceLiveEdgeState("following", { type: "programmatic_scroll" })).toBe("following");
    expect(reduceLiveEdgeState("following", { type: "user_toward_history" })).toBe("detached");
  });

  it("reattaches only when the user reaches the edge or changes thread", () => {
    expect(reduceLiveEdgeState("detached", { type: "content_mutated" })).toBe("detached");
    expect(reduceLiveEdgeState("detached", { type: "programmatic_scroll" })).toBe("detached");
    expect(reduceLiveEdgeState("detached", { type: "user_reached_live_edge" })).toBe("following");
    expect(reduceLiveEdgeState("detached", { type: "thread_changed" })).toBe("following");
  });
});

describe("top history demand state", () => {
  const loaded = { canReveal: true, canFetch: true };
  const networkOnly = { canReveal: false, canFetch: true };
  const exhausted = { canReveal: false, canFetch: false };

  it("always reveals loaded history before fetching", () => {
    expect(reduceHistoryDemand(IDLE_HISTORY_DEMAND, { type: "older_intent" }, loaded)).toEqual({
      phase: "revealing",
      pending: false,
    });
    expect(reduceHistoryDemand(IDLE_HISTORY_DEMAND, { type: "older_intent" }, networkOnly)).toEqual({
      phase: "fetching",
      pending: false,
    });
  });

  it("coalesces any number of in-flight intents into one pending demand", () => {
    const revealing: HistoryDemandState = { phase: "revealing", pending: false };
    const queued = reduceHistoryDemand(revealing, { type: "older_intent" }, loaded);
    expect(queued).toEqual({ phase: "revealing", pending: true });
    expect(reduceHistoryDemand(queued, { type: "older_intent" }, loaded)).toBe(queued);
  });

  it("consumes one queued demand using post-commit availability", () => {
    expect(reduceHistoryDemand(
      { phase: "fetching", pending: true },
      { type: "settled" },
      loaded,
    )).toEqual({ phase: "revealing", pending: false });
    expect(reduceHistoryDemand(
      { phase: "revealing", pending: true },
      { type: "settled" },
      networkOnly,
    )).toEqual({ phase: "fetching", pending: false });
    expect(reduceHistoryDemand(
      { phase: "revealing", pending: true },
      { type: "settled" },
      exhausted,
    )).toEqual(IDLE_HISTORY_DEMAND);
  });

  it("returns to idle after one operation without a queued user intent", () => {
    expect(reduceHistoryDemand(
      { phase: "fetching", pending: false },
      { type: "settled" },
      loaded,
    )).toEqual(IDLE_HISTORY_DEMAND);
  });
});
