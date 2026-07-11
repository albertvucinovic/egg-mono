export interface ThreadEphemeralState {
  isCurrent: boolean;
  isStreaming: boolean;
  connectionStatus?: string;
  hasRetainedTools: boolean;
}

export function canEvictThreadEphemeralState(state: ThreadEphemeralState): boolean {
  return !state.isCurrent
    && !state.isStreaming
    && state.connectionStatus !== "connecting"
    && state.connectionStatus !== "connected"
    && state.connectionStatus !== "reconnecting"
    && !state.hasRetainedTools;
}
