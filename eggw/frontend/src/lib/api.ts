const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function fetchThreads() {
  const res = await fetch(`${API_BASE}/api/threads`);
  if (!res.ok) throw new Error("Failed to fetch threads");
  return res.json();
}

export async function fetchRootThreads() {
  const res = await fetch(`${API_BASE}/api/threads/roots`);
  if (!res.ok) throw new Error("Failed to fetch root threads");
  return res.json();
}

export async function fetchThread(threadId: string) {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}`);
  if (!res.ok) throw new Error("Failed to fetch thread");
  return res.json();
}

export async function fetchThreadChildren(threadId: string) {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/children`);
  if (!res.ok) throw new Error("Failed to fetch children");
  return res.json();
}

export async function createThread(data: {
  name?: string;
  parent_id?: string;
  model_key?: string;
  context?: string;
}) {
  const res = await fetch(`${API_BASE}/api/threads`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to create thread");
  return res.json();
}

export async function deleteThread(threadId: string, deleteSubtree = false) {
  const res = await fetch(
    `${API_BASE}/api/threads/${threadId}?delete_subtree=${deleteSubtree}`,
    { method: "DELETE" }
  );
  if (!res.ok) throw new Error("Failed to delete thread");
  return res.json();
}

export async function renameThread(threadId: string, name: string) {
  const res = await fetch(
    `${API_BASE}/api/threads/${threadId}?name=${encodeURIComponent(name)}`,
    { method: "PATCH" }
  );
  if (!res.ok) throw new Error("Failed to rename thread");
  return res.json();
}

export async function duplicateThread(threadId: string, name?: string) {
  const res = await fetch(
    `${API_BASE}/api/threads/${threadId}/duplicate${name ? `?name=${encodeURIComponent(name)}` : ""}`,
    { method: "POST" }
  );
  if (!res.ok) throw new Error("Failed to duplicate thread");
  return res.json();
}

export async function openThread(threadId: string) {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/open`, {
    method: "POST",
  });
  if (!res.ok) throw new Error("Failed to open thread");
  return res.json();
}

export async function interruptThread(threadId: string) {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/interrupt`, {
    method: "POST",
  });
  if (!res.ok) throw new Error("Failed to interrupt thread");
  return res.json();
}

export async function fetchMessages(threadId: string) {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/messages`);
  if (!res.ok) throw new Error("Failed to fetch messages");
  return res.json();
}

export async function sendMessage(threadId: string, content: string) {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  if (!res.ok) throw new Error("Failed to send message");
  return res.json();
}

export interface CommandResponse {
  success: boolean;
  message: string;
  data?: Record<string, any>;
}

export async function executeCommand(threadId: string, command: string): Promise<CommandResponse> {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/command`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command }),
  });
  if (!res.ok) throw new Error("Failed to execute command");
  return res.json();
}

export function isCommand(text: string): boolean {
  const trimmed = text.trim();
  return trimmed.startsWith('/') || trimmed.startsWith('$');
}

export async function fetchModels() {
  const res = await fetch(`${API_BASE}/api/models`);
  if (!res.ok) throw new Error("Failed to fetch models");
  return res.json();
}

export async function setThreadModel(threadId: string, modelKey: string) {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/model`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model_key: modelKey }),
  });
  if (!res.ok) throw new Error("Failed to set model");
  return res.json();
}

export async function fetchToolCalls(threadId: string) {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/tools`);
  if (!res.ok) throw new Error("Failed to fetch tool calls");
  return res.json();
}

export async function approveTool(
  threadId: string,
  toolCallId: string,
  approved: boolean,
  outputDecision?: string
) {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/tools/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      tool_call_id: toolCallId,
      approved,
      output_decision: outputDecision,
    }),
  });
  if (!res.ok) throw new Error("Failed to approve tool");
  return res.json();
}

export async function fetchTokenStats(threadId: string) {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/stats`);
  if (!res.ok) throw new Error("Failed to fetch stats");
  return res.json();
}

export async function fetchThreadSettings(threadId: string) {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/settings`);
  if (!res.ok) throw new Error("Failed to fetch settings");
  return res.json();
}

export async function fetchThreadState(threadId: string) {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/state`);
  if (!res.ok) throw new Error("Failed to fetch state");
  return res.json();
}

export async function setAutoApproval(threadId: string, enabled: boolean) {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/settings/auto-approval?enabled=${enabled}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error("Failed to set auto-approval");
  return res.json();
}

export function createEventSource(threadId: string) {
  return new EventSource(`${API_BASE}/api/threads/${threadId}/events`);
}

export function createWebSocket(threadId: string) {
  const wsBase = API_BASE.replace(/^http/, "ws");
  return new WebSocket(`${wsBase}/ws/${threadId}`);
}

export interface AutocompleteSuggestion {
  display: string;
  insert: string;
  replace?: number;
  meta?: string;
}

export async function fetchAutocomplete(
  line: string,
  cursor: number,
  threadId?: string
): Promise<AutocompleteSuggestion[]> {
  const params = new URLSearchParams({
    line,
    cursor: cursor.toString(),
  });
  if (threadId) {
    params.set("thread_id", threadId);
  }
  const res = await fetch(`${API_BASE}/api/autocomplete?${params}`);
  if (!res.ok) return [];
  const data = await res.json();
  return data.suggestions || [];
}

export interface SandboxStatus {
  enabled: boolean;
  effective: boolean;
  available: boolean;
  provider?: string;
  config_source?: string;
  config_path?: string;
  warning?: string;
  user_control_enabled?: boolean;
  error?: string;
}

export async function fetchSandboxStatus(threadId: string): Promise<SandboxStatus> {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/sandbox`);
  if (!res.ok) {
    return { enabled: false, effective: false, available: false };
  }
  return res.json();
}

export async function setSandboxEnabled(threadId: string, enabled: boolean): Promise<SandboxStatus> {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/sandbox?enabled=${enabled}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error("Failed to set sandbox");
  return res.json();
}
