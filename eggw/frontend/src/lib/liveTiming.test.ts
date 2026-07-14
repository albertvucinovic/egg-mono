import { describe, expect, it } from "vitest";
import { shouldUpdateLiveTiming } from "./liveTiming";

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

  it("keeps the timer alive for active tools, streams, and provider requests", () => {
    expect(shouldUpdateLiveTiming(false, { "call-a": output({ startedAtMs: 1000 }) }, null)).toBe(true);
    expect(shouldUpdateLiveTiming(true, {}, null)).toBe(true);
    expect(shouldUpdateLiveTiming(false, {}, { startedAtMs: 1000 })).toBe(true);
  });
});
