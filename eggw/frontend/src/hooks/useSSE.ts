"use client";

import { useEffect, useRef, useCallback } from "react";
import { createEventSource, fetchThreadState, type AuthenticatedEventSource } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { streamingBufferForThread } from "@/lib/streamingBuffer";
import { useQueryClient } from "@tanstack/react-query";
import { createThreadEventSyncState, reconcileThreadEventCursor, reduceThreadEvent, type ThreadEventSyncState } from "@/lib/eventSync";
import { transcriptInfiniteQueryOptions, transcriptQueryKey, transcriptSnapshotCursor } from "@/lib/transcript";
import { emptyThreadStreamingState } from "@/lib/store";

const TOOL_TIMEOUT_KEYS = [
  "timeout",
  "timeout_sec",
  "timeout_seconds",
  "timeout_secs",
  "timeout_s",
  "_tool_timeout_sec",
  "_egg_tool_timeout_sec",
];

function positiveTimeout(value: unknown): number | null {
  const timeout = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN;
  return Number.isFinite(timeout) && timeout > 0 ? timeout : null;
}

function timeoutFromPayload(payload: Record<string, unknown>): number | null {
  for (const key of TOOL_TIMEOUT_KEYS) {
    const timeout = positiveTimeout(payload[key]);
    if (timeout !== null) return timeout;
  }
  return null;
}

function stringifyToolArguments(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined || value === null) return "";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function toolCallArgumentsFromPayload(payload: Record<string, unknown>): string {
  if (Object.prototype.hasOwnProperty.call(payload, "arguments")) {
    return stringifyToolArguments(payload.arguments);
  }
  if (Object.prototype.hasOwnProperty.call(payload, "args")) {
    return stringifyToolArguments(payload.args);
  }
  const fn = payload.function;
  if (fn && typeof fn === "object" && Object.prototype.hasOwnProperty.call(fn, "arguments")) {
    return stringifyToolArguments((fn as Record<string, unknown>).arguments);
  }
  return "";
}

function eventStartedAtMs(value: unknown): number {
  if (typeof value !== "string" || !value.trim()) return Date.now();
  const raw = value.trim();
  const normalized = raw.includes("T") ? raw : `${raw.replace(" ", "T")}Z`;
  const parsed = Date.parse(normalized);
  return Number.isFinite(parsed) ? parsed : Date.now();
}

export function useSSE(threadId: string | null) {
  const eventSourceRef = useRef<AuthenticatedEventSource | null>(null);
  const messageRefreshTimeoutRef = useRef<number | null>(null);
  const syncStateRef = useRef<ThreadEventSyncState | null>(null);
  const queryClient = useQueryClient();
  const setThreadStreamingToolCalls = useAppStore((state) => state.setThreadStreamingToolCalls);
  const setThreadStreamingToolOutputs = useAppStore((state) => state.setThreadStreamingToolOutputs);
  const upsertThreadStreamingToolOutput = useAppStore((state) => state.upsertThreadStreamingToolOutput);
  const markThreadStreamingToolStarted = useAppStore((state) => state.markThreadStreamingToolStarted);
  const clearThreadStreamingToolTimeout = useAppStore((state) => state.clearThreadStreamingToolTimeout);
  const upsertThreadStreamingToolCall = useAppStore((state) => state.upsertThreadStreamingToolCall);
  const appendThreadToolCallArguments = useAppStore((state) => state.appendThreadToolCallArguments);
  const patchThreadStreaming = useAppStore((state) => state.patchThreadStreaming);
  const resetThreadStreaming = useAppStore((state) => state.resetThreadStreaming);
  const setThreadConnection = useAppStore((state) => state.setThreadConnection);
  const addSystemLog = useAppStore((state) => state.addSystemLog);

  const setStreamingToolCalls = useCallback((calls: Record<string, { name: string; arguments: string }>) => {
    if (threadId) setThreadStreamingToolCalls(threadId, calls);
  }, [setThreadStreamingToolCalls, threadId]);
  const setStreamingToolOutputs = useCallback((outputs: Record<string, import("@/lib/store").StreamingToolOutput>) => {
    if (threadId) setThreadStreamingToolOutputs(threadId, outputs);
  }, [setThreadStreamingToolOutputs, threadId]);
  const upsertStreamingToolOutput = useCallback((id: string, name: string, suppressed = false, summary?: string) => {
    if (threadId) upsertThreadStreamingToolOutput(threadId, id, name, suppressed, summary);
  }, [threadId, upsertThreadStreamingToolOutput]);
  const markStreamingToolStarted = useCallback((id: string, name: string, startedAtMs: number, timeoutSec?: number | null) => {
    if (threadId) markThreadStreamingToolStarted(threadId, id, name, startedAtMs, timeoutSec);
  }, [markThreadStreamingToolStarted, threadId]);
  const clearStreamingToolTimeout = useCallback((id: string) => {
    if (threadId) clearThreadStreamingToolTimeout(threadId, id);
  }, [clearThreadStreamingToolTimeout, threadId]);
  const upsertStreamingToolCall = useCallback((id: string, name: string, args: string) => {
    if (threadId) upsertThreadStreamingToolCall(threadId, id, name, args);
  }, [threadId, upsertThreadStreamingToolCall]);
  const appendToolCallArguments = useCallback((id: string, name: string, delta: string) => {
    if (threadId) appendThreadToolCallArguments(threadId, id, name, delta);
  }, [appendThreadToolCallArguments, threadId]);
  const patchStreaming = useCallback((patch: Partial<import("@/lib/store").ThreadStreamingState>) => {
    if (threadId) patchThreadStreaming(threadId, patch);
  }, [patchThreadStreaming, threadId]);
  const setIsStreaming = useCallback((isStreaming: boolean) => patchStreaming({ isStreaming }), [patchStreaming]);
  const setStreamingModelKey = useCallback((streamingModelKey: string | null) => patchStreaming({ streamingModelKey }), [patchStreaming]);
  const setStreamingKind = useCallback((streamingKind: string | null) => patchStreaming({ streamingKind }), [patchStreaming]);
  const setStreamingStartedAtMs = useCallback((streamingStartedAtMs: number | null) => patchStreaming({ streamingStartedAtMs }), [patchStreaming]);
  const setStreamingProviderRequest = useCallback((streamingProviderRequest: import("@/lib/store").StreamingProviderRequest | null) => patchStreaming({ streamingProviderRequest }), [patchStreaming]);
  const setActiveUserCommand = useCallback((activeUserCommand: import("@/lib/store").ActiveUserCommand | null) => patchStreaming({ activeUserCommand }), [patchStreaming]);

  const clearScheduledMessageRefresh = useCallback(() => {
    if (messageRefreshTimeoutRef.current !== null) {
      window.clearTimeout(messageRefreshTimeoutRef.current);
      messageRefreshTimeoutRef.current = null;
    }
  }, []);

  const refreshMessagesNow = useCallback(() => {
    if (!threadId) return;
    queryClient.invalidateQueries({ queryKey: transcriptQueryKey(threadId) });
  }, [queryClient, threadId]);

  const scheduleMessageRefresh = useCallback((delayMs = 750) => {
    if (!threadId) return;
    clearScheduledMessageRefresh();
    messageRefreshTimeoutRef.current = window.setTimeout(() => {
      messageRefreshTimeoutRef.current = null;
      refreshMessagesNow();
    }, delayMs);
  }, [clearScheduledMessageRefresh, refreshMessagesNow, threadId]);

  const connect = useCallback(async () => {
    if (!threadId) return null;

    // Close existing connection
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }
    clearScheduledMessageRefresh();

    // Establish the transcript/cursor first. React Query deduplicates this with
    // ChatPanel's initial request. Events committed after that exact snapshot
    // are then replayed from snapshot_cursor, closing the snapshot-to-live gap.
    let snapshotCursor = -1;
    let activeInvokeId: string | null = null;
    try {
      const [snapshot, threadState] = await Promise.all([
        queryClient.ensureInfiniteQueryData(transcriptInfiniteQueryOptions(threadId, queryClient)),
        fetchThreadState(threadId),
      ]);
      snapshotCursor = transcriptSnapshotCursor(snapshot);
      activeInvokeId = typeof threadState.streaming_invoke_id === "string"
        ? threadState.streaming_invoke_id
        : null;
      const previousStreaming = useAppStore.getState().streamingByThread[threadId];
      if (!activeInvokeId || (previousStreaming?.invokeId && previousStreaming.invokeId !== activeInvokeId)) {
        streamingBufferForThread(threadId).clear();
        resetThreadStreaming(threadId);
      }
      if (activeInvokeId) {
        patchThreadStreaming(threadId, {
          isStreaming: true,
          invokeId: activeInvokeId,
          streamingKind: typeof threadState.streaming_kind === "string" ? threadState.streaming_kind : null,
        });
      }
      syncStateRef.current = createThreadEventSyncState(threadId, snapshotCursor, activeInvokeId);
      setThreadConnection(threadId, { status: "connecting", lastEventSeq: snapshotCursor });
    } catch (error) {
      addSystemLog("Unable to establish message synchronization cursor", "error");
      setThreadConnection(threadId, { status: "disconnected", lastEventSeq: snapshotCursor });
      return null;
    }
    const es = createEventSource(threadId, snapshotCursor);
    eventSourceRef.current = es;

    es.onopen = (openEvent) => {
      const baseline = syncStateRef.current?.lastEventSeq ?? snapshotCursor;
      setThreadConnection(threadId, { status: "connected", lastEventSeq: baseline });
      addSystemLog("SSE connected", "info");
      const isReconnect = openEvent instanceof CustomEvent && Boolean(openEvent.detail?.reconnect);
      if (!isReconnect) return;
      // First refresh the authoritative transcript at its own exact cursor,
      // then reconcile run state against that cursor. Events after it continue
      // to arrive on the resumable feed and are sequence-deduplicated below.
      void queryClient.refetchQueries({ queryKey: transcriptQueryKey(threadId), type: "active" })
        .then(() => {
          const transcript = queryClient.getQueryData<import("@/lib/transcript").TranscriptData>(transcriptQueryKey(threadId));
          const authoritativeCursor = transcriptSnapshotCursor(transcript);
          const current = syncStateRef.current;
          if (!current) return null;
          syncStateRef.current = reconcileThreadEventCursor(current, authoritativeCursor, current.activeInvokeId);
          es.advanceCursor(authoritativeCursor);
          return fetchThreadState(threadId).then((threadState) => ({ authoritativeCursor, threadState }));
        })
        .then((result) => {
          if (!result) return;
          const current = syncStateRef.current;
          if (!current || current.lastEventSeq !== result.authoritativeCursor) return;
          const invokeId = typeof result.threadState.streaming_invoke_id === "string"
            ? result.threadState.streaming_invoke_id
            : null;
          if (invokeId && current.activeInvokeId && current.activeInvokeId !== invokeId) {
            resetThreadStreaming(threadId);
            streamingBufferForThread(threadId).clear();
          }
          syncStateRef.current = reconcileThreadEventCursor(current, result.authoritativeCursor, invokeId);
          if (invokeId) {
            patchThreadStreaming(threadId, {
              isStreaming: true,
              invokeId,
              streamingKind: typeof result.threadState.streaming_kind === "string"
                ? result.threadState.streaming_kind
                : null,
            });
          } else {
            resetThreadStreaming(threadId);
            streamingBufferForThread(threadId).clear();
          }
        })
        .catch(() => undefined);
    };

    es.onerror = () => {
      const lastEventSeq = syncStateRef.current?.lastEventSeq ?? snapshotCursor;
      setThreadConnection(threadId, { status: "reconnecting", lastEventSeq });
      addSystemLog("SSE connection error; reconnecting from cursor", "error");
    };

    const addThreadEventListener = (type: string, listener: (event: MessageEvent<string>) => void) => {
      es.addEventListener(type, (event) => {
        const current = syncStateRef.current || createThreadEventSyncState(threadId, snapshotCursor, activeInvokeId);
        const reduced = reduceThreadEvent(current, event.data, type);
        if (!reduced.accepted) return;
        syncStateRef.current = reduced.state;
        setThreadConnection(threadId, { status: "connected", lastEventSeq: reduced.state.lastEventSeq });
        patchThreadStreaming(threadId, { invokeId: reduced.state.activeInvokeId });
        listener(event);
      });
    };

    // Handle stream.open - streaming started
    addThreadEventListener("stream.open", (e) => {
      try {
        // Parse event data to get model_key
        let modelKey: string | null = null;
        let streamKind: string | null = null;
        try {
          const data = JSON.parse(e.data);
          const payload = data.payload || {};
          modelKey = payload.model_key || null;
          streamKind = payload.stream_kind || payload.purpose || null;
        } catch {
          // Data might not be JSON, ignore
        }

        streamingBufferForThread(threadId).clear();
        clearScheduledMessageRefresh();
        setStreamingToolCalls({});
        setStreamingToolOutputs({});
        setStreamingModelKey(modelKey);
        setStreamingKind(streamKind);
        try {
          const data = JSON.parse(e.data);
          setStreamingStartedAtMs(eventStartedAtMs(data.ts));
        } catch {
          setStreamingStartedAtMs(Date.now());
        }
        setStreamingProviderRequest(null);
        setIsStreaming(true);
        // A stream can begin from another client immediately after that client
        // appends the user turn.  If EggW connected/reconnected between the
        // msg.create and stream.open events, it may not have observed the user
        // msg.create event directly.  Refresh the transcript at the stream
        // boundary so the visible streaming answer has its triggering message.
        refreshMessagesNow();
        queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
        addSystemLog(`Streaming started${modelKey ? ` (${modelKey})` : ""}`, "info");
      } catch (err) {
        console.error("Failed to handle stream.open:", err);
      }
    });

    // Handle stream.delta - streaming content/reasoning/tool_call chunks
    // Direct buffer updates - O(1) per chunk, no React re-render
    addThreadEventListener("stream.delta", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};

        // Direct buffer append - O(1), bypasses React entirely
        if (payload.reason) {
          streamingBufferForThread(threadId).appendReasoning(payload.reason);
        }

        if (typeof payload.reasoning_summary === "string" && payload.reasoning_summary) {
          streamingBufferForThread(threadId).appendReasoningSummary(payload.reasoning_summary);
        }

        if (payload.text) {
          streamingBufferForThread(threadId).appendContent(payload.text);
        }

        if (payload.tool) {
          const tool = payload.tool;
          const toolId = tool.id || tool.name || "tool";
          const toolName = tool.name || "tool";
          if (toolId) {
            const streamingState = useAppStore.getState().streamingByThread[threadId] || emptyThreadStreamingState();
            if (!streamingState.isStreaming) {
              setIsStreaming(true);
            }
            if (streamingState.streamingKind !== "tool") {
              setStreamingKind("tool");
            }
            if (tool.text) {
              streamingBufferForThread(threadId).appendToolOutput(toolId, tool.text);
            }
            // Only update React state when a tool output block first appears,
            // or when the preview limiter emits a suppressed indicator. Text
            // chunks go directly to streamingBuffer/DOM to avoid re-rendering
            // the whole chat for every output line.
            const existingOutput = (useAppStore.getState().streamingByThread[threadId] || emptyThreadStreamingState()).streamingToolOutputs[toolId];
            if (!existingOutput || tool.suppressed) {
              upsertStreamingToolOutput(toolId, toolName, !!tool.suppressed);
            }
          }
        }

        // Tool calls still go through Zustand (less frequent, acceptable)
        if (payload.tool_call) {
          const tc = payload.tool_call;
          const tcId = tc.id || "";
          const tcName = tc.name || "";
          const argsDelta = tc.arguments_delta || "";
          if (tcId && argsDelta) {
            appendToolCallArguments(tcId, tcName, argsDelta);
          }
        }
      } catch (err) {
        console.error("Failed to parse stream.delta:", err);
      }
    });

    // Handle stream.close - streaming finished
    addThreadEventListener("stream.close", () => {
      try {
        streamingBufferForThread(threadId).clear();
        setStreamingToolCalls({});
        setStreamingToolOutputs({});
        setStreamingModelKey(null);
        setStreamingKind(null);
        setStreamingStartedAtMs(null);
        setStreamingProviderRequest(null);
        setIsStreaming(false);
        addSystemLog("Streaming complete", "info");
        scheduleMessageRefresh(100);
        queryClient.invalidateQueries({ queryKey: ["stats", threadId] });
        queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
        queryClient.invalidateQueries({ queryKey: ["toolCalls", threadId] });
      } catch (err) {
        console.error("Failed to handle stream.close:", err);
      }
    });

    // Handle msg.create - new message created
    addThreadEventListener("msg.create", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const role = payload.role || "unknown";
        addSystemLog(`Message created: ${role}`, "info");
        // This event is an authoritative persistence boundary. Refetch the
        // thread-keyed tail immediately so a successful optimistic message is
        // replaced by the complete server record (timestamp/metadata included).
        refreshMessagesNow();
        queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
      } catch (err) {
        console.error("Failed to parse msg.create:", err);
      }
    });

    // Handle tool_call.execution_started
    addThreadEventListener("tool_call.execution_started", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const toolId = payload.tool_call_id || payload.id || payload.name || "tool";
        const toolName = payload.name || payload.tool_name || "tool";
        const timeoutSec = timeoutFromPayload(payload);
        addSystemLog(`Tool executing: ${toolName || "unknown"}`, "info");
        setStreamingKind("tool");
        setIsStreaming(true);
        if (toolId) {
          const toolIdText = String(toolId);
          const toolNameText = String(toolName || "tool");
          const args = toolCallArgumentsFromPayload(payload);
          markStreamingToolStarted(toolIdText, toolNameText, eventStartedAtMs(data.ts), timeoutSec);
          if (args) {
            upsertStreamingToolCall(toolIdText, toolNameText, args);
          }
        }
        queryClient.invalidateQueries({ queryKey: ["toolCalls", threadId] });
        queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
      } catch (err) {
        console.error("Failed to parse tool_call.execution_started:", err);
      }
    });

    addThreadEventListener("provider_request.started", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const timeoutSec = positiveTimeout(payload.timeout ?? payload.timeout_sec);
        setStreamingProviderRequest({
          startedAtMs: eventStartedAtMs(data.ts),
          ...(timeoutSec !== null ? { timeoutSec } : {}),
          modelKey: typeof payload.model_key === "string" ? payload.model_key : null,
        });
        if (typeof payload.model_key === "string" && payload.model_key) {
          setStreamingModelKey(payload.model_key);
        }
        setStreamingKind("llm");
        setIsStreaming(true);
      } catch (err) {
        console.error("Failed to parse provider_request.started:", err);
      }
    });

    addThreadEventListener("user_command.started", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const commandName = String(payload.command_name || "command");
        const commandText = String(payload.command || "");
        const commandId = String(payload.command_id || data.event_seq || commandName);
        setActiveUserCommand({
          id: commandId,
          name: commandName,
          command: commandText,
          startedAtMs: eventStartedAtMs(data.ts || payload.started_at),
        });
        addSystemLog(`Running command: ${commandName.startsWith("$") ? commandName : `/${commandName}`}`, "info");
      } catch (err) {
        console.error("Failed to parse user_command.started:", err);
      }
    });

    addThreadEventListener("user_command.finished", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const elapsed = typeof payload.elapsed_sec === "number" ? ` in ${payload.elapsed_sec.toFixed(1)}s` : "";
        const commandName = String(payload.command_name || "command");
        setActiveUserCommand(null);
        addSystemLog(`Command finished: ${commandName.startsWith("$") ? commandName : `/${commandName}`}${elapsed}`, payload.success === false ? "error" : "success");
      } catch (err) {
        console.error("Failed to parse user_command.finished:", err);
      }
    });

    addThreadEventListener("user_command.status", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const message = typeof payload.message === "string" ? payload.message : "";
        const timeoutSec = timeoutFromPayload(payload);
        if (payload.command_name === "imageGenerate" && timeoutSec !== null) {
          const streamingState = useAppStore.getState().streamingByThread[threadId] || emptyThreadStreamingState();
          if (streamingState.activeUserCommand) {
            setActiveUserCommand({
              ...streamingState.activeUserCommand,
              timeoutSec,
            });
          }
        }
        if (message) {
          addSystemLog(message, "info");
        }
      } catch (err) {
        console.error("Failed to parse user_command.status:", err);
      }
    });

    // Handle tool status summaries (for example timeout countdowns).
    addThreadEventListener("tool_call.summary", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const toolId = payload.tool_call_id || payload.id || payload.name || "tool";
        const toolName = payload.name || "tool";
        const summary = typeof payload.summary === "string" ? payload.summary : "";
        if (toolId && summary) {
          upsertStreamingToolOutput(toolId, toolName, false, summary);
        }
        queryClient.invalidateQueries({ queryKey: ["toolCalls", threadId] });
      } catch (err) {
        console.error("Failed to parse tool_call.summary:", err);
      }
    });

    // Handle tool_call.finished
    addThreadEventListener("tool_call.finished", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const toolId = payload.tool_call_id || payload.id || payload.name;
        if (toolId) {
          clearStreamingToolTimeout(String(toolId));
        }
        addSystemLog("Tool finished", "info");
        queryClient.invalidateQueries({ queryKey: ["toolCalls", threadId] });
        queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
        scheduleMessageRefresh(250);
      } catch (err) {
        console.error("Failed to parse tool_call.finished:", err);
      }
    });

    // Handle tool_call.approval
    addThreadEventListener("tool_call.approval", () => {
      try {
        addSystemLog("Tool approval processed", "info");
        queryClient.invalidateQueries({ queryKey: ["toolCalls", threadId] });
        queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
      } catch (err) {
        console.error("Failed to parse tool_call.approval:", err);
      }
    });

    // Handle tool_call.output_approval
    addThreadEventListener("tool_call.output_approval", () => {
      try {
        addSystemLog("Tool output approval needed", "info");
        queryClient.invalidateQueries({ queryKey: ["toolCalls", threadId] });
        queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
      } catch (err) {
        console.error("Failed to parse tool_call.output_approval:", err);
      }
    });

    // Handle sandbox.config events
    addThreadEventListener("sandbox.config", () => {
      try {
        addSystemLog("Sandbox config changed", "info");
        queryClient.invalidateQueries({ queryKey: ["sandbox", threadId] });
      } catch (err) {
        console.error("Failed to parse sandbox.config:", err);
      }
    });

    // Runtime threads are created as real child threads, then linked by a
    // runtime.config event on the parent. Refresh child/root thread queries so
    // @runtime:* entries appear in the Children panel and tree without a page
    // reload after /pythonRepl or /bashRepl starts them.
    addThreadEventListener("runtime.config", () => {
      try {
        addSystemLog("Runtime thread linked", "info");
        queryClient.invalidateQueries({ queryKey: ["threadChildren", threadId] });
        queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
        queryClient.invalidateQueries({ queryKey: ["thread", threadId] });
      } catch (err) {
        console.error("Failed to parse runtime.config:", err);
      }
    });

    // Child threads can be created by LLM tools, slash commands, runtime
    // setup, or another Egg frontend.  The parent receives a lightweight
    // thread.child_created event; refresh active tree/children queries so the
    // Children panel and thread tree update without a page reload.
    addThreadEventListener("thread.child_created", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const parentId = typeof payload.parent_id === "string" && payload.parent_id ? payload.parent_id : threadId;
        const childId = typeof payload.child_id === "string" ? payload.child_id : "";
        addSystemLog(`Child thread linked${childId ? `: ${childId.slice(-8)}` : ""}`, "info");
        queryClient.invalidateQueries({ queryKey: ["threadChildren", parentId] });
        queryClient.invalidateQueries({ queryKey: ["thread", parentId] });
        queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
        queryClient.invalidateQueries({ queryKey: ["threads"] });
      } catch (err) {
        console.error("Failed to parse thread.child_created:", err);
      }
    });

    // Handle control.interrupt events (e.g., from delayed /continue)
    addThreadEventListener("control.interrupt", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const purpose = payload.purpose || "";
        if (purpose === "continue") {
          addSystemLog("Continue applied - refreshing", "info");
          scheduleMessageRefresh(0);
          queryClient.invalidateQueries({ queryKey: ["toolCalls", threadId] });
          queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
        }
      } catch (err) {
        console.error("Failed to parse control.interrupt:", err);
      }
    });

    return es;
  }, [
    threadId,
    clearScheduledMessageRefresh,
    refreshMessagesNow,
    scheduleMessageRefresh,
    setStreamingToolCalls,
    setStreamingToolOutputs,
    upsertStreamingToolOutput,
    markStreamingToolStarted,
    clearStreamingToolTimeout,
    upsertStreamingToolCall,
    appendToolCallArguments,
    setIsStreaming,
    setStreamingModelKey,
    setStreamingKind,
    setStreamingStartedAtMs,
    setStreamingProviderRequest,
    setActiveUserCommand,
    patchThreadStreaming,
    resetThreadStreaming,
    setThreadConnection,
    addSystemLog,
    queryClient,
  ]);

  const disconnect = useCallback(() => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    clearScheduledMessageRefresh();
    if (threadId) {
      const lastEventSeq = syncStateRef.current?.lastEventSeq ?? -1;
      setThreadConnection(threadId, { status: "disconnected", lastEventSeq });
    }
  }, [clearScheduledMessageRefresh, setThreadConnection, threadId]);

  useEffect(() => {
    let cancelled = false;
    let es: AuthenticatedEventSource | null = null;
    void connect().then((connected) => {
      if (cancelled) {
        connected?.close();
      } else {
        es = connected;
      }
    });
    return () => {
      cancelled = true;
      clearScheduledMessageRefresh();
      es?.close();
      if (threadId) {
        const lastEventSeq = syncStateRef.current?.lastEventSeq ?? -1;
        setThreadConnection(threadId, { status: "disconnected", lastEventSeq });
      }
    };
  }, [clearScheduledMessageRefresh, connect, setThreadConnection, threadId]);

  return { connect, disconnect };
}
