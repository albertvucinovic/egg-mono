import { create } from "zustand";

export interface Thread {
  id: string;
  name?: string;
  parent_id?: string;
  model_key?: string;
  has_children: boolean;
}

export interface Message {
  id: string;
  role: string;
  content?: string;
  reasoning?: string;
  tool_calls?: any[];
  tool_call_id?: string;
  model_key?: string;
  timestamp?: string;  // ISO datetime string
  tokens?: number;     // Per-message token count
  tps?: number;
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: any;
  state: string;
  output?: string;
  approval_decision?: string;
  output_decision?: string;
}

export interface SystemLog {
  timestamp: Date;
  message: string;
  type: "info" | "error" | "success";
}

interface AppState {
  // Current thread
  currentThreadId: string | null;
  setCurrentThreadId: (id: string | null) => void;

  // Threads
  threads: Thread[];
  setThreads: (threads: Thread[]) => void;

  // Messages
  messages: Message[];
  setMessages: (messages: Message[]) => void;
  addMessage: (message: Message) => void;

  // Streaming content - stored as array of chunks for O(1) append
  streamingContent: string;
  streamingContentChunks: string[];
  setStreamingContent: (content: string) => void;
  appendStreamingContent: (chunk: string) => void;

  // Streaming reasoning - stored as array of chunks for O(1) append
  streamingReasoning: string;
  streamingReasoningChunks: string[];
  setStreamingReasoning: (content: string) => void;
  appendStreamingReasoning: (chunk: string) => void;

  // Streaming tool calls (tool_call_id -> {name, arguments})
  streamingToolCalls: Record<string, { name: string; arguments: string }>;
  setStreamingToolCalls: (tcs: Record<string, { name: string; arguments: string }>) => void;
  appendToolCallArguments: (tcId: string, name: string, argsDelta: string) => void;

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

  // UI state
  isStreaming: boolean;
  setIsStreaming: (streaming: boolean) => void;
  streamingModelKey: string | null;
  setStreamingModelKey: (key: string | null) => void;
  streamingKind: string | null;
  setStreamingKind: (kind: string | null) => void;

  // Panel visibility
  panelVisibility: { chat: boolean; children: boolean; system: boolean };
  togglePanel: (panel: "chat" | "children" | "system") => void;

  // UI preferences
  showBorders: boolean;
  toggleBorders: () => void;
  enterMode: "send" | "newline";
  setEnterMode: (mode: "send" | "newline") => void;

  // Theme
  theme: string;
  setTheme: (theme: string) => void;

  // Scroll trigger - incremented when UI-only messages are added
  scrollTrigger: number;
  triggerScroll: () => void;
}

export const useAppStore = create<AppState>((set) => ({
  // Current thread
  currentThreadId: null,
  setCurrentThreadId: (id) => set({
    currentThreadId: id,
    // Clear messages immediately for instant UI feedback when switching threads
    messages: [],
    streamingContent: "",
    streamingContentChunks: [],
    streamingReasoning: "",
    streamingReasoningChunks: [],
    streamingToolCalls: {},
    streamingModelKey: null,
    streamingKind: null,
    isStreaming: false,
  }),

  // Threads
  threads: [],
  setThreads: (threads) => set({ threads }),

  // Messages
  messages: [],
  setMessages: (messages) => set({ messages }),
  addMessage: (message) =>
    set((state) => ({
      messages: [...state.messages, message],
      scrollTrigger: state.scrollTrigger + 1,  // Trigger scroll when UI-only message added
    })),

  // Streaming content - use chunks array for O(1) append
  // Components should use streamingContentChunks.join("") for display (memoized)
  streamingContent: "",  // Kept for backwards compat, set on clear only
  streamingContentChunks: [],
  setStreamingContent: (content) => set({
    streamingContent: content,
    streamingContentChunks: content ? [content] : [],
  }),
  appendStreamingContent: (chunk) =>
    set((state) => ({
      // O(1) array spread - no join here, components memoize the join
      streamingContentChunks: [...state.streamingContentChunks, chunk],
    })),

  // Streaming reasoning - use chunks array for O(1) append
  streamingReasoning: "",  // Kept for backwards compat, set on clear only
  streamingReasoningChunks: [],
  setStreamingReasoning: (content) => set({
    streamingReasoning: content,
    streamingReasoningChunks: content ? [content] : [],
  }),
  appendStreamingReasoning: (chunk) =>
    set((state) => ({
      streamingReasoningChunks: [...state.streamingReasoningChunks, chunk],
    })),

  // Streaming tool calls
  streamingToolCalls: {},
  setStreamingToolCalls: (tcs) => set({ streamingToolCalls: tcs }),
  appendToolCallArguments: (tcId, name, argsDelta) =>
    set((state) => {
      const existing = state.streamingToolCalls[tcId] || { name: "", arguments: "" };
      return {
        streamingToolCalls: {
          ...state.streamingToolCalls,
          [tcId]: {
            name: name || existing.name,
            arguments: existing.arguments + argsDelta,
          },
        },
      };
    }),

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

  // UI state
  isStreaming: false,
  setIsStreaming: (streaming) => set({ isStreaming: streaming }),
  streamingModelKey: null,
  setStreamingModelKey: (key) => set({ streamingModelKey: key }),
  streamingKind: null,
  setStreamingKind: (kind) => set({ streamingKind: kind }),

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
  showBorders: true,
  toggleBorders: () => set((state) => ({ showBorders: !state.showBorders })),
  enterMode: "send",
  setEnterMode: (mode) => set({ enterMode: mode }),

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

  // Scroll trigger - incremented when UI-only messages are added
  scrollTrigger: 0,
  triggerScroll: () => set((state) => ({ scrollTrigger: state.scrollTrigger + 1 })),
}));

// Initialize theme from localStorage on client side
if (typeof window !== "undefined") {
  const savedTheme = localStorage.getItem("eggw-theme");
  if (savedTheme) {
    document.documentElement.setAttribute("data-theme", savedTheme);
    useAppStore.setState({ theme: savedTheme });
  }
}
