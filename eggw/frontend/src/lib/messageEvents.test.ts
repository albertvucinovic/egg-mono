import { describe, expect, it } from "vitest";
import { messageFromCreateEvent } from "./messageEvents";

it("maps the actual canonical msg.create envelope without inventing normalized fields", () => {
  const message = messageFromCreateEvent({
    event_id: "event-a",
    event_seq: 42,
    type: "msg.create",
    ts: "2026-07-10T19:30:00Z",
    msg_id: "message-a",
    invoke_id: "invoke-a",
    chunk_seq: null,
    payload: {
      role: "tool",
      content: "done",
      tool_call_id: "call-a",
      name: "bash",
    },
  });

  expect(message).toEqual({
    id: "message-a",
    role: "tool",
    content: "done",
    tool_call_id: "call-a",
    name: "bash",
    timestamp: "2026-07-10T19:30:00Z",
    event_seq: 42,
  });
  expect(message).not.toHaveProperty("content_text");
  expect(message).not.toHaveProperty("tokens");
});
