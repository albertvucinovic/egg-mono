"use client";

import { useEffect, useRef, useCallback } from "react";
import {
  createEventSource,
  fetchThreadState,
  type AuthenticatedEventSource,
} from "@/lib/api";
import { useAppStore } from "@/lib/store";
import {
  evictStreamingBufferForThread,
  streamingBufferForThread,
  streamingBufferThreadIds,
} from "@/lib/streamingBuffer";
import { applyStreamingDelta } from "@/lib/streamingDelta";
import {
  getUserAnswerToolCallId,
  toolDisplayName,
} from "@/lib/toolPresentation";
import { messageFromCreateEvent } from "@/lib/messageEvents";
import {
  cleanUpEvictedLiveTools,
  clearLiveToolsForThread,
  hasRetainedLiveToolsForThread,
  liveToolRegistryForThread,
} from "@/lib/liveToolContinuity";
import { useQueryClient } from "@tanstack/react-query";
import {
  createThreadEventSyncState,
  reduceThreadEvent,
  type ThreadEventSyncState,
} from "@/lib/eventSync";
import {
  invalidateTranscriptAuthoritatively,
  patchTranscriptMessage,
  refreshTranscriptTail,
  rewindTranscriptForContinuation,
  transcriptInfiniteQueryOptions,
  transcriptQueryKey,
  transcriptSnapshotCursor,
  upsertTranscriptTailMessage,
  type TranscriptData,
} from "@/lib/transcript";
import { emptyThreadStreamingState } from "@/lib/store";
import { canEvictThreadEphemeralState } from "@/lib/threadEphemeral";
import { applyCanonicalModelSwitch, reconcileThreadModelSnapshot } from "@/lib/modelSync";

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
  const timeout =
    typeof value === "number"
      ? value
      : typeof value === "string"
        ? Number(value)
        : NaN;
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

function toolCallArgumentsFromPayload(
  payload: Record<string, unknown>,
): string {
  if (Object.prototype.hasOwnProperty.call(payload, "arguments")) {
    return stringifyToolArguments(payload.arguments);
  }
  if (Object.prototype.hasOwnProperty.call(payload, "args")) {
    return stringifyToolArguments(payload.args);
  }
  const fn = payload.function;
  if (
    fn &&
    typeof fn === "object" &&
    Object.prototype.hasOwnProperty.call(fn, "arguments")
  ) {
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
  const eventSourceThreadIdRef = useRef<string | null>(null);
  const syncStateRef = useRef<ThreadEventSyncState | null>(null);
  const setupGenerationRef = useRef(0);
  const pendingContinuationRewindRef = useRef<{
    threadId: string;
    continueEventId: string;
  } | null>(null);
  const queryClient = useQueryClient();
  const upsertThreadStreamingToolOutput = useAppStore(
    (state) => state.upsertThreadStreamingToolOutput,
  );
  const markThreadStreamingToolStarted = useAppStore(
    (state) => state.markThreadStreamingToolStarted,
  );
  const markThreadStreamingToolFinished = useAppStore(
    (state) => state.markThreadStreamingToolFinished,
  );
  const removeThreadStreamingToolCall = useAppStore(
    (state) => state.removeThreadStreamingToolCall,
  );
  const removeThreadStreamingTool = useAppStore(
    (state) => state.removeThreadStreamingTool,
  );
  const clearThreadStreamingAssistant = useAppStore(
    (state) => state.clearThreadStreamingAssistant,
  );
  const upsertThreadStreamingToolCall = useAppStore(
    (state) => state.upsertThreadStreamingToolCall,
  );
  const patchThreadStreaming = useAppStore(
    (state) => state.patchThreadStreaming,
  );
  const resetThreadStreaming = useAppStore(
    (state) => state.resetThreadStreaming,
  );
  const setThreadConnection = useAppStore((state) => state.setThreadConnection);
  const evictThreadEphemeralState = useAppStore(
    (state) => state.evictThreadEphemeralState,
  );
  const addSystemLog = useAppStore((state) => state.addSystemLog);

  const upsertStreamingToolOutput = useCallback(
    (id: string, name: string, suppressed = false, summary?: string) => {
      if (threadId)
        upsertThreadStreamingToolOutput(
          threadId,
          id,
          name,
          suppressed,
          summary,
        );
    },
    [threadId, upsertThreadStreamingToolOutput],
  );
  const markStreamingToolStarted = useCallback(
    (
      id: string,
      name: string,
      startedAtMs: number,
      timeoutSec?: number | null,
    ) => {
      if (threadId)
        markThreadStreamingToolStarted(
          threadId,
          id,
          name,
          startedAtMs,
          timeoutSec,
        );
    },
    [markThreadStreamingToolStarted, threadId],
  );
  const markStreamingToolFinished = useCallback(
    (id: string) => {
      if (threadId) markThreadStreamingToolFinished(threadId, id);
    },
    [markThreadStreamingToolFinished, threadId],
  );
  const removeStreamingTool = useCallback(
    (id: string) => {
      if (!threadId) return;
      streamingBufferForThread(threadId).removeTool(id);
      removeThreadStreamingTool(threadId, id);
    },
    [removeThreadStreamingTool, threadId],
  );
  const liveToolsForThread = useCallback(
    (sourceThreadId: string) => {
      const access = liveToolRegistryForThread(sourceThreadId);
      cleanUpEvictedLiveTools(access.evicted, (evictedThreadId, toolCallId) => {
        streamingBufferForThread(evictedThreadId).removeTool(toolCallId);
        removeThreadStreamingTool(evictedThreadId, toolCallId);
      });
      return access.registry;
    },
    [removeThreadStreamingTool],
  );
  const hideStreamingToolCall = useCallback(
    (id: string) => {
      if (!threadId) return;
      streamingBufferForThread(threadId).removeToolCall(id);
      removeThreadStreamingToolCall(threadId, id);
    },
    [removeThreadStreamingToolCall, threadId],
  );
  const clearRetainedTools = useCallback(() => {
    if (!threadId) return;
    clearLiveToolsForThread(threadId).forEach(removeStreamingTool);
  }, [removeStreamingTool, threadId]);
  const reconcileDurableToolMessage = useCallback(
    (message: import("@/lib/store").Message) => {
      if (!threadId) return;
      const reconciliation =
        liveToolsForThread(threadId).reconcileMessage(message);
      reconciliation.hideCalls.forEach(hideStreamingToolCall);
      reconciliation.removeTools.forEach(removeStreamingTool);
    },
    [hideStreamingToolCall, liveToolsForThread, removeStreamingTool, threadId],
  );
  const upsertStreamingToolCall = useCallback(
    (id: string, name: string) => {
      if (threadId) upsertThreadStreamingToolCall(threadId, id, name);
    },
    [threadId, upsertThreadStreamingToolCall],
  );
  const patchStreaming = useCallback(
    (patch: Partial<import("@/lib/store").ThreadStreamingState>) => {
      if (threadId) patchThreadStreaming(threadId, patch);
    },
    [patchThreadStreaming, threadId],
  );
  const setIsStreaming = useCallback(
    (isStreaming: boolean) => patchStreaming({ isStreaming }),
    [patchStreaming],
  );
  const setStreamingModelKey = useCallback(
    (streamingModelKey: string | null) => patchStreaming({ streamingModelKey }),
    [patchStreaming],
  );
  const setStreamingKind = useCallback(
    (streamingKind: string | null) => patchStreaming({ streamingKind }),
    [patchStreaming],
  );
  const setStreamingStartedAtMs = useCallback(
    (streamingStartedAtMs: number | null) =>
      patchStreaming({ streamingStartedAtMs }),
    [patchStreaming],
  );
  const setStreamingProviderRequest = useCallback(
    (
      streamingProviderRequest:
        import("@/lib/store").StreamingProviderRequest | null,
    ) => patchStreaming({ streamingProviderRequest }),
    [patchStreaming],
  );
  const setActiveUserCommand = useCallback(
    (activeUserCommand: import("@/lib/store").ActiveUserCommand | null) =>
      patchStreaming({ activeUserCommand }),
    [patchStreaming],
  );

  const refreshMessagesNow = useCallback(() => {
    if (!threadId) return;
    void refreshTranscriptTail(queryClient, threadId).catch((error) => {
      console.error("Failed to refresh transcript tail:", error);
    });
  }, [queryClient, threadId]);

  const rewindMessagesNow = useCallback(() => {
    if (!threadId) return;
    rewindTranscriptForContinuation(queryClient, threadId);
    void queryClient.fetchInfiniteQuery(
      transcriptInfiniteQueryOptions(threadId, queryClient),
    ).catch((error) => {
      console.error("Failed to rebuild transcript after continuation:", error);
    });
  }, [queryClient, threadId]);

  const evictInactiveThreadState = useCallback(
    (protectedThreadId: string | null = threadId) => {
      const state = useAppStore.getState();
      const candidates = new Set([
        ...streamingBufferThreadIds(),
        ...Object.keys(state.streamingByThread),
        ...Object.keys(state.connectionByThread),
      ]);
      candidates.forEach((candidateThreadId) => {
        const streaming = state.streamingByThread[candidateThreadId];
        if (
          !canEvictThreadEphemeralState({
            isCurrent:
              candidateThreadId === protectedThreadId ||
              state.currentThreadId === candidateThreadId,
            isStreaming: Boolean(streaming?.isStreaming),
            connectionStatus:
              state.connectionByThread[candidateThreadId]?.status,
            hasRetainedTools: hasRetainedLiveToolsForThread(candidateThreadId),
          })
        )
          return;
        evictStreamingBufferForThread(candidateThreadId);
        evictThreadEphemeralState(candidateThreadId);
      });
    },
    [evictThreadEphemeralState, threadId],
  );

  const invalidateSetup = useCallback(() => {
    setupGenerationRef.current += 1;
    syncStateRef.current = null;
    pendingContinuationRewindRef.current = null;
  }, []);

  const closeOwnedEventSource = useCallback(() => {
    if (!eventSourceRef.current) return;
    eventSourceRef.current.close();
    eventSourceRef.current = null;
    eventSourceThreadIdRef.current = null;
  }, []);

  const connect = useCallback(async () => {
    if (!threadId) return null;
    const generation = ++setupGenerationRef.current;
    const ownsSetup = () => setupGenerationRef.current === generation;
    evictInactiveThreadState(threadId);

    // Close existing connection
    closeOwnedEventSource();

    // Establish a durable bounded tail first. A cached route refreshes only that
    // tail while preserving loaded history; /state then resolves a distinct live
    // replay cursor from the exact committed snapshot before transport starts.
    let snapshotCursor = -1;
    let replayCursor = -1;
    let activeInvokeId: string | null = null;
    try {
      const cachedSnapshot = queryClient.getQueryData<TranscriptData>(
        transcriptQueryKey(threadId),
      );
      const snapshot = cachedSnapshot
        ? await refreshTranscriptTail(queryClient, threadId)
        : await queryClient.ensureInfiniteQueryData(
            transcriptInfiniteQueryOptions(threadId, queryClient),
          );
      snapshotCursor = transcriptSnapshotCursor(snapshot);
      if (!ownsSetup()) return null;
      const threadState = await fetchThreadState(threadId, snapshotCursor);
      if (!ownsSetup()) return null;
      replayCursor = Number.isSafeInteger(threadState.live_replay_cursor)
        ? Number(threadState.live_replay_cursor)
        : snapshotCursor;
      activeInvokeId =
        typeof threadState.streaming_invoke_id === "string"
          ? threadState.streaming_invoke_id
          : null;
      const previousStreaming =
        useAppStore.getState().streamingByThread[threadId];
      if (!activeInvokeId) {
        streamingBufferForThread(threadId).clear();
        clearRetainedTools();
        resetThreadStreaming(threadId);
      } else if (
        previousStreaming?.invokeId &&
        previousStreaming.invokeId !== activeInvokeId
      ) {
        // Invocation changes clear assistant text only. Phase 2 tool state may
        // legitimately publish its result from this later runner invocation.
        streamingBufferForThread(threadId).clearAssistantText();
        clearThreadStreamingAssistant(threadId);
      }
      if (activeInvokeId) {
        patchThreadStreaming(threadId, {
          isStreaming: true,
          invokeId: activeInvokeId,
          streamingKind:
            typeof threadState.streaming_kind === "string"
              ? threadState.streaming_kind
              : null,
        });
      }
      syncStateRef.current = createThreadEventSyncState(
        threadId,
        replayCursor,
        activeInvokeId,
      );
      setThreadConnection(threadId, "connecting");
    } catch (error) {
      if (ownsSetup()) {
        addSystemLog(
          "Unable to establish message synchronization cursor",
          "error",
        );
        setThreadConnection(threadId, "disconnected");
      }
      return null;
    }
    if (!ownsSetup()) return null;
    const es = createEventSource(threadId, replayCursor);
    if (!ownsSetup()) {
      es.close();
      return null;
    }
    eventSourceRef.current = es;
    eventSourceThreadIdRef.current = threadId;

    es.onopen = (openEvent) => {
      if (!ownsSetup()) return;
      setThreadConnection(threadId, "connected");
      addSystemLog("SSE connected", "info");
      const isReconnect =
        openEvent instanceof CustomEvent &&
        Boolean(openEvent.detail?.reconnect);
      if (!isReconnect) return;
      void reconcileThreadModelSnapshot(queryClient, threadId).catch((error) => {
        console.error("Failed to reconcile model settings after SSE reconnect:", error);
      });
      // Refresh durable messages independently. Neither their projection cursor
      // nor /state may acknowledge queued transport frames; Last-Event-ID and
      // reducer sequence advance only inside the ordered listener below.
      void refreshTranscriptTail(queryClient, threadId).catch((error) => {
        console.error(
          "Failed to refresh transcript tail after reconnect:",
          error,
        );
      });
      // Invocation reconciliation also stays frame-ordered: a replacement is
      // adopted only when its stream.open arrives through the listener below.
    };

    es.onerror = () => {
      if (!ownsSetup()) return;
      setThreadConnection(threadId, "reconnecting");
      addSystemLog("SSE connection error; reconnecting from cursor", "error");
    };

    const addThreadEventListener = (
      type: string,
      listener: (event: MessageEvent<string>) => void,
    ) => {
      es.addEventListener(type, (event) => {
        if (!ownsSetup()) return;
        const current =
          syncStateRef.current ||
          createThreadEventSyncState(threadId, replayCursor, activeInvokeId);
        const reduced = reduceThreadEvent(current, event.data, type);
        if (!reduced.accepted) return;
        syncStateRef.current = reduced.state;
        if (reduced.state.activeInvokeId !== current.activeInvokeId) {
          patchThreadStreaming(threadId, {
            invokeId: reduced.state.activeInvokeId,
          });
        }
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

        streamingBufferForThread(threadId).clearAssistantText();
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
        addSystemLog(
          `Streaming started${modelKey ? ` (${modelKey})` : ""}`,
          "info",
        );
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

        const notifications = applyStreamingDelta(
          streamingBufferForThread(threadId),
          payload,
        );
        if (notifications.toolOutput) {
          const { id, name, suppressed } = notifications.toolOutput;
          liveToolsForThread(threadId)
            .observe(id, true)
            .forEach(removeStreamingTool);
          setIsStreaming(true);
          setStreamingKind("tool");
          upsertStreamingToolOutput(id, name, suppressed);
        }
        if (notifications.toolCall) {
          liveToolsForThread(threadId)
            .observe(notifications.toolCall.id)
            .forEach(removeStreamingTool);
          upsertStreamingToolCall(
            notifications.toolCall.id,
            notifications.toolCall.name,
          );
        }
      } catch (err) {
        console.error("Failed to parse stream.delta:", err);
      }
    });

    // Handle stream.close - streaming finished
    addThreadEventListener("stream.close", () => {
      try {
        streamingBufferForThread(threadId).clearAssistantText();
        clearThreadStreamingAssistant(threadId);
        addSystemLog("Streaming complete", "info");
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
        const message = messageFromCreateEvent(data);
        if (!message) throw new Error("Invalid canonical msg.create envelope");
        // Install the exact durable event synchronously before stream.close or
        // any subsequent lifecycle event can remove its live representation.
        upsertTranscriptTailMessage(queryClient, threadId, message);
        reconcileDurableToolMessage(message);
        const answeredGetUserId = getUserAnswerToolCallId(message);
        if (answeredGetUserId) markStreamingToolFinished(answeredGetUserId);
        addSystemLog(`Message created: ${message.role}`, "info");
        // The event carries canonical identity/content. Refetch only to fill
        // projection-derived metadata such as content_text, tokens, and TPS.
        refreshMessagesNow();
        queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
      } catch (err) {
        console.error("Failed to parse msg.create:", err);
      }
    });

    // Canonical edits retain the original message position. Get-user answers
    // use this event to acquire display-only consumed-by identity after their
    // normal user msg.create, while provider-facing semantics remain no_api.
    // Continuation is different: one transaction emits one skipped-on-continue
    // edit per removed message, followed by control.interrupt purpose=continue.
    // The first edit must fence every old page request and discard bridge history;
    // patching it in place would let a disjoint fresh tail retain the other skipped
    // messages forever. All edits in the transaction share continue_event_id, so
    // advance the generation only once even when hundreds of messages are skipped.
    addThreadEventListener("msg.edit", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        if (payload.skipped_on_continue === true) {
          const continueEventId = typeof payload.continue_event_id === "string"
            ? payload.continue_event_id
            : "";
          const alreadyRewound = Boolean(
            continueEventId &&
            pendingContinuationRewindRef.current?.threadId === threadId &&
            pendingContinuationRewindRef.current.continueEventId === continueEventId,
          );
          if (!alreadyRewound) {
            pendingContinuationRewindRef.current = continueEventId
              ? { threadId, continueEventId }
              : null;
            rewindMessagesNow();
          }
          queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
          return;
        }
        const eventSeq = Number(data.event_seq);
        if (
          typeof data.msg_id === "string" &&
          data.msg_id &&
          Number.isSafeInteger(eventSeq) &&
          eventSeq >= 0
        ) {
          patchTranscriptMessage(
            queryClient,
            threadId,
            data.msg_id,
            payload,
            eventSeq,
          );
        }
        const answeredGetUserId = getUserAnswerToolCallId({
          id: String(data.msg_id || ""),
          role: "user",
          ...payload,
        });
        if (answeredGetUserId) markStreamingToolFinished(answeredGetUserId);
        refreshMessagesNow();
        queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
      } catch (err) {
        console.error("Failed to parse msg.edit:", err);
      }
    });

    // Destructive canonical events are the only authority allowed to discard
    // loaded pages. Remove the cache before rebuilding so ordinary refreshes
    // remain monotonic while explicit deletion is reflected immediately.
    addThreadEventListener("msg.delete", () => {
      invalidateTranscriptAuthoritatively(queryClient, threadId);
      refreshMessagesNow();
      queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
    });

    // Compaction changes the authoritative marker/page topology even when raw
    // messages remain visible. Fence stale same-frontier page responses and
    // rebuild from the post-event projection rather than patching one marker.
    addThreadEventListener("thread.compaction", () => {
      invalidateTranscriptAuthoritatively(queryClient, threadId);
      refreshMessagesNow();
      queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
      queryClient.invalidateQueries({ queryKey: ["stats", threadId] });
    });

    // Handle tool_call.execution_started
    addThreadEventListener("tool_call.execution_started", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const toolId = String(payload.tool_call_id || payload.id || "").trim();
        const toolName = toolDisplayName(
          payload.name || payload.tool_name,
          toolId,
          "Tool call",
        );
        const timeoutSec = timeoutFromPayload(payload);
        addSystemLog(`Tool executing: ${toolName || "unknown"}`, "info");
        setStreamingKind("tool");
        setIsStreaming(true);
        if (toolId) {
          const toolIdText = toolId;
          const toolNameText = toolName;
          liveToolsForThread(threadId)
            .observe(toolIdText)
            .forEach(removeStreamingTool);
          const args = toolCallArgumentsFromPayload(payload);
          markStreamingToolStarted(
            toolIdText,
            toolNameText,
            eventStartedAtMs(data.ts),
            timeoutSec,
          );
          if (args) {
            streamingBufferForThread(threadId).setToolCallArgs(
              toolIdText,
              toolNameText,
              args,
            );
            upsertStreamingToolCall(toolIdText, toolNameText);
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
        const timeoutSec = positiveTimeout(
          payload.timeout ?? payload.timeout_sec,
        );
        setStreamingProviderRequest({
          startedAtMs: eventStartedAtMs(data.ts),
          ...(timeoutSec !== null ? { timeoutSec } : {}),
          modelKey:
            typeof payload.model_key === "string" ? payload.model_key : null,
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
        const commandId = String(
          payload.command_id || data.event_seq || commandName,
        );
        setActiveUserCommand({
          id: commandId,
          name: commandName,
          command: commandText,
          startedAtMs: eventStartedAtMs(data.ts || payload.started_at),
        });
        addSystemLog(
          `Running command: ${commandName.startsWith("$") ? commandName : `/${commandName}`}`,
          "info",
        );
      } catch (err) {
        console.error("Failed to parse user_command.started:", err);
      }
    });

    addThreadEventListener("user_command.finished", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const elapsed =
          typeof payload.elapsed_sec === "number"
            ? ` in ${payload.elapsed_sec.toFixed(1)}s`
            : "";
        const commandName = String(payload.command_name || "command");
        setActiveUserCommand(null);
        queryClient.invalidateQueries({
          queryKey: ["threadSettings", threadId],
        });
        addSystemLog(
          `Command finished: ${commandName.startsWith("$") ? commandName : `/${commandName}`}${elapsed}`,
          payload.success === false ? "error" : "success",
        );
      } catch (err) {
        console.error("Failed to parse user_command.finished:", err);
      }
    });

    addThreadEventListener("user_command.status", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const message =
          typeof payload.message === "string" ? payload.message : "";
        const timeoutSec = timeoutFromPayload(payload);
        if (payload.command_name === "imageGenerate" && timeoutSec !== null) {
          const streamingState =
            useAppStore.getState().streamingByThread[threadId] ||
            emptyThreadStreamingState();
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
        const toolId = String(payload.tool_call_id || payload.id || "").trim();
        const toolName = toolDisplayName(payload.name, toolId, "Tool call");
        const summary =
          typeof payload.summary === "string" ? payload.summary : "";
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
        const toolId = String(payload.tool_call_id || payload.id || "").trim();
        if (toolId) {
          markStreamingToolFinished(toolId);
        }
        addSystemLog("Tool finished", "info");
        queryClient.invalidateQueries({ queryKey: ["toolCalls", threadId] });
        queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
        refreshMessagesNow();
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

    // model.switch is the persisted cross-client authority. Apply its model
    // immediately in canonical event order; targeted snapshots fill the other
    // model-bearing query surfaces without polling or optimistic authority.
    addThreadEventListener("model.switch", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const modelKey = typeof payload.model_key === "string" ? payload.model_key.trim() : "";
        if (!modelKey) return;
        void applyCanonicalModelSwitch(queryClient, threadId, {
          eventSeq: Number(data.event_seq),
          modelKey,
        }).catch((error) => {
          console.error("Failed to apply model.switch:", error);
        });
        addSystemLog(`Model changed: ${modelKey}`, "info");
      } catch (err) {
        console.error("Failed to parse model.switch:", err);
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
        queryClient.invalidateQueries({
          queryKey: ["threadChildren", threadId],
        });
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
        const parentId =
          typeof payload.parent_id === "string" && payload.parent_id
            ? payload.parent_id
            : threadId;
        const childId =
          typeof payload.child_id === "string" ? payload.child_id : "";
        addSystemLog(
          `Child thread linked${childId ? `: ${childId.slice(-8)}` : ""}`,
          "info",
        );
        queryClient.invalidateQueries({
          queryKey: ["threadChildren", parentId],
        });
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
        if (purpose === "tool" && typeof payload.old_invoke_id === "string") {
          const streaming = useAppStore.getState().streamingByThread[threadId];
          Object.keys(streaming?.streamingToolOutputs || {}).forEach(
            markStreamingToolFinished,
          );
        }
        if (purpose === "continue") {
          addSystemLog("Continue applied - refreshing", "info");
          const alreadyRewound = pendingContinuationRewindRef.current?.threadId === threadId;
          pendingContinuationRewindRef.current = null;
          // A zero-skip continuation has no preceding msg.edit, and a reconnect
          // may resume directly at this control boundary. It still has to fence
          // requests from the superseded invocation before rebuilding the tail.
          if (!alreadyRewound) rewindMessagesNow();
          else refreshMessagesNow();
          queryClient.invalidateQueries({ queryKey: ["toolCalls", threadId] });
          queryClient.invalidateQueries({
            queryKey: ["threadState", threadId],
          });
        }
      } catch (err) {
        console.error("Failed to parse control.interrupt:", err);
      }
    });

    return es;
  }, [
    threadId,
    closeOwnedEventSource,
    evictInactiveThreadState,
    refreshMessagesNow,
    rewindMessagesNow,
    upsertStreamingToolOutput,
    markStreamingToolStarted,
    markStreamingToolFinished,
    removeStreamingTool,
    liveToolsForThread,
    clearRetainedTools,
    hideStreamingToolCall,
    reconcileDurableToolMessage,
    upsertStreamingToolCall,
    setIsStreaming,
    setStreamingModelKey,
    setStreamingKind,
    setStreamingStartedAtMs,
    setStreamingProviderRequest,
    setActiveUserCommand,
    clearThreadStreamingAssistant,
    patchThreadStreaming,
    resetThreadStreaming,
    setThreadConnection,
    addSystemLog,
    queryClient,
  ]);

  const disconnect = useCallback(() => {
    invalidateSetup();
    closeOwnedEventSource();
    if (threadId) {
      setThreadConnection(threadId, "disconnected");
    }
  }, [closeOwnedEventSource, invalidateSetup, setThreadConnection, threadId]);

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
      invalidateSetup();
      es?.close();
      if (eventSourceRef.current === es) {
        eventSourceRef.current = null;
        eventSourceThreadIdRef.current = null;
      }
      if (threadId) {
        // A replacement setup for the same thread may already own the shared
        // connection. Do not let this stale effect downgrade its status.
        const replacementOwnsThread =
          eventSourceThreadIdRef.current === threadId;
        if (!replacementOwnsThread) {
          setThreadConnection(threadId, "disconnected");
        }
        queueMicrotask(() => {
          if (
            replacementOwnsThread ||
            eventSourceThreadIdRef.current === threadId
          )
            return;
          const state = useAppStore.getState();
          const streaming = state.streamingByThread[threadId];
          if (
            !canEvictThreadEphemeralState({
              isCurrent: state.currentThreadId === threadId,
              isStreaming: Boolean(streaming?.isStreaming),
              connectionStatus: state.connectionByThread[threadId]?.status,
              hasRetainedTools: hasRetainedLiveToolsForThread(threadId),
            })
          )
            return;
          evictStreamingBufferForThread(threadId);
          evictThreadEphemeralState(threadId);
        });
      }
    };
  }, [
    connect,
    evictThreadEphemeralState,
    invalidateSetup,
    setThreadConnection,
    threadId,
  ]);

  return { connect, disconnect };
}
