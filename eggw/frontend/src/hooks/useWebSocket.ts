"use client";

import { useEffect, useRef, useCallback } from "react";
import { createWebSocket } from "@/lib/api";
import { useAppStore } from "@/lib/store";

export function useWebSocket(threadId: string | null) {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const { addSystemLog } = useAppStore();

  const connect = useCallback(() => {
    if (!threadId) return;

    // Close existing connection
    if (wsRef.current) {
      wsRef.current.close();
    }

    const ws = createWebSocket(threadId);
    wsRef.current = ws;

    ws.onopen = () => {
      addSystemLog("WebSocket connected", "info");
    };

    ws.onclose = () => {
      addSystemLog("WebSocket disconnected", "info");
      // Attempt reconnect after 5 seconds
      reconnectTimeoutRef.current = setTimeout(() => {
        if (threadId) {
          connect();
        }
      }, 5000);
    };

    ws.onerror = () => {
      addSystemLog("WebSocket error", "error");
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        handleMessage(data);
      } catch (err) {
        console.error("Failed to parse WebSocket message:", err);
      }
    };

    return ws;
  }, [threadId, addSystemLog]);

  const disconnect = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
  }, []);

  const send = useCallback((message: object) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(message));
    }
  }, []);

  const sendMessage = useCallback(
    (content: string) => {
      send({ type: "send_message", content });
    },
    [send]
  );

  const approveTool = useCallback(
    (toolCallId: string, approved: boolean, outputDecision?: string) => {
      send({
        type: "approve_tool",
        tool_call_id: toolCallId,
        approved,
        output_decision: outputDecision,
      });
    },
    [send]
  );

  const handleMessage = (data: { type: string; [key: string]: any }) => {
    switch (data.type) {
      case "pong":
        // Heartbeat response
        break;
      case "message_sent":
        addSystemLog("Message sent via WebSocket", "info");
        break;
      case "error":
        addSystemLog(`WebSocket error: ${data.message}`, "error");
        break;
      default:
        console.log("Unknown WebSocket message type:", data.type);
    }
  };

  // Start heartbeat
  useEffect(() => {
    const interval = setInterval(() => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        send({ type: "ping" });
      }
    }, 30000);

    return () => clearInterval(interval);
  }, [send]);

  useEffect(() => {
    const ws = connect();
    return () => {
      disconnect();
    };
  }, [connect, disconnect]);

  return { send, sendMessage, approveTool, disconnect };
}
