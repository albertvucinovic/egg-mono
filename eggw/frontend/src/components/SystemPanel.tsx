"use client";

import { useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Trash2, RefreshCw, ArrowUp, ArrowDown, GitBranch } from "lucide-react";
import { fetchTokenStats, fetchModels, setThreadModel, fetchThread, fetchThreadChildren, openThread, fetchThreadSettings, fetchThreadState, setAutoApproval, fetchSandboxStatus, executeCommand } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import clsx from "clsx";

interface SystemPanelProps {
  showBorders?: boolean;
}

export function SystemPanel({ showBorders = true }: SystemPanelProps) {
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
    onSuccess: () => {
      addSystemLog("Model changed", "success");
      // Refetch thread settings to get updated model
      refetchSettings();
    },
    onError: () => {
      addSystemLog("Failed to change model", "error");
      // Refetch to restore correct state
      refetchSettings();
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

  // Fetch thread state
  const { data: threadState } = useQuery({
    queryKey: ["threadState", currentThreadId],
    queryFn: () => fetchThreadState(currentThreadId!),
    enabled: !!currentThreadId,
    refetchInterval: 1000, // Poll frequently for state changes
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

  // Fetch sandbox status
  const { data: sandboxStatus, refetch: refetchSandbox } = useQuery({
    queryKey: ["sandbox", currentThreadId],
    queryFn: () => fetchSandboxStatus(currentThreadId!),
    enabled: !!currentThreadId,
    refetchInterval: 5000,
  });

  // Sandbox toggle mutation (uses /toggleSandboxing command)
  const sandboxMutation = useMutation({
    mutationFn: () => executeCommand(currentThreadId!, "/toggleSandboxing"),
    onSuccess: (result) => {
      if (result.success) {
        addSystemLog(result.message, "success");
      } else {
        addSystemLog(result.message, "error");
      }
      refetchSandbox();
      // Also invalidate the sandbox query in page.tsx header
      queryClient.invalidateQueries({ queryKey: ["sandbox", currentThreadId] });
    },
    onError: () => {
      addSystemLog("Failed to toggle sandboxing", "error");
    },
  });

  // Helper to get state display info
  const getStateDisplay = (state: string) => {
    switch (state) {
      case "running":
        return { label: "Running", color: "bg-green-500", pulse: true };
      case "waiting_tool_approval":
        return { label: "Waiting Approval", color: "bg-yellow-500", pulse: true };
      case "waiting_output_approval":
        return { label: "Output Approval", color: "bg-purple-500", pulse: true };
      case "waiting_user":
        return { label: "Ready", color: "bg-blue-500", pulse: false };
      case "paused":
        return { label: "Paused", color: "bg-gray-500", pulse: false };
      default:
        return { label: state, color: "bg-gray-500", pulse: false };
    }
  };

  // Fetch children of current thread
  const { data: children } = useQuery({
    queryKey: ["threadChildren", currentThreadId],
    queryFn: () => fetchThreadChildren(currentThreadId!),
    enabled: !!currentThreadId && currentThreadData?.has_children,
  });

  const { isStreaming } = useAppStore();

  // Fetch token stats for current thread - faster during streaming
  const { data: stats, refetch: refetchStats } = useQuery({
    queryKey: ["stats", currentThreadId],
    queryFn: () => fetchTokenStats(currentThreadId!),
    enabled: !!currentThreadId,
    refetchInterval: isStreaming ? 1000 : 5000, // 1s during streaming, 5s otherwise
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

  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* Thread info - scrollable if needed */}
      {currentThreadId && (
        <div className={`p-3 overflow-auto max-h-[50%] flex-shrink-0 ${showBorders ? 'border-b border-[var(--panel-border)]' : ''}`}>
          <h3 className="text-sm font-medium mb-2">Thread Info</h3>

          <div className="text-xs space-y-1">
            <div className="flex justify-between">
              <span style={{ color: "var(--muted)" }}>ID:</span>
              <span className="font-mono">{currentThreadId.slice(-12)}</span>
            </div>

            {/* Thread State */}
            {threadState && (
              <div className="flex justify-between items-center">
                <span style={{ color: "var(--muted)" }}>Status:</span>
                <div className="flex items-center gap-1.5">
                  <span
                    className={clsx(
                      "w-2 h-2 rounded-full",
                      getStateDisplay(threadState.state).color,
                      getStateDisplay(threadState.state).pulse && "animate-pulse"
                    )}
                  />
                  <span className={clsx(
                    threadState.state === "running" && "text-green-400",
                    threadState.state === "waiting_tool_approval" && "text-yellow-400",
                    threadState.state === "waiting_output_approval" && "text-purple-400",
                    threadState.state === "waiting_user" && "text-blue-400",
                  )}>
                    {getStateDisplay(threadState.state).label}
                  </span>
                </div>
              </div>
            )}

            {/* Model selector */}
            <div className="flex justify-between items-center">
              <span style={{ color: "var(--muted)" }}>Model:</span>
              <select
                value={threadSettings?.model_key || ""}
                onChange={(e) => {
                  if (currentThreadId && e.target.value) {
                    modelMutation.mutate({ threadId: currentThreadId, modelKey: e.target.value });
                  }
                }}
                disabled={modelMutation.isPending}
                className="border rounded px-1 py-0.5 text-xs disabled:opacity-50"
                style={{ background: "var(--code-bg)", borderColor: "var(--panel-border)", color: "var(--foreground)" }}
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
              <span style={{ color: "var(--muted)" }}>Auto-approve:</span>
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

            {/* Sandbox toggle */}
            <div className="flex justify-between items-center">
              <span
                className={clsx(
                  "text-xs px-1.5 py-0.5 rounded border",
                  sandboxStatus?.effective
                    ? "bg-green-900/50 text-green-300 border-green-700"
                    : sandboxStatus?.enabled
                    ? "bg-yellow-900/50 text-yellow-300 border-yellow-700"
                    : "bg-red-900/30 text-red-400 border-red-800"
                )}
                title={
                  sandboxStatus?.effective
                    ? `Sandbox ON (${sandboxStatus.provider || 'unknown'})`
                    : sandboxStatus?.enabled
                    ? `Enabled but not effective: ${sandboxStatus.warning || 'provider unavailable'}`
                    : "Sandbox OFF"
                }
              >
                Sandbox[{sandboxStatus?.effective ? "ON" : sandboxStatus?.enabled ? "!" : "OFF"}]
              </span>
              <button
                onClick={() => sandboxMutation.mutate()}
                disabled={sandboxMutation.isPending || sandboxStatus?.user_control_enabled === false}
                title={sandboxStatus?.user_control_enabled === false ? "User sandbox control is disabled" : "Toggle sandboxing"}
                className={clsx(
                  "relative w-10 h-5 rounded-full transition-colors disabled:opacity-50",
                  sandboxStatus?.enabled ? "bg-green-600" : "bg-gray-600"
                )}
              >
                <span
                  className={clsx(
                    "absolute top-0.5 w-4 h-4 bg-white rounded-full transition-transform",
                    sandboxStatus?.enabled ? "left-5" : "left-0.5"
                  )}
                />
              </button>
            </div>
          </div>

          {/* Thread Navigation */}
          <div className="mt-3 text-xs">
            <div className="flex items-center gap-1 mb-1" style={{ color: "var(--muted)" }}>
              <GitBranch className="w-3 h-3" />
              <span>Navigation</span>
            </div>

            {/* Parent */}
            {currentThreadData?.parent_id && (
              <button
                onClick={() => navigateToThread(currentThreadData.parent_id!)}
                className="flex items-center gap-1 w-full px-2 py-1 text-left rounded"
                style={{ color: "var(--accent)" }}
              >
                <ArrowUp className="w-3 h-3" />
                Parent: {currentThreadData.parent_id.slice(-8)}
              </button>
            )}

            {/* Children */}
            {children && children.length > 0 && (
              <div className="mt-1">
                <div style={{ color: "var(--muted)" }} className="mb-1">Children ({children.length}):</div>
                <div className="max-h-24 overflow-auto space-y-0.5">
                  {children.map((child: any) => (
                    <button
                      key={child.id}
                      onClick={() => navigateToThread(child.id)}
                      className="flex items-center gap-1 w-full px-2 py-0.5 text-left rounded"
                      style={{ color: "var(--accent)" }}
                    >
                      <ArrowDown className="w-3 h-3" />
                      {child.name || child.id.slice(-8)}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {!currentThreadData?.parent_id && (!children || children.length === 0) && (
              <div style={{ color: "var(--muted)" }} className="px-2">Root thread, no children</div>
            )}
          </div>

          {/* Token stats */}
          {stats && (
            <div className="mt-3 text-xs">
              <div className="flex justify-between items-center mb-1">
                <span style={{ color: "var(--muted)" }}>Token Stats</span>
                <button
                  onClick={() => refetchStats()}
                  className="p-0.5 rounded"
                  style={{ color: "var(--muted)" }}
                >
                  <RefreshCw className="w-3 h-3" />
                </button>
              </div>
              <div className="grid grid-cols-2 gap-1" style={{ color: "var(--foreground)" }}>
                <span>Input:</span>
                <span className="text-right">{(stats.input_tokens || 0).toLocaleString()}</span>
                <span>Output:</span>
                <span className="text-right">{(stats.output_tokens || 0).toLocaleString()}</span>
                <span>Reasoning:</span>
                <span className="text-right">{(stats.reasoning_tokens || 0).toLocaleString()}</span>
                <span style={{ color: "var(--tool-msg-border)" }}>Cached:</span>
                <span className="text-right" style={{ color: "var(--tool-msg-border)" }}>{(stats.cached_tokens || 0).toLocaleString()}</span>
                <span>Context:</span>
                <span className="text-right">{(stats.context_tokens || 0).toLocaleString()}</span>
                <span className="font-medium">Total:</span>
                <span className="text-right font-medium">{(stats.total_tokens || 0).toLocaleString()}</span>
                <span className="font-medium" style={{ color: "var(--reasoning-border)" }}>Cost:</span>
                <span className="text-right font-medium" style={{ color: "var(--reasoning-border)" }}>
                  ${(stats.cost_usd || 0).toFixed(4)}
                </span>
              </div>
            </div>
          )}
        </div>
      )}

      {/* System log header */}
      <div className={`p-2 flex items-center justify-between flex-shrink-0 ${showBorders ? 'border-b border-[var(--panel-border)]' : ''}`}>
        <span className="text-sm font-medium">System Log</span>
        <button
          onClick={clearSystemLogs}
          className="p-1 rounded"
          style={{ color: "var(--muted)" }}
          title="Clear logs"
        >
          <Trash2 className="w-4 h-4" />
        </button>
      </div>

      {/* Log entries */}
      <div ref={scrollRef} className="flex-1 overflow-auto p-2 min-h-0">
        {systemLogs.length === 0 ? (
          <div className="text-center text-sm py-4" style={{ color: "var(--muted)" }}>
            No log entries
          </div>
        ) : (
          <div className="space-y-1">
            {systemLogs.map((log, idx) => (
              <div
                key={idx}
                className="text-xs p-1 rounded"
                style={{
                  background: log.type === "error" ? "var(--user-msg-bg)" : log.type === "success" ? "var(--tool-msg-bg)" : undefined,
                  color: log.type === "error" ? "var(--user-msg-border)" : log.type === "success" ? "var(--tool-msg-border)" : "var(--muted)",
                }}
              >
                <span style={{ color: "var(--muted)" }}>
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
