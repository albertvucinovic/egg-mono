import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "./store";

const SESSION_KEY = "eggw-transcript-mount-boundaries";
const storage = new Map<string, string>();

const sessionStorage = {
  getItem: (key: string) => storage.get(key) ?? null,
  setItem: (key: string, value: string) => { storage.set(key, value); },
  removeItem: (key: string) => { storage.delete(key); },
};

vi.stubGlobal("window", {});
vi.stubGlobal("sessionStorage", sessionStorage);

describe("thread-scoped transcript mount boundaries", () => {
  beforeEach(() => {
    storage.clear();
    useAppStore.setState(useAppStore.getInitialState(), true);
  });

  afterEach(() => {
    storage.clear();
  });

  it("retains and session-persists independent boundaries for each thread", () => {
    const state = useAppStore.getState();
    state.setTranscriptMountBoundary("thread-a", { startMessageId: "a-120", startIndex: 120 });
    state.setTranscriptMountBoundary("thread-b", { startMessageId: "b-60", startIndex: 60 });

    expect(useAppStore.getState().transcriptMountBoundaryByThread).toEqual({
      "thread-a": { startMessageId: "a-120", startIndex: 120 },
      "thread-b": { startMessageId: "b-60", startIndex: 60 },
    });
    expect(JSON.parse(storage.get(SESSION_KEY) || "{}")).toEqual({
      "thread-a": { startMessageId: "a-120", startIndex: 120 },
      "thread-b": { startMessageId: "b-60", startIndex: 60 },
    });
  });
});
