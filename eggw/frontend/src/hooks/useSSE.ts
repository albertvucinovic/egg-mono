"use client";

import { useEffect, useRef, useCallback } from "react";
import { createEventSource } from "@/lib/api";
import { useAppStore } from "@/lib/store";

export function useSSE(threadId: string | null) {
  const eventSourceRef = useRef<EventSource | null>(null);
  const {
    setStreamingContent,
    appendStreamingContent,
    setIsStreaming,
    addSystemLog,
    addMessage,
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
      addSystemLog("SSE connection opened", "info");
    };

    es.onerror = () => {
      addSystemLog("SSE connection error", "error");
      setIsStreaming(false);
    };

    // Handle different event types
    es.addEventListener("message.content", (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.delta) {
          appendStreamingContent(data.delta);
          setIsStreaming(true);
        }
      } catch (err) {
        console.error("Failed to parse SSE data:", err);
      }
    });

    es.addEventListener("message.complete", (e) => {
      try {
        const data = JSON.parse(e.data);
        setStreamingContent("");
        setIsStreaming(false);
        if (data.message) {
          addMessage(data.message);
        }
      } catch (err) {
        console.error("Failed to parse SSE data:", err);
      }
    });

    es.addEventListener("tool_call", (e) => {
      try {
        const data = JSON.parse(e.data);
        addSystemLog(`Tool call: ${data.name || "unknown"}`, "info");
      } catch (err) {
        console.error("Failed to parse SSE data:", err);
      }
    });

    es.addEventListener("tool_result", (e) => {
      try {
        const data = JSON.parse(e.data);
        addSystemLog(`Tool result: ${data.tool_call_id?.slice(-8) || "unknown"}`, "info");
      } catch (err) {
        console.error("Failed to parse SSE data:", err);
      }
    });

    es.addEventListener("approval_needed", (e) => {
      try {
        const data = JSON.parse(e.data);
        addSystemLog(`Approval needed: ${data.tool_name || "unknown"}`, "info");
      } catch (err) {
        console.error("Failed to parse SSE data:", err);
      }
    });

    es.addEventListener("error", (e) => {
      try {
        const data = JSON.parse((e as MessageEvent).data);
        addSystemLog(`Error: ${data.message || "unknown"}`, "error");
      } catch (err) {
        // Ignore parse errors for error events
      }
    });

    return es;
  }, [
    threadId,
    setStreamingContent,
    appendStreamingContent,
    setIsStreaming,
    addSystemLog,
    addMessage,
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
