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

export function ChildrenPanel() {
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
    <div className="border-b border-[var(--panel-border)] bg-[var(--panel-bg)]">
      <div className="px-3 py-2 text-xs text-gray-400 border-b border-[var(--panel-border)] flex items-center justify-between">
        <span>Children ({children.length})</span>
      </div>

      {isLoading ? (
        <div className="px-3 py-2 text-xs text-gray-500">Loading...</div>
      ) : children.length === 0 ? (
        <div className="px-3 py-2 text-xs text-gray-500">
          No children. Use /spawn to create one.
        </div>
      ) : (
        <div className="max-h-32 overflow-auto">
          {children.map((child: ChildThread) => (
            <button
              key={child.id}
              onClick={() => navigateToChild(child.id)}
              className="w-full px-3 py-1.5 text-left text-sm hover:bg-[var(--item-hover)] flex items-center gap-2 group"
            >
              <ChevronRight className="w-3 h-3 text-gray-500" />
              <span className="flex-1 truncate">
                {child.name || child.id.slice(-8)}
              </span>
              {child.model_key && (
                <span className="text-xs text-gray-500 group-hover:text-gray-400">
                  {child.model_key}
                </span>
              )}
              {child.has_children && (
                <Plus className="w-3 h-3 text-gray-500" />
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
