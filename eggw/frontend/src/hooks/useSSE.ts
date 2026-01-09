"use client";

import { useEffect, useRef, useCallback } from "react";
import { createEventSource } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { streamingBuffer } from "@/lib/streamingBuffer";
import { useQueryClient } from "@tanstack/react-query";

export function useSSE(threadId: string | null) {
  const eventSourceRef = useRef<EventSource | null>(null);
  const queryClient = useQueryClient();
  const {
    setStreamingToolCalls,
    appendToolCallArguments,
    setIsStreaming,
    setStreamingModelKey,
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
        try {
          const data = JSON.parse(e.data);
          const payload = data.payload || {};
          modelKey = payload.model_key || null;
        } catch {
          // Data might not be JSON, ignore
        }

        streamingBuffer.clear();
        setStreamingToolCalls({});
        setStreamingModelKey(modelKey);
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

        if (payload.text) {
          streamingBuffer.appendContent(payload.text);
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
        setStreamingModelKey(null);
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
        addSystemLog(`Tool executing: ${payload.name || "unknown"}`, "info");
        queryClient.invalidateQueries({ queryKey: ["toolCalls", threadId] });
        queryClient.invalidateQueries({ queryKey: ["threadState", threadId] });
      } catch (err) {
        console.error("Failed to parse tool_call.execution_started:", err);
      }
    });

    // Handle tool_call.finished
    es.addEventListener("tool_call.finished", () => {
      try {
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

    return es;
  }, [
    threadId,
    setStreamingToolCalls,
    appendToolCallArguments,
    setIsStreaming,
    setStreamingModelKey,
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

  // Disconnect SSE when tab is hidden to free up connection pool for other tabs
  // Browsers limit HTTP/1.1 connections (~6 per origin), and SSE holds one open
  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.hidden) {
        // Tab hidden - disconnect to free connection
        disconnect();
      } else {
        // Tab visible - reconnect
        connect();
      }
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [connect, disconnect]);

  return { connect, disconnect };
}
