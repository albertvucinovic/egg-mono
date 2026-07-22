import { describe, expect, it } from "vitest";
import type { Thread } from "./store";
import { buildThreadForest, threadAncestorIds } from "./threadTree";

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

    expect(forest.map((node) => node.id)).toEqual(["root", "other-root"]);
    expect(forest[0].children.map((node) => node.id)).toEqual(["child"]);
    expect(forest[0].children[0].children.map((node) => node.id)).toEqual(["grandchild"]);
  });

  it("keeps legacy orphan rows visible as top-level entries", () => {
    const forest = buildThreadForest([
      thread("orphan", "missing-parent", "1"),
      thread("root", undefined, "2"),
    ]);

    expect(forest.map((node) => node.id)).toEqual(["orphan", "root"]);
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

    expect(forest.map((node) => node.id)).toEqual(["cycle-a", "cycle-b"]);
    expect(forest.every((node) => node.children.length === 0)).toBe(true);
  });
});
