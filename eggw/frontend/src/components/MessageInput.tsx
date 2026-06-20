"use client";

import { useState, useRef, useEffect, useCallback, type Dispatch, type SetStateAction } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Paperclip, Send, Loader2, Terminal, StopCircle, X, ImageIcon } from "lucide-react";
import { sendMessage, executeCommand, isCommand, interruptThread, fetchAutocomplete, fetchThreadState, uploadAttachment, generateThreadImage } from "@/lib/api";
import type { AutocompleteSuggestion, ImageGenerationRequest } from "@/lib/api";
import { attachmentFilename, attachmentPlaceholder, buildMessageContentWithAttachments, formatBytes, type AttachmentContentPart, type EggMessageContent } from "@/lib/contentParts";
import { useAppStore } from "@/lib/store";
import clsx from "clsx";

interface MessageInputProps {
  showBorders?: boolean;
  stagedAttachments: AttachmentContentPart[];
  setStagedAttachments: Dispatch<SetStateAction<AttachmentContentPart[]>>;
}

export function MessageInput({ showBorders = true, stagedAttachments, setStagedAttachments }: MessageInputProps) {
  const [input, setInput] = useState("");
  const [shouldFocusAfterCancel, setShouldFocusAfterCancel] = useState(false);
  const [suggestions, setSuggestions] = useState<AutocompleteSuggestion[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [showImageForm, setShowImageForm] = useState(false);
  const [imagePrompt, setImagePrompt] = useState("");
  const [imageModel, setImageModel] = useState("");
  const [imageCount, setImageCount] = useState("");
  const [imageSize, setImageSize] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const suggestionsRef = useRef<HTMLDivElement>(null);
  const fetchTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const router = useRouter();
  const queryClient = useQueryClient();
  const {
    currentThreadId,
    isStreaming,
    setIsStreaming,
    setStreamingContent,
    setStreamingReasoning,
    setStreamingToolCalls,
    setStreamingToolOutputs,
    setStreamingKind,
    addSystemLog,
    addMessage,
    setTheme,
    togglePanel,
    toggleBorders,
    setEnterMode,
    setDisplayVerbosity,
    enterMode,
  } = useAppStore();

  const { data: threadState } = useQuery({
    queryKey: ["threadState", currentThreadId],
    queryFn: () => fetchThreadState(currentThreadId!),
    enabled: !!currentThreadId,
  });
  const activeGetUserWait = Boolean(threadState?.active_get_user_wait);
  const getUserWaitingNote = threadState?.get_user_waiting_note;

  // Regular message mutation
  const messageMutation = useMutation({
    mutationFn: (content: EggMessageContent) => sendMessage(currentThreadId!, content),
    onMutate: (content: EggMessageContent) => {
      // Immediately add message to store for instant display
      addMessage({
        id: `temp-${Date.now()}`,
        role: "user",
        content: content,
        content_text: typeof content === "string" ? content : undefined,
      });
      setInput("");
      setSuggestions([]);
      setShowSuggestions(false);
      // Focus input after sending
      textareaRef.current?.focus();
    },
    onSuccess: () => {
      setStagedAttachments([]);
      queryClient.invalidateQueries({ queryKey: ["messages", currentThreadId] });
      queryClient.invalidateQueries({ queryKey: ["threadState", currentThreadId] });
      addSystemLog("Message sent", "success");
    },
    onError: () => {
      addSystemLog("Failed to send message", "error");
    },
  });

  const uploadMutation = useMutation({
    mutationFn: async (files: File[]) => {
      if (!currentThreadId) throw new Error("No thread selected");
      return Promise.all(files.map((file) => uploadAttachment(currentThreadId, file)));
    },
    onSuccess: (uploads) => {
      setStagedAttachments((prev) => [...prev, ...uploads.map((upload) => upload.content_part)]);
      addSystemLog(`Attached ${uploads.length} file${uploads.length === 1 ? "" : "s"}`, "success");
      textareaRef.current?.focus();
    },
    onError: (error) => {
      addSystemLog(error instanceof Error ? error.message : "Failed to upload attachment", "error");
    },
  });

  const imageGenerationMutation = useMutation({
    mutationFn: async ({ threadId, request }: { threadId: string; request: ImageGenerationRequest }) => {
      const response = await generateThreadImage(threadId, request);
      return {
        artifactCount: response.content_parts.filter((part) => part.type === "artifact").length,
      };
    },
    onSuccess: (result, variables) => {
      if (variables.threadId === currentThreadId) {
        setImagePrompt("");
        setImageModel("");
        setImageCount("");
        setImageSize("");
        setShowImageForm(false);
        queryClient.invalidateQueries({ queryKey: ["threadState", variables.threadId] });
      }
      queryClient.invalidateQueries({ queryKey: ["messages", variables.threadId] });
      const count = result.artifactCount;
      addSystemLog(
        `Generated ${count || "image"} artifact${count === 1 ? "" : "s"}; appended result to transcript`,
        "success",
      );
    },
    onError: (error) => {
      addSystemLog(error instanceof Error ? error.message : "Failed to generate image", "error");
    },
  });

  // Cancel/interrupt mutation
  const cancelMutation = useMutation({
    mutationFn: () => interruptThread(currentThreadId!),
    onSuccess: () => {
      setStreamingContent("");
      setStreamingReasoning("");
      setStreamingToolCalls({});
      setStreamingToolOutputs({});
      setStreamingKind(null);
      setIsStreaming(false);
      // Refetch messages to get the saved partial content from backend
      queryClient.invalidateQueries({ queryKey: ["messages", currentThreadId] });
      queryClient.invalidateQueries({ queryKey: ["threadState", currentThreadId] });
      queryClient.invalidateQueries({ queryKey: ["threadSettings", currentThreadId] });
      queryClient.invalidateQueries({ queryKey: ["toolCalls", currentThreadId] });
      addSystemLog("Streaming cancelled", "info");
      // Set flag to focus after state update
      setShouldFocusAfterCancel(true);
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
    onSuccess: (response) => {
      if (response.success) {
        // Show every command response in Chat Messages. The System Log stays
        // as a compact event log, but command output should be visible in the
        // transcript area consistently (like /help).
        if (response.message) {
          addMessage({
            id: `cmd-${Date.now()}`,
            role: "system",
            content: response.message,
          });
        }
        addSystemLog(response.message || "Command completed", "success");

        if (response.data?.action === "reload") {
          setTimeout(() => {
            window.location.href = response.data?.thread_id ? `/${response.data.thread_id}` : window.location.href;
          }, 7000);
          return;
        }

        // Handle specific command responses
        if (response.data?.child_id) {
          // Spawned a child thread - refresh thread list but stay on parent
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
          queryClient.invalidateQueries({ queryKey: ["threadChildren"] });
          // Don't navigate to child - stay on parent
        } else if (response.data?.thread_id) {
          // Thread created/switched/duplicated - refresh and navigate
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
          queryClient.invalidateQueries({ queryKey: ["threadChildren"] });
          router.push(`/${response.data.thread_id}`);
        } else if (response.data?.deleted_id) {
          // Thread deleted - refresh lists
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
          queryClient.invalidateQueries({ queryKey: ["threadChildren"] });
        }

        // Model changed - always refresh settings so dropdown updates
        if (response.data?.model_key) {
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
          queryClient.invalidateQueries({ queryKey: ["threadSettings", currentThreadId] });
        }

        if (response.data?.reload) {
          // Command requests a full refresh (e.g., /continue)
          queryClient.invalidateQueries({ queryKey: ["messages", currentThreadId] });
          queryClient.invalidateQueries({ queryKey: ["toolCalls", currentThreadId] });
        }

        if (response.data?.tool_call_id) {
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
        } else if (response.data?.action === "set_display_verbosity" && response.data?.display_verbosity) {
          // Set transcript display verbosity
          setDisplayVerbosity(response.data.display_verbosity as "max" | "medium" | "min");
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
      // But stop at '=' to preserve named argument prefixes like "msg_id="
      while (tokenStart > 0 && !/[\s=]/.test(input[tokenStart - 1])) {
        tokenStart--;
      }

      // For commands, include the / if present (but not after =)
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
    if (currentThreadId && (!isStreaming || activeGetUserWait)) {
      textareaRef.current?.focus();
    }
  }, [currentThreadId, isStreaming, activeGetUserWait]);

  useEffect(() => {
    setStagedAttachments([]);
    setShowImageForm(false);
    setImagePrompt("");
    setImageModel("");
    setImageCount("");
    setImageSize("");
  }, [currentThreadId]);

  // Global key capture - focus input when user starts typing
  useEffect(() => {
    const handleGlobalKeyDown = (e: KeyboardEvent) => {
      // Skip if already focused on an input/textarea
      if (document.activeElement?.tagName === "INPUT" ||
          document.activeElement?.tagName === "TEXTAREA" ||
          document.activeElement?.tagName === "SELECT") {
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
    const hasAttachments = stagedAttachments.length > 0;
    if ((!trimmed && !hasAttachments) || !currentThreadId) return;

    // Commands can run during streaming (navigation, status, etc.). Regular
    // messages are blocked during streaming except while the get-user tool is
    // explicitly waiting for the next normal user message.
    if (isCommand(trimmed)) {
      if (hasAttachments) {
        addSystemLog("Attachments cannot be sent with slash or shell commands. Remove staged attachments or send a normal message.", "error");
        return;
      }
      commandMutation.mutate(trimmed);
    } else if (!isStreaming || activeGetUserWait) {
      messageMutation.mutate(buildMessageContentWithAttachments(trimmed, stagedAttachments));
    }
  };

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (!files.length || !currentThreadId) return;
    uploadMutation.mutate(files);
  };

  const handleGenerateImage = () => {
    if (!currentThreadId) return;
    if (isStreaming) {
      addSystemLog("Wait for streaming to finish before generating an image", "error");
      return;
    }
    if (!imagePrompt.trim()) {
      addSystemLog("Image prompt is required", "error");
      return;
    }
    const request: ImageGenerationRequest = {
      prompt: imagePrompt.trim(),
      ...(imageModel.trim() ? { model: imageModel.trim() } : {}),
      ...(imageCount ? { n: Number(imageCount) } : {}),
      ...(imageSize.trim() ? { size: imageSize.trim() } : {}),
    };
    imageGenerationMutation.mutate({ threadId: currentThreadId, request });
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

  const isPending = messageMutation.isPending || commandMutation.isPending || uploadMutation.isPending || imageGenerationMutation.isPending;
  const inputIsCommand = isCommand(input);
  const showGetUserAnswerButton = isStreaming && activeGetUserWait && !inputIsCommand;
  const canSend = Boolean(currentThreadId) && (Boolean(input.trim()) || stagedAttachments.length > 0);
  const canGenerateImage = Boolean(currentThreadId) && Boolean(imagePrompt.trim()) && !isStreaming && !imageGenerationMutation.isPending;

  return (
    <div
      className={`p-4 bg-[var(--panel-bg)] relative ${showBorders || activeGetUserWait ? 'border-t' : ''}`}
      style={{ borderColor: activeGetUserWait ? "#d946ef" : "var(--panel-border)" }}
    >
      {activeGetUserWait && (
        <div
          data-testid="get-user-input-mode"
          className="mb-2 rounded px-3 py-2 text-xs border"
          style={{ color: "#f0abfc", borderColor: "#d946ef", background: "rgba(217, 70, 239, 0.12)" }}
        >
          <div className="font-medium">Message Input (get answer tool)</div>
          <div>
            The next normal message answers the waiting get-user tool and preserves the assistant turn.
          </div>
          {getUserWaitingNote?.content && (
            <div className="mt-1 truncate" title={getUserWaitingNote.content}>
              Waiting prompt: {getUserWaitingNote.content}
            </div>
          )}
        </div>
      )}
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
                background: index === selectedIndex ? "var(--selection-bg)" : undefined,
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

      {stagedAttachments.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-2" data-testid="staged-attachments">
          {stagedAttachments.map((attachment, index) => (
            <div
              key={`${attachment.input_id}-${index}`}
              className={`flex max-w-full items-center gap-2 rounded px-3 py-2 text-xs ${showBorders ? "border" : ""}`}
              style={{ background: "var(--code-bg)", borderColor: "var(--panel-border)", color: "var(--foreground)" }}
              title={attachmentPlaceholder(attachment)}
            >
              <Paperclip className="h-3.5 w-3.5 flex-shrink-0" />
              <div className="min-w-0">
                <div className="truncate font-medium">{attachmentFilename(attachment)}</div>
                <div className="truncate" style={{ color: "var(--muted)" }}>
                  {attachment.presentation || "file"} · {attachment.mime_type || "application/octet-stream"} · {formatBytes(attachment.size_bytes)}
                </div>
              </div>
              <button
                type="button"
                onClick={() => setStagedAttachments((prev) => prev.filter((_, i) => i !== index))}
                className="ml-1 rounded p-1 hover:bg-red-500/20"
                title="Remove attachment"
                aria-label={`Remove attachment ${attachmentFilename(attachment)}`}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}

      {showImageForm && (
        <div
          className={`mb-3 rounded p-3 text-sm ${showBorders ? "border" : ""}`}
          style={{ background: "var(--code-bg)", borderColor: "var(--panel-border)", color: "var(--foreground)" }}
          data-testid="image-generation-form"
        >
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 font-medium">
              <ImageIcon className="h-4 w-4" />
              Generate image
            </div>
            <button
              type="button"
              onClick={() => setShowImageForm(false)}
              className="rounded p-1 hover:bg-slate-700/60"
              title="Close image generation"
              aria-label="Close image generation"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          <textarea
            value={imagePrompt}
            onChange={(e) => setImagePrompt(e.target.value)}
            placeholder="Describe the image to generate..."
            disabled={!currentThreadId || imageGenerationMutation.isPending}
            className={`w-full rounded px-3 py-2 resize-none focus:outline-none disabled:opacity-50 ${showBorders ? "border" : ""}`}
            style={{ background: "var(--panel-bg)", borderColor: "var(--panel-border)", color: "var(--foreground)" }}
            rows={2}
            data-testid="image-generation-prompt"
          />
          <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-[minmax(0,1fr)_auto_minmax(8rem,auto)_auto]">
            <input
              value={imageModel}
              onChange={(e) => setImageModel(e.target.value)}
              placeholder="Model/backend (optional)"
              disabled={!currentThreadId || imageGenerationMutation.isPending}
              className={`rounded px-3 py-2 text-sm focus:outline-none disabled:opacity-50 ${showBorders ? "border" : ""}`}
              style={{ background: "var(--panel-bg)", borderColor: "var(--panel-border)", color: "var(--foreground)" }}
              data-testid="image-generation-model"
            />
            <select
              value={imageCount}
              onChange={(e) => setImageCount(e.target.value)}
              disabled={!currentThreadId || imageGenerationMutation.isPending}
              className={`rounded px-3 py-2 text-sm focus:outline-none disabled:opacity-50 ${showBorders ? "border" : ""}`}
              style={{ background: "var(--panel-bg)", borderColor: "var(--panel-border)", color: "var(--foreground)" }}
              title="Number of images"
              data-testid="image-generation-count"
            >
              <option value="">n: default</option>
              <option value="1">n: 1</option>
              <option value="2">n: 2</option>
              <option value="4">n: 4</option>
            </select>
            <input
              value={imageSize}
              onChange={(e) => setImageSize(e.target.value)}
              placeholder="Size (optional)"
              disabled={!currentThreadId || imageGenerationMutation.isPending}
              className={`rounded px-3 py-2 text-sm focus:outline-none disabled:opacity-50 ${showBorders ? "border" : ""}`}
              style={{ background: "var(--panel-bg)", borderColor: "var(--panel-border)", color: "var(--foreground)" }}
              data-testid="image-generation-size"
            />
            <button
              type="button"
              onClick={handleGenerateImage}
              disabled={!canGenerateImage}
              className="rounded bg-purple-600 px-4 py-2 text-sm hover:bg-purple-500 disabled:cursor-not-allowed disabled:opacity-50 flex items-center justify-center gap-2"
              title={isStreaming ? "Wait for streaming to finish before generating" : "Generate image"}
              data-testid="image-generation-submit"
            >
              {imageGenerationMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <ImageIcon className="h-4 w-4" />
              )}
              Generate
            </button>
          </div>
          <div className="mt-2 text-xs" style={{ color: "var(--muted)" }}>
            Generated bytes stay in provider-output storage; the backend appends artifact references to the transcript.
          </div>
        </div>
      )}

      <div className="flex gap-2 items-end">
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={handleFileChange}
          data-testid="attachment-file-input"
        />
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={!currentThreadId || isPending}
          className="px-3 py-2 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
          title="Attach files"
          data-testid="attach-button"
        >
          {uploadMutation.isPending ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : (
            <Paperclip className="w-4 h-4" />
          )}
          Attach
        </button>
        <button
          type="button"
          onClick={() => setShowImageForm((prev) => !prev)}
          disabled={!currentThreadId || imageGenerationMutation.isPending}
          className="px-3 py-2 rounded bg-purple-700 hover:bg-purple-600 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
          title="Generate image"
          aria-pressed={showImageForm}
          data-testid="image-generation-toggle"
        >
          {imageGenerationMutation.isPending ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : (
            <ImageIcon className="w-4 h-4" />
          )}
          Image
        </button>
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={
            currentThreadId
              ? activeGetUserWait
                ? "Answer the waiting get-user tool..."
                : "Message, /command, or $ shell..."
              : "Select a thread first"
          }
          disabled={!currentThreadId}
          className={`flex-1 rounded px-3 py-2 resize-none focus:outline-none disabled:opacity-50 min-h-[40px] ${showBorders || activeGetUserWait ? 'border' : ''}`}
          style={{ background: "var(--panel-bg)", borderColor: activeGetUserWait ? "#d946ef" : "var(--panel-border)", color: "var(--foreground)" }}
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
        {showGetUserAnswerButton && (
          <button
            onClick={handleSubmit}
            disabled={!canSend || isPending}
            className="px-4 py-2 rounded disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
            style={{ background: "#c026d3", color: "white" }}
            title="Answer the waiting get-user tool"
          >
            {isPending ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Send className="w-4 h-4" />
            )}
            Answer
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
            disabled={!canSend || isPending}
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
            {activeGetUserWait ? "Waiting for get-user answer..." : "Streaming..."}
          </span>
        )}
        <span title={enterMode === "send" ? "Enter to send, Shift+Enter for newline" : "Ctrl+Enter to send, Enter for newline"}>
          [{enterMode === "send" ? "⏎ send" : "^⏎ send"}]
        </span>
      </div>
    </div>
  );
}
