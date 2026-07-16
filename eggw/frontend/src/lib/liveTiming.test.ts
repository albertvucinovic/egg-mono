import { describe, expect, it } from "vitest";
import { liveTimingSnapshot, shouldUpdateLiveTiming } from "./liveTiming";

const output = (overrides: Record<string, unknown> = {}) => ({
  id: "call-a",
  name: "bash",
  suppressed: false,
  suppressedFrames: 0,
  ...overrides,
});

describe("live timing ownership", () => {
  it("does not keep the timer alive for retained finished tools", () => {
    expect(shouldUpdateLiveTiming(false, {
      "call-a": output({ finished: true, startedAtMs: 1000 }),
    }, null)).toBe(false);
  });

  it("does not repaint once per second solely for an active get-user wait", () => {
    expect(shouldUpdateLiveTiming(true, {
      "call-wait": output({
        id: "call-wait",
        name: "get_user_message_while_preserving_llm_turn",
        startedAtMs: 1000,
        timeout: { startedAtMs: 1000, timeoutSec: 86400 },
      }),
    }, null, "tool")).toBe(false);
  });

  it("keeps the timer alive for active tools, streams, and provider requests", () => {
    expect(shouldUpdateLiveTiming(false, { "call-a": output({ startedAtMs: 1000 }) }, null)).toBe(true);
    expect(shouldUpdateLiveTiming(true, {}, null)).toBe(true);
    expect(shouldUpdateLiveTiming(false, {}, { startedAtMs: 1000 })).toBe(true);
  });
});

describe("live timing snapshots", () => {
  it("keeps wait tools stable while timing ordinary siblings", () => {
    const snapshot = liveTimingSnapshot(11_000, true, "tool", 1_000, null, {
      wait: output({ id: "wait", name: "get_user_message_while_preserving_llm_turn", startedAtMs: 1_000, timeout: { startedAtMs: 1_000, timeoutSec: 86_400 } }),
      bash: output({ id: "bash", name: "bash", startedAtMs: 1_000, timeout: { startedAtMs: 1_000, timeoutSec: 60 } }),
    });
    expect(snapshot.tools.wait).toEqual({ elapsed: null, timeout: null });
    expect(snapshot.tools.bash).toEqual({ elapsed: "running 10s", timeout: "timeout in 50s (limit 60s)" });
    expect(snapshot.generic).toBe("streaming 10s");
  });

  it("examines only active tool metadata, independent of transcript size", () => {
    const snapshot = liveTimingSnapshot(3_000, false, "tool", null, null, {
      bash: output({ id: "bash", startedAtMs: 1_000 }),
    });
    expect(Object.keys(snapshot.tools)).toEqual(["bash"]);
    expect(snapshot.tools.bash.elapsed).toBe("running 2s");
  });
});
