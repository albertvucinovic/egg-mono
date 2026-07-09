"use client";

import { useEffect, useRef, useCallback } from "react";
import { createEventSource, fetchMessages, type AuthenticatedEventSource } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { streamingBuffer } from "@/lib/streamingBuffer";
import { useQueryClient } from "@tanstack/react-query";

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
  const queryClient = useQueryClient();
  const setStreamingToolCalls = useAppStore((state) => state.setStreamingToolCalls);
  const setStreamingToolOutputs = useAppStore((state) => state.setStreamingToolOutputs);
  const upsertStreamingToolOutput = useAppStore((state) => state.upsertStreamingToolOutput);
  const markStreamingToolStarted = useAppStore((state) => state.markStreamingToolStarted);
  const clearStreamingToolTimeout = useAppStore((state) => state.clearStreamingToolTimeout);
  const upsertStreamingToolCall = useAppStore((state) => state.upsertStreamingToolCall);
  const appendToolCallArguments = useAppStore((state) => state.appendToolCallArguments);
  const setIsStreaming = useAppStore((state) => state.setIsStreaming);
  const setStreamingModelKey = useAppStore((state) => state.setStreamingModelKey);
  const setStreamingKind = useAppStore((state) => state.setStreamingKind);
  const setStreamingStartedAtMs = useAppStore((state) => state.setStreamingStartedAtMs);
  const setStreamingProviderRequest = useAppStore((state) => state.setStreamingProviderRequest);
  const setActiveUserCommand = useAppStore((state) => state.setActiveUserCommand);
  const addSystemLog = useAppStore((state) => state.addSystemLog);

  const clearScheduledMessageRefresh = useCallback(() => {
    if (messageRefreshTimeoutRef.current !== null) {
      window.clearTimeout(messageRefreshTimeoutRef.current);
      messageRefreshTimeoutRef.current = null;
    }
  }, []);

  const refreshMessagesNow = useCallback(() => {
    if (!threadId) return;
    queryClient.invalidateQueries({ queryKey: ["messages", threadId] });
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
    let snapshot: { snapshot_cursor?: number };
    try {
      snapshot = await queryClient.ensureQueryData({
        queryKey: ["messages", threadId],
        queryFn: () => fetchMessages(threadId, { limit: 300 }),
      });
    } catch (error) {
      addSystemLog("Unable to establish message synchronization cursor", "error");
      return null;
    }
    // Initial synchronization owns the baseline streaming reset. Transport
    // reconnects stay inside AuthenticatedEventSource and never clear durable
    // stream state merely because the network dropped.
    streamingBuffer.clear();
    setStreamingToolCalls({});
    setStreamingToolOutputs({});
    setStreamingKind(null);
    setStreamingStartedAtMs(null);
    setStreamingProviderRequest(null);
    setActiveUserCommand(null);
    setIsStreaming(false);

    const snapshotCursor = Number.isSafeInteger(snapshot.snapshot_cursor)
      ? Number(snapshot.snapshot_cursor)
      : -1;
    const es = createEventSource(threadId, snapshotCursor);
    eventSourceRef.current = es;

    es.onopen = () => {
      addSystemLog("SSE connected", "info");
    };

    es.onerror = () => {
      addSystemLog("SSE connection error; reconnecting from cursor", "error");
    };

    // Handle stream.open - streaming started
    es.addEventListener("stream.open", (e) => {
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

        streamingBuffer.clear();
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
    es.addEventListener("stream.delta", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};

        // Direct buffer append - O(1), bypasses React entirely
        if (payload.reason) {
          streamingBuffer.appendReasoning(payload.reason);
        }

        if (typeof payload.reasoning_summary === "string" && payload.reasoning_summary) {
          streamingBuffer.appendReasoningSummary(payload.reasoning_summary);
        }

        if (payload.text) {
          streamingBuffer.appendContent(payload.text);
        }

        if (payload.tool) {
          const tool = payload.tool;
          const toolId = tool.id || tool.name || "tool";
          const toolName = tool.name || "tool";
          if (toolId) {
            const streamingState = useAppStore.getState();
            if (!streamingState.isStreaming) {
              setIsStreaming(true);
            }
            if (streamingState.streamingKind !== "tool") {
              setStreamingKind("tool");
            }
            if (tool.text) {
              streamingBuffer.appendToolOutput(toolId, tool.text);
            }
            // Only update React state when a tool output block first appears,
            // or when the preview limiter emits a suppressed indicator. Text
            // chunks go directly to streamingBuffer/DOM to avoid re-rendering
            // the whole chat for every output line.
            const existingOutput = useAppStore.getState().streamingToolOutputs[toolId];
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
    es.addEventListener("stream.close", () => {
      try {
        streamingBuffer.clear();
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
    es.addEventListener("msg.create", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const role = payload.role || "unknown";
        addSystemLog(`Message created: ${role}`, "info");
        if (role === "user") {
          // User messages can be produced by another client (terminal Egg) just
          // before stream.open.  stream.open intentionally clears delayed
          // transcript refreshes, so fetch user turns immediately instead of
          // relying only on the debounced refresh.  This keeps EggW's transcript
          // synchronized while provider streaming is already visible.
          refreshMessagesNow();
        } else {
          scheduleMessageRefresh(750);
        }
        queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
      } catch (err) {
        console.error("Failed to parse msg.create:", err);
      }
    });

    // Handle tool_call.execution_started
    es.addEventListener("tool_call.execution_started", (e) => {
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

    es.addEventListener("provider_request.started", (e) => {
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

    es.addEventListener("user_command.started", (e) => {
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

    es.addEventListener("user_command.finished", (e) => {
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

    es.addEventListener("user_command.status", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const message = typeof payload.message === "string" ? payload.message : "";
        const timeoutSec = timeoutFromPayload(payload);
        if (payload.command_name === "imageGenerate" && timeoutSec !== null) {
          const streamingState = useAppStore.getState();
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
    es.addEventListener("tool_call.summary", (e) => {
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
    es.addEventListener("tool_call.finished", (e) => {
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
    es.addEventListener("tool_call.approval", () => {
      try {
        addSystemLog("Tool approval processed", "info");
        queryClient.invalidateQueries({ queryKey: ["toolCalls", threadId] });
        queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
      } catch (err) {
        console.error("Failed to parse tool_call.approval:", err);
      }
    });

    // Handle tool_call.output_approval
    es.addEventListener("tool_call.output_approval", () => {
      try {
        addSystemLog("Tool output approval needed", "info");
        queryClient.invalidateQueries({ queryKey: ["toolCalls", threadId] });
        queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
      } catch (err) {
        console.error("Failed to parse tool_call.output_approval:", err);
      }
    });

    // Handle sandbox.config events
    es.addEventListener("sandbox.config", () => {
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
    es.addEventListener("runtime.config", () => {
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
    es.addEventListener("thread.child_created", (e) => {
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
    es.addEventListener("control.interrupt", (e) => {
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
    addSystemLog,
    queryClient,
  ]);

  const disconnect = useCallback(() => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    clearScheduledMessageRefresh();
  }, [clearScheduledMessageRefresh]);

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
    };
  }, [clearScheduledMessageRefresh, connect]);

  return { connect, disconnect };
}
