import type { StreamingBuffer } from "./streamingBuffer";
import { toolDisplayName } from "./toolPresentation";

export interface StreamingDeltaNotifications {
  toolCall?: { id: string; name: string };
  toolOutput?: { id: string; name: string; suppressed: boolean };
}

/**
 * Apply a high-rate stream.delta without touching React or Zustand. The caller
 * receives only semantic metadata transitions that may need a store update;
 * chunk bodies remain in the thread-owned mutable buffer.
 */
export function applyStreamingDelta(
  buffer: StreamingBuffer,
  payload: Record<string, any>,
): StreamingDeltaNotifications {
  if (payload.reason) buffer.appendReasoning(payload.reason);
  if (typeof payload.reasoning_summary === "string" && payload.reasoning_summary) {
    buffer.appendReasoningSummary(payload.reasoning_summary);
  }
  if (payload.text) buffer.appendContent(payload.text);

  let toolOutput: StreamingDeltaNotifications["toolOutput"];
  if (payload.tool) {
    const tool = payload.tool;
    // Canonical tool-output deltas carry call identity. Never collapse malformed
    // concurrent output into a shared name/"tool" bucket.
    const id = String(tool.id || "").trim();
    if (id) {
      const name = toolDisplayName(tool.name, id, "Tool result");
      const suppressed = Boolean(tool.suppressed);
      const metadataChanged = buffer.registerToolOutput(id, suppressed);
      if (tool.text) buffer.appendToolOutput(id, tool.text);
      if (metadataChanged) {
        toolOutput = { id, name, suppressed };
      }
    }
  }

  let toolCall: StreamingDeltaNotifications["toolCall"];
  if (payload.tool_call) {
    const call = payload.tool_call;
    const id = String(call.id || "");
    const name = toolDisplayName(call.name, id, "Tool call");
    const argumentsDelta = typeof call.arguments_delta === "string" ? call.arguments_delta : "";
    if (id && argumentsDelta && buffer.appendToolCallArgs(id, name, argumentsDelta)) {
      toolCall = { id, name };
    }
  }

  return { toolCall, toolOutput };
}
