"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Plus } from "lucide-react";
import { fetchThreadChildren, openThread } from "@/lib/api";
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
  const { currentThreadId, setCurrentThreadId, addSystemLog } = useAppStore();

  const { data: children = [], isLoading } = useQuery({
    queryKey: ["threadChildren", currentThreadId],
    queryFn: () => fetchThreadChildren(currentThreadId!),
    enabled: !!currentThreadId,
  });

  const navigateToChild = (childId: string) => {
    setCurrentThreadId(childId);
    openThread(childId).then(() => {
      addSystemLog(`Switched to child ${childId.slice(-8)}`, "info");
    });
  };

  if (!currentThreadId) {
    return null;
  }

  return (
    <div className={`bg-[var(--panel-bg)] ${showBorders ? 'border-b border-[var(--panel-border)]' : ''}`}>
      <div className={`px-3 py-2 text-xs flex items-center justify-between ${showBorders ? 'border-b border-[var(--panel-border)]' : ''}`} style={{ color: "var(--muted)" }}>
        <span>Children ({children.length})</span>
      </div>

      {isLoading ? (
        <div className="px-3 py-2 text-xs" style={{ color: "var(--muted)" }}>Loading...</div>
      ) : children.length === 0 ? (
        <div className="px-3 py-2 text-xs" style={{ color: "var(--muted)" }}>
          No children. Use /spawn to create one.
        </div>
      ) : (
        <div className="max-h-32 overflow-auto">
          {children.map((child: ChildThread) => (
            <button
              key={child.id}
              onClick={() => navigateToChild(child.id)}
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
