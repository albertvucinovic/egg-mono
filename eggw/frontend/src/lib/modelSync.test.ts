import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import {
  applyCanonicalModelSwitch,
  reconcileThreadModelSnapshot,
  refreshThreadModelQueries,
} from "./modelSync";

function client(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function seed(queryClient: QueryClient, threadId = "thread-a"): void {
  queryClient.setQueryData(["threadSettings", threadId], {
    auto_approval: false,
    model_key: "Model A",
  });
  queryClient.setQueryData(["thread", threadId], { id: threadId, model_key: "Model A" });
  queryClient.setQueryData(["rootThreads"], [
    { id: threadId, model_key: "Model A" },
    { id: "thread-b", model_key: "Model B" },
  ]);
  queryClient.setQueryData(["threads"], [{ id: threadId, model_key: "Model A" }]);
  queryClient.setQueryData(["threadChildren", "parent"], [{ id: threadId, model_key: "Model A" }]);
}

describe("canonical model synchronization", () => {
  it("applies an ordered model.switch to every model-bearing cache", async () => {
    const queryClient = client();
    seed(queryClient);

    expect(await applyCanonicalModelSwitch(queryClient, "thread-a", { eventSeq: 10, modelKey: "Model C" })).toBe(true);

    expect(queryClient.getQueryData(["threadSettings", "thread-a"])).toMatchObject({
      auto_approval: false,
      model_key: "Model C",
    });
    expect(queryClient.getQueryData(["thread", "thread-a"])).toMatchObject({ model_key: "Model C" });
    expect(queryClient.getQueryData<Array<{ id: string; model_key: string }>>(["rootThreads"])).toEqual([
      { id: "thread-a", model_key: "Model C" },
      { id: "thread-b", model_key: "Model B" },
    ]);
    expect(queryClient.getQueryData(["threads"])).toEqual([{ id: "thread-a", model_key: "Model C" }]);
    expect(queryClient.getQueryData(["threadChildren", "parent"])).toEqual([
      { id: "thread-a", model_key: "Model C" },
    ]);
  });

  it("can install the first model event before a settings snapshot resolves", async () => {
    const queryClient = client();

    await applyCanonicalModelSwitch(queryClient, "thread-a", { eventSeq: 15, modelKey: "Terminal Model" });

    expect(queryClient.getQueryData(["threadSettings", "thread-a"])).toEqual({
      model_key: "Terminal Model",
    });
  });

  it("uses canonical event order for rapid opposing writes", async () => {
    const queryClient = client();
    seed(queryClient);

    await applyCanonicalModelSwitch(queryClient, "thread-a", { eventSeq: 20, modelKey: "Egg model" });
    await applyCanonicalModelSwitch(queryClient, "thread-a", { eventSeq: 21, modelKey: "EggW model" });
    expect(await applyCanonicalModelSwitch(queryClient, "thread-a", { eventSeq: 20, modelKey: "Egg model" })).toBe(false);

    expect(queryClient.getQueryData(["threadSettings", "thread-a"])).toMatchObject({
      model_key: "EggW model",
    });
  });

  it("fences an older handler that is still cancelling stale HTTP requests", async () => {
    const queryClient = client();
    seed(queryClient);
    const originalCancel = queryClient.cancelQueries.bind(queryClient);
    let releaseFirst!: () => void;
    const firstBlocked = new Promise<void>((resolve) => { releaseFirst = resolve; });
    let calls = 0;
    vi.spyOn(queryClient, "cancelQueries").mockImplementation(async (filters) => {
      calls += 1;
      if (calls === 1) await firstBlocked;
      return originalCancel(filters);
    });

    const older = applyCanonicalModelSwitch(queryClient, "thread-a", { eventSeq: 30, modelKey: "Older" });
    await vi.waitFor(() => expect(calls).toBeGreaterThan(0));
    const newer = applyCanonicalModelSwitch(queryClient, "thread-a", { eventSeq: 31, modelKey: "Newer" });
    releaseFirst();

    expect(await older).toBe(false);
    expect(await newer).toBe(true);
    expect(queryClient.getQueryData(["threadSettings", "thread-a"])).toMatchObject({ model_key: "Newer" });
  });

  it("reconciles attach/reconnect and local writes through targeted active queries", async () => {
    const queryClient = client();
    seed(queryClient);
    const cancel = vi.spyOn(queryClient, "cancelQueries");
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");

    await reconcileThreadModelSnapshot(queryClient, "thread-a");
    await refreshThreadModelQueries(queryClient, "thread-a");

    expect(cancel).toHaveBeenCalledWith({ queryKey: ["threadSettings", "thread-a"], exact: true });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["threadSettings", "thread-a"],
      exact: true,
      refetchType: "active",
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["thread", "thread-a"],
      exact: true,
      refetchType: "active",
    });
  });
});
