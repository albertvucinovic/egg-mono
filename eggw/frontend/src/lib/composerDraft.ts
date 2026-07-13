export type DraftReader = (threadId: string) => string;
export type DraftWriter = (threadId: string, text: string) => void;

/**
 * Owns the immediate composer value without publishing each edit globally.
 * The durable thread-keyed store is touched only by explicit flushes or by
 * external writers (shortcuts, edit-answer insertion, and async rollback).
 */
export class ComposerDraftBuffer {
  private threadId: string;
  private value: string;
  private persistedValue: string;

  constructor(
    threadId: string,
    private readonly read: DraftReader,
    private readonly write: DraftWriter,
  ) {
    this.threadId = threadId;
    this.value = read(threadId);
    this.persistedValue = this.value;
  }

  get currentThreadId(): string { return this.threadId; }
  get currentValue(): string { return this.value; }

  update(value: string): void {
    this.value = value;
  }

  flush(): string | null {
    const externalValue = this.read(this.threadId);
    if (externalValue !== this.persistedValue && externalValue !== this.value) {
      // An external insertion won the race after the last render. Hydrate it
      // rather than overwriting it with an older local snapshot.
      this.value = externalValue;
      this.persistedValue = externalValue;
      return externalValue;
    }
    if (externalValue === this.value) {
      this.persistedValue = this.value;
      return null;
    }
    // Mark before the synchronous Zustand publication so acceptExternal can
    // distinguish this buffer's own flush from a genuinely external update.
    this.persistedValue = this.value;
    this.write(this.threadId, this.value);
    return null;
  }

  switchThread(threadId: string): string {
    if (threadId === this.threadId) return this.value;
    this.flush();
    this.threadId = threadId;
    this.value = this.read(threadId);
    this.persistedValue = this.value;
    return this.value;
  }

  /** Return a new local value only for a genuinely external store update. */
  acceptExternal(threadId: string, value: string): string | null {
    if (threadId !== this.threadId || value === this.persistedValue) return null;
    this.value = value;
    this.persistedValue = value;
    return value;
  }
}

export function restoreFailedDraft(submitted: string, newer: string): string {
  if (!newer.trim() || newer === submitted) return submitted;
  if (!submitted.trim()) return newer;
  return `${submitted.trimEnd()}\n\n${newer}`;
}
