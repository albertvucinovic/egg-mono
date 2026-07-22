import type { Thread } from "./store";

export interface ThreadTreeNode extends Thread {
  children: ThreadTreeNode[];
}

function normalizedFilter(value: string): string[] {
  return value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
}

function matchesThread(thread: Thread, terms: string[]): boolean {
  const searchable = [thread.name, thread.short_recap, thread.id, thread.model_key]
    .filter((value): value is string => Boolean(value))
    .join("\n")
    .toLocaleLowerCase();
  return terms.every((term) => searchable.includes(term));
}

/** Keep matching rows and their ancestor paths while preserving tree order. */
export function filterThreadForest(forest: ThreadTreeNode[], value: string): ThreadTreeNode[] {
  const terms = normalizedFilter(value);
  if (terms.length === 0) return forest;

  const filterNodes = (nodes: ThreadTreeNode[]): ThreadTreeNode[] => nodes.flatMap((node) => {
    const children = filterNodes(node.children);
    if (!matchesThread(node, terms) && children.length === 0) return [];
    return [{ ...node, children }];
  });
  return filterNodes(forest);
}

function compareThreads(left: Thread, right: Thread): number {
  const createdAt = (right.created_at || "").localeCompare(left.created_at || "");
  if (createdAt !== 0) return createdAt;
  return right.id.localeCompare(left.id);
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
