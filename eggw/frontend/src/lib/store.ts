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

  // Streaming content
  streamingContent: string;
  setStreamingContent: (content: string) => void;
  appendStreamingContent: (chunk: string) => void;

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
}

export const useAppStore = create<AppState>((set) => ({
  // Current thread
  currentThreadId: null,
  setCurrentThreadId: (id) => set({ currentThreadId: id }),

  // Threads
  threads: [],
  setThreads: (threads) => set({ threads }),

  // Messages
  messages: [],
  setMessages: (messages) => set({ messages }),
  addMessage: (message) =>
    set((state) => ({ messages: [...state.messages, message] })),

  // Streaming content
  streamingContent: "",
  setStreamingContent: (content) => set({ streamingContent: content }),
  appendStreamingContent: (chunk) =>
    set((state) => ({ streamingContent: state.streamingContent + chunk })),

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
}));
