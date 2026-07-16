import type { StreamingProviderRequest, StreamingToolOutput } from "./store";
import { isGetUserMessageTool } from "./toolPresentation";

export function shouldUpdateLiveTiming(
  isStreaming: boolean,
  toolOutputs: Record<string, StreamingToolOutput>,
  providerRequest: StreamingProviderRequest | null,
  streamingKind: string | null = null,
): boolean {
  const activeTools = Object.values(toolOutputs).filter((tool) => !tool.finished);
  const hasTimedNonWaitTool = activeTools.some((tool) => (
    !isGetUserMessageTool(tool.name) && Boolean(tool.startedAtMs || tool.timeout)
  ));
  const waitOnlyToolStream = streamingKind === "tool"
    && activeTools.length > 0
    && activeTools.every((tool) => isGetUserMessageTool(tool.name))
    && !providerRequest;
  return Boolean(providerRequest)
    || hasTimedNonWaitTool
    || (isStreaming && !waitOnlyToolStream);
}

export interface LiveTimingSnapshot {
  provider: string | null;
  generic: string | null;
  tools: Record<string, { elapsed: string | null; timeout: string | null }>;
}

function elapsedSecondsText(startedAtMs: number | null | undefined, nowMs: number, label = "streaming"): string | null {
  const started = Number(startedAtMs);
  if (!Number.isFinite(started) || started <= 0) return null;
  const elapsedSec = Math.max(0, (nowMs - started) / 1000);
  return `${label} ${elapsedSec.toFixed(0)}s`;
}

function timeoutCountdown(timeout: StreamingToolOutput["timeout"], nowMs: number): string | null {
  if (!timeout) return null;
  const limit = Number(timeout.timeoutSec);
  const startedAtMs = Number(timeout.startedAtMs);
  if (!Number.isFinite(limit) || limit <= 0 || !Number.isFinite(startedAtMs) || startedAtMs <= 0) return null;
  const elapsedSec = Math.max(0, (nowMs - startedAtMs) / 1000);
  return `timeout in ${Math.max(0, limit - elapsedSec).toFixed(0)}s (limit ${limit.toFixed(0)}s)`;
}

function providerTimingText(request: StreamingProviderRequest | null, nowMs: number): string | null {
  if (!request) return null;
  const elapsed = elapsedSecondsText(request.startedAtMs, nowMs);
  if (!elapsed) return null;
  const limit = Number(request.timeoutSec || 0);
  return Number.isFinite(limit) && limit > 0
    ? `${elapsed} (limit ${limit.toFixed(0)}s)`
    : elapsed;
}

/** Compute only over active live tools; transcript history is never examined. */
export function liveTimingSnapshot(
  nowMs: number,
  isStreaming: boolean,
  streamingKind: string | null,
  streamingStartedAtMs: number | null,
  providerRequest: StreamingProviderRequest | null,
  toolOutputs: Record<string, StreamingToolOutput>,
): LiveTimingSnapshot {
  const tools: LiveTimingSnapshot["tools"] = {};
  let waitOnly = streamingKind === "tool";
  let activeCount = 0;
  for (const [id, tool] of Object.entries(toolOutputs)) {
    if (tool.finished) continue;
    activeCount += 1;
    const wait = isGetUserMessageTool(tool.name);
    waitOnly = waitOnly && wait;
    tools[id] = wait
      ? { elapsed: null, timeout: null }
      : {
          elapsed: tool.startedAtMs ? elapsedSecondsText(tool.startedAtMs, nowMs, "running") : null,
          timeout: timeoutCountdown(tool.timeout, nowMs),
        };
  }
  waitOnly = waitOnly && activeCount > 0;
  const provider = streamingKind === "llm"
    ? providerTimingText(providerRequest, nowMs) || elapsedSecondsText(streamingStartedAtMs, nowMs)
    : null;
  return {
    provider,
    generic: isStreaming && streamingKind !== "llm" && !waitOnly
      ? elapsedSecondsText(streamingStartedAtMs, nowMs)
      : null,
    tools,
  };
}
