import type { StreamingProviderRequest, StreamingToolOutput } from "./store";

export function shouldUpdateLiveTiming(
  isStreaming: boolean,
  toolOutputs: Record<string, StreamingToolOutput>,
  providerRequest: StreamingProviderRequest | null,
): boolean {
  return isStreaming
    || Boolean(providerRequest)
    || Object.values(toolOutputs).some((tool) => (
      !tool.finished && Boolean(tool.startedAtMs || tool.timeout)
    ));
}
