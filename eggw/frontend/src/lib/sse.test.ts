import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthenticatedEventSource } from "./sse";

function response(body: string): Response {
  return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
}

afterEach(() => {
  vi.useRealTimers();
});

describe("AuthenticatedEventSource reconnect", () => {
  it("resumes from the delivered cursor and rejects replayed event IDs", async () => {
    vi.useFakeTimers();
    const cursors: string[] = [];
    const opened: boolean[] = [];
    const delivered: string[] = [];
    let attempt = 0;
    const source = new AuthenticatedEventSource(async (_signal, cursor, reconnect) => {
      cursors.push(`${cursor}:${reconnect}`);
      attempt += 1;
      if (attempt === 1) {
        return response([
          "id: 1", "event: msg.create", 'data: {"event_seq":1}', "",
          "id: 2", "event: msg.create", 'data: {"event_seq":2}', "", "",
        ].join("\n"));
      }
      return response([
        "id: 2", "event: msg.create", 'data: {"event_seq":2}', "",
        "id: 3", "event: msg.create", 'data: {"event_seq":3}', "", "",
      ].join("\n"));
    }, 0);
    source.onopen = (event) => opened.push(Boolean((event as CustomEvent).detail?.reconnect));
    source.addEventListener("msg.create", (event) => delivered.push(event.lastEventId));

    await vi.waitFor(() => expect(delivered).toEqual(["1", "2"]));
    await vi.advanceTimersByTimeAsync(1000);
    await vi.waitFor(() => expect(delivered).toEqual(["1", "2", "3"]));

    expect(cursors.slice(0, 2)).toEqual(["0:false", "2:true"]);
    expect(opened.slice(0, 2)).toEqual([false, true]);
    source.close();
  });
  it("does not fast-forward reconnect past queued response frames", async () => {
    vi.useFakeTimers();
    const cursors: string[] = [];
    const delivered: string[] = [];
    let attempt = 0;
    let resolveFirst!: (response: Response) => void;
    const firstResponse = new Promise<Response>((resolve) => { resolveFirst = resolve; });
    const source = new AuthenticatedEventSource(async (_signal, cursor) => {
      cursors.push(cursor);
      attempt += 1;
      if (attempt === 1) return firstResponse;
      return response([
        "id: 11", "event: stream.delta", 'data: {"event_seq":11}', "",
        "id: 12", "event: stream.delta", 'data: {"event_seq":12}', "", "",
      ].join("\n"));
    }, 9);
    source.addEventListener("stream.open", (event) => delivered.push(event.lastEventId));
    source.addEventListener("stream.delta", (event) => delivered.push(event.lastEventId));
    await vi.waitFor(() => expect(cursors).toEqual(["9"]));
    resolveFirst(response([
      "id: 10", "event: stream.open", 'data: {"event_seq":10}', "", "",
    ].join("\n")));

    await vi.waitFor(() => expect(delivered).toEqual(["10"]));
    await vi.advanceTimersByTimeAsync(1000);
    await vi.waitFor(() => expect(delivered).toEqual(["10", "11", "12"]));

    expect(cursors.slice(0, 2)).toEqual(["9", "10"]);
    source.close();
  });

});
