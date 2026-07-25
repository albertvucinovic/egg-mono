import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "./store";

describe("session-local display verbosity", () => {
  beforeEach(() => {
    useAppStore.setState(useAppStore.getInitialState(), true);
  });

  it("starts fresh frontend state at min", () => {
    expect(useAppStore.getInitialState().displayVerbosity).toBe("min");
    expect(useAppStore.getState().displayVerbosity).toBe("min");
  });

  it("keeps explicit medium and max overrides for the current session", () => {
    useAppStore.getState().setDisplayVerbosity("medium");
    expect(useAppStore.getState().displayVerbosity).toBe("medium");
    useAppStore.getState().setDisplayVerbosity("max");
    expect(useAppStore.getState().displayVerbosity).toBe("max");
  });

  it("cycles min to medium to max to min", () => {
    for (const level of ["medium", "max", "min"] as const) {
      useAppStore.getState().cycleDisplayVerbosity();
      expect(useAppStore.getState().displayVerbosity).toBe(level);
    }
  });
});
