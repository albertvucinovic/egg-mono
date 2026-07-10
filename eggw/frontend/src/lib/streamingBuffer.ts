/**
 * Thread-keyed mutable streaming buffers. Text chunks bypass React so append is
 * O(1), while ownership remains explicit across navigation and reconnects.
 */
type StreamingListener = () => void;

export class StreamingBuffer {
  contentChunks: string[] = [];
  reasoningChunks: string[] = [];
  reasoningSummaryChunks: string[] = [];
  toolOutputChunks: Map<string, string[]> = new Map();
  toolCalls: Map<string, { name: string; arguments: string }> = new Map();

  private contentListeners = new Set<StreamingListener>();
  private reasoningListeners = new Set<StreamingListener>();
  private toolOutputListeners = new Set<StreamingListener>();

  appendContent(chunk: string) {
    this.contentChunks.push(chunk);
    this.contentListeners.forEach((listener) => listener());
  }

  appendReasoning(chunk: string) {
    this.reasoningChunks.push(chunk);
    this.reasoningListeners.forEach((listener) => listener());
  }

  appendReasoningSummary(chunk: string) {
    this.reasoningSummaryChunks.push(chunk);
    this.reasoningListeners.forEach((listener) => listener());
  }

  appendToolOutput(key: string, chunk: string) {
    const chunks = this.toolOutputChunks.get(key);
    if (chunks) chunks.push(chunk);
    else this.toolOutputChunks.set(key, [chunk]);
    this.toolOutputListeners.forEach((listener) => listener());
  }

  appendToolCallArgs(tcId: string, name: string, argsDelta: string) {
    const existing = this.toolCalls.get(tcId);
    if (existing) {
      existing.arguments += argsDelta;
      if (name) existing.name = name;
    } else {
      this.toolCalls.set(tcId, { name, arguments: argsDelta });
    }
  }

  clear() {
    this.contentChunks = [];
    this.reasoningChunks = [];
    this.reasoningSummaryChunks = [];
    this.toolOutputChunks = new Map();
    this.toolCalls = new Map();
  }

  getContent(): string { return this.contentChunks.join(""); }
  getReasoning(): string { return this.reasoningChunks.join(""); }
  getReasoningSummary(): string { return this.reasoningSummaryChunks.join(""); }
  getToolOutput(key: string): string { return (this.toolOutputChunks.get(key) || []).join(""); }

  subscribeContent(listener: StreamingListener): () => void {
    this.contentListeners.add(listener);
    return () => this.contentListeners.delete(listener);
  }
  subscribeReasoning(listener: StreamingListener): () => void {
    this.reasoningListeners.add(listener);
    return () => this.reasoningListeners.delete(listener);
  }
  subscribeToolOutput(listener: StreamingListener): () => void {
    this.toolOutputListeners.add(listener);
    return () => this.toolOutputListeners.delete(listener);
  }
}

const buffers = new Map<string, StreamingBuffer>();

export function streamingBufferForThread(threadId: string): StreamingBuffer {
  const existing = buffers.get(threadId);
  if (existing) return existing;
  const created = new StreamingBuffer();
  buffers.set(threadId, created);
  return created;
}
