"use client";

import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ArrowUp, ChevronRight, Copy, GitBranch, Plus } from "lucide-react";
import { fetchThread, fetchThreadChildren } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { Button, IconButton } from "@/components/ui/primitives";
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
            <div key={child.id} className="eggw-thread-row">
              <Button
                variant="ghost"
                onClick={() => router.push(`/${child.id}`)}
                className="eggw-thread-link"
                title={`Open child thread ${child.id}`}
              >
                <ChevronRight className="h-4 w-4" aria-hidden="true" />
                <span>{child.name || "Unnamed child"}</span>
                {child.model_key && <small>{child.model_key}</small>}
                <code title={child.id} aria-label={`Thread ID ${child.id}`}>{child.id.slice(-8)}</code>
                {child.has_children && <Plus className="h-3.5 w-3.5" aria-label="Has children" />}
              </Button>
              <IconButton
                className="eggw-thread-id-copy"
                aria-label={`Copy thread ID ${child.id}`}
                title="Copy full thread ID"
                onClick={() => {
                  if (!navigator.clipboard?.writeText) return;
                  void navigator.clipboard.writeText(child.id);
                }}
              >
                <Copy className="h-3.5 w-3.5" aria-hidden="true" />
              </IconButton>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
