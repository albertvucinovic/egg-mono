"use client";

import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ArrowUp, ChevronRight, GitBranch, Plus } from "lucide-react";
import { fetchThread, fetchThreadChildren } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { Button } from "@/components/ui/primitives";
import clsx from "clsx";

interface ChildThread {
  id: string;
  name?: string;
  model_key?: string;
  has_children: boolean;
}

export function ChildrenPanel({ showBorders = true }: { showBorders?: boolean }) {
  const router = useRouter();
  const currentThreadId = useAppStore((state) => state.currentThreadId);
  const { data: currentThread } = useQuery({ queryKey: ["thread", currentThreadId], queryFn: () => fetchThread(currentThreadId!), enabled: !!currentThreadId });
  const { data: children = [], isLoading } = useQuery({ queryKey: ["threadChildren", currentThreadId], queryFn: () => fetchThreadChildren(currentThreadId!), enabled: !!currentThreadId });
  if (!currentThreadId) return null;

  return (
    <section className={clsx("eggw-children-panel", showBorders && "eggw-children-panel-bordered")} aria-labelledby="children-panel-heading">
      <div className="eggw-children-heading">
        <GitBranch className="h-3.5 w-3.5" aria-hidden="true" />
        <h2 id="children-panel-heading">Thread branches</h2>
        <span>{children.length} children</span>
      </div>
      {currentThread?.parent_id && (
        <Button variant="ghost" onClick={() => router.push(`/${currentThread.parent_id}`)} className="eggw-thread-link" title={`Open parent thread ${currentThread.parent_id}`}>
          <ArrowUp className="h-4 w-4" aria-hidden="true" /><span>Parent</span><code>{currentThread.parent_id.slice(-8)}</code>
        </Button>
      )}
      {isLoading ? (
        <div className="eggw-compact-state" role="status">Loading branches…</div>
      ) : children.length === 0 ? (
        <div className="eggw-compact-state">No children. Use /spawnChildThread to create one.</div>
      ) : (
        <div className="eggw-children-list">
          {children.map((child: ChildThread) => (
            <Button key={child.id} variant="ghost" onClick={() => router.push(`/${child.id}`)} className="eggw-thread-link">
              <ChevronRight className="h-4 w-4" aria-hidden="true" />
              <span>{child.name || child.id.slice(-8)}</span>
              {child.model_key && <small>{child.model_key}</small>}
              {child.has_children && <Plus className="h-3.5 w-3.5" aria-label="Has children" />}
            </Button>
          ))}
        </div>
      )}
    </section>
  );
}
