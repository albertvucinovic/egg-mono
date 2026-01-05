"use client";

import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ChevronRight,
  ChevronDown,
  Plus,
  Trash2,
  Copy,
  MessageSquare,
} from "lucide-react";
import {
  fetchRootThreads,
  fetchThreadChildren,
  createThread,
  deleteThread,
  duplicateThread,
} from "@/lib/api";
import { useAppStore, Thread } from "@/lib/store";
import clsx from "clsx";

interface TreeNodeProps {
  thread: Thread;
  level: number;
}

function TreeNode({ thread, level }: TreeNodeProps) {
  const [expanded, setExpanded] = useState(false);
  const { currentThreadId, setCurrentThreadId, addSystemLog } = useAppStore();
  const queryClient = useQueryClient();

  const { data: children } = useQuery({
    queryKey: ["threadChildren", thread.id],
    queryFn: () => fetchThreadChildren(thread.id),
    enabled: expanded && thread.has_children,
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteThread(thread.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["threads"] });
      queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
      addSystemLog(`Deleted thread ${thread.id.slice(-8)}`, "success");
    },
    onError: () => {
      addSystemLog(`Failed to delete thread`, "error");
    },
  });

  const duplicateMutation = useMutation({
    mutationFn: () => duplicateThread(thread.id),
    onSuccess: (newThread) => {
      queryClient.invalidateQueries({ queryKey: ["threads"] });
      queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
      addSystemLog(`Duplicated thread as ${newThread.id.slice(-8)}`, "success");
    },
  });

  const isSelected = currentThreadId === thread.id;

  return (
    <div>
      <div
        className={clsx(
          "flex items-center gap-1 px-2 py-1 cursor-pointer hover:bg-[#2a2a2a] rounded group",
          isSelected && "bg-[#2a2a2a] border-l-2 border-blue-500"
        )}
        style={{ paddingLeft: `${level * 16 + 8}px` }}
        onClick={() => setCurrentThreadId(thread.id)}
      >
        {thread.has_children ? (
          <button
            onClick={(e) => {
              e.stopPropagation();
              setExpanded(!expanded);
            }}
            className="p-0.5 hover:bg-[#333] rounded"
          >
            {expanded ? (
              <ChevronDown className="w-4 h-4" />
            ) : (
              <ChevronRight className="w-4 h-4" />
            )}
          </button>
        ) : (
          <span className="w-5" />
        )}

        <MessageSquare className="w-4 h-4 text-gray-400" />

        <span className="flex-1 truncate text-sm">
          {thread.name || thread.id.slice(-8)}
        </span>

        <span className="text-xs text-gray-500">
          {thread.model_key?.split(":")[0]}
        </span>

        {/* Actions (visible on hover) */}
        <div className="hidden group-hover:flex gap-1">
          <button
            onClick={(e) => {
              e.stopPropagation();
              duplicateMutation.mutate();
            }}
            className="p-1 hover:bg-[#333] rounded"
            title="Duplicate"
          >
            <Copy className="w-3 h-3" />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              if (confirm("Delete this thread?")) {
                deleteMutation.mutate();
              }
            }}
            className="p-1 hover:bg-red-900 rounded"
            title="Delete"
          >
            <Trash2 className="w-3 h-3" />
          </button>
        </div>
      </div>

      {/* Children */}
      {expanded && children && (
        <div>
          {children.map((child: Thread) => (
            <TreeNode key={child.id} thread={child} level={level + 1} />
          ))}
        </div>
      )}
    </div>
  );
}

export function ThreadTree() {
  const queryClient = useQueryClient();
  const { addSystemLog, setThreads } = useAppStore();

  const { data: threads, isLoading } = useQuery({
    queryKey: ["rootThreads"],
    queryFn: fetchRootThreads,
  });

  const createMutation = useMutation({
    mutationFn: () => createThread({}),
    onSuccess: (newThread) => {
      queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
      addSystemLog(`Created thread ${newThread.id.slice(-8)}`, "success");
    },
    onError: () => {
      addSystemLog("Failed to create thread", "error");
    },
  });

  useEffect(() => {
    if (threads) {
      setThreads(threads);
    }
  }, [threads, setThreads]);

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="p-2 border-b border-[var(--panel-border)] flex items-center justify-between">
        <span className="text-sm font-medium">Threads</span>
        <button
          onClick={() => createMutation.mutate()}
          className="p-1 hover:bg-[#333] rounded"
          title="New thread"
        >
          <Plus className="w-4 h-4" />
        </button>
      </div>

      {/* Thread list */}
      <div className="flex-1 overflow-auto py-1">
        {isLoading ? (
          <div className="p-4 text-center text-gray-500">Loading...</div>
        ) : threads?.length === 0 ? (
          <div className="p-4 text-center text-gray-500">
            <p>No threads yet</p>
            <button
              onClick={() => createMutation.mutate()}
              className="mt-2 px-3 py-1 bg-blue-600 rounded text-sm hover:bg-blue-500"
            >
              Create first thread
            </button>
          </div>
        ) : (
          threads?.map((thread: Thread) => (
            <TreeNode key={thread.id} thread={thread} level={0} />
          ))
        )}
      </div>
    </div>
  );
}
