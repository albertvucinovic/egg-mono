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
