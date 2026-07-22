"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronDown,
  ChevronRight,
  ClipboardCopy,
  CopyPlus,
  MessageSquare,
  Pencil,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import {
  createThread,
  deleteThread,
  duplicateThread,
  fetchThreads,
  renameThread,
} from "@/lib/api";
import { useAppStore, type Thread } from "@/lib/store";
import { buildThreadForest, threadAncestorIds, type ThreadTreeNode } from "@/lib/threadTree";
import { createClientOperationId } from "@/lib/messageOperations";
import clsx from "clsx";
import { Button, IconButton } from "@/components/ui/primitives";

interface TreeNodeProps {
  thread: ThreadTreeNode;
  level: number;
  expandedIds: Set<string>;
  toggleExpanded: (threadId: string) => void;
  onNavigate?: () => void;
}

function TreeNode({ thread, level, expandedIds, toggleExpanded, onNavigate }: TreeNodeProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [editName, setEditName] = useState(thread.name || "");
  const inputRef = useRef<HTMLInputElement>(null);
  const router = useRouter();
  const currentThreadId = useAppStore((state) => state.currentThreadId);
  const addSystemLog = useAppStore((state) => state.addSystemLog);
  const queryClient = useQueryClient();
  const hasChildren = thread.children.length > 0;
  const expanded = hasChildren && expandedIds.has(thread.id);

  const refreshTree = () => {
    void queryClient.invalidateQueries({ queryKey: ["threads"] });
  };

  const deleteMutation = useMutation({
    mutationFn: ({ threadId }: { threadId: string; operationId: string }) => deleteThread(threadId),
    onSuccess: (_data, variables) => {
      refreshTree();
      queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
      queryClient.invalidateQueries({ queryKey: ["threadChildren"] });
      addSystemLog(`Deleted thread ${variables.threadId.slice(-8)}`, "success");
    },
    onError: () => addSystemLog("Failed to delete thread", "error"),
  });

  const duplicateMutation = useMutation({
    mutationFn: ({ threadId }: { threadId: string; operationId: string }) => duplicateThread(threadId),
    onSuccess: (newThread) => {
      refreshTree();
      queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
      queryClient.invalidateQueries({ queryKey: ["threadChildren"] });
      addSystemLog(`Duplicated thread as ${newThread.id.slice(-8)}`, "success");
    },
    onError: () => addSystemLog("Failed to duplicate thread", "error"),
  });

  const renameMutation = useMutation({
    mutationFn: ({ threadId, name }: { threadId: string; operationId: string; name: string }) => renameThread(threadId, name),
    onSuccess: (_data, variables) => {
      refreshTree();
      queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
      queryClient.invalidateQueries({ queryKey: ["threadChildren"] });
      queryClient.invalidateQueries({ queryKey: ["thread", variables.threadId] });
      addSystemLog(`Renamed thread to "${variables.name}"`, "success");
      setIsEditing(false);
    },
    onError: () => addSystemLog("Failed to rename thread", "error"),
  });

  const navigate = () => {
    router.push(`/${thread.id}`);
    onNavigate?.();
  };

  const saveEdit = () => {
    const name = editName.trim();
    if (!name) {
      setIsEditing(false);
      return;
    }
    renameMutation.mutate({
      threadId: thread.id,
      operationId: createClientOperationId("rename"),
      name,
    });
  };

  return (
    <div>
      <div
        role="treeitem"
        data-thread-id={thread.id}
        aria-label={`${thread.name || "Unnamed thread"} ${thread.id.slice(-8)}`}
        aria-level={level + 1}
        aria-selected={currentThreadId === thread.id}
        aria-expanded={hasChildren ? expanded : undefined}
        tabIndex={0}
        className={clsx("eggw-tree-row group", currentThreadId === thread.id && "eggw-tree-row-selected")}
        style={{ paddingLeft: `${level * 16 + 8}px` }}
        onClick={navigate}
        onKeyDown={(event) => {
          if (event.target !== event.currentTarget) return;
          const tree = event.currentTarget.closest('[role="tree"]');
          const items = tree ? Array.from(tree.querySelectorAll<HTMLElement>('[role="treeitem"]')) : [];
          const index = items.indexOf(event.currentTarget);
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            navigate();
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
          } else if (hasChildren && event.key === "ArrowRight" && !expanded) {
            event.preventDefault();
            toggleExpanded(thread.id);
          } else if (hasChildren && event.key === "ArrowLeft" && expanded) {
            event.preventDefault();
            toggleExpanded(thread.id);
          }
        }}
      >
        {hasChildren ? (
          <IconButton
            onClick={(event) => {
              event.stopPropagation();
              toggleExpanded(thread.id);
            }}
            className="eggw-tree-icon-button"
            aria-label={expanded ? `Collapse ${thread.name || thread.id}` : `Expand ${thread.name || thread.id}`}
          >
            {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          </IconButton>
        ) : (
          <span className="eggw-tree-expander-spacer" aria-hidden="true" />
        )}

        <MessageSquare className="eggw-tree-icon h-4 w-4" aria-hidden="true" />

        {isEditing ? (
          <>
            <input
              ref={inputRef}
              type="text"
              value={editName}
              onChange={(event) => setEditName(event.target.value)}
              onClick={(event) => event.stopPropagation()}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  saveEdit();
                } else if (event.key === "Escape") {
                  event.preventDefault();
                  setEditName(thread.name || "");
                  setIsEditing(false);
                }
              }}
              className="eggw-form-control min-w-0 flex-1 px-2 text-sm"
              placeholder="Thread name"
              aria-label={`Name for thread ${thread.id}`}
            />
            <IconButton onClick={(event) => { event.stopPropagation(); saveEdit(); }} className="eggw-tree-icon-button" aria-label="Save thread name" title="Save">
              <Check className="h-3 w-3" />
            </IconButton>
            <IconButton onClick={(event) => { event.stopPropagation(); setEditName(thread.name || ""); setIsEditing(false); }} className="eggw-tree-icon-button" aria-label="Cancel thread rename" title="Cancel">
              <X className="h-3 w-3" />
            </IconButton>
          </>
        ) : (
          <>
            <span className="eggw-tree-label">
              <span>{thread.name || "Unnamed thread"}</span>
              <code title={thread.id}>{thread.id.slice(-8)}</code>
            </span>
            <div className="eggw-tree-actions">
              <IconButton
                onClick={(event) => {
                  event.stopPropagation();
                  setEditName(thread.name || "");
                  setIsEditing(true);
                  window.setTimeout(() => inputRef.current?.focus(), 0);
                }}
                className="eggw-tree-icon-button"
                aria-label={`Rename ${thread.name || thread.id}`}
                title="Rename"
              >
                <Pencil className="h-3 w-3" />
              </IconButton>
              <IconButton
                onClick={(event) => {
                  event.stopPropagation();
                  duplicateMutation.mutate({ threadId: thread.id, operationId: createClientOperationId("duplicate") });
                }}
                className="eggw-tree-icon-button"
                aria-label={`Duplicate ${thread.name || thread.id}`}
                title="Duplicate"
              >
                <CopyPlus className="h-3 w-3" />
              </IconButton>
              <IconButton
                onClick={(event) => {
                  event.stopPropagation();
                  if (!navigator.clipboard?.writeText) return;
                  void navigator.clipboard.writeText(thread.id);
                }}
                className="eggw-tree-icon-button"
                aria-label={`Copy thread ID ${thread.id}`}
                title="Copy full thread ID"
              >
                <ClipboardCopy className="h-3 w-3" />
              </IconButton>
              <IconButton
                variant="danger"
                onClick={(event) => {
                  event.stopPropagation();
                  if (window.confirm("Delete this thread?")) {
                    deleteMutation.mutate({ threadId: thread.id, operationId: createClientOperationId("delete") });
                  }
                }}
                className="eggw-tree-icon-button"
                aria-label={`Delete ${thread.name || thread.id}`}
                title="Delete"
              >
                <Trash2 className="h-3 w-3" />
              </IconButton>
            </div>
          </>
        )}
      </div>

      {expanded && (
        <div role="group">
          {thread.children.map((child) => (
            <TreeNode
              key={child.id}
              thread={child}
              level={level + 1}
              expandedIds={expandedIds}
              toggleExpanded={toggleExpanded}
              onNavigate={onNavigate}
            />
          ))}
        </div>
      )}
    </div>
  );
}

interface ThreadTreeProps {
  onNavigate?: () => void;
}

export function ThreadTree({ onNavigate }: ThreadTreeProps = {}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const addSystemLog = useAppStore((state) => state.addSystemLog);
  const currentThreadId = useAppStore((state) => state.currentThreadId);
  const setThreads = useAppStore((state) => state.setThreads);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());

  const { data: threads = [], isLoading, isError } = useQuery<Thread[]>({
    queryKey: ["threads"],
    queryFn: fetchThreads,
  });
  const forest = useMemo(() => buildThreadForest(threads), [threads]);
  const selectedAncestors = useMemo(() => threadAncestorIds(threads, currentThreadId), [currentThreadId, threads]);

  useEffect(() => {
    setThreads(threads);
  }, [setThreads, threads]);

  useEffect(() => {
    const required = new Set(selectedAncestors);
    if (currentThreadId && threads.some((thread) => thread.id === currentThreadId && thread.has_children)) {
      required.add(currentThreadId);
    }
    if (required.size === 0) return;
    setExpandedIds((previous) => {
      const next = new Set(previous);
      let changed = false;
      required.forEach((id) => {
        if (!next.has(id)) {
          next.add(id);
          changed = true;
        }
      });
      return changed ? next : previous;
    });
  }, [currentThreadId, selectedAncestors, threads]);

  const createMutation = useMutation({
    mutationFn: ({ operationId: _operationId }: { operationId: string }) => createThread({}),
    onSuccess: (newThread) => {
      queryClient.invalidateQueries({ queryKey: ["threads"] });
      queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
      router.push(`/${newThread.id}`);
      onNavigate?.();
      addSystemLog(`Created thread ${newThread.id.slice(-8)}`, "success");
    },
    onError: () => addSystemLog("Failed to create thread", "error"),
  });

  const toggleExpanded = (threadId: string) => {
    setExpandedIds((previous) => {
      const next = new Set(previous);
      if (next.has(threadId)) next.delete(threadId);
      else next.add(threadId);
      return next;
    });
  };

  return (
    <div className="eggw-thread-tree h-full min-h-0 flex flex-col">
      <div className="eggw-tree-header">
        <div className="min-w-0">
          <h2 className="text-sm font-medium">Threads</h2>
          <span>{threads.length} total</span>
        </div>
        <IconButton
          onClick={() => createMutation.mutate({ operationId: createClientOperationId("create-thread") })}
          className="eggw-tree-icon-button"
          aria-label="New thread"
          title="New thread"
          data-testid="new-thread-btn"
        >
          <Plus className="h-4 w-4" />
        </IconButton>
      </div>

      <div className="eggw-tree-list flex-1 overflow-auto py-1" role="tree" aria-label="Threads">
        {isLoading ? (
          <div className="eggw-compact-state" role="status">Loading threads…</div>
        ) : isError ? (
          <div className="eggw-compact-state" role="alert">Could not load threads.</div>
        ) : forest.length === 0 ? (
          <div className="eggw-compact-state text-center">
            <p>No threads yet</p>
            <Button variant="primary" onClick={() => createMutation.mutate({ operationId: createClientOperationId("create-thread") })} className="mt-2">
              Create first thread
            </Button>
          </div>
        ) : (
          forest.map((thread) => (
            <TreeNode
              key={thread.id}
              thread={thread}
              level={0}
              expandedIds={expandedIds}
              toggleExpanded={toggleExpanded}
              onNavigate={onNavigate}
            />
          ))
        )}
      </div>
    </div>
  );
}
