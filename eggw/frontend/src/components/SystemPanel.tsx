"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Trash2, RefreshCw, ArrowUp, ArrowDown, GitBranch } from "lucide-react";
import { fetchTokenStats, fetchThread, fetchThreadChildren, fetchThreadState, fetchThreadSettings } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { formatStreamingTps, formatTokenCount } from "@/lib/tps";
import clsx from "clsx";
import { Button, IconButton, StatusChip, type StatusTone } from "@/components/ui/primitives";

interface SystemPanelProps {
  showBorders?: boolean;
}

export function SystemPanel({ showBorders = true }: SystemPanelProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const router = useRouter();
  const currentThreadId = useAppStore((state) => state.currentThreadId);
  const systemLogs = useAppStore((state) => state.systemLogs);
  const clearSystemLogs = useAppStore((state) => state.clearSystemLogs);
  const streamingKind = useAppStore((state) => currentThreadId ? state.streamingByThread[currentThreadId]?.streamingKind || null : null);

  // Fetch current thread details
  const { data: currentThreadData } = useQuery({
    queryKey: ["thread", currentThreadId],
    queryFn: () => fetchThread(currentThreadId!),
    enabled: !!currentThreadId,
  });

  // Fetch thread state - updated via SSE events (no polling needed)
  const { data: threadState } = useQuery({
    queryKey: ["threadState", currentThreadId],
    queryFn: () => fetchThreadState(currentThreadId!),
    enabled: !!currentThreadId,
  });

  const { data: threadSettings } = useQuery({
    queryKey: ["threadSettings", currentThreadId],
    queryFn: () => fetchThreadSettings(currentThreadId!),
    enabled: !!currentThreadId,
  });

  // Helper to get state display info
  const getStateDisplay = (state: string): { label: string; tone: StatusTone; pulse: boolean } => {
    switch (state) {
      case "running":
        return { label: "Running", tone: "success", pulse: true };
      case "waiting_tool_approval":
        return { label: "Waiting Approval", tone: "warning", pulse: true };
      case "waiting_output_approval":
        return { label: "Output Approval", tone: "special", pulse: true };
      case "waiting_user":
        return { label: "Ready", tone: "info", pulse: false };
      case "paused":
        return { label: "Paused", tone: "neutral", pulse: false };
      default:
        return { label: state, tone: "neutral", pulse: false };
    }
  };

  // Fetch children of current thread
  const { data: children } = useQuery({
    queryKey: ["threadChildren", currentThreadId],
    queryFn: () => fetchThreadChildren(currentThreadId!),
    enabled: !!currentThreadId && currentThreadData?.has_children,
  });

  const isStreaming = useAppStore((state) => currentThreadId ? state.streamingByThread[currentThreadId]?.isStreaming || false : false);

  // Fetch token stats for current thread.  ThreadPage owns live polling for the
  // shared stats query so this side panel does not duplicate expensive reads on
  // very large threads.
  const { data: stats, refetch: refetchStats } = useQuery({
    queryKey: ["stats", currentThreadId],
    queryFn: () => fetchTokenStats(currentThreadId!),
    enabled: !!currentThreadId,
  });

  // Navigate to thread helper
  const navigateToThread = (threadId: string) => {
    router.push(`/${threadId}`);
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
        <div className={clsx("eggw-system-section p-3 overflow-auto max-h-[50%] flex-shrink-0", showBorders && "eggw-system-section-emphasized")}>
          <h3 className="text-sm font-medium mb-2">Thread Info</h3>

          <div className="text-xs space-y-1">
            <div className="flex justify-between">
              <span className="eggw-ui-muted">ID:</span>
              <span className="font-mono">{currentThreadId.slice(-12)}</span>
            </div>

            {/* Thread State */}
            {threadState && (
              <div className="flex justify-between items-center">
                <span className="eggw-ui-muted">Status:</span>
                <StatusChip tone={getStateDisplay(threadState.state).tone} className={getStateDisplay(threadState.state).pulse ? "eggw-status-pulse" : undefined}>
                  {getStateDisplay(threadState.state).label}
                </StatusChip>
              </div>
            )}

          </div>

          {/* Thread Navigation */}
          <div className="mt-3 text-xs">
            <div className="eggw-ui-muted mb-1 flex items-center gap-1">
              <GitBranch className="w-3 h-3" />
              <span>Navigation</span>
            </div>

            {/* Parent */}
            {currentThreadData?.parent_id && (
              <Button
                variant="ghost"
                onClick={() => navigateToThread(currentThreadData.parent_id!)}
                className="eggw-link w-full justify-start px-2 py-1 text-left"
              >
                <ArrowUp className="w-3 h-3" />
                Parent: {currentThreadData.parent_id.slice(-8)}
              </Button>
            )}

            {/* Children */}
            {children && children.length > 0 && (
              <div className="mt-1">
                <div className="eggw-ui-muted mb-1">Children ({children.length}):</div>
                <div className="max-h-24 overflow-auto space-y-0.5">
                  {children.map((child: any) => (
                    <Button
                      variant="ghost"
                      key={child.id}
                      onClick={() => navigateToThread(child.id)}
                      className="eggw-link w-full justify-start px-2 py-0.5 text-left"
                    >
                      <ArrowDown className="w-3 h-3" />
                      {child.name || child.id.slice(-8)}
                    </Button>
                  ))}
                </div>
              </div>
            )}

            {!currentThreadData?.parent_id && (!children || children.length === 0) && (
              <div className="eggw-ui-muted px-2">Root thread, no children</div>
            )}
          </div>

          {/* Token stats */}
          {stats && (
            <div className="mt-3 text-xs">
              <div className="flex justify-between items-center mb-1">
                <span className="eggw-ui-muted">Token Stats</span>
                <IconButton
                  onClick={() => refetchStats()}
                  aria-label="Refresh token stats"
                  title="Refresh token stats"
                  className="eggw-icon-button-compact"
                >
                  <RefreshCw className="w-3 h-3" />
                </IconButton>
              </div>
              <div className="grid grid-cols-2 gap-1" >
                <span>Input:</span>
                <span className="text-right">{formatTokenCount(stats.input_tokens || 0)}</span>
                <span>Output:</span>
                <span className="text-right">{formatTokenCount(stats.output_tokens || 0)}</span>
                <span>Reasoning:</span>
                <span className="text-right">{formatTokenCount(stats.reasoning_tokens || 0)}</span>
                <span className="eggw-status-success-text">Cached:</span>
                <span className="eggw-status-success-text text-right">{formatTokenCount(stats.cached_tokens || 0)}</span>
                <span>Context:</span>
                <span className="text-right">{formatTokenCount(stats.context_tokens || 0)}</span>
                <span>Full Thread:</span>
                <span className="text-right">{formatTokenCount(stats.full_thread_tokens || stats.context_tokens || 0)}</span>
                {isStreaming && streamingKind === "llm" && typeof stats.streaming_tps === "number" && Number.isFinite(stats.streaming_tps) && stats.streaming_tps > 0 && (
                  <>
                    <span>TPS:</span>
                    <span className="text-right">{formatStreamingTps(stats.streaming_tps)}</span>
                  </>
                )}
                <span className="font-medium">Total:</span>
                <span className="text-right font-medium">{formatTokenCount(stats.total_tokens || 0)}</span>
                <span className="eggw-status-special-text font-medium">Cost:</span>
                <span className="eggw-status-special-text text-right font-medium">
                  ${Number(stats.cost_usd || 0).toFixed(4)} cost
                </span>
              </div>
            </div>
          )}
        </div>
      )}

      {/* System log header */}
      <div className={clsx("eggw-system-section p-2 flex items-center justify-between flex-shrink-0", showBorders && "eggw-system-section-emphasized")}>
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">System Log</span>
          {threadSettings && (
            <StatusChip tone={threadSettings.auto_approval ? "warning" : "success"}>
              Autoapproval[{threadSettings.auto_approval ? "On" : "Off"}]
            </StatusChip>
          )}
        </div>
        <IconButton
          onClick={clearSystemLogs}
          title="Clear logs"
          aria-label="Clear logs"
        >
          <Trash2 className="w-4 h-4" />
        </IconButton>
      </div>

      {/* Log entries */}
      <div ref={scrollRef} className="flex-1 overflow-auto p-2 min-h-0">
        {systemLogs.length === 0 ? (
          <div className="eggw-ui-muted py-4 text-center text-sm">
            No log entries
          </div>
        ) : (
          <div className="space-y-1">
            {systemLogs.map((log, idx) => (
              <div
                key={idx}
                className={clsx(
                  "eggw-system-log-entry",
                  log.type === "error" && "eggw-system-log-error",
                  log.type === "success" && "eggw-system-log-success",
                )}
              >
                <span className="eggw-ui-muted">
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
