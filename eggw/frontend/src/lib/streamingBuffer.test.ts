import { describe, expect, it, vi } from "vitest";
import { AnimationFrameCoalescer, StreamingBuffer } from "./streamingBuffer";

describe("StreamingBuffer tool arguments", () => {
  it("retains argument chunks without growing-string concatenation", () => {
    const buffer = new StreamingBuffer();
    const chunks = Array.from({ length: 1_100 }, (_, index) => `${index},`);

    chunks.forEach((chunk) => buffer.appendToolCallArgs("call-a", "bash", chunk));

    const call = buffer.toolCalls.get("call-a");
    expect(call?.argumentChunks).toEqual(chunks);
    expect(call?.argumentLength).toBe(chunks.reduce((total, chunk) => total + chunk.length, 0));
    expect(buffer.getToolCallArguments("call-a")).toBe(chunks.join(""));
    expect(buffer.getToolCallArgumentPrefix("call-a", 40)).toBe(chunks.join("").slice(0, 40));
  });

  it("coalesces an argument preview burst to one flush per animation frame", () => {
    const frames = new Map<number, FrameRequestCallback>();
    let nextFrameId = 1;
    const coalescer = new AnimationFrameCoalescer(
      (callback) => {
        const id = nextFrameId++;
        frames.set(id, callback);
        return id;
      },
      (id) => { frames.delete(id); },
    );
    const flush = vi.fn();

    for (let index = 0; index < 1_100; index += 1) coalescer.schedule(flush);

    expect(frames.size).toBe(1);
    expect(flush).not.toHaveBeenCalled();
    const callback = Array.from(frames.values())[0];
    frames.clear();
    callback(0);
    expect(flush).toHaveBeenCalledTimes(1);

    coalescer.schedule(flush);
    expect(frames.size).toBe(1);
  });
});
