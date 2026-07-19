export interface ThreadEventEnvelope {
  event_id: string;
  event_seq: number;
  type: string;
  ts: string;
  msg_id: string | null;
  invoke_id: string | null;
  chunk_seq: number | null;
  payload: Record<string, unknown>;
}

export interface ThreadEventSyncState {
  threadId: string;
  lastEventSeq: number;
  lastEventId: string | null;
  activeInvokeId: string | null;
}

// A route owns the transport, but the thread owns its last semantically applied
// accepted cursor. Keeping this tiny state beside the thread-keyed streaming buffers lets
// navigation resume an in-flight invocation instead of replaying (and appending)
// its complete live output every time the user returns to the thread.
const retainedThreadEventSyncStates = new Map<string, ThreadEventSyncState>();

export function retainedThreadEventSyncState(
  threadId: string,
): ThreadEventSyncState | null {
  return retainedThreadEventSyncStates.get(threadId) || null;
}

export function retainThreadEventSyncState(
  state: ThreadEventSyncState,
): void {
  retainedThreadEventSyncStates.set(state.threadId, state);
}

export function evictThreadEventSyncState(threadId: string): void {
  retainedThreadEventSyncStates.delete(threadId);
}

export interface AcceptedThreadEvent {
  accepted: true;
  event: ThreadEventEnvelope;
  state: ThreadEventSyncState;
}

export interface RejectedThreadEvent {
  accepted: false;
  state: ThreadEventSyncState;
  reason: "invalid" | "stale_sequence" | "stale_invocation";
}

export type ThreadEventReduction = AcceptedThreadEvent | RejectedThreadEvent;

export function createThreadEventSyncState(
  threadId: string,
  lastEventSeq = -1,
  activeInvokeId: string | null = null,
): ThreadEventSyncState {
  return { threadId, lastEventSeq, lastEventId: null, activeInvokeId };
}

function parseEnvelope(raw: string): ThreadEventEnvelope | null {
  try {
    const parsed = JSON.parse(raw) as Partial<ThreadEventEnvelope>;
    if (
      !parsed ||
      typeof parsed.event_id !== "string" ||
      parsed.event_id.length === 0 ||
      !Number.isSafeInteger(parsed.event_seq) ||
      Number(parsed.event_seq) < 0 ||
      typeof parsed.type !== "string" ||
      typeof parsed.ts !== "string" ||
      !parsed.payload ||
      typeof parsed.payload !== "object" ||
      Array.isArray(parsed.payload)
    ) {
      return null;
    }
    return {
      event_id: parsed.event_id,
      event_seq: Number(parsed.event_seq),
      type: parsed.type,
      ts: parsed.ts,
      msg_id: typeof parsed.msg_id === "string" ? parsed.msg_id : null,
      invoke_id: typeof parsed.invoke_id === "string" ? parsed.invoke_id : null,
      chunk_seq: Number.isSafeInteger(parsed.chunk_seq) ? Number(parsed.chunk_seq) : null,
      payload: parsed.payload as Record<string, unknown>,
    };
  } catch {
    return null;
  }
}

/** Pure cursor/invocation reducer used before an SSE event can mutate UI state. */
export function reduceThreadEvent(
  state: ThreadEventSyncState,
  raw: string,
  expectedType?: string,
): ThreadEventReduction {
  const event = parseEnvelope(raw);
  if (!event || (expectedType && event.type !== expectedType)) {
    return { accepted: false, state, reason: "invalid" };
  }
  if (event.event_seq <= state.lastEventSeq || event.event_id === state.lastEventId) {
    return { accepted: false, state, reason: "stale_sequence" };
  }

  const runEvent = event.type.startsWith("stream.") || event.type.startsWith("tool_call.");
  if (
    runEvent &&
    state.activeInvokeId &&
    event.invoke_id !== state.activeInvokeId &&
    event.type !== "stream.open"
  ) {
    return { accepted: false, state, reason: "stale_invocation" };
  }

  let activeInvokeId = state.activeInvokeId;
  if (event.type === "stream.open") {
    if (!event.invoke_id) return { accepted: false, state, reason: "invalid" };
    activeInvokeId = event.invoke_id;
  } else if (runEvent && !activeInvokeId && event.invoke_id) {
    // A snapshot cursor can land after stream.open. Adopt the lease-fenced
    // invocation from the first later run event.
    activeInvokeId = event.invoke_id;
  }
  if (event.type === "stream.close" && (!event.invoke_id || event.invoke_id === activeInvokeId)) {
    activeInvokeId = null;
  }

  return {
    accepted: true,
    event,
    state: { ...state, lastEventSeq: event.event_seq, lastEventId: event.event_id, activeInvokeId },
  };
}
