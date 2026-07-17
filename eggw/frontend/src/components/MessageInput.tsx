"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Paperclip, Send, Loader2, Terminal, StopCircle, X, ImageIcon } from "lucide-react";
import { sendMessage, executeCommand, isCommand, interruptThread, fetchAutocomplete, fetchThreadState, uploadAttachment, generateThreadImage, attachmentUrl, fetchImageGenerationModels } from "@/lib/api";
import type { AutocompleteSuggestion, ImageGenerationRequest } from "@/lib/api";
import { attachmentFilename, attachmentPlaceholder, buildMessageContentWithAttachments, formatBytes, isImageContentPart, type AttachmentContentPart, type EggMessageContent } from "@/lib/contentParts";
import { useAppStore } from "@/lib/store";
import { streamingBufferForThread } from "@/lib/streamingBuffer";
import { clearLiveToolsForThread } from "@/lib/liveToolContinuity";
import {
  appendClientTranscriptMessage,
  refreshTranscriptTail,
  reloadTranscriptFromCommand,
} from "@/lib/transcript";
import { AutocompleteRequestCoordinator, isAutocompleteEligible } from "@/lib/autocomplete";
import { ComposerDraftBuffer, restoreFailedDraft } from "@/lib/composerDraft";
import { beginOptimisticSend, completeOptimisticSend, createClientOperationId, rollbackOptimisticSend, type SendMessageOperation } from "@/lib/messageOperations";
import { ProtectedImage } from "@/components/ProtectedFileLink";
import clsx from "clsx";

function formatElapsed(startedAtMs: number | null | undefined): string | null {
  const started = Number(startedAtMs);
  if (!Number.isFinite(started) || started <= 0) return null;
  return `${Math.max(0, (Date.now() - started) / 1000).toFixed(0)}s`;
}

function commandNameFromText(command: string): string {
  const text = command.trim();
  if (text.startsWith("$$")) return "$$";
  if (text.startsWith("$")) return "$";
  if (text.startsWith("/")) return text.slice(1).split(/\s+/, 1)[0] || "/";
  return "command";
}

interface MessageInputProps {
  threadId: string;
  showBorders?: boolean;
}

function dataTransferHasFiles(dataTransfer: DataTransfer | null): boolean {
  if (!dataTransfer) return false;
  if (Array.from(dataTransfer.types || []).includes("Files")) return true;
  return Array.from(dataTransfer.items || []).some((item) => item.kind === "file");
}

function fallbackExtensionForMime(mimeType: string): string {
  const normalized = mimeType.toLowerCase();
  if (normalized === "image/png") return "png";
  if (normalized === "image/jpeg") return "jpg";
  if (normalized === "image/gif") return "gif";
  if (normalized === "image/webp") return "webp";
  if (normalized === "text/plain") return "txt";
  return "bin";
}

function ensureFileName(file: File, index: number): File {
  if (file.name.trim()) return file;
  const extension = fallbackExtensionForMime(file.type || "application/octet-stream");
  return new File([file], `clipboard-${index + 1}.${extension}`, {
    type: file.type || "application/octet-stream",
    lastModified: file.lastModified,
  });
}

function filesFromDataTransfer(dataTransfer: DataTransfer | null): File[] {
  if (!dataTransfer) return [];
  const directFiles = Array.from(dataTransfer.files || []);
  const files = directFiles.length
    ? directFiles
    : Array.from(dataTransfer.items || [])
        .filter((item) => item.kind === "file")
        .map((item) => item.getAsFile())
        .filter((file): file is File => Boolean(file));
  return files.map((file, index) => ensureFileName(file, index));
}

export function MessageInput({ threadId, showBorders = true }: MessageInputProps) {
  const [shouldFocusAfterCancel, setShouldFocusAfterCancel] = useState(false);
  const [input, setLocalInput] = useState(() => useAppStore.getState().composerDraftByThread[threadId] || "");
  const [suggestions, setSuggestions] = useState<AutocompleteSuggestion[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [autocompleteLoading, setAutocompleteLoading] = useState(false);
  const [isDraggingFiles, setIsDraggingFiles] = useState(false);
  const [showImageForm, setShowImageForm] = useState(false);
  const [imagePrompt, setImagePrompt] = useState("");
  const [imageModel, setImageModel] = useState("");
  const [imageCount, setImageCount] = useState("");
  const [imageSize, setImageSize] = useState("");
  const [commandPendingStartedAtMs, setCommandPendingStartedAtMs] = useState<number | null>(null);
  const [imagePendingStartedAtMs, setImagePendingStartedAtMs] = useState<number | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const suggestionsRef = useRef<HTMLDivElement>(null);
  const fetchTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const draftFlushTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const autocompleteRef = useRef<AutocompleteRequestCoordinator | null>(null);
  const dragDepthRef = useRef(0);
  const pendingCommandOpsRef = useRef(new Map<string, number>());
  const pendingImageOpsRef = useRef(new Map<string, number>());
  const router = useRouter();
  const queryClient = useQueryClient();
  const refreshMessages = useCallback((targetThreadId: string) => {
    void refreshTranscriptTail(queryClient, targetThreadId).catch((error) => {
      console.error("Failed to refresh transcript tail:", error);
    });
  }, [queryClient]);
  const currentThreadId = threadId;
  const storedInput = useAppStore((state) => state.composerDraftByThread[threadId] || "");
  const setComposerDraft = useAppStore((state) => state.setComposerDraft);
  const stagedAttachments = useAppStore((state) => state.stagedAttachmentsByThread[threadId] || []);
  const setStagedAttachments = useAppStore((state) => state.setStagedAttachments);
  const appendStagedAttachments = useAppStore((state) => state.appendStagedAttachments);
  const openEditAnswerModal = useAppStore((state) => state.openEditAnswerModal);
  const draftBufferRef = useRef<ComposerDraftBuffer | null>(null);
  if (!draftBufferRef.current) {
    draftBufferRef.current = new ComposerDraftBuffer(
      currentThreadId,
      (sourceThreadId) => useAppStore.getState().composerDraftByThread[sourceThreadId] || "",
      (sourceThreadId, value) => useAppStore.getState().setComposerDraft(sourceThreadId, value),
    );
  }
  if (!autocompleteRef.current) {
    autocompleteRef.current = new AutocompleteRequestCoordinator(
      (line, cursor, sourceThreadId, signal) => fetchAutocomplete(line, cursor, sourceThreadId, signal),
    );
  }
  const setInput = useCallback((value: string) => {
    draftBufferRef.current!.update(value);
    setLocalInput(value);
  }, []);
  const flushDraft = useCallback((): string | null => {
    const external = draftBufferRef.current?.flush();
    if (external !== null && external !== undefined) {
      setLocalInput(external);
      return external;
    }
    return null;
  }, []);
  const isStreaming = useAppStore((state) => state.streamingByThread[threadId]?.isStreaming || false);
  const activeUserCommand = useAppStore((state) => state.streamingByThread[threadId]?.activeUserCommand || null);
  const interruptThreadStreaming = useAppStore((state) => state.interruptThreadStreaming);
  const addSystemLog = useAppStore((state) => state.addSystemLog);
  const setTheme = useAppStore((state) => state.setTheme);
  const togglePanel = useAppStore((state) => state.togglePanel);
  const toggleBorders = useAppStore((state) => state.toggleBorders);
  const setEnterMode = useAppStore((state) => state.setEnterMode);
  const setDisplayVerbosity = useAppStore((state) => state.setDisplayVerbosity);
  const enterMode = useAppStore((state) => state.enterMode);

  const { data: threadState } = useQuery({
    queryKey: ["threadState", currentThreadId],
    queryFn: () => fetchThreadState(currentThreadId!),
    enabled: !!currentThreadId,
  });
  const activeGetUserWait = Boolean(threadState?.active_get_user_wait);
  const getUserWaitingNote = threadState?.get_user_waiting_note;

  const { data: imageModelsData } = useQuery({
    queryKey: ["imageModels"],
    queryFn: fetchImageGenerationModels,
    enabled: showImageForm,
  });
  const imageModelOptions = imageModelsData?.models || [];
  const imageDefaultModel = imageModelsData?.default_model || "";

  // Regular messages are optimistic in the source thread's query cache.
  const messageMutation = useMutation({
    mutationFn: ({ threadId: sourceThreadId, content }: SendMessageOperation) =>
      sendMessage(sourceThreadId, content),
    onMutate: (operation: SendMessageOperation) => {
      beginOptimisticSend(queryClient, operation, false);
      setComposerDraft(operation.threadId, "");
      setStagedAttachments(operation.threadId, []);
      if (draftBufferRef.current?.currentThreadId === operation.threadId) setInput("");
      setSuggestions([]);
      setShowSuggestions(false);
      textareaRef.current?.focus();
    },
    onSuccess: (response, operation) => {
      completeOptimisticSend(queryClient, operation, response.message_id);
      queryClient.invalidateQueries({ queryKey: ["threadState", operation.threadId] });
      addSystemLog("Message sent", "success");
    },
    onError: (_error, operation) => {
      const currentDraft = draftBufferRef.current?.currentThreadId === operation.threadId
        ? draftBufferRef.current.currentValue
        : useAppStore.getState().composerDraftByThread[operation.threadId] || "";
      const restoredDraft = restoreFailedDraft(operation.draft, currentDraft);
      rollbackOptimisticSend(queryClient, operation, restoredDraft);
      if (draftBufferRef.current?.currentThreadId === operation.threadId) setInput(restoredDraft);
      addSystemLog("Failed to send message", "error");
    },
  });

  const uploadMutation = useMutation({
    mutationFn: async ({ threadId: sourceThreadId, files }: { threadId: string; operationId: string; files: File[] }) =>
      Promise.all(files.map((file) => uploadAttachment(sourceThreadId, file))),
    onSuccess: (uploads, operation) => {
      appendStagedAttachments(
        operation.threadId,
        uploads.map((upload) => upload.content_part),
      );
      addSystemLog(`Attached ${uploads.length} file${uploads.length === 1 ? "" : "s"}`, "success");
      if (operation.threadId === currentThreadId) textareaRef.current?.focus();
    },
    onError: (error) => {
      addSystemLog(error instanceof Error ? error.message : "Failed to upload attachment", "error");
    },
  });

  const uploadFiles = (files: File[]) => {
    if (!files.length) return;
    uploadMutation.mutate({ threadId: currentThreadId, operationId: createClientOperationId("upload"), files });
  };

  const imageGenerationMutation = useMutation({
    mutationFn: async ({ threadId, request }: { threadId: string; operationId: string; request: ImageGenerationRequest }) => {
      const response = await generateThreadImage(threadId, request);
      return {
        artifactCount: response.content_parts.filter((part) => part.type === "artifact").length,
      };
    },
    onMutate: (variables) => {
      const startedAt = Date.now();
      pendingImageOpsRef.current.set(variables.operationId, startedAt);
      setImagePendingStartedAtMs(startedAt);
      const label = variables.request.model || variables.request.backend || imageDefaultModel || "default image model";
      const prompt = variables.request.prompt.length > 120 ? `${variables.request.prompt.slice(0, 117).trimEnd()}...` : variables.request.prompt;
      addSystemLog(`Generating image with ${label}: ${prompt}`, "info");
    },
    onSuccess: (result, variables) => {
      pendingImageOpsRef.current.delete(variables.operationId);
      if (variables.threadId === currentThreadId) {
        setImagePendingStartedAtMs(
          pendingImageOpsRef.current.values().next().value ?? null,
        );
        setImagePrompt("");
        setImageModel("");
        setImageCount("");
        setImageSize("");
        setShowImageForm(false);
        queryClient.invalidateQueries({ queryKey: ["threadState", variables.threadId] });
      }
      refreshMessages(variables.threadId);
      const count = result.artifactCount;
      addSystemLog(
        `Generated ${count || "image"} artifact${count === 1 ? "" : "s"}; appended result to transcript`,
        "success",
      );
    },
    onError: (error, variables) => {
      pendingImageOpsRef.current.delete(variables.operationId);
      if (variables.threadId === currentThreadId) {
        setImagePendingStartedAtMs(pendingImageOpsRef.current.values().next().value ?? null);
      }
      addSystemLog(error instanceof Error ? error.message : "Failed to generate image", "error");
    },
  });

  // Cancel/interrupt mutation
  const cancelMutation = useMutation({
    mutationFn: ({ threadId: sourceThreadId }: { threadId: string; operationId: string }) => interruptThread(sourceThreadId),
    onSuccess: (_response, operation) => {
      interruptThreadStreaming(operation.threadId);
      streamingBufferForThread(operation.threadId).clear();
      clearLiveToolsForThread(operation.threadId);
      // Refetch messages to get the saved partial content from backend
      refreshMessages(operation.threadId);
      queryClient.invalidateQueries({ queryKey: ["threadState", operation.threadId] });
      queryClient.invalidateQueries({ queryKey: ["threadSettings", operation.threadId] });
      queryClient.invalidateQueries({ queryKey: ["toolCalls", operation.threadId] });
      addSystemLog("Streaming cancelled", "info");
      // Focus only if the user is still viewing the operation's source.
      if (operation.threadId === currentThreadId) setShouldFocusAfterCancel(true);
    },
    onError: () => {
      addSystemLog("Failed to cancel streaming", "error");
    },
  });

  const restoreCommandInputs = useCallback((
    sourceThreadId: string,
    submittedDraft: string,
    submittedAttachments: AttachmentContentPart[],
  ) => {
    const currentDraft = draftBufferRef.current?.currentThreadId === sourceThreadId
      ? draftBufferRef.current.currentValue
      : useAppStore.getState().composerDraftByThread[sourceThreadId] || "";
    const restoredDraft = restoreFailedDraft(submittedDraft, currentDraft);
    setComposerDraft(sourceThreadId, restoredDraft);
    setStagedAttachments(sourceThreadId, submittedAttachments);
    if (draftBufferRef.current?.currentThreadId === sourceThreadId) setInput(restoredDraft);
  }, [setComposerDraft, setInput, setStagedAttachments]);

  // Command mutation
  const commandMutation = useMutation({
    mutationFn: ({ threadId: sourceThreadId, command, staged }: { threadId: string; operationId: string; command: string; draft: string; staged: AttachmentContentPart[] }) => executeCommand(sourceThreadId, command, staged),
    onMutate: ({ threadId: sourceThreadId, operationId: commandOperationId, command }: { threadId: string; operationId: string; command: string; draft: string; staged: AttachmentContentPart[] }) => {
      const startedAt = Date.now();
      pendingCommandOpsRef.current.set(commandOperationId, startedAt);
      setCommandPendingStartedAtMs(startedAt);
      setComposerDraft(sourceThreadId, "");
      if (draftBufferRef.current?.currentThreadId === sourceThreadId) setInput("");
      setSuggestions([]);
      setShowSuggestions(false);
      // Focus input after sending
      textareaRef.current?.focus();
      // For shell commands, show them in the chat
      if (command.startsWith('$')) {
        const nowIso = new Date().toISOString();
        appendClientTranscriptMessage(queryClient, sourceThreadId, {
          id: `temp-${Date.now()}`,
          role: "user",
          content: command,
          timestamp: nowIso,
          client_only: "command",
          client_operation_id: commandOperationId,
        });
      }
      if (command.startsWith('/imageGenerate')) {
        const nowIso = new Date().toISOString();
        appendClientTranscriptMessage(queryClient, sourceThreadId, {
          id: `cmd-start-${Date.now()}`,
          role: "system",
          content: "Starting /imageGenerate — generating image artifact and appending it to the transcript...",
          timestamp: nowIso,
          client_only: "command",
          client_operation_id: commandOperationId,
        });
      }
    },
    onSuccess: (response, variables) => {
      pendingCommandOpsRef.current.delete(variables.operationId);
      if (variables.threadId === currentThreadId) {
        setCommandPendingStartedAtMs(pendingCommandOpsRef.current.values().next().value ?? null);
      }
      if (response.success) {
        queryClient.invalidateQueries({ queryKey: ["threadSettings", variables.threadId] });
        const isEditAnswerModalAction = response.data?.action === "open_edit_answer_modal";
        if (isEditAnswerModalAction && variables.threadId === currentThreadId) {
          openEditAnswerModal({
            threadId: variables.threadId,
            draft: typeof response.data?.draft === "string" ? response.data.draft : "",
            sourceMsgId: typeof response.data?.source_msg_id === "string" ? response.data.source_msg_id : "",
            sourceKind: response.data?.source_kind === "assistant_note"
              ? "assistant_note"
              : response.data?.source_kind === "input_message"
                ? "input_message"
                : response.data?.source_kind === "message"
                  ? "message"
                  : "assistant_answer",
            sourceSuffix: typeof response.data?.source_suffix === "string" ? response.data.source_suffix : "",
            sourceLabel: typeof response.data?.source_label === "string" ? response.data.source_label : "",
            origin: "command",
            replaceCommandText: variables.command,
          });
        }

        // Show every command response in Chat Messages. The System Log stays
        // as a compact event log, but command output should be visible in the
        // transcript area consistently (like /help).
        if (response.message && !response.data?.suppress_transcript) {
          const timestamp = response.finished_at || new Date().toISOString();
          appendClientTranscriptMessage(queryClient, variables.threadId, {
            id: `cmd-${response.command_id || Date.now()}`,
            role: "system",
            content: response.message,
            command_name: response.command_name || commandNameFromText(variables.command),
            command_data: response.data,
            timestamp,
            client_only: "command",
            client_operation_id: variables.operationId,
          });
        }
        if (isEditAnswerModalAction) {
          addSystemLog(response.message || "Command completed", "success");
          return;
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
          // Thread created/switched/duplicated - refresh and navigate only if
          // this command's source is still the visible composer.
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
          queryClient.invalidateQueries({ queryKey: ["threadChildren"] });
          if (variables.threadId === currentThreadId) router.push(`/${response.data.thread_id}`);
        } else if (response.data?.deleted_id) {
          // Thread deleted - refresh lists
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
          queryClient.invalidateQueries({ queryKey: ["threadChildren"] });
        }

        // Model changed - always refresh settings so dropdown updates
        if (response.data?.model_key) {
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
          queryClient.invalidateQueries({ queryKey: ["threadSettings", variables.threadId] });
        }

        if (response.data?.action === "stage_attachment" && response.data?.content_part) {
          appendStagedAttachments(variables.threadId, [response.data!.content_part as AttachmentContentPart]);
        } else if (response.data?.action === "clear_staged_attachments") {
          setStagedAttachments(variables.threadId, []);
        } else if (response.data?.action === "image_generation") {
          refreshMessages(variables.threadId);
          queryClient.invalidateQueries({ queryKey: ["threadState", variables.threadId] });
        }

        if (response.data?.reload) {
          void reloadTranscriptFromCommand(
            queryClient,
            variables.threadId,
            response.data?.reload_mode,
          ).catch((error) => {
            console.error("Failed to reload transcript after command:", error);
          });
          queryClient.invalidateQueries({ queryKey: ["toolCalls", variables.threadId] });
        }

        if (response.data?.tool_call_id) {
          // Shell command - refresh messages and tools
          refreshMessages(variables.threadId);
          queryClient.invalidateQueries({ queryKey: ["toolCalls", variables.threadId] });
        } else if (response.data?.name !== undefined) {
          // Thread renamed - refresh thread data
          queryClient.invalidateQueries({ queryKey: ["thread", variables.threadId] });
          queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
        } else if (response.data?.action === "set_theme" && response.data?.theme) {
          if (variables.threadId === currentThreadId) setTheme(response.data.theme);
        } else if (response.data?.action === "toggle" && response.data?.panel) {
          const panel = response.data.panel as "chat" | "children" | "system";
          if (variables.threadId === currentThreadId) togglePanel(panel);
        } else if (response.data?.action === "toggle_borders") {
          if (variables.threadId === currentThreadId) toggleBorders();
        } else if (response.data?.action === "set_display_verbosity" && response.data?.display_verbosity) {
          if (variables.threadId === currentThreadId) {
            setDisplayVerbosity(response.data.display_verbosity as "max" | "medium" | "min");
          }
        } else if (response.data?.enter_mode) {
          if (variables.threadId === currentThreadId) {
            setEnterMode(response.data.enter_mode as "send" | "newline");
          }
        } else if (response.data?.action === "paste" && variables.threadId === currentThreadId) {
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
        const timestamp = response.finished_at || new Date().toISOString();
        appendClientTranscriptMessage(queryClient, variables.threadId, {
          id: `cmd-err-${response.command_id || Date.now()}`,
          role: "system",
          content: `Error: ${response.message}`,
          command_name: response.command_name || commandNameFromText(variables.command),
          command_data: response.data,
          timestamp,
          client_only: "command",
          client_operation_id: variables.operationId,
        });
        addSystemLog(response.message, "error");
        restoreCommandInputs(variables.threadId, variables.draft, variables.staged);
      }
    },
    onError: (_error, variables) => {
      pendingCommandOpsRef.current.delete(variables.operationId);
      if (variables.threadId === currentThreadId) {
        setCommandPendingStartedAtMs(pendingCommandOpsRef.current.values().next().value ?? null);
      }
      restoreCommandInputs(variables.threadId, variables.draft, variables.staged);
      addSystemLog("Failed to execute command", "error");
    },
  });

  useEffect(() => {
    const active = commandMutation.isPending || imageGenerationMutation.isPending || Boolean(activeUserCommand);
    if (!active) return;
    setNowMs(Date.now());
    const intervalId = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(intervalId);
  }, [commandMutation.isPending, imageGenerationMutation.isPending, activeUserCommand]);

  // Eligibility is checked before scheduling any backend work. Each edit
  // aborts the prior request; the coordinator also fences cancellation races.
  useEffect(() => {
    if (fetchTimeoutRef.current) clearTimeout(fetchTimeoutRef.current);
    autocompleteRef.current?.cancel();
    const cursorPos = textareaRef.current?.selectionStart ?? input.length;
    if (!currentThreadId || !isAutocompleteEligible(input, cursorPos)) {
      setAutocompleteLoading(false);
      setSuggestions([]);
      setShowSuggestions(false);
      return;
    }

    setAutocompleteLoading(true);
    fetchTimeoutRef.current = setTimeout(async () => {
      try {
        const results = await autocompleteRef.current!.request(input, cursorPos, currentThreadId);
        if (results === null) return;
        setSuggestions(results);
        setSelectedIndex(0);
        setShowSuggestions(results.length > 0);
      } catch {
        setSuggestions([]);
        setShowSuggestions(false);
      } finally {
        setAutocompleteLoading(false);
      }
    }, 100);

    return () => {
      if (fetchTimeoutRef.current) clearTimeout(fetchTimeoutRef.current);
      autocompleteRef.current?.cancel();
    };
  }, [input, currentThreadId]);

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

  // Persist edits only at coalesced/safe boundaries. Store publications from
  // external owners hydrate local state without being written back over them.
  useEffect(() => {
    const next = draftBufferRef.current!.switchThread(currentThreadId);
    setLocalInput(next);
  }, [currentThreadId]);

  useEffect(() => {
    const external = draftBufferRef.current!.acceptExternal(currentThreadId, storedInput);
    if (external !== null) setLocalInput(external);
  }, [currentThreadId, storedInput]);

  useEffect(() => {
    if (draftFlushTimeoutRef.current) clearTimeout(draftFlushTimeoutRef.current);
    draftFlushTimeoutRef.current = setTimeout(flushDraft, 500);
    return () => {
      if (draftFlushTimeoutRef.current) clearTimeout(draftFlushTimeoutRef.current);
    };
  }, [input, flushDraft]);

  useEffect(() => () => {
    if (draftFlushTimeoutRef.current) clearTimeout(draftFlushTimeoutRef.current);
    autocompleteRef.current?.cancel();
    draftBufferRef.current?.flush();
  }, []);

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
    // Mutation observers are component-global, but pending indicators are view
    // state. Never carry thread A's spinner into thread B.
    setCommandPendingStartedAtMs(null);
    setImagePendingStartedAtMs(null);
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
          document.activeElement?.tagName === "SELECT" ||
          document.activeElement?.closest('[role="dialog"]') ||
          document.querySelector('[role="dialog"][aria-modal="true"]')) {
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

    // If an external insertion raced the click/key event, hydrate it and let
    // the operator review it instead of sending and clearing a stale snapshot.
    if (flushDraft() !== null) return;
    if (isCommand(trimmed)) {
      const commandOperationId = createClientOperationId("cmd");
      setStagedAttachments(currentThreadId, []);
      commandMutation.mutate({
        threadId: currentThreadId,
        operationId: commandOperationId,
        command: trimmed,
        draft: input,
        staged: stagedAttachments,
      });
    } else {
      messageMutation.mutate({
        threadId: currentThreadId,
        operationId: createClientOperationId("temp"),
        content: buildMessageContentWithAttachments(trimmed, stagedAttachments),
        draft: input,
        attachments: [...stagedAttachments],
      });
    }
  };

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    uploadFiles(files);
  };

  const handleDragEnter = (event: React.DragEvent<HTMLDivElement>) => {
    if (!dataTransferHasFiles(event.dataTransfer)) return;
    event.preventDefault();
    event.stopPropagation();
    dragDepthRef.current += 1;
    setIsDraggingFiles(true);
  };

  const handleDragOver = (event: React.DragEvent<HTMLDivElement>) => {
    if (!dataTransferHasFiles(event.dataTransfer)) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = "copy";
    setIsDraggingFiles(true);
  };

  const handleDragLeave = (event: React.DragEvent<HTMLDivElement>) => {
    if (!dataTransferHasFiles(event.dataTransfer)) return;
    event.preventDefault();
    event.stopPropagation();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) {
      setIsDraggingFiles(false);
    }
  };

  const handleDrop = (event: React.DragEvent<HTMLDivElement>) => {
    if (!dataTransferHasFiles(event.dataTransfer)) return;
    event.preventDefault();
    event.stopPropagation();
    dragDepthRef.current = 0;
    setIsDraggingFiles(false);
    uploadFiles(filesFromDataTransfer(event.dataTransfer));
  };

  const handlePaste = (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const files = filesFromDataTransfer(event.clipboardData);
    if (!files.length) return;
    event.preventDefault();
    uploadFiles(files);
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
      ...((imageModel.trim() || imageDefaultModel) ? { model: imageModel.trim() || imageDefaultModel } : {}),
      ...(imageCount ? { n: Number(imageCount) } : {}),
      ...(imageSize.trim() ? { size: imageSize.trim() } : {}),
    };
    imageGenerationMutation.mutate({ threadId: currentThreadId, operationId: createClientOperationId("image"), request });
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
  const autocompleteOpen = showSuggestions && suggestions.length > 0;
  const autocompleteListboxId = "message-autocomplete-listbox";
  const activeSuggestionId = autocompleteOpen ? `message-autocomplete-option-${selectedIndex}` : undefined;
  const showGetUserAnswerButton = isStreaming && activeGetUserWait && !inputIsCommand;
  const canSend = Boolean(currentThreadId) && (Boolean(input.trim()) || stagedAttachments.length > 0);
  const canGenerateImage = Boolean(currentThreadId) && Boolean(imagePrompt.trim()) && !isStreaming && !imageGenerationMutation.isPending;
  const commandPendingElapsed = commandMutation.isPending ? formatElapsed(commandPendingStartedAtMs) : null;
  const imagePendingElapsed = imageGenerationMutation.isPending ? formatElapsed(imagePendingStartedAtMs) : null;
  const activeCommandElapsed = activeUserCommand ? `${Math.max(0, (nowMs - activeUserCommand.startedAtMs) / 1000).toFixed(0)}s` : null;
  const displayedCommandElapsed = activeCommandElapsed || commandPendingElapsed;
  const displayedCommandLabel = activeUserCommand
    ? activeUserCommand.name.startsWith("$") ? activeUserCommand.name : `/${activeUserCommand.name}`
    : "command";
  const activeCommandTimeout = activeUserCommand?.timeoutSec && activeUserCommand.timeoutSec > 0
    ? `; timeout in ${Math.max(0, activeUserCommand.timeoutSec - Math.max(0, (nowMs - activeUserCommand.startedAtMs) / 1000)).toFixed(0)}s (limit ${activeUserCommand.timeoutSec.toFixed(0)}s)`
    : "";

  return (
    <div
      className={clsx(
        "eggw-composer relative",
        showBorders && "eggw-composer-bordered",
        activeGetUserWait && "eggw-composer-waiting",
        inputIsCommand && "eggw-composer-command",
        isStreaming && "eggw-composer-streaming",
        isDraggingFiles && "eggw-composer-dragging",
      )}
      data-testid="message-composer"
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {isDraggingFiles && (
        <div
          className="eggw-composer-drop-overlay"
          data-testid="attachment-drop-overlay"
        >
          Drop files to attach
        </div>
      )}
      {activeGetUserWait && (
        <div
          data-testid="get-user-input-mode"
          className="eggw-composer-notice ui-status-special"
          role="status"
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
      {autocompleteOpen && (
        <div
          ref={suggestionsRef}
          id={autocompleteListboxId}
          className="eggw-autocomplete-listbox"
          role="listbox"
          aria-label="Command suggestions"
        >
          {suggestions.map((suggestion, index) => (
            <div
              key={`${suggestion.display}-${index}`}
              id={`message-autocomplete-option-${index}`}
              role="option"
              aria-selected={index === selectedIndex}
              data-index={index}
              className="eggw-autocomplete-option"
              onClick={() => applySuggestion(suggestion)}
              onMouseEnter={() => setSelectedIndex(index)}
            >
              <span className="font-mono text-sm flex-1 whitespace-nowrap overflow-hidden text-ellipsis">{suggestion.display}</span>
              {suggestion.meta && (
                <span className="eggw-autocomplete-meta">{suggestion.meta}</span>
              )}
            </div>
          ))}
          <div className="eggw-autocomplete-help">
            <kbd>Tab</kbd> select · <kbd>↑↓</kbd> navigate · <kbd>Esc</kbd> close
          </div>
        </div>
      )}
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true" data-testid="autocomplete-status">
        {autocompleteLoading ? "Loading suggestions" : autocompleteOpen ? `${suggestions.length} suggestions available` : ""}
      </div>

      {stagedAttachments.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-2" data-testid="staged-attachments">
          {stagedAttachments.map((attachment, index) => {
            const previewUrl = currentThreadId && attachment.owner_thread_id === currentThreadId && isImageContentPart(attachment)
              ? attachmentUrl(currentThreadId, attachment.input_id)
              : null;
            return (
              <div
                key={`${attachment.input_id}-${index}`}
                className="eggw-staged-attachment"
                title={attachmentPlaceholder(attachment)}
              >
                <Paperclip className="h-3.5 w-3.5 flex-shrink-0" />
                {previewUrl && (
                  <ProtectedImage
                    url={previewUrl}
                    alt={`Preview of ${attachmentFilename(attachment)}`}
                    loading="lazy"
                    decoding="async"
                    data-testid="staged-attachment-preview"
                    className="eggw-staged-attachment-preview"
                    onError={(event) => {
                      event.currentTarget.style.display = "none";
                    }}
                  />
                )}
                <div className="min-w-0">
                  <div className="truncate font-medium">{attachmentFilename(attachment)}</div>
                  <div className="eggw-staged-attachment-meta">
                    {attachment.presentation || "file"} · {attachment.mime_type || "application/octet-stream"} · {formatBytes(attachment.size_bytes)}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => setStagedAttachments(currentThreadId, stagedAttachments.filter((_, i) => i !== index))}
                  className="ui-button ui-button-danger ui-icon-button eggw-compact-icon-button"
                  title="Remove attachment"
                  aria-label={`Remove attachment ${attachmentFilename(attachment)}`}
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            );
          })}
        </div>
      )}

      {showImageForm && (
        <div
          className="eggw-image-form"
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
              className="ui-button ui-button-ghost ui-icon-button eggw-compact-icon-button"
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
            className="eggw-form-control w-full resize-none px-3 py-2 disabled:opacity-50"
            aria-label="Image prompt"
            rows={2}
            data-testid="image-generation-prompt"
          />
          <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-[minmax(0,1fr)_auto_minmax(8rem,auto)_auto]">
            <input
              value={imageModel}
              onChange={(e) => setImageModel(e.target.value)}
              list="image-generation-model-options"
              placeholder={imageDefaultModel ? `Default: ${imageDefaultModel}` : "Image model (optional)"}
              disabled={!currentThreadId || imageGenerationMutation.isPending}
              className="eggw-form-control px-3 py-2 text-sm disabled:opacity-50"
              aria-label="Image model"
              data-testid="image-generation-model"
            />
            {imageModelOptions.length > 0 && (
              <datalist id="image-generation-model-options">
                {imageModelOptions.map((model: { key: string }) => (
                  <option key={model.key} value={model.key} />
                ))}
              </datalist>
            )}
            <select
              value={imageCount}
              onChange={(e) => setImageCount(e.target.value)}
              disabled={!currentThreadId || imageGenerationMutation.isPending}
              className="eggw-form-control px-3 py-2 text-sm disabled:opacity-50"
              aria-label="Number of images"
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
              className="eggw-form-control px-3 py-2 text-sm disabled:opacity-50"
              aria-label="Image size"
              data-testid="image-generation-size"
            />
            <button
              type="button"
              onClick={handleGenerateImage}
              disabled={!canGenerateImage}
              className="ui-button ui-button-primary"
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
          <div className="eggw-field-help">
            Generated bytes stay in provider-output storage; the backend appends artifact references to the transcript.
          </div>
        </div>
      )}

      <div className="eggw-composer-main-row">
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={handleFileChange}
          data-testid="attachment-file-input"
        />
        <div className="eggw-composer-tools">
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={!currentThreadId || isPending}
            className="eggw-composer-action"
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
            className={clsx("eggw-composer-action", showImageForm && "eggw-composer-action-active")}
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
        </div>
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onBlur={flushDraft}
          onPaste={handlePaste}
          onKeyDown={handleKeyDown}
          placeholder={
            currentThreadId
              ? activeGetUserWait
                ? "Answer the waiting get-user tool..."
                : "Message, /command, or $ shell..."
              : "Select a thread first"
          }
          disabled={!currentThreadId}
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={autocompleteOpen}
          aria-controls={autocompleteOpen ? autocompleteListboxId : undefined}
          aria-activedescendant={activeSuggestionId}
          aria-describedby="composer-shortcut-hint"
          className={clsx(
            "eggw-composer-input min-w-0 flex-1 resize-none px-3 py-2 focus:outline-none disabled:opacity-50",
            (showBorders || activeGetUserWait) && "border",
          )}
          rows={1}
          data-testid="message-input"
        />
        {/* During streaming: show Run button for commands, Cancel button always */}
        <div className="eggw-composer-submit">
          {showGetUserAnswerButton ? (
            <button
              onClick={handleSubmit}
              disabled={!canSend || isPending}
              className="eggw-composer-action eggw-composer-action-primary"
              title="Answer the waiting get-user tool"
            >
              {isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Send className="w-4 h-4" />
              )}
              Answer
            </button>
          ) : (
            <button
              onClick={handleSubmit}
              disabled={!canSend || isPending}
              className={clsx("eggw-composer-action", inputIsCommand ? "eggw-composer-action-warn" : "eggw-composer-action-primary")}
              title={isStreaming && !inputIsCommand ? "Queue message after the current stream" : undefined}
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
          {isStreaming && (
            <button
              onClick={() => cancelMutation.mutate({ threadId: currentThreadId, operationId: createClientOperationId("interrupt") })}
              disabled={cancelMutation.isPending}
              className="eggw-composer-action eggw-composer-action-danger"
              title="Cancel streaming (Escape)"
            >
              {cancelMutation.isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <StopCircle className="w-4 h-4" />
              )}
              Cancel
            </button>
          )}
        </div>
      </div>

      {/* Status line */}
      <div className="eggw-composer-status mt-2 flex flex-wrap items-center justify-between gap-2 text-[11px]">
        <div className="flex flex-wrap items-center gap-2">
          <span className="eggw-composer-status-pill">
            {currentThreadId ? `Thread ${currentThreadId.slice(-8)}` : "No thread"}
          </span>
          {inputIsCommand && (
            <span className="eggw-composer-status-pill eggw-status-command">
              {input.startsWith('$') ? "Shell command" : "Slash command"}
            </span>
          )}
          {isStreaming && (
            <span className="eggw-composer-live-status">
              <Loader2 className="w-3 h-3 animate-spin" />
              {activeGetUserWait ? "Waiting for get-user answer..." : "Streaming; new messages will queue..."}
            </span>
          )}
          {commandMutation.isPending && (
            <span className="eggw-composer-live-status">
              <Loader2 className="w-3 h-3 animate-spin" />
              Running {displayedCommandLabel}{displayedCommandElapsed ? ` ${displayedCommandElapsed}` : ""}{activeCommandTimeout}...
            </span>
          )}
          {!commandMutation.isPending && activeUserCommand && (
            <span className="eggw-composer-live-status">
              <Loader2 className="w-3 h-3 animate-spin" />
              Running {activeUserCommand.name.startsWith("$") ? activeUserCommand.name : `/${activeUserCommand.name}`}{activeCommandElapsed ? ` ${activeCommandElapsed}` : ""}{activeCommandTimeout}...
            </span>
          )}
          {imageGenerationMutation.isPending && (
            <span className="eggw-composer-live-status">
              <Loader2 className="w-3 h-3 animate-spin" />
              Generating image{imagePendingElapsed ? ` ${imagePendingElapsed}` : ""}...
            </span>
          )}
        </div>
        <span id="composer-shortcut-hint" className="eggw-composer-status-pill" title={enterMode === "send" ? "Enter to send, Shift+Enter for newline" : "Ctrl+Enter to send, Enter for newline"}>
          [{enterMode === "send" ? "⏎ send" : "^⏎ send"}]
        </span>
      </div>
    </div>
  );
}
