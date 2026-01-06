"use client";

import { useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Trash2, RefreshCw } from "lucide-react";
import { fetchTokenStats, fetchModels, setThreadModel } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import clsx from "clsx";

export function SystemPanel() {
  const scrollRef = useRef<HTMLDivElement>(null);
  const queryClient = useQueryClient();
  const {
    currentThreadId,
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

  // Fetch token stats for current thread
  const { data: stats, refetch: refetchStats } = useQuery({
    queryKey: ["stats", currentThreadId],
    queryFn: () => fetchTokenStats(currentThreadId!),
    enabled: !!currentThreadId,
    refetchInterval: 5000,
  });

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
                <span className="text-right">{stats.input_tokens}</span>
                <span>Output:</span>
                <span className="text-right">{stats.output_tokens}</span>
                <span>Reasoning:</span>
                <span className="text-right">{stats.reasoning_tokens}</span>
                <span className="font-medium">Total:</span>
                <span className="text-right font-medium">{stats.total_tokens}</span>
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
