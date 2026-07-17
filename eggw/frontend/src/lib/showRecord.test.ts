import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore, type ShowRecordTarget } from "./store";

const target: ShowRecordTarget = {
  record_id: "call-show",
  kind: "tool_declaration",
  thread_id: "thread-a",
  message_id: "message-a",
  tool_call_id: "call-show",
  event_seq: 10,
  watermark_event_seq: 12,
  label: "Tool declaration: bash",
  preview: "bash(echo hi)",
  paired_message_ids: ["result-a"],
  message: { id: "message-a", role: "assistant", content: "answer" },
  tool_call: { id: "call-show", name: "bash", arguments: { script: "echo hi" } },
};

describe("show record presentation target", () => {
  beforeEach(() => {
    useAppStore.setState(useAppStore.getInitialState(), true);
  });

  it("keeps authoritative targets thread-scoped and clears without mutating verbosity", () => {
    useAppStore.getState().setDisplayVerbosity("medium");
    useAppStore.getState().setShowRecordTarget("thread-a", target);
    expect(useAppStore.getState().showRecordTargetByThread["thread-a"]).toEqual(target);
    expect(useAppStore.getState().showRecordTargetByThread["thread-b"]).toBeUndefined();

    useAppStore.getState().setShowRecordTarget("thread-a", null);
    expect(useAppStore.getState().showRecordTargetByThread["thread-a"]).toBeUndefined();
    expect(useAppStore.getState().displayVerbosity).toBe("medium");
  });
});
