import type { Thread } from "./store";

export interface ThreadTreeNode extends Thread {
  children: ThreadTreeNode[];
}

function compareThreads(left: Thread, right: Thread): number {
  const createdAt = (left.created_at || "").localeCompare(right.created_at || "");
  if (createdAt !== 0) return createdAt;
  return left.id.localeCompare(right.id);
}

/** Build a deterministic forest from the flat, authoritative all-threads list. */
export function buildThreadForest(threads: Thread[]): ThreadTreeNode[] {
  const nodes = new Map<string, ThreadTreeNode>();
  for (const thread of threads) {
    nodes.set(thread.id, { ...thread, children: [] });
  }

  const roots: ThreadTreeNode[] = [];
  const wouldCreateCycle = (node: ThreadTreeNode, parent: ThreadTreeNode): boolean => {
    const visited = new Set<string>([node.id]);
    let current: ThreadTreeNode | undefined = parent;
    while (current) {
      if (visited.has(current.id)) return true;
      visited.add(current.id);
      current = current.parent_id ? nodes.get(current.parent_id) : undefined;
    }
    return false;
  };
  nodes.forEach((node) => {
    const parent = node.parent_id ? nodes.get(node.parent_id) : undefined;
    if (parent && !wouldCreateCycle(node, parent)) parent.children.push(node);
    else roots.push(node);
  });

  const sortTree = (items: ThreadTreeNode[]) => {
    items.sort(compareThreads);
    items.forEach((item) => sortTree(item.children));
  };
  sortTree(roots);
  return roots;
}

/** Return every ancestor needed to expose the selected thread in the tree. */
export function threadAncestorIds(threads: Thread[], threadId: string | null): Set<string> {
  const byId = new Map(threads.map((thread) => [thread.id, thread]));
  const ancestors = new Set<string>();
  const visited = new Set<string>();
  let current = threadId ? byId.get(threadId) : undefined;

  while (current?.parent_id && !visited.has(current.id)) {
    visited.add(current.id);
    const parent = byId.get(current.parent_id);
    if (!parent || visited.has(parent.id)) break;
    ancestors.add(parent.id);
    current = parent;
  }
  return ancestors;
}
