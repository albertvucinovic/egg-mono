import type { AttachmentUploadResponse, ContentPart, EggMessageContent } from "./contentParts";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function readErrorDetail(res: Response, fallback: string): Promise<string> {
  try {
    const payload = await res.json();
    if (typeof payload?.detail === "string") return payload.detail;
    if (payload?.detail !== undefined) return JSON.stringify(payload.detail);
  } catch {
    // Keep generic message.
  }
  return fallback;
}

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

export async function fetchMessages(threadId: string, options: { limit?: number; beforeId?: string } = {}) {
  const params = new URLSearchParams();
  if (options.limit && options.limit > 0) params.set("limit", String(Math.trunc(options.limit)));
  if (options.beforeId) params.set("before_id", options.beforeId);
  const query = params.toString();
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/messages${query ? `?${query}` : ""}`);
  if (!res.ok) throw new Error("Failed to fetch messages");
  return res.json();
}

export async function sendMessage(threadId: string, content: EggMessageContent) {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  if (!res.ok) throw new Error("Failed to send message");
  return res.json();
}

export async function uploadAttachment(threadId: string, file: File): Promise<AttachmentUploadResponse> {
  const formData = new FormData();
  formData.append("file", file);
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/attachments`, {
    method: "POST",
    body: formData,
  });
  if (!res.ok) {
    throw new Error(await readErrorDetail(res, "Failed to upload attachment"));
  }
  return res.json();
}

export function attachmentUrl(
  threadId: string,
  inputId: string,
  options: { descendantThreadId?: string; download?: boolean } = {},
): string {
  const params = new URLSearchParams();
  if (options.descendantThreadId) params.set("descendant_thread_id", options.descendantThreadId);
  if (options.download) params.set("download", "true");
  const query = params.toString();
  return `${API_BASE}/api/threads/${encodeURIComponent(threadId)}/attachments/${encodeURIComponent(inputId)}${query ? `?${query}` : ""}`;
}

export async function promoteProviderOutput(
  threadId: string,
  artifactId: string,
  options: { descendantThreadId?: string } = {},
): Promise<AttachmentUploadResponse> {
  const params = new URLSearchParams();
  if (options.descendantThreadId) params.set("descendant_thread_id", options.descendantThreadId);
  const query = params.toString();
  const res = await fetch(
    `${API_BASE}/api/threads/${encodeURIComponent(threadId)}/provider-output/${encodeURIComponent(artifactId)}/promote${query ? `?${query}` : ""}`,
    { method: "POST" },
  );
  if (!res.ok) {
    throw new Error(await readErrorDetail(res, "Failed to use provider output as attachment"));
  }
  return res.json();
}

export interface ImageGenerationRequest {
  prompt: string;
  model?: string;
  backend?: string;
  n?: number;
  size?: string;
  quality?: string;
  output_format?: string;
  background?: string;
}

export interface ImageGenerationResponse {
  message_id: string;
  prompt: string;
  model_key: string;
  provider_name: string;
  model_name: string;
  metadata: Record<string, unknown>[];
  content_parts: ContentPart[];
  content_text: string;
  response_metadata: Record<string, unknown>;
}

export async function generateThreadImage(
  threadId: string,
  request: ImageGenerationRequest,
): Promise<ImageGenerationResponse> {
  const res = await fetch(`${API_BASE}/api/threads/${encodeURIComponent(threadId)}/image-generation`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!res.ok) {
    throw new Error(await readErrorDetail(res, "Failed to generate image"));
  }
  return res.json();
}

export function providerOutputUrl(
  threadId: string,
  artifactId: string,
  options: { descendantThreadId?: string; download?: boolean } = {},
): string {
  const params = new URLSearchParams();
  if (options.descendantThreadId) params.set("descendant_thread_id", options.descendantThreadId);
  if (options.download) params.set("download", "true");
  const query = params.toString();
  return `${API_BASE}/api/threads/${encodeURIComponent(threadId)}/provider-output/${encodeURIComponent(artifactId)}${query ? `?${query}` : ""}`;
}

export interface CommandResponse {
  success: boolean;
  message: string;
  data?: Record<string, any>;
}

export async function executeCommand(
  threadId: string,
  command: string,
  stagedAttachments: ContentPart[] = [],
): Promise<CommandResponse> {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/command`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command, staged_attachments: stagedAttachments }),
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

export async function fetchImageGenerationModels() {
  const res = await fetch(`${API_BASE}/api/image-models`);
  if (!res.ok) throw new Error("Failed to fetch image generation models");
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
  outputDecision?: string,
  decision?: string  // For special decisions like 'all-in-turn'
) {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/tools/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      tool_call_id: toolCallId,
      approved,
      output_decision: outputDecision,
      decision,
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

// --- Auth (ChatGPT OAuth) ---

export interface AuthStatus {
  logged_in: boolean;
  expires_at: number | null;
  auth_mode: string | null;
}

export async function fetchAuthStatus(): Promise<AuthStatus> {
  const res = await fetch(`${API_BASE}/api/auth/status`);
  if (!res.ok) throw new Error("Failed to fetch auth status");
  return res.json();
}

export async function triggerLogin(): Promise<{ success: boolean; message: string }> {
  const res = await fetch(`${API_BASE}/api/auth/login`, { method: "POST" });
  if (!res.ok) throw new Error("Failed to trigger login");
  return res.json();
}

export async function triggerLogout(): Promise<{ success: boolean; message: string }> {
  const res = await fetch(`${API_BASE}/api/auth/logout`, { method: "POST" });
  if (!res.ok) throw new Error("Failed to trigger logout");
  return res.json();
}
