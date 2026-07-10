import { create } from "zustand";
import type { AttachmentContentPart, EggMessageContent } from "./contentParts";

export interface Thread {
  id: string;
  name?: string;
  parent_id?: string;
  model_key?: string;
  created_at?: string;
  has_children: boolean;
}

export interface Message {
  id: string;
  role: string;
  content?: EggMessageContent;
  content_text?: string;
  kind?: string;
  start_msg_id?: string;
  start_event_seq?: number;
  marker_event_seq?: number;
  selector?: string;
  created_by?: string;
  reasoning?: string;
  tool_calls?: any[];
  tool_stream?: Record<string, any>;
  tool_calls_stream?: Record<string, any>;
  tool_call_id?: string;
  output_optimizer?: Record<string, any>;
  name?: string;
  model_key?: string;
  timestamp?: string;  // ISO datetime string
  tokens?: number;     // Per-message token count
  tps?: number;
  answer_user_preserve_turn?: boolean;
  recovery_notice?: boolean;
  command_name?: string;
  command_data?: Record<string, any>;
  client_only?: "optimistic" | "command";
  client_operation_id?: string;
  /** Canonical msg.create sequence retained until an HTTP snapshot covers it. */
  event_seq?: number;
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: any;
  state: string;
  output?: string;
  approval_decision?: string;
  output_decision?: string;
  summary?: string;
}

export type DisplayVerbosity = "max" | "medium" | "min";

export interface StreamingToolTimeout {
  startedAtMs: number;
  timeoutSec: number;
}

export interface StreamingToolOutput {
  id: string;
  name: string;
  suppressed: boolean;
  suppressedFrames: number;
  summary?: string;
  startedAtMs?: number;
  timeout?: StreamingToolTimeout;
}

export interface StreamingProviderRequest {
  startedAtMs: number;
  timeoutSec?: number;
  modelKey?: string | null;
}

export interface ActiveUserCommand {
  id: string;
  name: string;
  command: string;
  startedAtMs: number;
  timeoutSec?: number;
}

export type SSEConnectionStatus = "disconnected" | "connecting" | "connected" | "reconnecting";

export interface ThreadStreamingState {
  isStreaming: boolean;
  invokeId: string | null;
  streamingModelKey: string | null;
  streamingKind: string | null;
  streamingStartedAtMs: number | null;
  streamingProviderRequest: StreamingProviderRequest | null;
  activeUserCommand: ActiveUserCommand | null;
  streamingToolCalls: Record<string, { name: string }>;
  streamingToolOutputs: Record<string, StreamingToolOutput>;
}

export interface ThreadConnectionState {
  status: SSEConnectionStatus;
}

const EMPTY_THREAD_STREAMING_STATE: ThreadStreamingState = {
  isStreaming: false,
  invokeId: null,
  streamingModelKey: null,
  streamingKind: null,
  streamingStartedAtMs: null,
  streamingProviderRequest: null,
  activeUserCommand: null,
  streamingToolCalls: {},
  streamingToolOutputs: {},
};

export function emptyThreadStreamingState(): ThreadStreamingState {
  return { ...EMPTY_THREAD_STREAMING_STATE };
}

export interface SystemLog {
  timestamp: Date;
  message: string;
  type: "info" | "error" | "success";
}

export type EditAnswerSourceKind = "assistant_answer" | "assistant_note" | "input_message" | "message";
export type EditAnswerOrigin = "command" | "quote_button";

export interface EditAnswerModalState {
  isOpen: boolean;
  threadId: string | null;
  draft: string;
  sourceMsgId: string;
  sourceKind: EditAnswerSourceKind;
  sourceSuffix: string;
  sourceLabel: string;
  origin: EditAnswerOrigin;
  replaceCommandText?: string;
}

export type OpenEditAnswerModalPayload = Omit<EditAnswerModalState, "isOpen">;

const CLOSED_EDIT_ANSWER_MODAL: EditAnswerModalState = {
  isOpen: false,
  threadId: null,
  draft: "",
  sourceMsgId: "",
  sourceKind: "assistant_answer",
  sourceSuffix: "",
  sourceLabel: "",
  origin: "command",
};

interface AppState {
  // Current thread
  currentThreadId: string | null;
  setCurrentThreadId: (id: string | null) => void;

  // Threads
  threads: Thread[];
  setThreads: (threads: Thread[]) => void;

  // Thread-scoped composer drafts
  composerDraftByThread: Record<string, string>;
  setComposerDraft: (threadId: string, text: string) => void;
  appendComposerDraft: (threadId: string, text: string) => void;

  // Thread-scoped staged inputs survive navigation and async completion.
  stagedAttachmentsByThread: Record<string, AttachmentContentPart[]>;
  setStagedAttachments: (threadId: string, attachments: AttachmentContentPart[]) => void;
  appendStagedAttachments: (threadId: string, attachments: AttachmentContentPart[]) => void;

  // Edit-answer modal
  editAnswerModal: EditAnswerModalState;
  openEditAnswerModal: (payload: OpenEditAnswerModalPayload) => void;
  closeEditAnswerModal: () => void;
  setEditAnswerDraft: (text: string) => void;

  // Thread-scoped ephemeral run state. Persisted messages live in React Query.
  streamingByThread: Record<string, ThreadStreamingState>;
  connectionByThread: Record<string, ThreadConnectionState>;
  patchThreadStreaming: (threadId: string, patch: Partial<ThreadStreamingState>) => void;
  resetThreadStreaming: (threadId: string) => void;
  setThreadConnection: (threadId: string, status: SSEConnectionStatus) => void;
  setThreadStreamingToolCalls: (threadId: string, calls: Record<string, { name: string }>) => void;
  upsertThreadStreamingToolCall: (threadId: string, tcId: string, name: string) => void;
  setThreadStreamingToolOutputs: (threadId: string, outputs: Record<string, StreamingToolOutput>) => void;
  upsertThreadStreamingToolOutput: (threadId: string, id: string, name: string, suppressed?: boolean, summary?: string) => void;
  markThreadStreamingToolStarted: (threadId: string, id: string, name: string, startedAtMs: number, timeoutSec?: number | null) => void;
  clearThreadStreamingToolTimeout: (threadId: string, id: string) => void;
  removeThreadStreamingToolCall: (threadId: string, id: string) => void;
  removeThreadStreamingTool: (threadId: string, id: string) => void;
  clearThreadStreamingAssistant: (threadId: string) => void;
  interruptThreadStreaming: (threadId: string) => void;

  // Tool calls
  pendingTools: ToolCall[];
  setPendingTools: (tools: ToolCall[]) => void;

  // System log
  systemLogs: SystemLog[];
  addSystemLog: (message: string, type?: "info" | "error" | "success") => void;
  clearSystemLogs: () => void;

  // Models
  models: { key: string; provider: string; model_id: string }[];
  setModels: (models: { key: string; provider: string; model_id: string }[]) => void;

  // Panel visibility
  panelVisibility: { chat: boolean; children: boolean; system: boolean };
  togglePanel: (panel: "chat" | "children" | "system") => void;

  // UI preferences
  showBorders: boolean;
  toggleBorders: () => void;
  enterMode: "send" | "newline";
  setEnterMode: (mode: "send" | "newline") => void;
  displayVerbosity: DisplayVerbosity;
  setDisplayVerbosity: (level: DisplayVerbosity) => void;

  // Theme
  theme: string;
  setTheme: (theme: string) => void;
}

export const useAppStore = create<AppState>((set) => ({
  // Current thread
  currentThreadId: null,
  setCurrentThreadId: (id) => set({ currentThreadId: id }),

  // Threads
  threads: [],
  setThreads: (threads) => set({ threads }),

  // Thread-scoped composer drafts
  composerDraftByThread: {},
  setComposerDraft: (threadId, text) =>
    set((state) => ({
      composerDraftByThread: {
        ...state.composerDraftByThread,
        [threadId]: text,
      },
    })),
  appendComposerDraft: (threadId, text) =>
    set((state) => {
      const existing = state.composerDraftByThread[threadId] || "";
      const separator = existing.trim() ? "\n\n" : "";
      return {
        composerDraftByThread: {
          ...state.composerDraftByThread,
          [threadId]: `${existing.trimEnd()}${separator}${text}`,
        },
      };
    }),

  stagedAttachmentsByThread: {},
  setStagedAttachments: (threadId, attachments) =>
    set((state) => ({
      stagedAttachmentsByThread: { ...state.stagedAttachmentsByThread, [threadId]: attachments },
    })),
  appendStagedAttachments: (threadId, attachments) =>
    set((state) => ({
      stagedAttachmentsByThread: {
        ...state.stagedAttachmentsByThread,
        [threadId]: [...(state.stagedAttachmentsByThread[threadId] || []), ...attachments],
      },
    })),

  // Edit-answer modal
  editAnswerModal: CLOSED_EDIT_ANSWER_MODAL,
  openEditAnswerModal: (payload) =>
    set({
      editAnswerModal: {
        isOpen: true,
        ...payload,
      },
    }),
  closeEditAnswerModal: () => set({ editAnswerModal: CLOSED_EDIT_ANSWER_MODAL }),
  setEditAnswerDraft: (text) =>
    set((state) => ({
      editAnswerModal: state.editAnswerModal.isOpen
        ? { ...state.editAnswerModal, draft: text }
        : state.editAnswerModal,
    })),

  // Thread-scoped run and transport state
  streamingByThread: {},
  connectionByThread: {},
  patchThreadStreaming: (threadId, patch) =>
    set((state) => {
      const current = state.streamingByThread[threadId] || EMPTY_THREAD_STREAMING_STATE;
      if (Object.entries(patch).every(([key, value]) => Object.is(current[key as keyof ThreadStreamingState], value))) {
        return state;
      }
      return {
        streamingByThread: {
          ...state.streamingByThread,
          [threadId]: { ...current, ...patch },
        },
      };
    }),
  resetThreadStreaming: (threadId) =>
    set((state) => ({
      streamingByThread: { ...state.streamingByThread, [threadId]: emptyThreadStreamingState() },
    })),
  setThreadConnection: (threadId, status) =>
    set((state) => {
      if (state.connectionByThread[threadId]?.status === status) return state;
      return { connectionByThread: { ...state.connectionByThread, [threadId]: { status } } };
    }),
  setThreadStreamingToolCalls: (threadId, calls) =>
    set((state) => ({
      streamingByThread: {
        ...state.streamingByThread,
        [threadId]: { ...(state.streamingByThread[threadId] || emptyThreadStreamingState()), streamingToolCalls: calls },
      },
    })),
  upsertThreadStreamingToolCall: (threadId, tcId, name) =>
    set((state) => {
      const streaming = state.streamingByThread[threadId] || emptyThreadStreamingState();
      const existing = streaming.streamingToolCalls[tcId];
      if (existing && (!name || existing.name === name)) return state;
      return {
        streamingByThread: {
          ...state.streamingByThread,
          [threadId]: {
            ...streaming,
            streamingToolCalls: {
              ...streaming.streamingToolCalls,
              [tcId]: { name: name || existing?.name || "" },
            },
          },
        },
      };
    }),
  setThreadStreamingToolOutputs: (threadId, outputs) =>
    set((state) => {
      const streaming = state.streamingByThread[threadId] || emptyThreadStreamingState();
      return {
        streamingByThread: { ...state.streamingByThread, [threadId]: { ...streaming, streamingToolOutputs: outputs } },
      };
    }),
  upsertThreadStreamingToolOutput: (threadId, id, name, suppressed = false, summary) =>
    set((state) => {
      const streaming = state.streamingByThread[threadId] || emptyThreadStreamingState();
      const existing = streaming.streamingToolOutputs[id] || { id, name, suppressed: false, suppressedFrames: 0 };
      return {
        streamingByThread: {
          ...state.streamingByThread,
          [threadId]: {
            ...streaming,
            streamingToolOutputs: {
              ...streaming.streamingToolOutputs,
              [id]: {
                ...existing,
                name: name || existing.name,
                suppressed: existing.suppressed || suppressed,
                suppressedFrames: suppressed ? existing.suppressedFrames + 1 : existing.suppressedFrames,
                summary: summary || existing.summary,
              },
            },
          },
        },
      };
    }),
  markThreadStreamingToolStarted: (threadId, id, name, startedAtMs, timeoutSec = null) =>
    set((state) => {
      const streaming = state.streamingByThread[threadId] || emptyThreadStreamingState();
      const existing = streaming.streamingToolOutputs[id] || { id, name, suppressed: false, suppressedFrames: 0 };
      return {
        streamingByThread: {
          ...state.streamingByThread,
          [threadId]: {
            ...streaming,
            streamingToolOutputs: {
              ...streaming.streamingToolOutputs,
              [id]: {
                ...existing,
                name: name || existing.name,
                startedAtMs,
                ...(timeoutSec && timeoutSec > 0 ? { timeout: { startedAtMs, timeoutSec } } : {}),
              },
            },
          },
        },
      };
    }),
  clearThreadStreamingToolTimeout: (threadId, id) =>
    set((state) => {
      const streaming = state.streamingByThread[threadId] || emptyThreadStreamingState();
      const existing = streaming.streamingToolOutputs[id];
      if (!existing?.timeout) return {};
      const { timeout: _timeout, ...withoutTimeout } = existing;
      return {
        streamingByThread: {
          ...state.streamingByThread,
          [threadId]: {
            ...streaming,
            streamingToolOutputs: { ...streaming.streamingToolOutputs, [id]: withoutTimeout },
          },
        },
      };
    }),

  removeThreadStreamingToolCall: (threadId, id) =>
    set((state) => {
      const streaming = state.streamingByThread[threadId];
      if (!streaming?.streamingToolCalls[id]) return state;
      const streamingToolCalls = { ...streaming.streamingToolCalls };
      delete streamingToolCalls[id];
      return {
        streamingByThread: {
          ...state.streamingByThread,
          [threadId]: { ...streaming, streamingToolCalls },
        },
      };
    }),
  removeThreadStreamingTool: (threadId, id) =>
    set((state) => {
      const streaming = state.streamingByThread[threadId];
      if (!streaming || (!streaming.streamingToolCalls[id] && !streaming.streamingToolOutputs[id])) return state;
      const streamingToolCalls = { ...streaming.streamingToolCalls };
      const streamingToolOutputs = { ...streaming.streamingToolOutputs };
      delete streamingToolCalls[id];
      delete streamingToolOutputs[id];
      return {
        streamingByThread: {
          ...state.streamingByThread,
          [threadId]: { ...streaming, streamingToolCalls, streamingToolOutputs },
        },
      };
    }),
  clearThreadStreamingAssistant: (threadId) =>
    set((state) => {
      const streaming = state.streamingByThread[threadId];
      if (!streaming) return state;
      return {
        streamingByThread: {
          ...state.streamingByThread,
          [threadId]: {
            ...streaming,
            isStreaming: false,
            invokeId: null,
            streamingModelKey: null,
            streamingKind: null,
            streamingStartedAtMs: null,
            streamingProviderRequest: null,
          },
        },
      };
    }),
  interruptThreadStreaming: (threadId) =>
    set((state) => ({
      streamingByThread: { ...state.streamingByThread, [threadId]: emptyThreadStreamingState() },
    })),

  // Tool calls
  pendingTools: [],
  setPendingTools: (tools) => set({ pendingTools: tools }),

  // System log
  systemLogs: [],
  addSystemLog: (message, type = "info") =>
    set((state) => ({
      systemLogs: [
        ...state.systemLogs,
        { timestamp: new Date(), message, type },
      ].slice(-100), // Keep last 100 logs
    })),
  clearSystemLogs: () => set({ systemLogs: [] }),

  // Models
  models: [],
  setModels: (models) => set({ models }),

  // Panel visibility (sidebar hidden by default to maximize screen space)
  panelVisibility: { chat: true, children: true, system: false },
  togglePanel: (panel) =>
    set((state) => ({
      panelVisibility: {
        ...state.panelVisibility,
        [panel]: !state.panelVisibility[panel],
      },
    })),

  // UI preferences
  showBorders: false,
  toggleBorders: () => set((state) => ({ showBorders: !state.showBorders })),
  enterMode: "send",
  setEnterMode: (mode) => set({ enterMode: mode }),
  displayVerbosity: "max",
  setDisplayVerbosity: (level) => set({ displayVerbosity: level }),

  // Theme
  theme: "dark",
  setTheme: (theme) => {
    // Apply theme to document
    if (typeof document !== "undefined") {
      document.documentElement.setAttribute("data-theme", theme);
      // Persist to localStorage
      localStorage.setItem("eggw-theme", theme);
    }
    set({ theme });
  },
}));

// Initialize theme from localStorage on client side
if (typeof window !== "undefined") {
  const savedTheme = localStorage.getItem("eggw-theme");
  if (savedTheme) {
    document.documentElement.setAttribute("data-theme", savedTheme);
    useAppStore.setState({ theme: savedTheme });
  }
}
