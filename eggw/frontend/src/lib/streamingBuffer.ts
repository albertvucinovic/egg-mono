/**
 * Thread-keyed mutable streaming buffers. High-rate chunks bypass React and
 * Zustand, while ownership remains explicit across navigation and reconnects.
 */
type StreamingListener = () => void;

export interface BufferedToolCall {
  name: string;
  argumentChunks: string[];
  argumentLength: number;
}

/** Coalesce an imperative DOM flush to at most one callback per frame. */
export class AnimationFrameCoalescer {
  private frameId: number | null = null;

  constructor(
    private readonly requestFrame: (callback: FrameRequestCallback) => number,
    private readonly cancelFrame: (id: number) => void,
  ) {}

  schedule(flush: () => void): void {
    if (this.frameId !== null) return;
    this.frameId = this.requestFrame(() => {
      this.frameId = null;
      flush();
    });
  }

  cancel(): void {
    if (this.frameId === null) return;
    this.cancelFrame(this.frameId);
    this.frameId = null;
  }
}


/**
 * Coalesce frequent notifications to a bounded interval while preserving the
 * latest buffered value. Used only for compact previews; full bodies still
 * append through the RAF path without delay.
 */
export class IntervalCoalescer<TimeoutId = ReturnType<typeof setTimeout>> {
  private timeoutId: TimeoutId | null = null;
  private lastFlushAt = Number.NEGATIVE_INFINITY;

  constructor(
    private readonly intervalMs: number,
    private readonly now: () => number,
    private readonly scheduleTimeout: (callback: () => void, delayMs: number) => TimeoutId,
    private readonly cancelTimeout: (id: TimeoutId) => void,
  ) {}

  schedule(flush: () => void): void {
    if (this.timeoutId !== null) return;
    const delayMs = Math.max(0, this.intervalMs - (this.now() - this.lastFlushAt));
    if (delayMs === 0) {
      this.lastFlushAt = this.now();
      flush();
      return;
    }
    this.timeoutId = this.scheduleTimeout(() => {
      this.timeoutId = null;
      this.lastFlushAt = this.now();
      flush();
    }, delayMs);
  }

  cancel(): void {
    if (this.timeoutId === null) return;
    this.cancelTimeout(this.timeoutId);
    this.timeoutId = null;
  }
}

export class StreamingBuffer {
  contentChunks: string[] = [];
  reasoningChunks: string[] = [];
  reasoningSummaryChunks: string[] = [];
  toolOutputChunks: Map<string, string[]> = new Map();
  toolCalls: Map<string, BufferedToolCall> = new Map();
  private seenToolOutputs = new Set<string>();
  private suppressedToolOutputs = new Set<string>();

  private contentListeners = new Set<StreamingListener>();
  private reasoningListeners = new Set<StreamingListener>();
  private toolOutputListeners = new Set<StreamingListener>();
  private toolCallListeners = new Set<StreamingListener>();

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

  /** Return true only when output metadata crosses a semantic boundary. */
  registerToolOutput(key: string, suppressed: boolean): boolean {
    const isNew = !this.seenToolOutputs.has(key);
    const suppressionStarted = suppressed && !this.suppressedToolOutputs.has(key);
    this.seenToolOutputs.add(key);
    if (suppressed) this.suppressedToolOutputs.add(key);
    return isNew || suppressionStarted;
  }

  /**
   * Append in O(1), retaining chunks rather than repeatedly copying the full
   * argument string. The return value reports a metadata transition that needs
   * one semantic Zustand publication (new call or newly learned name).
   */
  appendToolCallArgs(tcId: string, name: string, argsDelta: string): boolean {
    const existing = this.toolCalls.get(tcId);
    if (existing) {
      const metadataChanged = Boolean(name && name !== existing.name);
      if (metadataChanged) existing.name = name;
      if (argsDelta) {
        existing.argumentChunks.push(argsDelta);
        existing.argumentLength += argsDelta.length;
        this.toolCallListeners.forEach((listener) => listener());
      }
      return metadataChanged;
    }
    this.toolCalls.set(tcId, {
      name,
      argumentChunks: argsDelta ? [argsDelta] : [],
      argumentLength: argsDelta.length,
    });
    this.toolCallListeners.forEach((listener) => listener());
    return true;
  }

  /** Install an authoritative full argument value at a lifecycle boundary. */
  setToolCallArgs(tcId: string, name: string, args: string): boolean {
    const existing = this.toolCalls.get(tcId);
    if (!existing) {
      this.toolCalls.set(tcId, {
        name,
        argumentChunks: args ? [args] : [],
        argumentLength: args.length,
      });
      this.toolCallListeners.forEach((listener) => listener());
      return true;
    }
    const metadataChanged = Boolean(name && name !== existing.name);
    if (metadataChanged) existing.name = name;
    if (args && args.length >= existing.argumentLength) {
      existing.argumentChunks = [args];
      existing.argumentLength = args.length;
      this.toolCallListeners.forEach((listener) => listener());
    }
    return metadataChanged;
  }

  clearAssistantText() {
    this.contentChunks = [];
    this.reasoningChunks = [];
    this.reasoningSummaryChunks = [];
  }

  removeToolCall(toolCallId: string) {
    this.toolCalls.delete(toolCallId);
  }

  removeTool(toolCallId: string) {
    this.removeToolCall(toolCallId);
    this.toolOutputChunks.delete(toolCallId);
    this.seenToolOutputs.delete(toolCallId);
    this.suppressedToolOutputs.delete(toolCallId);
  }

  clear() {
    this.contentChunks = [];
    this.reasoningChunks = [];
    this.reasoningSummaryChunks = [];
    this.toolOutputChunks = new Map();
    this.toolCalls = new Map();
    this.seenToolOutputs = new Set();
    this.suppressedToolOutputs = new Set();
  }

  getContent(): string { return this.contentChunks.join(""); }
  getReasoning(): string { return this.reasoningChunks.join(""); }
  getReasoningSummary(): string { return this.reasoningSummaryChunks.join(""); }
  getToolOutput(key: string): string { return (this.toolOutputChunks.get(key) || []).join(""); }
  getToolCallArguments(tcId: string): string {
    return (this.toolCalls.get(tcId)?.argumentChunks || []).join("");
  }
  getToolCallArgumentPrefix(tcId: string, maxChars = 320): string {
    const chunks = this.toolCalls.get(tcId)?.argumentChunks || [];
    let prefix = "";
    for (const chunk of chunks) {
      prefix += chunk.slice(0, Math.max(0, maxChars - prefix.length));
      if (prefix.length >= maxChars) break;
    }
    return prefix;
  }

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
  subscribeToolCalls(listener: StreamingListener): () => void {
    this.toolCallListeners.add(listener);
    return () => this.toolCallListeners.delete(listener);
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

/** Remove only an explicitly inactive thread; callers own the safety policy. */
export function evictStreamingBufferForThread(threadId: string): boolean {
  const buffer = buffers.get(threadId);
  if (!buffer) return false;
  buffer.clear();
  return buffers.delete(threadId);
}

export function hasStreamingBufferForThread(threadId: string): boolean {
  return buffers.has(threadId);
}

export function streamingBufferThreadIds(): string[] {
  return Array.from(buffers.keys());
}
