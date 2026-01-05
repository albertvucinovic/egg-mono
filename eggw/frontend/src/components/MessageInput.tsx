"use client";

import { useState, useRef, useEffect } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Send, Loader2 } from "lucide-react";
import { sendMessage } from "@/lib/api";
import { useAppStore } from "@/lib/store";

export function MessageInput() {
  const [input, setInput] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const queryClient = useQueryClient();
  const { currentThreadId, isStreaming, addSystemLog, addMessage } = useAppStore();

  const mutation = useMutation({
    mutationFn: (content: string) => sendMessage(currentThreadId!, content),
    onMutate: (content: string) => {
      // Immediately add message to store for instant display
      addMessage({
        id: `temp-${Date.now()}`,
        role: "user",
        content: content,
      });

      // Clear input immediately
      setInput("");
    },
    onSuccess: () => {
      // Refetch to sync with real data
      queryClient.invalidateQueries({ queryKey: ["messages", currentThreadId] });
      addSystemLog("Message sent", "success");
    },
    onError: () => {
      addSystemLog("Failed to send message", "error");
    },
  });

  // Auto-resize textarea
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 200)}px`;
    }
  }, [input]);

  const handleSubmit = () => {
    if (!input.trim() || !currentThreadId || isStreaming) return;
    mutation.mutate(input.trim());
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  return (
    <div className="border-t border-[var(--panel-border)] p-4 bg-[var(--panel-bg)]">
      <div className="flex gap-2 items-end">
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={
            currentThreadId
              ? "Type a message... (Enter to send, Shift+Enter for newline)"
              : "Select a thread first"
          }
          disabled={!currentThreadId || isStreaming}
          className="flex-1 bg-[#111] border border-[var(--panel-border)] rounded px-3 py-2 resize-none focus:outline-none focus:border-blue-500 disabled:opacity-50 min-h-[40px]"
          rows={1}
        />
        <button
          onClick={handleSubmit}
          disabled={!input.trim() || !currentThreadId || isStreaming || mutation.isPending}
          className="px-4 py-2 bg-blue-600 rounded hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
        >
          {mutation.isPending || isStreaming ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : (
            <Send className="w-4 h-4" />
          )}
          Send
        </button>
      </div>

      {/* Status line */}
      <div className="mt-2 text-xs text-gray-500 flex items-center gap-4">
        <span>
          {currentThreadId ? `Thread: ${currentThreadId.slice(-8)}` : "No thread"}
        </span>
        {isStreaming && (
          <span className="text-blue-400 flex items-center gap-1">
            <Loader2 className="w-3 h-3 animate-spin" />
            Streaming...
          </span>
        )}
      </div>
    </div>
  );
}
