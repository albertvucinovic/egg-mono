"use client";

import { useEffect, useState, useRef } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ChevronRight,
  ChevronDown,
  Plus,
  Trash2,
  Copy,
  MessageSquare,
  Pencil,
  Check,
  X,
} from "lucide-react";
import {
  fetchRootThreads,
  fetchThreadChildren,
  createThread,
  deleteThread,
  duplicateThread,
  renameThread,
} from "@/lib/api";
import { useAppStore, Thread } from "@/lib/store";
import clsx from "clsx";

interface TreeNodeProps {
  thread: Thread;
  level: number;
}

function TreeNode({ thread, level }: TreeNodeProps) {
  const [expanded, setExpanded] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [editName, setEditName] = useState(thread.name || "");
  const inputRef = useRef<HTMLInputElement>(null);
  const router = useRouter();
  const { currentThreadId, addSystemLog } = useAppStore();
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

  const renameMutation = useMutation({
    mutationFn: (name: string) => renameThread(thread.id, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["threads"] });
      queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
      queryClient.invalidateQueries({ queryKey: ["thread", thread.id] });
      addSystemLog(`Renamed thread to "${editName}"`, "success");
      setIsEditing(false);
    },
    onError: () => {
      addSystemLog(`Failed to rename thread`, "error");
    },
  });

  const isSelected = currentThreadId === thread.id;

  const handleStartEdit = (e: React.MouseEvent) => {
    e.stopPropagation();
    setEditName(thread.name || "");
    setIsEditing(true);
    setTimeout(() => inputRef.current?.focus(), 0);
  };

  const handleSaveEdit = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (editName.trim()) {
      renameMutation.mutate(editName.trim());
    } else {
      setIsEditing(false);
    }
  };

  const handleCancelEdit = (e: React.MouseEvent) => {
    e.stopPropagation();
    setIsEditing(false);
    setEditName(thread.name || "");
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      if (editName.trim()) {
        renameMutation.mutate(editName.trim());
      } else {
        setIsEditing(false);
      }
    } else if (e.key === "Escape") {
      setIsEditing(false);
      setEditName(thread.name || "");
    }
  };

  return (
    <div>
      <div
        className={clsx(
          "flex items-center gap-1 px-2 py-1 cursor-pointer rounded group",
          isSelected && "border-l-2"
        )}
        style={{
          paddingLeft: `${level * 16 + 8}px`,
          background: isSelected ? "var(--code-bg)" : undefined,
          borderColor: isSelected ? "var(--accent)" : undefined,
        }}
        onClick={() => {
          router.push(`/${thread.id}`);
        }}
      >
        {thread.has_children ? (
          <button
            onClick={(e) => {
              e.stopPropagation();
              setExpanded(!expanded);
            }}
            className="p-0.5 rounded"
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

        <MessageSquare className="w-4 h-4" style={{ color: "var(--muted)" }} />

        {isEditing ? (
          <>
            <input
              ref={inputRef}
              type="text"
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
              onKeyDown={handleKeyDown}
              onClick={(e) => e.stopPropagation()}
              className="flex-1 border rounded px-1 text-sm outline-none"
              style={{ background: "var(--code-bg)", borderColor: "var(--accent)", color: "var(--foreground)" }}
              placeholder="Thread name"
            />
            <button
              onClick={handleSaveEdit}
              className="p-1 rounded"
              style={{ color: "var(--tool-msg-border)" }}
              title="Save"
            >
              <Check className="w-3 h-3" />
            </button>
            <button
              onClick={handleCancelEdit}
              className="p-1 rounded"
              style={{ color: "var(--user-msg-border)" }}
              title="Cancel"
            >
              <X className="w-3 h-3" />
            </button>
          </>
        ) : (
          <>
            <span className="flex-1 truncate text-sm">
              {thread.name || thread.id.slice(-8)}
            </span>

            <span className="text-xs" style={{ color: "var(--muted)" }}>
              {thread.model_key?.split(":")[0]}
            </span>

            {/* Actions (visible on hover) */}
            <div className="hidden group-hover:flex gap-1">
              <button
                onClick={handleStartEdit}
                className="p-1 rounded"
                title="Rename"
              >
                <Pencil className="w-3 h-3" />
              </button>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  duplicateMutation.mutate();
                }}
                className="p-1 rounded"
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
                className="p-1 rounded"
                style={{ color: "var(--user-msg-border)" }}
                title="Delete"
              >
                <Trash2 className="w-3 h-3" />
              </button>
            </div>
          </>
        )}
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
  const router = useRouter();
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
      router.push(`/${newThread.id}`);
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
          className="p-1 rounded"
          title="New thread"
          data-testid="new-thread-btn"
        >
          <Plus className="w-4 h-4" />
        </button>
      </div>

      {/* Thread list */}
      <div className="flex-1 overflow-auto py-1">
        {isLoading ? (
          <div className="p-4 text-center" style={{ color: "var(--muted)" }}>Loading...</div>
        ) : threads?.length === 0 ? (
          <div className="p-4 text-center" style={{ color: "var(--muted)" }}>
            <p>No threads yet</p>
            <button
              onClick={() => createMutation.mutate()}
              className="mt-2 px-3 py-1 rounded text-sm"
              style={{ background: "var(--accent)", color: "var(--background)" }}
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
