"use client";

import { useEffect, useRef, useCallback } from "react";
import { createEventSource } from "@/lib/api";
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

function eventStartedAtMs(value: unknown): number {
  if (typeof value !== "string" || !value.trim()) return Date.now();
  const raw = value.trim();
  const normalized = raw.includes("T") ? raw : `${raw.replace(" ", "T")}Z`;
  const parsed = Date.parse(normalized);
  return Number.isFinite(parsed) ? parsed : Date.now();
}

export function useSSE(threadId: string | null) {
  const eventSourceRef = useRef<EventSource | null>(null);
  const queryClient = useQueryClient();
  const {
    setStreamingToolCalls,
    setStreamingToolOutputs,
    upsertStreamingToolOutput,
    markStreamingToolStarted,
    clearStreamingToolTimeout,
    appendToolCallArguments,
    setIsStreaming,
    setStreamingModelKey,
    setStreamingKind,
    addSystemLog,
  } = useAppStore();

  const connect = useCallback(() => {
    if (!threadId) return;

    // Close existing connection
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }

    // Clear streaming state
    streamingBuffer.clear();
    setStreamingToolCalls({});
    setStreamingToolOutputs({});
    setStreamingKind(null);
    setIsStreaming(false);

    const es = createEventSource(threadId);
    eventSourceRef.current = es;

    es.onopen = () => {
      addSystemLog("SSE connected", "info");
    };

    es.onerror = () => {
      addSystemLog("SSE connection error", "error");
      setIsStreaming(false);
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
        setStreamingToolCalls({});
        setStreamingToolOutputs({});
        setStreamingModelKey(modelKey);
        setStreamingKind(streamKind);
        setIsStreaming(true);
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
        setIsStreaming(false);
        addSystemLog("Streaming complete", "info");
        queryClient.invalidateQueries({ queryKey: ["messages", threadId] });
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
        queryClient.invalidateQueries({ queryKey: ["messages", threadId] });
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
        if (toolId && timeoutSec !== null) {
          markStreamingToolStarted(String(toolId), String(toolName || "tool"), eventStartedAtMs(data.ts), timeoutSec);
        }
        queryClient.invalidateQueries({ queryKey: ["toolCalls", threadId] });
        queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
      } catch (err) {
        console.error("Failed to parse tool_call.execution_started:", err);
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
        queryClient.invalidateQueries({ queryKey: ["messages", threadId] });
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

    // Handle control.interrupt events (e.g., from delayed /continue)
    es.addEventListener("control.interrupt", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        const purpose = payload.purpose || "";
        if (purpose === "continue") {
          addSystemLog("Continue applied - refreshing", "info");
          queryClient.invalidateQueries({ queryKey: ["messages", threadId] });
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
    setStreamingToolCalls,
    setStreamingToolOutputs,
    upsertStreamingToolOutput,
    markStreamingToolStarted,
    clearStreamingToolTimeout,
    appendToolCallArguments,
    setIsStreaming,
    setStreamingModelKey,
    setStreamingKind,
    addSystemLog,
    queryClient,
  ]);

  const disconnect = useCallback(() => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
  }, []);

  useEffect(() => {
    const es = connect();
    return () => {
      if (es) {
        es.close();
      }
    };
  }, [connect]);

  return { connect, disconnect };
}
