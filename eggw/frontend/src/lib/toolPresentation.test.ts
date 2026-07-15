import { describe, expect, it } from "vitest";
import type { Message } from "./store";
import {
  correlateHiddenToolDetails,
  getUserAnswerToolCallId,
  getUserToolCallIds,
  isGetUserMessageTool,
  resolveToolResultNames,
  toolDisplayName,
  type HiddenToolDetail,
} from "./toolPresentation";

function call(id: string, name: string, body: string): HiddenToolDetail {
  return { kind: "tool_calls", source: "tool_call", tool_call_id: id, name, header: name, body };
}

function result(id: string | undefined, name: string, body: string): HiddenToolDetail {
  return { kind: "tool_results", source: "tool_result", tool_call_id: id, name, header: name, body };
}

describe("tool transcript presentation", () => {
  it("pairs simultaneous same-name calls only by exact identity", () => {
    const details = correlateHiddenToolDetails([
      call("call-a", "bash", "ARG_A"),
      call("call-b", "bash", "ARG_B"),
      result("call-b", "bash", "RESULT_B"),
      result("call-a", "bash", "RESULT_A"),
    ]);

    expect(details.map((detail) => detail.name)).toEqual(["bash", "bash"]);
    expect(details[0].body).toContain("ARG_A");
    expect(details[0].body).toContain("RESULT_A");
    expect(details[0].body).not.toContain("RESULT_B");
    expect(details[1].body).toContain("ARG_B");
    expect(details[1].body).toContain("RESULT_B");
  });

  it("refuses FIFO pairing when an identical tool_call_id is duplicated", () => {
    const details = correlateHiddenToolDetails([
      call("call-duplicate", "bash", "ARG_FIRST"),
      call("call-duplicate", "bash", "ARG_SECOND"),
      result("call-duplicate", "bash", "RESULT_FIRST"),
      result("call-duplicate", "bash", "RESULT_SECOND"),
    ]);

    expect(details).toHaveLength(4);
    expect(details[0].body).toContain("ARG_FIRST");
    expect(details[0].body).toContain("(not found in the loaded transcript)");
    expect(details[0].body).not.toContain("RESULT_FIRST");
    expect(details[1].body).toContain("ARG_SECOND");
    expect(details[1].body).not.toContain("RESULT_SECOND");
    expect(details.slice(2).map((detail) => detail.body)).toEqual(["RESULT_FIRST", "RESULT_SECOND"]);
  });

  it("keeps missing halves and ID-less legacy previews separate", () => {
    const details = correlateHiddenToolDetails([
      call("call-pending", "python", "ARG_PENDING"),
      result("call-orphan", "Tool result · call-orphan", "ORPHAN_RESULT"),
      result(undefined, "bash", "IDLESS_RESULT"),
      { kind: "tool_results", source: "tool_stream", name: "python", header: "preview", body: "STREAM_PREVIEW" },
    ]);

    expect(details).toHaveLength(4);
    expect(details[0].body).toContain("(not found in the loaded transcript)");
    expect(details[0].body).not.toContain("STREAM_PREVIEW");
    expect(details.slice(1).map((detail) => detail.body)).toEqual([
      "ORPHAN_RESULT",
      "IDLESS_RESULT",
      "STREAM_PREVIEW",
    ]);
  });

  it("resolves result names by exact ID and rejects ambiguous reused IDs", () => {
    const messages: Message[] = [
      { id: "calls", role: "assistant", tool_calls: [
        { id: "call-a", name: "bash", arguments: {} },
        { id: "call-b", function: { name: "python", arguments: "{}" } },
        { id: "call-reused", name: "bash", arguments: {} },
        { id: "call-reused", name: "python", arguments: {} },
      ] },
      { id: "result-b", role: "tool", tool_call_id: "call-b", content: "B" },
      { id: "result-a", role: "tool", tool_call_id: "call-a", content: "A" },
      { id: "result-reused", role: "tool", tool_call_id: "call-reused", content: "?" },
      { id: "result-before-call", role: "tool", tool_call_id: "call-later", content: "old" },
      { id: "later-call", role: "assistant", tool_calls: [{ id: "call-later", name: "bash", arguments: {} }] },
    ];

    const resolved = resolveToolResultNames(messages);
    expect(resolved[1].name).toBe("python");
    expect(resolved[2].name).toBe("bash");
    expect(resolved[3].name).toBeUndefined();
    expect(resolved[4].name).toBeUndefined();
    expect(messages[1].name).toBeUndefined();
  });

  it("recognizes get-user call and answer lifecycle only from durable identity metadata", () => {
    expect(isGetUserMessageTool("get_user_message_while_preserving_llm_turn")).toBe(true);
    expect(isGetUserMessageTool("wait")).toBe(false);
    expect(getUserToolCallIds({
      id: "calls",
      role: "assistant",
      tool_calls: [
        { id: "call-get-user", name: "get_user_message_while_preserving_llm_turn", arguments: {} },
        { id: "call-bash", name: "bash", arguments: {} },
      ],
    })).toEqual(["call-get-user"]);
    expect(getUserAnswerToolCallId({
      id: "answer",
      role: "user",
      content: "Continue",
      consumed_by_tool_name: "get_user_message_while_preserving_llm_turn",
      consumed_by_tool_call_id: "call-get-user",
    })).toBe("call-get-user");
    expect(getUserAnswerToolCallId({ id: "ordinary", role: "user", content: "Continue" })).toBe("");
  });

  it("uses stable identity instead of a bare tool placeholder", () => {
    expect(toolDisplayName("", "call-1234567890abcdef", "Tool result")).toBe("Tool result · 567890abcdef");
    expect(toolDisplayName("", "", "Tool result")).toBe("Tool result");
  });
});
