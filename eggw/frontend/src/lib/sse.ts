export interface SSEMessage {
  event: string;
  data: string;
  id?: string;
}

function parseBlock(block: string): SSEMessage | null {
  let event = "message";
  let id: string | undefined;
  const data: string[] = [];

  for (const line of block.split(/\r\n|\r|\n/)) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
    else if (field === "id" && !value.includes("\0")) id = value;
  }

  if (data.length === 0) return null;
  return { event, data: data.join("\n"), id };
}

/** Decode an authenticated fetch response using the browser SSE framing rules. */
export async function consumeSSE(
  response: Response,
  onMessage: (message: SSEMessage) => void,
): Promise<void> {
  if (!response.body) throw new Error("Event stream response has no body");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let boundary = /\r\n\r\n|\n\n|\r\r/.exec(buffer);
      while (boundary?.index !== undefined) {
        const message = parseBlock(buffer.slice(0, boundary.index));
        buffer = buffer.slice(boundary.index + boundary[0].length);
        if (message) onMessage(message);
        boundary = /\r\n\r\n|\n\n|\r\r/.exec(buffer);
      }
      if (done) break;
    }
  } finally {
    reader.releaseLock();
  }
}

type SSEListener = (event: MessageEvent<string>) => void;

/** EventSource-shaped client backed by authenticated fetch with reconnects. */
export class AuthenticatedEventSource {
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  private readonly connectResponse: (
    signal: AbortSignal,
    cursor: string,
    reconnect: boolean,
  ) => Promise<Response>;
  private readonly listeners = new Map<string, Set<SSEListener>>();
  private controller: AbortController | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;
  private lastEventId: string;
  private hasConnected = false;
  private reconnectDelayMs = 1000;

  constructor(
    connectResponse: (
      signal: AbortSignal,
      cursor: string,
      reconnect: boolean,
    ) => Promise<Response>,
    initialCursor = -1,
  ) {
    this.connectResponse = connectResponse;
    this.lastEventId = String(initialCursor);
    void this.connect();
  }

  addEventListener(type: string, listener: SSEListener): void {
    const listeners = this.listeners.get(type) || new Set<SSEListener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: SSEListener): void {
    this.listeners.get(type)?.delete(listener);
  }

  close(): void {
    this.closed = true;
    this.controller?.abort();
    this.controller = null;
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  private async connect(): Promise<void> {
    if (this.closed) return;
    const controller = new AbortController();
    this.controller = controller;
    try {
      const reconnect = this.hasConnected;
      const response = await this.connectResponse(
        controller.signal,
        this.lastEventId,
        reconnect,
      );
      if (this.closed || controller.signal.aborted) return;
      this.hasConnected = true;
      this.reconnectDelayMs = 1000;
      this.onopen?.(new Event("open"));
      await consumeSSE(response, ({ event, data, id }) => {
        if (!id) return;
        const next = Number(id);
        const current = Number(this.lastEventId);
        if (!Number.isSafeInteger(next) || next < 0) return;
        if (Number.isSafeInteger(current) && next <= current) return;
        this.lastEventId = id;
        const message = new MessageEvent(event, { data, lastEventId: id });
        this.listeners.get(event)?.forEach((listener) => listener(message));
      });
      if (!this.closed) this.reportErrorAndReconnect();
    } catch (error) {
      if (!this.closed && !controller.signal.aborted) this.reportErrorAndReconnect();
    }
  }

  private reportErrorAndReconnect(): void {
    this.onerror?.(new Event("error"));
    if (this.closed || this.reconnectTimer !== null) return;
    const delay = this.reconnectDelayMs;
    this.reconnectDelayMs = Math.min(this.reconnectDelayMs * 2, 10000);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.connect();
    }, delay);
  }
}
