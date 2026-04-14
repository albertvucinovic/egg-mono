export function formatStreamingTps(streamingTps: number | null | undefined): string | null {
  if (typeof streamingTps !== "number" || !Number.isFinite(streamingTps) || streamingTps <= 0) {
    return null;
  }
  return streamingTps < 10 ? `${streamingTps.toFixed(1)} tps` : `${Math.round(streamingTps)} tps`;
}

export function formatTokenCount(tokens: number | null | undefined): string | null {
  if (typeof tokens !== "number" || !Number.isFinite(tokens) || tokens <= 0) {
    return null;
  }
  const rounded = Math.round(tokens);
  if (rounded < 1000) {
    return `${rounded} tok`;
  }
  return `${(rounded / 1000).toFixed(2)}k tok`;
}