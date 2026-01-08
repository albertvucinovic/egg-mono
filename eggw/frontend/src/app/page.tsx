"use client";

import { useState, useEffect, useCallback } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChatPanel } from "@/components/ChatPanel";
import { ChildrenPanel } from "@/components/ChildrenPanel";
import { MessageInput } from "@/components/MessageInput";
import { SystemPanel } from "@/components/SystemPanel";
import { ApprovalPanel } from "@/components/ApprovalPanel";
import { useAppStore } from "@/lib/store";
import { useSSE } from "@/hooks/useSSE";
import { createThread, openThread, interruptThread, fetchRootThreads, fetchThread, executeCommand, fetchSandboxStatus, SandboxStatus, fetchModels, fetchThreadSettings, setThreadModel, setAutoApproval, fetchTokenStats } from "@/lib/api";
import { useMutation } from "@tanstack/react-query";
import { PanelRight } from "lucide-react";
import clsx from "clsx";

export default function Home() {
  const queryClient = useQueryClient();
  const {
    currentThreadId,
    setCurrentThreadId,
    addSystemLog,
    isStreaming,
    setIsStreaming,
    setStreamingContent,
    setStreamingReasoning,
    setStreamingToolCalls,
    panelVisibility,
    togglePanel,
    showBorders,
    toggleBorders,
    enterMode,
    setEnterMode,
  } = useAppStore();
  const [showHelp, setShowHelp] = useState(false);
  const [initialized, setInitialized] = useState(false);

  // Fetch current thread data for header
  const { data: currentThreadData } = useQuery({
    queryKey: ["thread", currentThreadId],
    queryFn: () => fetchThread(currentThreadId!),
    enabled: !!currentThreadId,
  });

  // Fetch sandbox status (shared query with SystemPanel, which handles polling)
  const { data: sandboxStatus } = useQuery({
    queryKey: ["sandbox", currentThreadId],
    queryFn: () => fetchSandboxStatus(currentThreadId!),
    enabled: !!currentThreadId,
  });

  // Fetch models for header selector
  const { data: modelsData } = useQuery({
    queryKey: ["models"],
    queryFn: fetchModels,
  });

  // Fetch thread settings for model and auto-approval
  const { data: threadSettings, refetch: refetchSettings } = useQuery({
    queryKey: ["threadSettings", currentThreadId],
    queryFn: () => fetchThreadSettings(currentThreadId!),
    enabled: !!currentThreadId,
  });

  // Model change mutation
  const modelMutation = useMutation({
    mutationFn: ({ threadId, modelKey }: { threadId: string; modelKey: string }) =>
      setThreadModel(threadId, modelKey),
    onSuccess: () => {
      addSystemLog("Model changed", "success");
      refetchSettings();
    },
    onError: () => {
      addSystemLog("Failed to change model", "error");
      refetchSettings();
    },
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

  // Sandbox toggle mutation
  const sandboxMutation = useMutation({
    mutationFn: () => executeCommand(currentThreadId!, "/toggleSandboxing"),
    onSuccess: (result) => {
      if (result.success) {
        addSystemLog(result.message, "success");
      } else {
        addSystemLog(result.message, "error");
      }
      queryClient.invalidateQueries({ queryKey: ["sandbox", currentThreadId] });
    },
    onError: () => {
      addSystemLog("Failed to toggle sandboxing", "error");
    },
  });

  // Fetch token stats for cost display - poll during streaming
  const { data: tokenStats } = useQuery({
    queryKey: ["stats", currentThreadId],
    queryFn: () => fetchTokenStats(currentThreadId!),
    enabled: !!currentThreadId,
    refetchInterval: isStreaming ? 5000 : false,
  });

  // Auto-create or select a thread on app load
  useEffect(() => {
    if (initialized || currentThreadId) return;

    const initThread = async () => {
      try {
        const roots = await fetchRootThreads();
        if (roots && roots.length > 0) {
          // Select the most recent thread
          const latest = roots[roots.length - 1];
          setCurrentThreadId(latest.id);
          await openThread(latest.id);
          addSystemLog(`Opened thread ${latest.id.slice(-8)}`, "info");
        } else {
          // No existing threads - create a new one
          const thread = await createThread({});
          setCurrentThreadId(thread.id);
          await openThread(thread.id);
          addSystemLog(`Created thread ${thread.id.slice(-8)}`, "success");
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
        }
      } catch (err) {
        console.error("Failed to initialize thread:", err);
        addSystemLog("Failed to initialize thread", "error");
      }
      setInitialized(true);
    };

    initThread();
  }, [initialized, currentThreadId, setCurrentThreadId, addSystemLog, queryClient]);

  // Connect to SSE for real-time streaming
  useSSE(currentThreadId);

  // Keyboard shortcuts
  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    // Escape - Cancel streaming or blur input
    if (e.key === "Escape") {
      const target = e.target as HTMLElement;
      if (target.tagName === "INPUT" || target.tagName === "TEXTAREA") {
        target.blur();
      }
      // Cancel streaming if active
      if (isStreaming && currentThreadId) {
        e.preventDefault();
        interruptThread(currentThreadId).then(() => {
          setStreamingContent("");
          setStreamingReasoning("");
          setStreamingToolCalls({});
          setIsStreaming(false);
          // Refetch messages to get the saved partial content from backend
          queryClient.invalidateQueries({ queryKey: ["messages", currentThreadId] });
          addSystemLog("Streaming cancelled (Escape)", "info");
        });
      }
      return;
    }

    // Don't trigger other shortcuts when typing in input fields
    const target = e.target as HTMLElement;
    if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT") {
      return;
    }

    // Ctrl/Cmd + N - New thread
    if ((e.ctrlKey || e.metaKey) && e.key === "n") {
      e.preventDefault();
      createThread({}).then((thread) => {
        queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
        setCurrentThreadId(thread.id);
        openThread(thread.id);
        addSystemLog(`Created thread ${thread.id.slice(-8)}`, "success");
      });
    }

    // Ctrl/Cmd + S - Spawn child thread
    if ((e.ctrlKey || e.metaKey) && e.key === "s" && currentThreadId) {
      e.preventDefault();
      executeCommand(currentThreadId, "/spawn").then((result) => {
        if (result.success && result.data?.child_id) {
          queryClient.invalidateQueries({ queryKey: ["threadChildren", currentThreadId] });
          setCurrentThreadId(result.data.child_id);
          openThread(result.data.child_id);
          addSystemLog(`Spawned child ${result.data.child_id.slice(-8)}`, "success");
        } else {
          addSystemLog(result.message || "Failed to spawn child", "error");
        }
      });
    }

    // / - Focus input with slash
    if (e.key === "/" && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      const input = document.querySelector("textarea") as HTMLTextAreaElement;
      if (input) {
        input.focus();
        input.value = "/";
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }

    // ? - Show help
    if (e.key === "?" && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      setShowHelp(!showHelp);
    }

    // i - Focus input
    if (e.key === "i" && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      const input = document.querySelector("textarea") as HTMLTextAreaElement;
      if (input) input.focus();
    }

    // Ctrl/Cmd + E - Clear input
    if ((e.ctrlKey || e.metaKey) && e.key === "e") {
      e.preventDefault();
      const input = document.querySelector("textarea") as HTMLTextAreaElement;
      if (input) {
        input.value = "";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.focus();
        addSystemLog("Input cleared (Ctrl+E)", "info");
      }
    }

    // Ctrl/Cmd + P - Paste from clipboard
    if ((e.ctrlKey || e.metaKey) && e.key === "p") {
      e.preventDefault();
      const input = document.querySelector("textarea") as HTMLTextAreaElement;
      if (input) {
        navigator.clipboard.readText().then((text) => {
          if (text) {
            const start = input.selectionStart || 0;
            const end = input.selectionEnd || 0;
            const before = input.value.substring(0, start);
            const after = input.value.substring(end);
            input.value = before + text + after;
            input.selectionStart = input.selectionEnd = start + text.length;
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.focus();
            addSystemLog("Pasted from clipboard (Ctrl+P)", "info");
          }
        }).catch(() => {
          addSystemLog("Failed to read clipboard", "error");
        });
      }
    }
  }, [queryClient, setCurrentThreadId, addSystemLog, showHelp, isStreaming, currentThreadId, setIsStreaming, setStreamingContent, setStreamingReasoning, setStreamingToolCalls]);

  useEffect(() => {
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  return (
    <main className="h-screen flex flex-col">
      {/* Help Modal */}
      {showHelp && (
        <div
          className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
          onClick={() => setShowHelp(false)}
        >
          <div
            className="border rounded-lg p-6 max-w-md"
            style={{ background: "var(--panel-bg)", borderColor: "var(--panel-border)", color: "var(--foreground)" }}
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="text-lg font-semibold mb-4">Keyboard Shortcuts</h2>
            <div className="space-y-2 text-sm">
              <div className="flex justify-between">
                <span style={{ color: "var(--muted)" }}>Cancel streaming</span>
                <kbd className="px-2 py-0.5 rounded text-xs" style={{ background: "var(--code-bg)" }}>Esc</kbd>
              </div>
              <div className="flex justify-between">
                <span style={{ color: "var(--muted)" }}>New thread</span>
                <kbd className="px-2 py-0.5 rounded text-xs" style={{ background: "var(--code-bg)" }}>Ctrl+N</kbd>
              </div>
              <div className="flex justify-between">
                <span style={{ color: "var(--muted)" }}>Spawn child thread</span>
                <kbd className="px-2 py-0.5 rounded text-xs" style={{ background: "var(--code-bg)" }}>Ctrl+S</kbd>
              </div>
              <div className="flex justify-between">
                <span style={{ color: "var(--muted)" }}>Clear input</span>
                <kbd className="px-2 py-0.5 rounded text-xs" style={{ background: "var(--code-bg)" }}>Ctrl+E</kbd>
              </div>
              <div className="flex justify-between">
                <span style={{ color: "var(--muted)" }}>Paste clipboard</span>
                <kbd className="px-2 py-0.5 rounded text-xs" style={{ background: "var(--code-bg)" }}>Ctrl+P</kbd>
              </div>
              <div className="flex justify-between">
                <span style={{ color: "var(--muted)" }}>Focus input</span>
                <kbd className="px-2 py-0.5 rounded text-xs" style={{ background: "var(--code-bg)" }}>i</kbd>
              </div>
              <div className="flex justify-between">
                <span style={{ color: "var(--muted)" }}>Start command</span>
                <kbd className="px-2 py-0.5 rounded text-xs" style={{ background: "var(--code-bg)" }}>/</kbd>
              </div>
              <div className="flex justify-between">
                <span style={{ color: "var(--muted)" }}>Show this help</span>
                <kbd className="px-2 py-0.5 rounded text-xs" style={{ background: "var(--code-bg)" }}>?</kbd>
              </div>
            </div>
            <div className="mt-4 pt-4 border-t border-[var(--panel-border)] text-sm" style={{ color: "var(--muted)" }}>
              <p className="font-medium mb-2" style={{ color: "var(--foreground)" }}>Commands:</p>
              <p>/model, /updateAllModels, /spawn, /spawnAutoApprovedChildThread</p>
              <p>/newThread, /threads, /thread, /rename, /waitForThreads</p>
              <p>/parentThread, /listChildren, /deleteThread, /duplicateThread</p>
              <p>/toggleAutoApproval, /toolsOn, /toolsOff, /toolsStatus</p>
              <p>/disableTool, /enableTool, /toolsSecrets</p>
              <p>/toggleSandboxing, /setSandboxConfiguration, /getSandboxingConfig</p>
              <p>/togglePanel, /toggleBorders, /enterMode, /cost, /quit</p>
              <p>$ cmd - Shell, $$ cmd - Hidden shell</p>
            </div>
            <button
              onClick={() => setShowHelp(false)}
              className="mt-4 w-full py-2 rounded"
              style={{ background: "var(--accent)", color: "var(--background)" }}
            >
              Close
            </button>
          </div>
        </div>
      )}

      {/* Header - Two rows */}
      <header className="border-b border-[var(--panel-border)] bg-[var(--panel-bg)]">
        {/* Row 1: Thread info and sidebar toggle */}
        <div className="h-8 flex items-center px-4 border-b border-[var(--panel-border)]">
          <h1 className="text-sm font-semibold">eggw</h1>
          {currentThreadId && (
            <span className="ml-3 text-sm">
              <span style={{ color: "var(--muted)" }}>Thread:</span>{" "}
              {currentThreadData?.name ? (
                <>
                  <span style={{ color: "var(--foreground)" }}>{currentThreadData.name}</span>
                  <span style={{ color: "var(--muted)" }} className="ml-1">({currentThreadId.slice(-8)})</span>
                </>
              ) : (
                <span style={{ color: "var(--muted)" }}>{currentThreadId.slice(-8)}</span>
              )}
            </span>
          )}
          {!currentThreadId && (
            <span className="ml-3 text-sm" style={{ color: "var(--muted)" }}>No thread selected</span>
          )}

          {/* Context length */}
          {currentThreadId && tokenStats && (
            <span className="ml-4 text-xs" style={{ color: "var(--muted)" }}>
              Context: <span style={{ color: "var(--foreground)" }}>{(tokenStats.context_tokens || 0).toLocaleString()}</span>
            </span>
          )}

          <div className="ml-auto flex items-center gap-2">
            {/* Help button */}
            <button
              onClick={() => setShowHelp(true)}
              className="text-xs px-1.5 py-0.5"
              style={{ color: "var(--muted)" }}
              title="Help (?)"
            >
              ?
            </button>

            {/* Sidebar toggle */}
            <button
              onClick={() => togglePanel("system")}
              title={panelVisibility.system ? "Hide sidebar" : "Show sidebar"}
              className={clsx(
                "p-1 rounded transition-colors",
                panelVisibility.system
                  ? "bg-[var(--accent)] text-[var(--background)]"
                  : "text-[var(--muted)] hover:text-[var(--foreground)]"
              )}
            >
              <PanelRight className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Row 2: Controls */}
        <div className="h-7 flex items-center px-4 gap-4 text-xs">
          {/* Model selector */}
          {currentThreadId && modelsData?.models && (
            <div className="flex items-center gap-1.5">
              <span style={{ color: "var(--muted)" }}>Model:</span>
              <select
                value={threadSettings?.model_key || ""}
                onChange={(e) => {
                  if (currentThreadId && e.target.value) {
                    modelMutation.mutate({ threadId: currentThreadId, modelKey: e.target.value });
                  }
                }}
                disabled={modelMutation.isPending}
                className="border rounded px-1.5 py-0.5 text-xs disabled:opacity-50"
                style={{ background: "var(--code-bg)", borderColor: "var(--panel-border)", color: "var(--foreground)" }}
              >
                {modelsData.models.map((m: { key: string }) => (
                  <option key={m.key} value={m.key}>
                    {m.key}
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* Auto-approval toggle */}
          {currentThreadId && (
            <div className="flex items-center gap-1.5">
              <span style={{ color: "var(--muted)" }}>Auto:</span>
              <button
                onClick={() => autoApprovalMutation.mutate(!threadSettings?.auto_approval)}
                disabled={autoApprovalMutation.isPending}
                title={threadSettings?.auto_approval ? "Auto-approval ON" : "Auto-approval OFF"}
                className={clsx(
                  "relative w-8 h-4 rounded-full transition-colors disabled:opacity-50",
                  threadSettings?.auto_approval ? "bg-green-600" : "bg-gray-600"
                )}
              >
                <span
                  className={clsx(
                    "absolute top-0.5 w-3 h-3 bg-white rounded-full transition-transform",
                    threadSettings?.auto_approval ? "left-4" : "left-0.5"
                  )}
                />
              </button>
            </div>
          )}

          {/* Sandbox toggle with status */}
          {currentThreadId && sandboxStatus && (
            <div className="flex items-center gap-1.5">
              <span
                className={clsx(
                  "px-1.5 py-0.5 rounded border cursor-default",
                  sandboxStatus.effective
                    ? "bg-green-900/50 text-green-300 border-green-700"
                    : sandboxStatus.enabled
                    ? "bg-yellow-900/50 text-yellow-300 border-yellow-700"
                    : "bg-red-900/30 text-red-400 border-red-800"
                )}
                title={
                  sandboxStatus.effective
                    ? `Sandbox ON (${sandboxStatus.provider || 'unknown'})`
                    : sandboxStatus.enabled
                    ? `Enabled but not effective: ${sandboxStatus.warning || 'provider unavailable'}`
                    : "Sandbox OFF"
                }
              >
                Sandbox[{sandboxStatus.effective ? "ON" : sandboxStatus.enabled ? "!" : "OFF"}]
              </span>
              <button
                onClick={() => sandboxMutation.mutate()}
                disabled={sandboxMutation.isPending || sandboxStatus?.user_control_enabled === false}
                title={sandboxStatus?.user_control_enabled === false ? "User sandbox control is disabled" : "Toggle sandboxing"}
                className={clsx(
                  "relative w-8 h-4 rounded-full transition-colors disabled:opacity-50",
                  sandboxStatus.enabled ? "bg-green-600" : "bg-gray-600"
                )}
              >
                <span
                  className={clsx(
                    "absolute top-0.5 w-3 h-3 bg-white rounded-full transition-transform",
                    sandboxStatus.enabled ? "left-4" : "left-0.5"
                  )}
                />
              </button>
            </div>
          )}

          {/* Cost display */}
          {currentThreadId && tokenStats && (
            <div className="flex items-center gap-1.5">
              <span style={{ color: "var(--muted)" }}>Cost:</span>
              <span style={{ color: "var(--reasoning-border)" }} className="font-medium">
                ${(tokenStats.cost_usd || 0).toFixed(4)}
              </span>
            </div>
          )}
        </div>
      </header>

      {/* Main content */}
      <div className="flex-1 flex overflow-hidden">
        {/* Center - Chat */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {panelVisibility.children && <ChildrenPanel showBorders={showBorders} />}
          {panelVisibility.chat && <ChatPanel showBorders={showBorders} />}
          <ApprovalPanel showBorders={showBorders} />
          <MessageInput showBorders={showBorders} />
        </div>

        {/* Right sidebar - System log */}
        {panelVisibility.system && (
          <div className={`w-80 flex flex-col overflow-hidden ${showBorders ? 'border-l border-[var(--panel-border)]' : ''}`}>
            <SystemPanel showBorders={showBorders} />
          </div>
        )}
      </div>
    </main>
  );
}
