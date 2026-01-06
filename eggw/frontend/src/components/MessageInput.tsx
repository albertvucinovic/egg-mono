"use client";

import { useState, useRef, useEffect } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Send, Loader2, Terminal, StopCircle } from "lucide-react";
import { sendMessage, executeCommand, isCommand, interruptThread } from "@/lib/api";
import { useAppStore } from "@/lib/store";

export function MessageInput() {
  const [input, setInput] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const queryClient = useQueryClient();
  const {
    currentThreadId,
    isStreaming,
    addSystemLog,
    addMessage,
    setCurrentThreadId,
  } = useAppStore();

  // Regular message mutation
  const messageMutation = useMutation({
    mutationFn: (content: string) => sendMessage(currentThreadId!, content),
    onMutate: (content: string) => {
      // Immediately add message to store for instant display
      addMessage({
        id: `temp-${Date.now()}`,
        role: "user",
        content: content,
      });
      setInput("");
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["messages", currentThreadId] });
      addSystemLog("Message sent", "success");
    },
    onError: () => {
      addSystemLog("Failed to send message", "error");
    },
  });

  // Cancel/interrupt mutation
  const cancelMutation = useMutation({
    mutationFn: () => interruptThread(currentThreadId!),
    onSuccess: () => {
      addSystemLog("Streaming cancelled", "info");
    },
    onError: () => {
      addSystemLog("Failed to cancel streaming", "error");
    },
  });

  // Command mutation
  const commandMutation = useMutation({
    mutationFn: (command: string) => executeCommand(currentThreadId!, command),
    onMutate: (command: string) => {
      setInput("");
      // For shell commands, show them in the chat
      if (command.startsWith('$')) {
        addMessage({
          id: `temp-${Date.now()}`,
          role: "user",
          content: command,
        });
      }
    },
    onSuccess: (response, command) => {
      if (response.success) {
        addSystemLog(response.message, "success");

        // Handle specific command responses
        if (response.data?.child_id) {
          // Spawned a child thread - refresh thread list and switch to it
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
          queryClient.invalidateQueries({ queryKey: ["threadChildren"] });
          setCurrentThreadId(response.data.child_id);
        } else if (response.data?.thread_id && command.startsWith('/newThread')) {
          // Created a new thread - refresh and switch
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
          setCurrentThreadId(response.data.thread_id);
        } else if (response.data?.model_key) {
          // Model changed - refresh threads
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
        } else if (response.data?.tool_call_id) {
          // Shell command - refresh messages and tools
          queryClient.invalidateQueries({ queryKey: ["messages", currentThreadId] });
          queryClient.invalidateQueries({ queryKey: ["toolCalls", currentThreadId] });
        }
      } else {
        addSystemLog(response.message, "error");
      }
    },
    onError: () => {
      addSystemLog("Failed to execute command", "error");
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
    const trimmed = input.trim();
    if (!trimmed || !currentThreadId || isStreaming) return;

    if (isCommand(trimmed)) {
      commandMutation.mutate(trimmed);
    } else {
      messageMutation.mutate(trimmed);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const isPending = messageMutation.isPending || commandMutation.isPending;
  const inputIsCommand = isCommand(input);

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
              ? "Message, /command, or $ shell..."
              : "Select a thread first"
          }
          disabled={!currentThreadId || isStreaming}
          className="flex-1 bg-[#111] border border-[var(--panel-border)] rounded px-3 py-2 resize-none focus:outline-none focus:border-blue-500 disabled:opacity-50 min-h-[40px]"
          rows={1}
        />
        {isStreaming ? (
          <button
            onClick={() => cancelMutation.mutate()}
            disabled={cancelMutation.isPending}
            className="px-4 py-2 bg-red-600 rounded hover:bg-red-500 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
            title="Cancel streaming (Ctrl+C)"
          >
            {cancelMutation.isPending ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <StopCircle className="w-4 h-4" />
            )}
            Cancel
          </button>
        ) : (
          <button
            onClick={handleSubmit}
            disabled={!input.trim() || !currentThreadId || isPending}
            className="px-4 py-2 bg-blue-600 rounded hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
          >
            {isPending ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : inputIsCommand ? (
              <Terminal className="w-4 h-4" />
            ) : (
              <Send className="w-4 h-4" />
            )}
            {inputIsCommand ? "Run" : "Send"}
          </button>
        )}
      </div>

      {/* Status line */}
      <div className="mt-2 text-xs text-gray-500 flex items-center gap-4">
        <span>
          {currentThreadId ? `Thread: ${currentThreadId.slice(-8)}` : "No thread"}
        </span>
        {inputIsCommand && (
          <span className="text-amber-400">
            {input.startsWith('$') ? "Shell command" : "Slash command"}
          </span>
        )}
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
