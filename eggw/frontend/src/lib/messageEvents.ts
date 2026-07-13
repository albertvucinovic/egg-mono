import type { ThreadEventEnvelope } from "./eventSync";
import type { Message } from "./store";

/** Build the immediate transcript representation from the canonical feed. */
export function messageFromCreateEvent(event: ThreadEventEnvelope): Message | null {
  if (event.type !== "msg.create" || !event.msg_id || typeof event.payload.role !== "string") {
    return null;
  }
  // The backend projection copies the exact msg.create payload, then supplies
  // msg_id and event timestamp at the projection boundary. Derived API fields
  // (content_text, tokens, optimizer metadata) arrive on the targeted refetch.
  return {
    ...(event.payload as Omit<Message, "id">),
    id: event.msg_id,
    timestamp: event.ts,
    event_seq: event.event_seq,
  };
}
