import type { AutocompleteSuggestion } from "./api";

export function isAutocompleteEligible(line: string, cursor: number): boolean {
  const prefix = line.slice(0, Math.max(0, cursor));
  if (prefix.startsWith("/") || prefix.startsWith("$")) return true;
  const token = prefix.match(/\S+$/)?.[0] || "";
  return token.startsWith("./") || token.startsWith("../") || token.startsWith("~/");
}

type AutocompleteFetcher = (
  line: string,
  cursor: number,
  threadId: string,
  signal: AbortSignal,
) => Promise<AutocompleteSuggestion[]>;

/** One active request plus a sequence fence for fetchers that race cancellation. */
export class AutocompleteRequestCoordinator {
  private sequence = 0;
  private controller: AbortController | null = null;

  constructor(private readonly fetcher: AutocompleteFetcher) {}

  cancel(): void {
    this.sequence += 1;
    this.controller?.abort();
    this.controller = null;
  }

  async request(line: string, cursor: number, threadId: string): Promise<AutocompleteSuggestion[] | null> {
    this.cancel();
    const requestSequence = this.sequence;
    const controller = new AbortController();
    this.controller = controller;
    try {
      const result = await this.fetcher(line, cursor, threadId, controller.signal);
      return requestSequence === this.sequence && !controller.signal.aborted ? result : null;
    } catch (error) {
      if (controller.signal.aborted || requestSequence !== this.sequence) return null;
      throw error;
    } finally {
      if (requestSequence === this.sequence) this.controller = null;
    }
  }
}
