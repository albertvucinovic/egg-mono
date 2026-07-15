import type { StreamingProviderRequest, StreamingToolOutput } from "./store";
import { isGetUserMessageTool } from "./toolPresentation";

export function shouldUpdateLiveTiming(
  isStreaming: boolean,
  toolOutputs: Record<string, StreamingToolOutput>,
  providerRequest: StreamingProviderRequest | null,
): boolean {
  return isStreaming
    || Boolean(providerRequest)
    || Object.values(toolOutputs).some((tool) => (
      !tool.finished && !isGetUserMessageTool(tool.name) && Boolean(tool.startedAtMs || tool.timeout)
    ));
}
