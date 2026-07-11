import { describe, expect, it } from "vitest";
import { ComposerDraftBuffer, restoreFailedDraft } from "./composerDraft";

describe("ComposerDraftBuffer", () => {
  it("keeps 200 rapid edits local until a safe flush boundary", () => {
    const drafts: Record<string, string> = { "thread-a": "" };
    let publications = 0;
    const buffer = new ComposerDraftBuffer(
      "thread-a",
      (threadId) => drafts[threadId] || "",
      (threadId, value) => { publications += 1; drafts[threadId] = value; },
    );

    for (let index = 1; index <= 200; index += 1) buffer.update("x".repeat(index));
    expect(publications).toBe(0);
    expect(buffer.currentValue).toHaveLength(200);

    buffer.flush();
    expect(publications).toBe(1);
    expect(drafts["thread-a"]).toHaveLength(200);
  });

  it("flushes the source on navigation and hydrates the destination without cross-thread writes", () => {
    const drafts: Record<string, string> = { "thread-a": "draft a", "thread-b": "draft b" };
    const writes: Array<[string, string]> = [];
    const buffer = new ComposerDraftBuffer(
      "thread-a",
      (threadId) => drafts[threadId] || "",
      (threadId, value) => { writes.push([threadId, value]); drafts[threadId] = value; },
    );
    buffer.update("latest a");

    expect(buffer.switchThread("thread-b")).toBe("draft b");
    expect(writes).toEqual([["thread-a", "latest a"]]);
    expect(buffer.switchThread("thread-a")).toBe("latest a");
  });

  it("does not overwrite an external insertion that races a local flush", () => {
    const drafts = { "thread-a": "start" };
    let publications = 0;
    const buffer = new ComposerDraftBuffer(
      "thread-a",
      () => drafts["thread-a"],
      (_threadId, value) => { publications += 1; drafts["thread-a"] = value; },
    );
    buffer.update("local edit");
    drafts["thread-a"] = "external quote";

    buffer.flush();
    expect(publications).toBe(0);
    expect(buffer.currentValue).toBe("external quote");
  });

  it("hydrates external edit insertion and keeps failed content alongside a newer draft", () => {
    const drafts = { "thread-a": "local" };
    const buffer = new ComposerDraftBuffer(
      "thread-a",
      () => drafts["thread-a"],
      (_threadId, value) => { drafts["thread-a"] = value; },
    );

    expect(buffer.acceptExternal("thread-a", "quoted answer")).toBe("quoted answer");
    expect(restoreFailedDraft("failed send", "newer text")).toBe("failed send\n\nnewer text");
    expect(restoreFailedDraft("failed send", "")).toBe("failed send");
  });
});
