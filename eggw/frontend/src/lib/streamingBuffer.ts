/**
 * Global streaming buffer that bypasses React state management.
 *
 * This solves the O(n²) problem with React state updates during streaming:
 * - Zustand array spread [...arr, item] is O(n) per update = O(n²) total
 * - React re-renders on every state change
 *
 * Instead, we use a mutable buffer that:
 * - Accumulates text with O(1) array push (no spread)
 * - Components read directly from buffer
 * - Only triggers React when streaming starts/stops
 */

type StreamingListener = () => void;

class StreamingBuffer {
  // Content chunks - mutable array, O(1) push
  contentChunks: string[] = [];
  reasoningChunks: string[] = [];
  reasoningSummaryChunks: string[] = [];
  toolOutputChunks: Map<string, string[]> = new Map();
  toolCalls: Map<string, { name: string; arguments: string }> = new Map();

  // Listeners for DOM updates (called on every chunk)
  private contentListeners: Set<StreamingListener> = new Set();
  private reasoningListeners: Set<StreamingListener> = new Set();
  private toolOutputListeners: Set<StreamingListener> = new Set();

  // Append content chunk - O(1)
  appendContent(chunk: string) {
    this.contentChunks.push(chunk);
    this.notifyContentListeners();
  }

  // Append reasoning chunk - O(1)
  appendReasoning(chunk: string) {
    this.reasoningChunks.push(chunk);
    this.notifyReasoningListeners();
  }

  // Append display-only reasoning summary chunk - O(1)
  appendReasoningSummary(chunk: string) {
    this.reasoningSummaryChunks.push(chunk);
    this.notifyReasoningListeners();
  }

  // Append tool output chunk - O(1). Key is normally the tool_call_id.
  appendToolOutput(key: string, chunk: string) {
    const chunks = this.toolOutputChunks.get(key);
    if (chunks) {
      chunks.push(chunk);
    } else {
      this.toolOutputChunks.set(key, [chunk]);
    }
    this.notifyToolOutputListeners();
  }

  // Append tool call arguments - O(1)
  appendToolCallArgs(tcId: string, name: string, argsDelta: string) {
    const existing = this.toolCalls.get(tcId);
    if (existing) {
      existing.arguments += argsDelta;
      if (name) existing.name = name;
    } else {
      this.toolCalls.set(tcId, { name, arguments: argsDelta });
    }
  }

  // Clear all buffers
  clear() {
    this.contentChunks = [];
    this.reasoningChunks = [];
    this.reasoningSummaryChunks = [];
    this.toolOutputChunks = new Map();
    this.toolCalls = new Map();
  }

  // Get joined content (for final display after streaming)
  getContent(): string {
    return this.contentChunks.join('');
  }

  getReasoning(): string {
    return this.reasoningChunks.join('');
  }

  getReasoningSummary(): string {
    return this.reasoningSummaryChunks.join('');
  }

  getToolOutput(key: string): string {
    return (this.toolOutputChunks.get(key) || []).join('');
  }

  // Subscribe to content updates
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

  private notifyContentListeners() {
    this.contentListeners.forEach(l => l());
  }

  private notifyReasoningListeners() {
    this.reasoningListeners.forEach(l => l());
  }

  private notifyToolOutputListeners() {
    this.toolOutputListeners.forEach(l => l());
  }
}

// Global singleton
export const streamingBuffer = new StreamingBuffer();
