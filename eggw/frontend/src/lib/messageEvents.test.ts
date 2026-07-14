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


it("preserves canonical consumed get-user answer metadata", () => {
  const message = messageFromCreateEvent({
    event_id: "event-answer",
    event_seq: 43,
    type: "msg.create",
    ts: "2026-07-14T00:00:00Z",
    msg_id: "answer-a",
    invoke_id: "invoke-a",
    chunk_seq: null,
    payload: {
      role: "user",
      content: "Continue",
      consumed_by_tool_name: "get_user_message_while_preserving_llm_turn",
      consumed_by_tool_call_id: "call-get-user",
      origin: "manager_message",
      from_thread_id: "manager-a",
    },
  });

  expect(message).toMatchObject({
    role: "user",
    consumed_by_tool_name: "get_user_message_while_preserving_llm_turn",
    consumed_by_tool_call_id: "call-get-user",
    origin: "manager_message",
    from_thread_id: "manager-a",
  });
});
