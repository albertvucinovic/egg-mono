"use client";

import { useEffect, useRef, useCallback } from "react";
import { createEventSource } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { useQueryClient } from "@tanstack/react-query";

export function useSSE(threadId: string | null) {
  const eventSourceRef = useRef<EventSource | null>(null);
  const queryClient = useQueryClient();
  const {
    setStreamingContent,
    appendStreamingContent,
    setStreamingReasoning,
    appendStreamingReasoning,
    setIsStreaming,
    addSystemLog,
  } = useAppStore();

  const connect = useCallback(() => {
    if (!threadId) return;

    // Close existing connection
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }

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
        setStreamingContent("");
        setStreamingReasoning("");
        setIsStreaming(true);
        addSystemLog("Streaming started", "info");
      } catch (err) {
        console.error("Failed to handle stream.open:", err);
      }
    });

    // Handle stream.delta - streaming content chunks
    es.addEventListener("stream.delta", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};

        // Handle reasoning separately from content
        if (payload.reasoning) {
          appendStreamingReasoning(payload.reasoning);
        }

        // Content can be in different fields depending on what's streaming
        // Backend sends "text" for content deltas
        const contentDelta = payload.text || payload.content || payload.delta || "";
        if (contentDelta) {
          appendStreamingContent(contentDelta);
        }
      } catch (err) {
        console.error("Failed to parse stream.delta:", err);
      }
    });

    // Handle stream.close - streaming finished
    es.addEventListener("stream.close", (e) => {
      try {
        setStreamingContent("");
        setStreamingReasoning("");
        setIsStreaming(false);
        addSystemLog("Streaming complete", "info");
        // Refresh messages to get the final content
        queryClient.invalidateQueries({ queryKey: ["messages", threadId] });
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
        // Refresh messages
        queryClient.invalidateQueries({ queryKey: ["messages", threadId] });
      } catch (err) {
        console.error("Failed to parse msg.create:", err);
      }
    });

    // Handle tool_call events
    es.addEventListener("tool_call.create", (e) => {
      try {
        const data = JSON.parse(e.data);
        const payload = data.payload || {};
        addSystemLog(`Tool call: ${payload.name || "unknown"}`, "info");
        queryClient.invalidateQueries({ queryKey: ["toolCalls", threadId] });
      } catch (err) {
        console.error("Failed to parse tool_call.create:", err);
      }
    });

    // Handle tool_call approval events
    es.addEventListener("tool_call.approval", (e) => {
      try {
        const data = JSON.parse(e.data);
        addSystemLog("Tool approval needed", "info");
        queryClient.invalidateQueries({ queryKey: ["toolCalls", threadId] });
      } catch (err) {
        console.error("Failed to parse tool_call.approval:", err);
      }
    });

    return es;
  }, [
    threadId,
    setStreamingContent,
    appendStreamingContent,
    setStreamingReasoning,
    appendStreamingReasoning,
    setIsStreaming,
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
