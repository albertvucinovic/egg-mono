export interface EggwPerformanceCounters {
  chatPanelCommits: number;
  transcriptCommits: number;
  chatPanelCommitDurationMs: number;
  transcriptCommitDurationMs: number;
  streamingTextFlushes: number;
  streamingToolOutputFlushes: number;
  streamingToolArgumentFlushes: number;
  streamingToolPreviewFlushes: number;
}

declare global {
  interface Window {
    __EGGW_PERFORMANCE__?: EggwPerformanceCounters;
  }
}

const EMPTY_COUNTERS: EggwPerformanceCounters = {
  chatPanelCommits: 0,
  transcriptCommits: 0,
  chatPanelCommitDurationMs: 0,
  transcriptCommitDurationMs: 0,
  streamingTextFlushes: 0,
  streamingToolOutputFlushes: 0,
  streamingToolArgumentFlushes: 0,
  streamingToolPreviewFlushes: 0,
};

function counters(): EggwPerformanceCounters | null {
  if (process.env.NODE_ENV === "production" || typeof window === "undefined") return null;
  window.__EGGW_PERFORMANCE__ ||= { ...EMPTY_COUNTERS };
  return window.__EGGW_PERFORMANCE__;
}

export function recordReactCommit(id: "ChatPanel" | "StaticTranscript", durationMs: number): void {
  const current = counters();
  if (!current) return;
  if (id === "ChatPanel") {
    current.chatPanelCommits += 1;
    current.chatPanelCommitDurationMs += durationMs;
  } else {
    current.transcriptCommits += 1;
    current.transcriptCommitDurationMs += durationMs;
  }
}

export function recordStreamingFlush(
  kind: "text" | "toolOutput" | "toolArguments" | "toolPreview",
): void {
  const current = counters();
  if (!current) return;
  if (kind === "text") current.streamingTextFlushes += 1;
  else if (kind === "toolOutput") current.streamingToolOutputFlushes += 1;
  else if (kind === "toolArguments") current.streamingToolArgumentFlushes += 1;
  else current.streamingToolPreviewFlushes += 1;
}
