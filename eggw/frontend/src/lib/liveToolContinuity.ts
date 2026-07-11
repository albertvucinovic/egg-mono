import type { Message } from "./store";

export const MAX_RETAINED_LIVE_TOOLS = 100;
export const MAX_LIVE_TOOL_THREADS = 20;

export interface LiveToolReconciliation {
  hideCalls: string[];
  removeTools: string[];
}

export interface LiveToolRegistryEntry {
  toolCallId: string;
  ordinal: number;
  callDurable: boolean;
  resultDurable: boolean;
  terminalWithoutDurable: boolean;
  hasOutput: boolean;
}

/**
 * Thread-owned lifecycle registry for live tool cards. Entries remain visible
 * across invocation close until canonical transcript messages cover them.
 */
export class LiveToolRegistry {
  private entries = new Map<string, LiveToolRegistryEntry>();
  private nextOrdinal = 0;

  constructor(private readonly limit = MAX_RETAINED_LIVE_TOOLS) {}

  observe(toolCallId: string, hasOutput = false): string[] {
    if (!toolCallId) return [];
    const existing = this.entries.get(toolCallId);
    if (existing) {
      existing.hasOutput = existing.hasOutput || hasOutput;
      return [];
    }
    this.entries.set(toolCallId, {
      toolCallId,
      ordinal: this.nextOrdinal++,
      callDurable: false,
      resultDurable: false,
      terminalWithoutDurable: false,
      hasOutput,
    });
    return this.enforceBound();
  }

  reconcileMessage(message: Message): LiveToolReconciliation {
    const matched = durableToolCallIds(message);
    const hideCalls: string[] = [];
    for (const toolCallId of matched.callIds) {
      const entry = this.entries.get(toolCallId);
      if (!entry) continue;
      entry.callDurable = true;
      if (!entry.hasOutput) hideCalls.push(toolCallId);
    }
    for (const toolCallId of matched.resultIds) {
      const entry = this.entries.get(toolCallId);
      if (entry) entry.resultDurable = true;
    }
    return { hideCalls, removeTools: this.collectRemovable() };
  }

  markTerminalWithoutDurable(toolCallId: string): string[] {
    const entry = this.entries.get(toolCallId);
    if (!entry) return [];
    entry.terminalWithoutDurable = true;
    return this.collectRemovable();
  }

  has(toolCallId: string): boolean {
    return this.entries.has(toolCallId);
  }

  clear(): string[] {
    const toolCallIds = Array.from(this.entries.keys());
    this.entries.clear();
    return toolCallIds;
  }

  get size(): number {
    return this.entries.size;
  }

  private collectRemovable(): string[] {
    const removable: string[] = [];
    this.entries.forEach((entry, toolCallId) => {
      if (!entry.resultDurable && !entry.terminalWithoutDurable) return;
      this.entries.delete(toolCallId);
      removable.push(toolCallId);
    });
    return removable;
  }

  private enforceBound(): string[] {
    const evicted: string[] = [];
    while (this.entries.size > this.limit) {
      const settled = Array.from(this.entries.values())
        .filter((entry) => entry.resultDurable || entry.terminalWithoutDurable)
        .sort((left, right) => left.ordinal - right.ordinal)[0];
      const oldest = settled || Array.from(this.entries.values())
        .sort((left, right) => left.ordinal - right.ordinal)[0];
      if (!oldest) return evicted;
      this.entries.delete(oldest.toolCallId);
      evicted.push(oldest.toolCallId);
    }
    return evicted;
  }
}

export interface EvictedLiveToolRegistry {
  threadId: string;
  toolCallIds: string[];
}

export interface LiveToolRegistryAccess {
  registry: LiveToolRegistry;
  evicted: EvictedLiveToolRegistry[];
}

export function cleanUpEvictedLiveTools(
  evicted: EvictedLiveToolRegistry[],
  removeTool: (threadId: string, toolCallId: string) => void,
): void {
  evicted.forEach((registry) => {
    registry.toolCallIds.forEach((toolCallId) => removeTool(registry.threadId, toolCallId));
  });
}

export class LiveToolRegistryOwner {
  private registries = new Map<string, LiveToolRegistry>();

  constructor(private readonly threadLimit = MAX_LIVE_TOOL_THREADS) {}

  forThread(threadId: string): LiveToolRegistryAccess {
    const existing = this.registries.get(threadId);
    if (existing) {
      // Map insertion order doubles as a bounded least-recently-used list.
      this.registries.delete(threadId);
      this.registries.set(threadId, existing);
      return { registry: existing, evicted: [] };
    }

    const registry = new LiveToolRegistry();
    this.registries.set(threadId, registry);
    const evicted: EvictedLiveToolRegistry[] = [];
    while (this.registries.size > this.threadLimit) {
      const oldestThreadId = this.registries.keys().next().value;
      if (!oldestThreadId) break;
      const oldest = this.registries.get(oldestThreadId);
      this.registries.delete(oldestThreadId);
      evicted.push({ threadId: oldestThreadId, toolCallIds: oldest?.clear() || [] });
    }
    return { registry, evicted };
  }


  hasRetainedTools(threadId: string): boolean {
    return (this.registries.get(threadId)?.size || 0) > 0;
  }

  clearThread(threadId: string): string[] {
    const registry = this.registries.get(threadId);
    if (!registry) return [];
    this.registries.delete(threadId);
    return registry.clear();
  }
}

const registryOwner = new LiveToolRegistryOwner();

export function liveToolRegistryForThread(threadId: string): LiveToolRegistryAccess {
  return registryOwner.forThread(threadId);
}

export function clearLiveToolsForThread(threadId: string): string[] {
  return registryOwner.clearThread(threadId);
}

export function hasRetainedLiveToolsForThread(threadId: string): boolean {
  return registryOwner.hasRetainedTools(threadId);
}

export function durableToolCallIds(message: Message): { callIds: string[]; resultIds: string[] } {
  const callIds: string[] = [];
  if (message.role === "assistant" && Array.isArray(message.tool_calls)) {
    for (const toolCall of message.tool_calls) {
      const id = toolCall && typeof toolCall === "object"
        ? String(toolCall.id || toolCall.tool_call_id || "")
        : "";
      if (id) callIds.push(id);
    }
  }
  const resultIds = message.role === "tool" && message.tool_call_id
    ? [message.tool_call_id]
    : [];
  return { callIds, resultIds };
}
