import { describe, expect, it } from "vitest";
import { AutocompleteRequestCoordinator, isAutocompleteEligible } from "./autocomplete";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("autocomplete request ownership", () => {
  it("permits global three-character ID/path tokens plus command and shell contexts", () => {
    expect(isAutocompleteEligible("ordinary pr", 11)).toBe(false);
    expect(isAutocompleteEligible("mention abc", 11)).toBe(true);
    expect(isAutocompleteEligible("open ./", 7)).toBe(true);
    expect(isAutocompleteEligible("copy @x", 7)).toBe(true);
    expect(isAutocompleteEligible("mention a__", 11)).toBe(false);
    expect(isAutocompleteEligible("/model op", 9)).toBe(true);
    expect(isAutocompleteEligible("$ echo", 6)).toBe(true);
    expect(isAutocompleteEligible("open ./src/li", 13)).toBe(true);
    expect(isAutocompleteEligible("open ../other", 13)).toBe(true);
  });

  it("aborts the prior request and fences a reverse-order stale response", async () => {
    const requests: Array<{ signal: AbortSignal; result: ReturnType<typeof deferred<Array<{ display: string; insert: string }>>> }> = [];
    let active = 0;
    let maxActive = 0;
    const coordinator = new AutocompleteRequestCoordinator(async (_line, _cursor, _threadId, signal) => {
      const result = deferred<Array<{ display: string; insert: string }>>();
      requests.push({ signal, result });
      active += 1;
      maxActive = Math.max(maxActive, active);
      signal.addEventListener("abort", () => { active -= 1; }, { once: true });
      const value = await result.promise;
      if (!signal.aborted) active -= 1;
      return value;
    });

    const first = coordinator.request("/h", 2, "thread-a");
    const second = coordinator.request("/he", 3, "thread-a");
    expect(requests[0].signal.aborted).toBe(true);
    expect(maxActive).toBe(1);

    requests[1].result.resolve([{ display: "/help", insert: "/help" }]);
    await expect(second).resolves.toEqual([{ display: "/help", insert: "/help" }]);
    requests[0].result.resolve([{ display: "/history", insert: "/history" }]);
    await expect(first).resolves.toBeNull();
  });
});
