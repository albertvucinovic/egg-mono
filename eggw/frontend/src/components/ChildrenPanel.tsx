"use client";

import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ArrowUp, ChevronRight, Plus } from "lucide-react";
import { fetchThread, fetchThreadChildren } from "@/lib/api";
import { useAppStore } from "@/lib/store";

interface ChildThread {
  id: string;
  name?: string;
  model_key?: string;
  has_children: boolean;
}

interface ChildrenPanelProps {
  showBorders?: boolean;
}

export function ChildrenPanel({ showBorders = true }: ChildrenPanelProps) {
  const router = useRouter();
  const currentThreadId = useAppStore((state) => state.currentThreadId);

  const { data: currentThread } = useQuery({
    queryKey: ["thread", currentThreadId],
    queryFn: () => fetchThread(currentThreadId!),
    enabled: !!currentThreadId,
  });

  const { data: children = [], isLoading } = useQuery({
    queryKey: ["threadChildren", currentThreadId],
    queryFn: () => fetchThreadChildren(currentThreadId!),
    enabled: !!currentThreadId,
  });

  const navigateToThread = (threadId: string) => {
    router.push(`/${threadId}`);
  };

  if (!currentThreadId) {
    return null;
  }

  return (
    <div className={`bg-[var(--panel-bg)] ${showBorders ? 'border-b border-[var(--panel-border)]' : ''}`}>
      <div className={`px-3 py-2 text-xs flex items-center justify-between ${showBorders ? 'border-b border-[var(--panel-border)]' : ''}`} style={{ color: "var(--muted)" }}>
        <span>Children ({children.length})</span>
      </div>

      {currentThread?.parent_id && (
        <button
          type="button"
          onClick={() => navigateToThread(currentThread.parent_id)}
          className={`w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 group ${showBorders ? 'border-b border-[var(--panel-border)]' : ''}`}
          style={{ color: "var(--foreground)" }}
          title={`Open parent thread ${currentThread.parent_id}`}
        >
          <ArrowUp className="w-3 h-3" style={{ color: "var(--muted)" }} />
          <span className="flex-1 truncate">Parent</span>
          <span className="text-xs font-mono" style={{ color: "var(--accent)" }}>
            {currentThread.parent_id.slice(-8)}
          </span>
        </button>
      )}

      {isLoading ? (
        <div className="px-3 py-2 text-xs" style={{ color: "var(--muted)" }}>Loading...</div>
      ) : children.length === 0 ? (
        <div className="px-3 py-2 text-xs" style={{ color: "var(--muted)" }}>
          No children. Use /spawnChildThread to create one.
        </div>
      ) : (
        <div className="max-h-32 overflow-auto">
          {children.map((child: ChildThread) => (
            <button
              key={child.id}
              onClick={() => navigateToThread(child.id)}
              className="w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 group"
              style={{ color: "var(--foreground)" }}
            >
              <ChevronRight className="w-3 h-3" style={{ color: "var(--muted)" }} />
              <span className="flex-1 truncate">
                {child.name || child.id.slice(-8)}
              </span>
              {child.model_key && (
                <span className="text-xs" style={{ color: "var(--muted)" }}>
                  {child.model_key}
                </span>
              )}
              {child.has_children && (
                <Plus className="w-3 h-3" style={{ color: "var(--muted)" }} />
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
