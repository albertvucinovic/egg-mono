"use client";

import { useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Trash2, RefreshCw, ArrowUp, ArrowDown, GitBranch } from "lucide-react";
import { fetchTokenStats, fetchModels, setThreadModel, fetchThread, fetchThreadChildren, openThread, fetchThreadSettings, setAutoApproval } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import clsx from "clsx";

export function SystemPanel() {
  const scrollRef = useRef<HTMLDivElement>(null);
  const queryClient = useQueryClient();
  const {
    currentThreadId,
    setCurrentThreadId,
    systemLogs,
    clearSystemLogs,
    addSystemLog,
    models,
    setModels,
    threads,
    setThreads,
  } = useAppStore();

  // Fetch models on mount
  const { data: modelsData } = useQuery({
    queryKey: ["models"],
    queryFn: fetchModels,
  });

  useEffect(() => {
    if (modelsData?.models) {
      setModels(modelsData.models);
    }
  }, [modelsData, setModels]);

  // Model change mutation
  const modelMutation = useMutation({
    mutationFn: ({ threadId, modelKey }: { threadId: string; modelKey: string }) =>
      setThreadModel(threadId, modelKey),
    onMutate: ({ threadId, modelKey }) => {
      // Optimistically update the thread's model in the store
      setThreads(threads.map(t =>
        t.id === threadId ? { ...t, model_key: modelKey } : t
      ));
    },
    onSuccess: () => {
      addSystemLog("Model changed", "success");
      // Refetch threads to ensure sync
      queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
    },
    onError: () => {
      addSystemLog("Failed to change model", "error");
      // Refetch to restore correct state
      queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
    },
  });

  // Fetch current thread details
  const { data: currentThreadData } = useQuery({
    queryKey: ["thread", currentThreadId],
    queryFn: () => fetchThread(currentThreadId!),
    enabled: !!currentThreadId,
  });

  // Fetch thread settings (including auto-approval status)
  const { data: threadSettings, refetch: refetchSettings } = useQuery({
    queryKey: ["threadSettings", currentThreadId],
    queryFn: () => fetchThreadSettings(currentThreadId!),
    enabled: !!currentThreadId,
    refetchInterval: 5000,
  });

  // Auto-approval toggle mutation
  const autoApprovalMutation = useMutation({
    mutationFn: (enabled: boolean) => setAutoApproval(currentThreadId!, enabled),
    onSuccess: (data) => {
      addSystemLog(
        `Auto-approval ${data.auto_approval ? "enabled" : "disabled"}`,
        "success"
      );
      refetchSettings();
    },
    onError: () => {
      addSystemLog("Failed to toggle auto-approval", "error");
    },
  });

  // Fetch children of current thread
  const { data: children } = useQuery({
    queryKey: ["threadChildren", currentThreadId],
    queryFn: () => fetchThreadChildren(currentThreadId!),
    enabled: !!currentThreadId && currentThreadData?.has_children,
  });

  // Fetch token stats for current thread
  const { data: stats, refetch: refetchStats } = useQuery({
    queryKey: ["stats", currentThreadId],
    queryFn: () => fetchTokenStats(currentThreadId!),
    enabled: !!currentThreadId,
    refetchInterval: 5000,
  });

  // Navigate to thread helper
  const navigateToThread = (threadId: string) => {
    setCurrentThreadId(threadId);
    openThread(threadId).then(() => {
      addSystemLog(`Switched to thread ${threadId.slice(-8)}`, "info");
    });
    queryClient.invalidateQueries({ queryKey: ["messages", threadId] });
  };

  // Auto-scroll to bottom
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [systemLogs]);

  const currentThread = threads.find((t) => t.id === currentThreadId);

  return (
    <div className="h-full flex flex-col">
      {/* Thread info */}
      {currentThreadId && (
        <div className="p-3 border-b border-[var(--panel-border)]">
          <h3 className="text-sm font-medium mb-2">Thread Info</h3>

          <div className="text-xs space-y-1">
            <div className="flex justify-between">
              <span className="text-gray-400">ID:</span>
              <span className="font-mono">{currentThreadId.slice(-12)}</span>
            </div>

            {/* Model selector */}
            <div className="flex justify-between items-center">
              <span className="text-gray-400">Model:</span>
              <select
                value={currentThread?.model_key || ""}
                onChange={(e) => {
                  if (currentThreadId && e.target.value) {
                    modelMutation.mutate({ threadId: currentThreadId, modelKey: e.target.value });
                  }
                }}
                disabled={modelMutation.isPending}
                className="bg-[#111] border border-[var(--panel-border)] rounded px-1 py-0.5 text-xs disabled:opacity-50"
              >
                {models.map((m) => (
                  <option key={m.key} value={m.key}>
                    {m.key}
                  </option>
                ))}
              </select>
            </div>

            {/* Auto-approval toggle */}
            <div className="flex justify-between items-center">
              <span className="text-gray-400">Auto-approve:</span>
              <button
                onClick={() => autoApprovalMutation.mutate(!threadSettings?.auto_approval)}
                disabled={autoApprovalMutation.isPending}
                className={clsx(
                  "relative w-10 h-5 rounded-full transition-colors disabled:opacity-50",
                  threadSettings?.auto_approval ? "bg-green-600" : "bg-gray-600"
                )}
              >
                <span
                  className={clsx(
                    "absolute top-0.5 w-4 h-4 bg-white rounded-full transition-transform",
                    threadSettings?.auto_approval ? "left-5" : "left-0.5"
                  )}
                />
              </button>
            </div>
          </div>

          {/* Thread Navigation */}
          <div className="mt-3 text-xs">
            <div className="flex items-center gap-1 mb-1">
              <GitBranch className="w-3 h-3 text-gray-400" />
              <span className="text-gray-400">Navigation</span>
            </div>

            {/* Parent */}
            {currentThreadData?.parent_id && (
              <button
                onClick={() => navigateToThread(currentThreadData.parent_id!)}
                className="flex items-center gap-1 w-full px-2 py-1 text-left hover:bg-[#333] rounded text-blue-400"
              >
                <ArrowUp className="w-3 h-3" />
                Parent: {currentThreadData.parent_id.slice(-8)}
              </button>
            )}

            {/* Children */}
            {children && children.length > 0 && (
              <div className="mt-1">
                <div className="text-gray-500 mb-1">Children ({children.length}):</div>
                <div className="max-h-24 overflow-auto space-y-0.5">
                  {children.map((child: any) => (
                    <button
                      key={child.id}
                      onClick={() => navigateToThread(child.id)}
                      className="flex items-center gap-1 w-full px-2 py-0.5 text-left hover:bg-[#333] rounded text-green-400"
                    >
                      <ArrowDown className="w-3 h-3" />
                      {child.name || child.id.slice(-8)}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {!currentThreadData?.parent_id && (!children || children.length === 0) && (
              <div className="text-gray-500 px-2">Root thread, no children</div>
            )}
          </div>

          {/* Token stats */}
          {stats && (
            <div className="mt-3 text-xs">
              <div className="flex justify-between items-center mb-1">
                <span className="text-gray-400">Token Stats</span>
                <button
                  onClick={() => refetchStats()}
                  className="p-0.5 hover:bg-[#333] rounded"
                >
                  <RefreshCw className="w-3 h-3" />
                </button>
              </div>
              <div className="grid grid-cols-2 gap-1 text-gray-300">
                <span>Input:</span>
                <span className="text-right">{stats.input_tokens?.toLocaleString()}</span>
                <span>Output:</span>
                <span className="text-right">{stats.output_tokens?.toLocaleString()}</span>
                <span>Reasoning:</span>
                <span className="text-right">{stats.reasoning_tokens?.toLocaleString()}</span>
                {stats.cached_tokens > 0 && (
                  <>
                    <span className="text-green-400">Cached:</span>
                    <span className="text-right text-green-400">{stats.cached_tokens?.toLocaleString()}</span>
                  </>
                )}
                <span>Context:</span>
                <span className="text-right">{stats.context_tokens?.toLocaleString()}</span>
                <span className="font-medium">Total:</span>
                <span className="text-right font-medium">{stats.total_tokens?.toLocaleString()}</span>
                {stats.cost_usd != null && (
                  <>
                    <span className="text-yellow-400 font-medium">Cost:</span>
                    <span className="text-right text-yellow-400 font-medium">
                      ${stats.cost_usd.toFixed(4)}
                    </span>
                  </>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {/* System log header */}
      <div className="p-2 border-b border-[var(--panel-border)] flex items-center justify-between">
        <span className="text-sm font-medium">System Log</span>
        <button
          onClick={clearSystemLogs}
          className="p-1 hover:bg-[#333] rounded"
          title="Clear logs"
        >
          <Trash2 className="w-4 h-4" />
        </button>
      </div>

      {/* Log entries */}
      <div ref={scrollRef} className="flex-1 overflow-auto p-2">
        {systemLogs.length === 0 ? (
          <div className="text-center text-gray-500 text-sm py-4">
            No log entries
          </div>
        ) : (
          <div className="space-y-1">
            {systemLogs.map((log, idx) => (
              <div
                key={idx}
                className={clsx(
                  "text-xs p-1 rounded",
                  log.type === "error" && "bg-red-900/30 text-red-300",
                  log.type === "success" && "bg-green-900/30 text-green-300",
                  log.type === "info" && "text-gray-400"
                )}
              >
                <span className="text-gray-500">
                  {log.timestamp.toLocaleTimeString()}
                </span>{" "}
                {log.message}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
