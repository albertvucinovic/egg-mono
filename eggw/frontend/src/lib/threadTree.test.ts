import { describe, expect, it } from "vitest";
import type { Thread } from "./store";
import { buildThreadForest, filterThreadForest, threadAncestorIds } from "./threadTree";

const thread = (id: string, parentId?: string, createdAt = id): Thread => ({
  id,
  parent_id: parentId,
  created_at: createdAt,
  has_children: false,
});

describe("thread tree", () => {
  it("builds the whole tree from an unordered flat thread list", () => {
    const forest = buildThreadForest([
      thread("grandchild", "child", "3"),
      thread("other-root", undefined, "4"),
      thread("child", "root", "2"),
      thread("root", undefined, "1"),
    ]);

    expect(forest.map((node) => node.id)).toEqual(["other-root", "root"]);
    expect(forest[1].children.map((node) => node.id)).toEqual(["child"]);
    expect(forest[1].children[0].children.map((node) => node.id)).toEqual(["grandchild"]);
  });

  it("keeps legacy orphan rows visible as top-level entries", () => {
    const forest = buildThreadForest([
      thread("orphan", "missing-parent", "1"),
      thread("root", undefined, "2"),
    ]);

    expect(forest.map((node) => node.id)).toEqual(["root", "orphan"]);
  });

  it("finds every ancestor of the selected thread and terminates on cycles", () => {
    const threads = [
      thread("root"),
      thread("child", "root"),
      thread("grandchild", "child"),
      thread("cycle-a", "cycle-b"),
      thread("cycle-b", "cycle-a"),
    ];

    expect(Array.from(threadAncestorIds(threads, "grandchild"))).toEqual(["child", "root"]);
    expect(Array.from(threadAncestorIds(threads, "cycle-a"))).toEqual(["cycle-b"]);
  });

  it("keeps malformed cycles visible instead of recursing forever", () => {
    const forest = buildThreadForest([
      thread("cycle-a", "cycle-b"),
      thread("cycle-b", "cycle-a"),
    ]);

    expect(forest.map((node) => node.id)).toEqual(["cycle-b", "cycle-a"]);
    expect(forest.every((node) => node.children.length === 0)).toBe(true);
  });

  it("sorts every level newest-first by creation time", () => {
    const forest = buildThreadForest([
      thread("old-child", "old-root", "2024-01-02"),
      thread("new-root", undefined, "2024-02-01"),
      thread("new-child", "old-root", "2024-01-03"),
      thread("old-root", undefined, "2024-01-01"),
    ]);

    expect(forest.map((node) => node.id)).toEqual(["new-root", "old-root"]);
    expect(forest[1].children.map((node) => node.id)).toEqual(["new-child", "old-child"]);
  });

  it("filters names, recaps, IDs, and models while retaining ancestor context", () => {
    const forest = buildThreadForest([
      { ...thread("root"), name: "Research" },
      { ...thread("matching-child", "root"), name: "Parser", short_recap: "Fix streaming continuity", model_key: "GPT Codex" },
      { ...thread("sibling", "root"), name: "Unrelated" },
    ]);

    for (const query of ["parser", "streaming continuity", "matching-child", "gpt codex"]) {
      const filtered = filterThreadForest(forest, query);
      expect(filtered.map((node) => node.id)).toEqual(["root"]);
      expect(filtered[0].children.map((node) => node.id)).toEqual(["matching-child"]);
    }
    expect(filterThreadForest(forest, "missing")).toEqual([]);
    expect(filterThreadForest(forest, " ")).toBe(forest);
  });
});
