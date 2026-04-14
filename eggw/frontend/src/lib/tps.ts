export function formatStreamingTps(streamingTps: number | null | undefined): string | null {
  if (typeof streamingTps !== "number" || !Number.isFinite(streamingTps) || streamingTps <= 0) {
    return null;
  }
  return streamingTps < 10 ? streamingTps.toFixed(1) : Math.round(streamingTps).toString();
}