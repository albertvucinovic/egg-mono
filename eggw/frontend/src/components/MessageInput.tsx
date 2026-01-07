"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Send, Loader2, Terminal, StopCircle } from "lucide-react";
import { sendMessage, executeCommand, isCommand, interruptThread, fetchAutocomplete, AutocompleteSuggestion } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import clsx from "clsx";

interface MessageInputProps {
  showBorders?: boolean;
}

export function MessageInput({ showBorders = true }: MessageInputProps) {
  const [input, setInput] = useState("");
  const [shouldFocusAfterCancel, setShouldFocusAfterCancel] = useState(false);
  const [suggestions, setSuggestions] = useState<AutocompleteSuggestion[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const suggestionsRef = useRef<HTMLDivElement>(null);
  const fetchTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const queryClient = useQueryClient();
  const {
    currentThreadId,
    isStreaming,
    setIsStreaming,
    setStreamingContent,
    setStreamingReasoning,
    setStreamingToolCalls,
    addSystemLog,
    addMessage,
    setCurrentThreadId,
    setTheme,
    togglePanel,
    toggleBorders,
    setEnterMode,
    enterMode,
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
      setSuggestions([]);
      setShowSuggestions(false);
      // Focus input after sending
      textareaRef.current?.focus();
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
      setStreamingContent("");
      setStreamingReasoning("");
      setStreamingToolCalls({});
      setIsStreaming(false);
      // Refetch messages to get the saved partial content from backend
      queryClient.invalidateQueries({ queryKey: ["messages", currentThreadId] });
      addSystemLog("Streaming cancelled", "info");
      // Set flag to focus after state update
      setShouldFocusAfterCancel(true);
    },
    onError: () => {
      addSystemLog("Failed to cancel streaming", "error");
    },
  });

  // Commands that should show output in chat (info/status commands)
  const commandsWithChatOutput = [
    '/help', '/threads', '/listChildren', '/cost', '/toolsStatus',
    '/schedulers', '/model', '/parentThread', '/theme'
  ];

  // Command mutation
  const commandMutation = useMutation({
    mutationFn: (command: string) => executeCommand(currentThreadId!, command),
    onMutate: (command: string) => {
      setInput("");
      setSuggestions([]);
      setShowSuggestions(false);
      // Focus input after sending
      textareaRef.current?.focus();
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
      // Check if this command should show output in chat
      const cmdBase = command.split(/\s+/)[0].toLowerCase();
      const showInChat = commandsWithChatOutput.some(c => cmdBase === c.toLowerCase());

      if (response.success) {
        // For info commands, show output as a system message in chat
        if (showInChat && response.message) {
          addMessage({
            id: `cmd-${Date.now()}`,
            role: "system",
            content: response.message,
          });
        } else {
          // For action commands, just log to system panel
          addSystemLog(response.message, "success");
        }

        // Handle specific command responses
        if (response.data?.child_id) {
          // Spawned a child thread - refresh thread list and switch to it
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
          queryClient.invalidateQueries({ queryKey: ["threadChildren"] });
          setCurrentThreadId(response.data.child_id);
        } else if (response.data?.thread_id) {
          // Thread created/switched/duplicated - refresh and switch
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
          queryClient.invalidateQueries({ queryKey: ["threadChildren"] });
          setCurrentThreadId(response.data.thread_id);
        } else if (response.data?.deleted_id) {
          // Thread deleted - refresh lists
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
          queryClient.invalidateQueries({ queryKey: ["threadChildren"] });
        } else if (response.data?.model_key && !showInChat) {
          // Model changed - refresh threads (if not showing in chat)
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
        } else if (response.data?.tool_call_id) {
          // Shell command - refresh messages and tools
          queryClient.invalidateQueries({ queryKey: ["messages", currentThreadId] });
          queryClient.invalidateQueries({ queryKey: ["toolCalls", currentThreadId] });
        } else if (response.data?.name !== undefined) {
          // Thread renamed - refresh thread data
          queryClient.invalidateQueries({ queryKey: ["thread", currentThreadId] });
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
        } else if (response.data?.action === "set_theme" && response.data?.theme) {
          // Theme changed - apply it
          setTheme(response.data.theme);
        } else if (response.data?.action === "toggle" && response.data?.panel) {
          // Toggle panel visibility
          const panel = response.data.panel as "chat" | "children" | "system";
          togglePanel(panel);
        } else if (response.data?.action === "toggle_borders") {
          // Toggle panel borders
          toggleBorders();
        } else if (response.data?.enter_mode) {
          // Set Enter key mode
          setEnterMode(response.data.enter_mode as "send" | "newline");
        } else if (response.data?.action === "paste") {
          // Paste from clipboard
          navigator.clipboard.readText().then((text) => {
            if (text && textareaRef.current) {
              const textarea = textareaRef.current;
              const start = textarea.selectionStart || 0;
              const end = textarea.selectionEnd || 0;
              const before = textarea.value.substring(0, start);
              const after = textarea.value.substring(end);
              setInput(before + text + after);
              setTimeout(() => {
                textarea.setSelectionRange(start + text.length, start + text.length);
                textarea.focus();
              }, 0);
              addSystemLog("Pasted from clipboard", "info");
            }
          }).catch(() => {
            addSystemLog("Failed to read clipboard", "error");
          });
        }
      } else {
        // Show errors in chat for better visibility
        addMessage({
          id: `cmd-err-${Date.now()}`,
          role: "system",
          content: `Error: ${response.message}`,
        });
        addSystemLog(response.message, "error");
      }
    },
    onError: () => {
      addSystemLog("Failed to execute command", "error");
    },
  });

  // Fetch autocomplete suggestions
  const fetchSuggestions = useCallback(async (value: string, cursorPos: number) => {
    if (!value || !currentThreadId) {
      setSuggestions([]);
      setShowSuggestions(false);
      return;
    }

    try {
      const results = await fetchAutocomplete(value, cursorPos, currentThreadId);
      setSuggestions(results);
      setSelectedIndex(0);
      setShowSuggestions(results.length > 0);
    } catch {
      setSuggestions([]);
      setShowSuggestions(false);
    }
  }, [currentThreadId]);

  // Debounced fetch on input change
  useEffect(() => {
    if (fetchTimeoutRef.current) {
      clearTimeout(fetchTimeoutRef.current);
    }

    fetchTimeoutRef.current = setTimeout(() => {
      const cursorPos = textareaRef.current?.selectionStart ?? input.length;
      fetchSuggestions(input, cursorPos);
    }, 100); // 100ms debounce

    return () => {
      if (fetchTimeoutRef.current) {
        clearTimeout(fetchTimeoutRef.current);
      }
    };
  }, [input, fetchSuggestions]);

  // Apply suggestion - use replace value to determine how much to delete
  const applySuggestion = useCallback((suggestion: AutocompleteSuggestion) => {
    const textarea = textareaRef.current;
    if (!textarea) return;

    const cursorPos = textarea.selectionStart;
    const replaceCount = suggestion.replace || 0;

    // Skip any trailing whitespace to find where content ends
    let contentEnd = cursorPos;
    while (contentEnd > 0 && /\s/.test(input[contentEnd - 1])) {
      contentEnd--;
    }

    let tokenStart: number;
    let tokenEnd: number;

    if (replaceCount > 0) {
      // Use replace value to determine how far back to go
      // This handles multi-word replacements like "/model gemini flash"
      tokenStart = Math.max(0, contentEnd - replaceCount);

      // Extend backwards to include any additional characters typed after suggestions fetch
      while (tokenStart > 0 && !/\s/.test(input[tokenStart - 1])) {
        tokenStart--;
      }

      // For commands, include the / if present
      if (tokenStart > 0 && input[tokenStart - 1] === '/') {
        tokenStart--;
      }

      tokenEnd = cursorPos; // Delete up to original cursor position
    } else {
      // No replace value - find single token at cursor
      tokenStart = cursorPos;
      while (tokenStart > 0 && /[\w\-.:/~]/.test(input[tokenStart - 1])) {
        tokenStart--;
      }

      tokenEnd = cursorPos;
      while (tokenEnd < input.length && /[\w\-.:/~]/.test(input[tokenEnd])) {
        tokenEnd++;
      }

      // For commands starting with /, include the /
      if (tokenStart > 0 && input[tokenStart - 1] === '/') {
        tokenStart--;
      }
    }

    const before = input.slice(0, tokenStart);
    const after = input.slice(tokenEnd);
    const newValue = before + suggestion.insert + after;

    setInput(newValue);
    setSuggestions([]);
    setShowSuggestions(false);

    // Set cursor position after the inserted text
    setTimeout(() => {
      const newCursorPos = before.length + suggestion.insert.length;
      textarea.setSelectionRange(newCursorPos, newCursorPos);
      textarea.focus();
    }, 0);
  }, [input]);

  // Auto-resize textarea
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 200)}px`;
    }
  }, [input]);

  // Focus input after cancel completes (when streaming stops)
  useEffect(() => {
    if (shouldFocusAfterCancel && !isStreaming) {
      textareaRef.current?.focus();
      setShouldFocusAfterCancel(false);
    }
  }, [shouldFocusAfterCancel, isStreaming]);

  // Auto-focus input when thread changes or on mount
  useEffect(() => {
    if (currentThreadId && !isStreaming) {
      textareaRef.current?.focus();
    }
  }, [currentThreadId, isStreaming]);

  // Global key capture - focus input when user starts typing
  useEffect(() => {
    const handleGlobalKeyDown = (e: KeyboardEvent) => {
      // Skip if already focused on an input/textarea
      if (document.activeElement?.tagName === "INPUT" ||
          document.activeElement?.tagName === "TEXTAREA") {
        return;
      }
      // Skip modifier keys, function keys, etc.
      if (e.ctrlKey || e.metaKey || e.altKey || e.key.length > 1) {
        return;
      }
      // Skip if no thread selected
      if (!currentThreadId) {
        return;
      }
      // Focus and let the key be captured
      textareaRef.current?.focus();
    };

    window.addEventListener("keydown", handleGlobalKeyDown);
    return () => window.removeEventListener("keydown", handleGlobalKeyDown);
  }, [currentThreadId]);

  // Close suggestions when clicking outside
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (suggestionsRef.current && !suggestionsRef.current.contains(e.target as Node) &&
          textareaRef.current && !textareaRef.current.contains(e.target as Node)) {
        setShowSuggestions(false);
      }
    };

    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  // Scroll selected suggestion into view
  useEffect(() => {
    if (showSuggestions && suggestionsRef.current) {
      const selectedEl = suggestionsRef.current.querySelector(`[data-index="${selectedIndex}"]`);
      if (selectedEl) {
        selectedEl.scrollIntoView({ block: "nearest" });
      }
    }
  }, [selectedIndex, showSuggestions]);

  const handleSubmit = () => {
    const trimmed = input.trim();
    if (!trimmed || !currentThreadId) return;

    // Commands can run during streaming (navigation, status, etc.)
    // Only block regular messages during streaming
    if (isCommand(trimmed)) {
      commandMutation.mutate(trimmed);
    } else if (!isStreaming) {
      messageMutation.mutate(trimmed);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    // Handle autocomplete navigation
    if (showSuggestions && suggestions.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex((prev) => (prev + 1) % suggestions.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex((prev) => (prev - 1 + suggestions.length) % suggestions.length);
        return;
      }
      if (e.key === "Tab") {
        e.preventDefault();
        applySuggestion(suggestions[selectedIndex]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setShowSuggestions(false);
        return;
      }
    }

    // Submit behavior depends on enterMode
    if (e.key === "Enter") {
      if (enterMode === "send") {
        // send mode: Enter sends, Shift+Enter for newline
        if (!e.shiftKey) {
          e.preventDefault();
          handleSubmit();
        }
      } else {
        // newline mode: Ctrl/Cmd+Enter sends, Enter for newline
        if (e.ctrlKey || e.metaKey) {
          e.preventDefault();
          handleSubmit();
        }
      }
    }
  };

  const isPending = messageMutation.isPending || commandMutation.isPending;
  const inputIsCommand = isCommand(input);

  return (
    <div className={`p-4 bg-[var(--panel-bg)] relative ${showBorders ? 'border-t border-[var(--panel-border)]' : ''}`}>
      {/* Autocomplete dropdown */}
      {showSuggestions && suggestions.length > 0 && (
        <div
          ref={suggestionsRef}
          className={`absolute bottom-full left-4 right-4 mb-1 rounded-lg shadow-lg max-h-64 overflow-auto z-50 ${showBorders ? 'border' : ''}`}
          style={{ background: "var(--panel-bg)", borderColor: "var(--panel-border)" }}
        >
          {suggestions.map((suggestion, index) => (
            <div
              key={`${suggestion.display}-${index}`}
              data-index={index}
              className="px-3 py-2 cursor-pointer flex items-center gap-3"
              style={{
                background: index === selectedIndex ? "var(--user-msg-bg)" : undefined,
              }}
              onClick={() => applySuggestion(suggestion)}
              onMouseEnter={() => setSelectedIndex(index)}
            >
              <span className="font-mono text-sm flex-1 whitespace-nowrap overflow-hidden text-ellipsis">{suggestion.display}</span>
              {suggestion.meta && (
                <span className="text-xs flex-shrink-0 max-w-[200px] truncate" style={{ color: "var(--muted)" }}>{suggestion.meta}</span>
              )}
            </div>
          ))}
          <div className="px-3 py-1 text-xs border-t border-[var(--panel-border)]" style={{ color: "var(--muted)" }}>
            <kbd className="px-1 rounded" style={{ background: "var(--code-bg)" }}>Tab</kbd> to select,{" "}
            <kbd className="px-1 rounded" style={{ background: "var(--code-bg)" }}>↑↓</kbd> to navigate,{" "}
            <kbd className="px-1 rounded" style={{ background: "var(--code-bg)" }}>Esc</kbd> to close
          </div>
        </div>
      )}

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
          disabled={!currentThreadId}
          className={`flex-1 rounded px-3 py-2 resize-none focus:outline-none disabled:opacity-50 min-h-[40px] ${showBorders ? 'border' : ''}`}
          style={{ background: "var(--panel-bg)", borderColor: "var(--panel-border)", color: "var(--foreground)" }}
          rows={1}
          data-testid="message-input"
        />
        {/* During streaming: show Run button for commands, Cancel button always */}
        {isStreaming && inputIsCommand && (
          <button
            onClick={handleSubmit}
            disabled={!input.trim() || !currentThreadId || isPending}
            className="px-4 py-2 bg-amber-600 rounded hover:bg-amber-500 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
            title="Run command while streaming"
          >
            {isPending ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Terminal className="w-4 h-4" />
            )}
            Run
          </button>
        )}
        {isStreaming ? (
          <button
            onClick={() => cancelMutation.mutate()}
            disabled={cancelMutation.isPending}
            className="px-4 py-2 bg-red-600 rounded hover:bg-red-500 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
            title="Cancel streaming (Escape)"
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
      <div className="mt-2 text-xs flex items-center gap-4" style={{ color: "var(--muted)" }}>
        <span>
          {currentThreadId ? `Thread: ${currentThreadId.slice(-8)}` : "No thread"}
        </span>
        {inputIsCommand && (
          <span style={{ color: "var(--tool-call-border)" }}>
            {input.startsWith('$') ? "Shell command" : "Slash command"}
          </span>
        )}
        {isStreaming && (
          <span className="flex items-center gap-1" style={{ color: "var(--accent)" }}>
            <Loader2 className="w-3 h-3 animate-spin" />
            Streaming...
          </span>
        )}
        <span title={enterMode === "send" ? "Enter to send, Shift+Enter for newline" : "Ctrl+Enter to send, Enter for newline"}>
          [{enterMode === "send" ? "⏎ send" : "^⏎ send"}]
        </span>
      </div>
    </div>
  );
}
