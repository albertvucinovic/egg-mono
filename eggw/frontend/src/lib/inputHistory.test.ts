import { describe, expect, it } from "vitest";
import {
  initialInputHistoryState,
  INPUT_SUBMITTED_EVENT_TYPE,
  inputHistoryQueryKey,
  isTextareaHistoryBoundary,
  navigateInputHistory,
  resetInputHistory,
} from "./inputHistory";

describe("input history navigation", () => {
  it("uses a thread-scoped query key", () => {
    expect(inputHistoryQueryKey("thread-a")).toEqual(["inputHistory", "thread-a"]);
    expect(INPUT_SUBMITTED_EVENT_TYPE).toBe("input.submitted");
  });

  it("walks older/newer inputs and restores the exact unsent draft", () => {
    let state = initialInputHistoryState(["oldest", "newest\nline"]);
    let result = navigateInputHistory(state, "older", "unsent\ndraft")!;
    state = result.state;
    expect(result.value).toBe("newest\nline");

    result = navigateInputHistory(state, "older", result.value)!;
    state = result.state;
    expect(result.value).toBe("oldest");
    expect(navigateInputHistory(state, "older", result.value)?.value).toBe("oldest");

    result = navigateInputHistory(state, "newer", result.value)!;
    state = result.state;
    expect(result.value).toBe("newest\nline");
    result = navigateInputHistory(state, "newer", result.value)!;
    expect(result.value).toBe("unsent\ndraft");
    expect(result.state.position).toBeNull();
  });

  it("uses first/last visual input lines and ignores selections", () => {
    const value = "first\nmiddle\nlast";
    expect(isTextareaHistoryBoundary({ value, selectionStart: 2, selectionEnd: 2 }, "older")).toBe(true);
    expect(isTextareaHistoryBoundary({ value, selectionStart: 7, selectionEnd: 7 }, "older")).toBe(false);
    expect(isTextareaHistoryBoundary({ value, selectionStart: 8, selectionEnd: 8 }, "newer")).toBe(false);
    expect(isTextareaHistoryBoundary({ value, selectionStart: value.length, selectionEnd: value.length }, "newer")).toBe(true);
    expect(isTextareaHistoryBoundary({ value, selectionStart: 0, selectionEnd: 2 }, "older")).toBe(false);
  });

  it("resets traversal without changing entries", () => {
    const active = navigateInputHistory(initialInputHistoryState(["one"]), "older", "draft")!.state;
    expect(resetInputHistory(active)).toEqual({ entries: ["one"], position: null, draft: "" });
  });

  it("does not recall anything before the thread history is loaded", () => {
    expect(navigateInputHistory(initialInputHistoryState(), "older", "draft")).toBeNull();
  });
});
