"use client";

import { useState, useEffect, useLayoutEffect, useCallback, useRef } from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChatPanel } from "@/components/ChatPanel";
import { ThreadTree } from "@/components/ThreadTree";
import { MessageInput } from "@/components/MessageInput";
import { SystemPanel } from "@/components/SystemPanel";
import { ApprovalPanel } from "@/components/ApprovalPanel";
import { EditAnswerModal } from "@/components/EditAnswerModal";
import { ShowRecordModal } from "@/components/ShowRecordModal";
import { useAppStore } from "@/lib/store";
import { useSSE } from "@/hooks/useSSE";
import { createThread, openThread, interruptThread, fetchThread, executeCommand, fetchSandboxStatus, SandboxStatus, fetchModels, fetchThreadSettings, setThreadModel, setAutoApproval, fetchTokenStats } from "@/lib/api";
import { useMutation } from "@tanstack/react-query";
import { CircleHelp, PanelLeft, PanelRight, SlidersHorizontal } from "lucide-react";
import clsx from "clsx";
import { formatTokenCount } from "@/lib/tps";
import { refreshTranscriptTail } from "@/lib/transcript";
import { streamingBufferForThread } from "@/lib/streamingBuffer";
import { createClientOperationId } from "@/lib/messageOperations";
import { clearLiveToolsForThread } from "@/lib/liveToolContinuity";
import { refreshThreadModelQueries } from "@/lib/modelSync";
import { HelpDialog } from "@/components/HelpDialog";
import { OverlayPanel } from "@/components/ui/OverlayPanel";
import { ControlGroup, IconButton, Select, StatusChip, Switch } from "@/components/ui/primitives";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { isToggleAutoApprovalShortcut, isToggleSandboxingShortcut } from "@/lib/keyboardShortcuts";

export default function ThreadPage() {
  const params = useParams();
  const router = useRouter();
  const threadId = params.threadId as string;

  const queryClient = useQueryClient();
  const currentThreadId = useAppStore((state) => state.currentThreadId);
  const setCurrentThreadId = useAppStore((state) => state.setCurrentThreadId);
  const addSystemLog = useAppStore((state) => state.addSystemLog);
  const isStreaming = useAppStore((state) => state.streamingByThread[threadId]?.isStreaming || false);
  const streamingKind = useAppStore((state) => state.streamingByThread[threadId]?.streamingKind || null);
  const interruptThreadStreaming = useAppStore((state) => state.interruptThreadStreaming);
  const panelVisibility = useAppStore((state) => state.panelVisibility);
  const togglePanel = useAppStore((state) => state.togglePanel);
  const showBorders = useAppStore((state) => state.showBorders);
  const toggleBorders = useAppStore((state) => state.toggleBorders);
  const enterMode = useAppStore((state) => state.enterMode);
  const setEnterMode = useAppStore((state) => state.setEnterMode);
  const displayVerbosity = useAppStore((state) => state.displayVerbosity);
  const setDisplayVerbosity = useAppStore((state) => state.setDisplayVerbosity);
  const setComposerDraft = useAppStore((state) => state.setComposerDraft);
  const [showHelp, setShowHelp] = useState(false);
  const [showMobileControls, setShowMobileControls] = useState(false);
  const isCompactLayout = useMediaQuery("(max-width: 639px)");
  const wasCompactLayoutRef = useRef(false);
  const appendStagedAttachments = useAppStore((state) => state.appendStagedAttachments);
  const stageAttachment = useCallback((attachment: import("@/lib/contentParts").AttachmentContentPart) => {
    appendStagedAttachments(threadId, [attachment]);
  }, [appendStagedAttachments, threadId]);

  // Preserve the usable chat-first mobile default without turning either
  // sidebar into a modal overlay. The explicit edge controls open them inline.
  useEffect(() => {
    if (isCompactLayout && !wasCompactLayoutRef.current && panelVisibility.threads) {
      togglePanel("threads");
    }
    wasCompactLayoutRef.current = isCompactLayout;
  }, [isCompactLayout, panelVisibility.threads, togglePanel]);

  // Publish route identity before child layout effects run. A detached prior
  // thread must not suppress the new transcript's first pre-paint correction.
  useLayoutEffect(() => {
    if (threadId && threadId !== currentThreadId) {
      setCurrentThreadId(threadId);
      openThread(threadId)
        .then(() => {
          addSystemLog(`Opened thread ${threadId.slice(-8)}`, "info");
        })
        .catch((err) => {
          // Thread not found - redirect to root
          addSystemLog(`Thread not found: ${threadId.slice(-8)}`, "error");
          router.replace("/");
        });
    }
  }, [threadId, currentThreadId, setCurrentThreadId, addSystemLog, router]);

  // Fetch current thread data for header
  const { data: currentThreadData, error: threadError } = useQuery({
    queryKey: ["thread", threadId],
    queryFn: () => fetchThread(threadId),
    enabled: !!threadId,
  });

  // Handle thread not found
  useEffect(() => {
    if (threadError) {
      addSystemLog("Thread no longer exists", "error");
      router.replace("/");
    }
  }, [threadError, addSystemLog, router]);

  // Fetch sandbox status (shared query with SystemPanel, which handles polling)
  const { data: sandboxStatus } = useQuery({
    queryKey: ["sandbox", threadId],
    queryFn: () => fetchSandboxStatus(threadId),
    enabled: !!threadId,
  });

  // Fetch models for header selector
  const { data: modelsData } = useQuery({
    queryKey: ["models"],
    queryFn: fetchModels,
  });

  // Fetch thread settings for model and auto-approval
  const { data: threadSettings } = useQuery({
    queryKey: ["threadSettings", threadId],
    queryFn: ({ signal }) => fetchThreadSettings(threadId, signal),
    enabled: !!threadId,
  });

  // Model change mutation
  const modelMutation = useMutation({
    mutationFn: ({ threadId, modelKey }: { threadId: string; operationId: string; modelKey: string }) =>
      setThreadModel(threadId, modelKey),
    onSuccess: (_data, variables) => {
      addSystemLog("Model changed", "success");
      void refreshThreadModelQueries(queryClient, variables.threadId);
    },
    onError: (_error, variables) => {
      addSystemLog("Failed to change model", "error");
      queryClient.invalidateQueries({ queryKey: ["threadSettings", variables.threadId] });
    },
  });

  // Auto-approval owns an explicit synchronous gate because React mutation
  // state updates after the key event; two same-tick events must not race.
  const autoApprovalPendingRef = useRef(false);

  // Auto-approval toggle mutation
  const autoApprovalMutation = useMutation({
    mutationFn: ({ threadId: sourceThreadId, enabled }: { threadId: string; operationId: string; enabled: boolean }) => setAutoApproval(sourceThreadId, enabled),
    onSuccess: (data, variables) => {
      queryClient.setQueryData(["threadSettings", variables.threadId], (previous: Record<string, unknown> | undefined) => ({
        ...(previous || {}),
        auto_approval: Boolean(data.auto_approval),
      }));
      addSystemLog(
        `Auto-approval ${data.auto_approval ? "enabled" : "disabled"}`,
        "success"
      );
      queryClient.invalidateQueries({ queryKey: ["threadSettings", variables.threadId] });
    },
    onError: (_error, variables) => {
      queryClient.invalidateQueries({ queryKey: ["threadSettings", variables.threadId] });
      addSystemLog("Failed to toggle auto-approval", "error");
    },
    onSettled: () => {
      autoApprovalPendingRef.current = false;
    },
  });

  const toggleAutoApproval = useCallback((operationPrefix: string) => {
    if (!threadId || autoApprovalPendingRef.current || !threadSettings || autoApprovalMutation.isPending) return;
    autoApprovalPendingRef.current = true;
    autoApprovalMutation.mutate({
      threadId,
      operationId: createClientOperationId(operationPrefix),
      enabled: !Boolean(threadSettings.auto_approval),
    });
  }, [autoApprovalMutation.isPending, autoApprovalMutation.mutate, threadId, threadSettings]);

  // Sandbox toggle mutation
  const sandboxMutation = useMutation({
    mutationFn: ({ threadId: sourceThreadId }: { threadId: string; operationId: string }) => executeCommand(sourceThreadId, "/toggleSandboxing"),
    onSuccess: (result, variables) => {
      if (result.success) {
        addSystemLog(result.message, "success");
      } else {
        addSystemLog(result.message, "error");
      }
      queryClient.invalidateQueries({ queryKey: ["sandbox", variables.threadId] });
    },
    onError: (_error, variables) => {
      queryClient.invalidateQueries({ queryKey: ["sandbox", variables.threadId] });
      addSystemLog("Failed to toggle sandboxing", "error");
    },
  });

  // Fetch token stats for cost display and live TPS while LLM text streams.
  const { data: tokenStats } = useQuery({
    queryKey: ["stats", threadId],
    queryFn: () => fetchTokenStats(threadId),
    enabled: !!threadId,
    refetchInterval: isStreaming && streamingKind === "llm" ? 2000 : false,
  });

  const contextHeaderText = formatTokenCount(tokenStats?.context_tokens ?? null);
  const costHeaderText = tokenStats ? `$${(tokenStats.cost_usd || 0).toFixed(4)} cost` : null;
  const currentModelKey = threadSettings?.model_key ?? currentThreadData?.model_key ?? "";
  const modelOptions = modelsData?.models || [];
  const hasCurrentModelOption = !!currentModelKey && modelOptions.some((m: { key: string }) => m.key === currentModelKey);

  // Connect to SSE for real-time streaming
  useSSE(threadId);

  // Keyboard shortcuts
  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    // Safety toggles remain available while the composer is focused. Ctrl+Alt+A/X
    // avoid common browser/terminal shortcuts and audited Readline/tmux/Sway bindings.
    if (!document.querySelector('[role="dialog"][aria-modal="true"]') && isToggleAutoApprovalShortcut(e)) {
      e.preventDefault();
      toggleAutoApproval("auto-approval-shortcut");
      return;
    }
    if (!document.querySelector('[role="dialog"][aria-modal="true"]') && isToggleSandboxingShortcut(e)) {
      e.preventDefault();
      if (threadId && !sandboxMutation.isPending && sandboxStatus?.user_control_enabled !== false) {
        sandboxMutation.mutate({ threadId, operationId: createClientOperationId("sandbox-shortcut") });
      } else if (sandboxStatus?.user_control_enabled === false) {
        addSystemLog("User sandbox control is disabled for this thread", "error");
      }
      return;
    }

    // Escape - Cancel streaming or blur input
    if (e.key === "Escape") {
      // Modal surfaces own Escape so dismissing them never also interrupts a stream.
      if (document.querySelector('[role="dialog"][aria-modal="true"]')) return;
      const target = e.target as HTMLElement;
      if (target.tagName === "INPUT" || target.tagName === "TEXTAREA") {
        target.blur();
      }
      // Cancel streaming if active
      if (isStreaming && threadId) {
        e.preventDefault();
        interruptThread(threadId).then(() => {
          interruptThreadStreaming(threadId);
          streamingBufferForThread(threadId).clear();
          clearLiveToolsForThread(threadId);
          // Refetch messages to get the saved partial content from backend
          void refreshTranscriptTail(queryClient, threadId).catch((error) => {
            console.error("Failed to refresh interrupted transcript tail:", error);
          });
          queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
          queryClient.invalidateQueries({ queryKey: ["threadSettings", threadId] });
          queryClient.invalidateQueries({ queryKey: ["toolCalls", threadId] });
          addSystemLog("Streaming cancelled (Escape)", "info");
        });
      }
      return;
    }

    // Don't trigger other shortcuts when typing in input fields
    const target = e.target as HTMLElement;
    if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.closest('[role="dialog"]')) {
      return;
    }
    if (document.querySelector('[role="dialog"][aria-modal="true"]')) {
      return;
    }

    // Ctrl/Cmd + N - New thread
    if ((e.ctrlKey || e.metaKey) && e.key === "n") {
      e.preventDefault();
      createThread({}).then((thread) => {
        queryClient.invalidateQueries({ queryKey: ["threads"] });
        queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
        router.push(`/${thread.id}`);
        addSystemLog(`Created thread ${thread.id.slice(-8)}`, "success");
      });
    }

    // Ctrl/Cmd + S - Spawn child thread (stays on parent)
    if ((e.ctrlKey || e.metaKey) && e.key === "s" && threadId) {
      e.preventDefault();
      executeCommand(threadId, "/spawnChildThread").then((result) => {
        if (result.success && result.data?.child_id) {
          queryClient.invalidateQueries({ queryKey: ["threads"] });
          queryClient.invalidateQueries({ queryKey: ["threadChildren", threadId] });
          // Don't navigate to child - stay on parent
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
        setComposerDraft(threadId, "/");
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
        setComposerDraft(threadId, "");
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
            setComposerDraft(threadId, before + text + after);
            window.setTimeout(() => {
              input.selectionStart = input.selectionEnd = start + text.length;
            }, 0);
            input.focus();
            addSystemLog("Pasted from clipboard (Ctrl+P)", "info");
          }
        }).catch(() => {
          addSystemLog("Failed to read clipboard", "error");
        });
      }
    }
  }, [queryClient, addSystemLog, showHelp, isStreaming, threadId, setComposerDraft, router, interruptThreadStreaming, toggleAutoApproval, sandboxMutation.mutate, sandboxMutation.isPending, sandboxStatus?.user_control_enabled]);

  useEffect(() => {
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  return (
    <main className="eggw-shell flex flex-col overflow-hidden">
      <HelpDialog open={showHelp} onClose={() => setShowHelp(false)} />

      {/* Responsive application header */}
      <header className="eggw-topbar" data-overlay-background>
        <div className="eggw-topbar-primary">
          <IconButton
            onClick={() => {
              if (isCompactLayout && !panelVisibility.threads && panelVisibility.system) togglePanel("system");
              togglePanel("threads");
            }}
            className="eggw-panel-trigger eggw-threads-trigger"
            aria-label={panelVisibility.threads ? "Hide threads panel" : "Show threads panel"}
            title={panelVisibility.threads ? "Hide threads" : "Show threads"}
            aria-expanded={panelVisibility.threads}
            aria-controls="threads-panel"
            variant={panelVisibility.threads ? "primary" : "secondary"}
          >
            <PanelLeft className="h-5 w-5" aria-hidden="true" />
          </IconButton>
          <div className="eggw-brand-group">
            <h1 className="eggw-brand">eggw</h1>
            <div className="eggw-thread-identity">
              <span className="eggw-eyebrow">Thread</span>
              <span className="eggw-thread-name">
                {currentThreadData?.name || (threadId ? threadId.slice(-8) : "No thread selected")}
              </span>
              {currentThreadData?.name && threadId && <span className="eggw-thread-id">{threadId.slice(-8)}</span>}
            </div>
          </div>
          {threadId && tokenStats && (
            <div className="eggw-header-metric" aria-label={`Context ${contextHeaderText}`}>
              <span>Context</span><strong>{contextHeaderText}</strong>
            </div>
          )}
          <div className="eggw-topbar-actions">
            <IconButton onClick={() => setShowHelp(true)} aria-label="Help" title="Help (?)">
              <CircleHelp className="h-5 w-5" aria-hidden="true" />
            </IconButton>
            <IconButton
              onClick={() => setShowMobileControls(true)}
              aria-label="Open settings"
              title="Open settings"
              className="eggw-mobile-settings-trigger"
            >
              <SlidersHorizontal className="h-5 w-5" aria-hidden="true" />
            </IconButton>
          </div>
          <IconButton
            onClick={() => {
              if (isCompactLayout && !panelVisibility.system && panelVisibility.threads) togglePanel("threads");
              togglePanel("system");
            }}
            className="eggw-panel-trigger eggw-system-trigger"
            aria-label={panelVisibility.system ? "Hide system panel" : "Show system panel"}
            title={panelVisibility.system ? "Hide system" : "Show system"}
            aria-expanded={panelVisibility.system}
            aria-controls="system-panel"
            variant={panelVisibility.system ? "primary" : "secondary"}
          >
            <PanelRight className="h-5 w-5" aria-hidden="true" />
          </IconButton>
        </div>

        <div className="eggw-topbar-controls" aria-label="Thread settings">
          {threadId && modelsData?.models && (
            <ControlGroup className="eggw-model-control">
              <label htmlFor="thread-model">Model</label>
              <Select
                id="thread-model"
                value={currentModelKey}
                onChange={(event) => {
                  if (threadId && event.target.value) {
                    modelMutation.mutate({ threadId, operationId: createClientOperationId("model"), modelKey: event.target.value });
                  }
                }}
                disabled={modelMutation.isPending}
              >
                {!currentModelKey && <option value="" disabled>Loading model...</option>}
                {currentModelKey && !hasCurrentModelOption && <option value={currentModelKey}>{currentModelKey}</option>}
                {modelOptions.map((model: { key: string }) => <option key={model.key} value={model.key}>{model.key}</option>)}
              </Select>
            </ControlGroup>
          )}
          {threadId && (
            <ControlGroup>
              <span>Auto-approve</span>
              <Switch
                checked={Boolean(threadSettings?.auto_approval)}
                onClick={() => toggleAutoApproval("auto-approval")}
                disabled={!threadSettings || autoApprovalMutation.isPending}
                label={threadSettings?.auto_approval ? "Auto-approval ON" : "Auto-approval OFF"}
                title={threadSettings?.auto_approval ? "Auto-approval ON" : "Auto-approval OFF"}
              />
            </ControlGroup>
          )}
          {threadId && sandboxStatus && (
            <ControlGroup>
              <StatusChip
                tone={sandboxStatus.effective ? "success" : sandboxStatus.enabled ? "warning" : "danger"}
                title={sandboxStatus.effective ? `Sandbox ON (${sandboxStatus.provider || "unknown"})` : sandboxStatus.enabled ? `Enabled but not effective: ${sandboxStatus.warning || "provider unavailable"}` : "Sandbox OFF"}
              >
                Sandbox {sandboxStatus.effective ? "on" : sandboxStatus.enabled ? "limited" : "off"}
              </StatusChip>
              <Switch
                checked={sandboxStatus.enabled}
                onClick={() => sandboxMutation.mutate({ threadId, operationId: createClientOperationId("sandbox") })}
                disabled={sandboxMutation.isPending || sandboxStatus.user_control_enabled === false}
                label="Toggle sandboxing"
                title={sandboxStatus.user_control_enabled === false ? "User sandbox control is disabled" : "Toggle sandboxing"}
              />
            </ControlGroup>
          )}
          {threadId && (
            <ControlGroup>
              <label htmlFor="display-verbosity">Verbosity</label>
              <Select id="display-verbosity" value={displayVerbosity} onChange={(event) => setDisplayVerbosity(event.target.value as "max" | "medium" | "min")} title="Transcript display verbosity">
                <option value="max">max</option><option value="medium">medium</option><option value="min">min</option>
              </Select>
            </ControlGroup>
          )}
          {threadId && tokenStats && (
            <div className="eggw-header-metric eggw-cost-metric"><span>Cost</span><strong>{costHeaderText}</strong></div>
          )}
        </div>
      </header>

      <OverlayPanel
        open={showMobileControls}
        onClose={() => setShowMobileControls(false)}
        title="Thread settings"
        description="Model, approval, sandbox, display, and usage settings."
        variant="drawer"
        closeLabel="Close settings"
        testId="settings-drawer"
        returnFocusSelector="[aria-label='Open settings']"
      >
        <div className="eggw-settings-drawer-content">
          {threadId && modelsData?.models && (
            <ControlGroup className="eggw-drawer-control">
              <label htmlFor="drawer-thread-model">Model</label>
              <Select id="drawer-thread-model" value={currentModelKey} onChange={(event) => {
                if (threadId && event.target.value) modelMutation.mutate({ threadId, operationId: createClientOperationId("model"), modelKey: event.target.value });
              }} disabled={modelMutation.isPending}>
                {!currentModelKey && <option value="" disabled>Loading model...</option>}
                {currentModelKey && !hasCurrentModelOption && <option value={currentModelKey}>{currentModelKey}</option>}
                {modelOptions.map((model: { key: string }) => <option key={model.key} value={model.key}>{model.key}</option>)}
              </Select>
            </ControlGroup>
          )}
          <div className="eggw-drawer-setting"><span>Auto-approval</span><Switch checked={Boolean(threadSettings?.auto_approval)} onClick={() => toggleAutoApproval("auto-approval")} disabled={!threadSettings || autoApprovalMutation.isPending} label={threadSettings?.auto_approval ? "Auto-approval ON" : "Auto-approval OFF"} /></div>
          {sandboxStatus && <div className="eggw-drawer-setting"><StatusChip tone={sandboxStatus.effective ? "success" : sandboxStatus.enabled ? "warning" : "danger"}>Sandbox {sandboxStatus.effective ? "on" : sandboxStatus.enabled ? "limited" : "off"}</StatusChip><Switch checked={sandboxStatus.enabled} onClick={() => threadId && sandboxMutation.mutate({ threadId, operationId: createClientOperationId("sandbox") })} disabled={sandboxMutation.isPending || sandboxStatus.user_control_enabled === false} label="Toggle sandboxing" /></div>}
          <ControlGroup className="eggw-drawer-control"><label htmlFor="drawer-verbosity">Verbosity</label><Select id="drawer-verbosity" value={displayVerbosity} onChange={(event) => setDisplayVerbosity(event.target.value as "max" | "medium" | "min")}><option value="max">max</option><option value="medium">medium</option><option value="min">min</option></Select></ControlGroup>
          {tokenStats && <div className="eggw-drawer-usage"><span>Context <strong>{contextHeaderText}</strong></span><span>Cost <strong>{costHeaderText}</strong></span></div>}
        </div>
      </OverlayPanel>

      {/* Main content */}
      <div className="eggw-main-grid flex min-h-0 flex-1 overflow-hidden">
        {/* Left sidebar - full thread tree */}
        <aside
          id="threads-panel"
          className={clsx(
            "eggw-side-card eggw-inline-sidebar eggw-threads-rail",
            panelVisibility.threads ? "eggw-inline-sidebar-open" : "eggw-inline-sidebar-closed",
            !showBorders && "eggw-chrome-borderless",
          )}
          aria-label="Threads panel"
          data-state={panelVisibility.threads ? "open" : "closed"}
          data-overlay-background
        >
          <ThreadTree />
        </aside>

        {/* Center - Chat */}
        <div className={clsx("eggw-chat-card min-w-0 flex-1 flex flex-col overflow-hidden", !showBorders && "eggw-chrome-borderless")} data-overlay-background>
          {panelVisibility.chat && (
            <ChatPanel
              threadId={threadId}
              showBorders={showBorders}
              streamingTps={tokenStats?.streaming_tps ?? null}
              onStageAttachment={stageAttachment}
            />
          )}
          <ApprovalPanel threadId={threadId} showBorders={showBorders} />
          <MessageInput threadId={threadId} showBorders={showBorders} />
        </div>

        {/* Right sidebar - System log */}
        <aside
          id="system-panel"
          className={clsx(
            "eggw-side-card eggw-inline-sidebar eggw-system-rail",
            panelVisibility.system ? "eggw-inline-sidebar-open" : "eggw-inline-sidebar-closed",
            !showBorders && "eggw-chrome-borderless",
          )}
          aria-label="System panel"
          data-state={panelVisibility.system ? "open" : "closed"}
          data-overlay-background
        >
          <SystemPanel showBorders={showBorders} />
        </aside>
      </div>
      <EditAnswerModal />
      <ShowRecordModal threadId={threadId} />
    </main>
  );
}
