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
import { createClientOperationId } from "@/lib/messageOperations";
import clsx from "clsx";
import { Button, IconButton } from "@/components/ui/primitives";

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
  const currentThreadId = useAppStore((state) => state.currentThreadId);
  const addSystemLog = useAppStore((state) => state.addSystemLog);
  const queryClient = useQueryClient();

  const { data: children } = useQuery({
    queryKey: ["threadChildren", thread.id],
    queryFn: () => fetchThreadChildren(thread.id),
    enabled: expanded && thread.has_children,
  });

  const deleteMutation = useMutation({
    mutationFn: ({ threadId }: { threadId: string; operationId: string }) => deleteThread(threadId),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ["threads"] });
      queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
      addSystemLog(`Deleted thread ${variables.threadId.slice(-8)}`, "success");
    },
    onError: () => {
      addSystemLog(`Failed to delete thread`, "error");
    },
  });

  const duplicateMutation = useMutation({
    mutationFn: ({ threadId }: { threadId: string; operationId: string }) => duplicateThread(threadId),
    onSuccess: (newThread) => {
      queryClient.invalidateQueries({ queryKey: ["threads"] });
      queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
      addSystemLog(`Duplicated thread as ${newThread.id.slice(-8)}`, "success");
    },
  });

  const renameMutation = useMutation({
    mutationFn: ({ threadId, name }: { threadId: string; operationId: string; name: string }) => renameThread(threadId, name),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ["threads"] });
      queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
      queryClient.invalidateQueries({ queryKey: ["thread", variables.threadId] });
      addSystemLog(`Renamed thread to "${variables.name}"`, "success");
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
      renameMutation.mutate({ threadId: thread.id, operationId: createClientOperationId("rename"), name: editName.trim() });
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
        renameMutation.mutate({ threadId: thread.id, operationId: createClientOperationId("rename"), name: editName.trim() });
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
        role="treeitem"
        aria-level={level + 1}
        aria-selected={isSelected}
        aria-expanded={thread.has_children ? expanded : undefined}
        tabIndex={0}
        className={clsx(
          "eggw-tree-row group",
          isSelected && "eggw-tree-row-selected"
        )}
        style={{
          paddingLeft: `${level * 16 + 8}px`,
        }}
        onClick={() => {
          router.push(`/${thread.id}`);
        }}
        onKeyDown={(event) => {
          if (event.target !== event.currentTarget) return;
          const tree = event.currentTarget.closest('[role="tree"]');
          const items = tree ? Array.from(tree.querySelectorAll<HTMLElement>('[role="treeitem"]')) : [];
          const index = items.indexOf(event.currentTarget);
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            router.push(`/${thread.id}`);
          } else if (event.key === "ArrowDown" && index >= 0) {
            event.preventDefault();
            items[Math.min(index + 1, items.length - 1)]?.focus();
          } else if (event.key === "ArrowUp" && index >= 0) {
            event.preventDefault();
            items[Math.max(index - 1, 0)]?.focus();
          } else if (event.key === "Home") {
            event.preventDefault();
            items[0]?.focus();
          } else if (event.key === "End") {
            event.preventDefault();
            items.at(-1)?.focus();
          } else if (thread.has_children && event.key === "ArrowRight") {
            event.preventDefault(); setExpanded(true);
          } else if (thread.has_children && event.key === "ArrowLeft") {
            event.preventDefault(); setExpanded(false);
          }
        }}
      >
        {thread.has_children ? (
          <IconButton
            onClick={(e) => {
              e.stopPropagation();
              setExpanded(!expanded);
            }}
            className="eggw-tree-icon-button"
            aria-label={expanded ? `Collapse ${thread.name || thread.id}` : `Expand ${thread.name || thread.id}`}
          >
            {expanded ? (
              <ChevronDown className="w-4 h-4" />
            ) : (
              <ChevronRight className="w-4 h-4" />
            )}
          </IconButton>
        ) : (
          <span className="w-5" />
        )}

        <MessageSquare className="eggw-tree-icon h-4 w-4" aria-hidden="true" />

        {isEditing ? (
          <>
            <input
              ref={inputRef}
              type="text"
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
              onKeyDown={handleKeyDown}
              onClick={(e) => e.stopPropagation()}
              className="eggw-form-control flex-1 px-2 text-sm"
              placeholder="Thread name"
            />
            <IconButton
              onClick={handleSaveEdit}
              className="eggw-tree-icon-button"
              aria-label="Save thread name"
              title="Save"
            >
              <Check className="w-3 h-3" />
            </IconButton>
            <IconButton
              onClick={handleCancelEdit}
              className="eggw-tree-icon-button"
              aria-label="Cancel thread rename"
              title="Cancel"
            >
              <X className="w-3 h-3" />
            </IconButton>
          </>
        ) : (
          <>
            <span className="flex-1 truncate text-sm">
              {thread.name || thread.id.slice(-8)}
            </span>

            <span className="eggw-ui-muted text-xs">
              {thread.model_key?.split(":")[0]}
            </span>

            {/* Actions (visible on hover) */}
            <div className="eggw-tree-actions">
              <IconButton
                onClick={handleStartEdit}
                className="eggw-tree-icon-button"
                aria-label={`Rename ${thread.name || thread.id}`}
                title="Rename"
              >
                <Pencil className="w-3 h-3" />
              </IconButton>
              <IconButton
                onClick={(e) => {
                  e.stopPropagation();
                  duplicateMutation.mutate({ threadId: thread.id, operationId: createClientOperationId("duplicate") });
                }}
                className="eggw-tree-icon-button"
                aria-label={`Duplicate ${thread.name || thread.id}`}
                title="Duplicate"
              >
                <Copy className="w-3 h-3" />
              </IconButton>
              <IconButton
                variant="danger"
                onClick={(e) => {
                  e.stopPropagation();
                  if (confirm("Delete this thread?")) {
                    deleteMutation.mutate({ threadId: thread.id, operationId: createClientOperationId("delete") });
                  }
                }}
                className="eggw-tree-icon-button"
                aria-label={`Delete ${thread.name || thread.id}`}
                title="Delete"
              >
                <Trash2 className="w-3 h-3" />
              </IconButton>
            </div>
          </>
        )}
      </div>

      {/* Children */}
      {expanded && children && (
        <div role="group">
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
  const addSystemLog = useAppStore((state) => state.addSystemLog);
  const setThreads = useAppStore((state) => state.setThreads);

  const { data: threads, isLoading } = useQuery({
    queryKey: ["rootThreads"],
    queryFn: fetchRootThreads,
  });

  const createMutation = useMutation({
    mutationFn: ({ operationId: _operationId }: { operationId: string }) => createThread({}),
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
      <div className="eggw-tree-header">
        <span className="text-sm font-medium">Threads</span>
        <IconButton
          onClick={() => createMutation.mutate({ operationId: createClientOperationId("create-thread") })}
          className="eggw-tree-icon-button"
          aria-label="New thread"
          title="New thread"
          data-testid="new-thread-btn"
        >
          <Plus className="w-4 h-4" />
        </IconButton>
      </div>

      {/* Thread list */}
      <div className="flex-1 overflow-auto py-1" role="tree" aria-label="Threads">
        {isLoading ? (
          <div className="eggw-compact-state" role="status">Loading threads…</div>
        ) : threads?.length === 0 ? (
          <div className="eggw-compact-state text-center">
            <p>No threads yet</p>
            <Button
              variant="primary"
              onClick={() => createMutation.mutate({ operationId: createClientOperationId("create-thread") })}
              className="mt-2"
            >
              Create first thread
            </Button>
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
