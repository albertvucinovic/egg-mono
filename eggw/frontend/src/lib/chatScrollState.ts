export type LiveEdgeState = "following" | "detached";

export type LiveEdgeEvent =
  | { type: "user_toward_history" }
  | { type: "user_reached_live_edge" }
  | { type: "thread_changed" }
  | { type: "content_mutated" }
  | { type: "programmatic_scroll" };

/**
 * Following is user-intent owned. Geometry and programmatic writes are effects,
 * not evidence that the reader chose to leave or re-enter the live edge.
 */
export function reduceLiveEdgeState(state: LiveEdgeState, event: LiveEdgeEvent): LiveEdgeState {
  switch (event.type) {
    case "user_toward_history":
      return "detached";
    case "user_reached_live_edge":
    case "thread_changed":
      return "following";
    case "content_mutated":
    case "programmatic_scroll":
      return state;
  }
}

export type HistoryDemandPhase = "idle" | "revealing" | "fetching";

export interface HistoryDemandState {
  phase: HistoryDemandPhase;
  pending: boolean;
}

export interface HistoryAvailability {
  canReveal: boolean;
  canFetch: boolean;
}

export type HistoryDemandEvent =
  | { type: "older_intent" }
  | { type: "settled" }
  | { type: "reset" };

export const IDLE_HISTORY_DEMAND: HistoryDemandState = { phase: "idle", pending: false };

function nextHistoryPhase(availability: HistoryAvailability): HistoryDemandPhase {
  if (availability.canReveal) return "revealing";
  if (availability.canFetch) return "fetching";
  return "idle";
}

/**
 * Serialize top-demand work and retain at most one intent while a reveal/fetch
 * is in flight. Loaded-but-unmounted history always wins over network fetches.
 */
export function reduceHistoryDemand(
  state: HistoryDemandState,
  event: HistoryDemandEvent,
  availability: HistoryAvailability,
): HistoryDemandState {
  if (event.type === "reset") return IDLE_HISTORY_DEMAND;

  if (event.type === "older_intent") {
    if (state.phase !== "idle") return state.pending ? state : { ...state, pending: true };
    return { phase: nextHistoryPhase(availability), pending: false };
  }

  if (!state.pending) return IDLE_HISTORY_DEMAND;
  return { phase: nextHistoryPhase(availability), pending: false };
}
