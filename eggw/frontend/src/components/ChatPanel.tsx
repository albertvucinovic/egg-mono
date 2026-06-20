"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode, type WheelEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import rehypeRaw from "rehype-raw";
import "katex/dist/katex.min.css";
import { attachmentUrl, fetchMessages, promoteProviderOutput, providerOutputUrl } from "@/lib/api";
import { useAppStore, type Message, type DisplayVerbosity, type StreamingToolTimeout } from "@/lib/store";
import {
  artifactFilename,
  artifactPlaceholder,
  attachmentFilename,
  attachmentPlaceholder,
  contentToPlainText,
  formatBytes,
  isArtifactPart,
  isAttachmentPart,
  isContentPartArray,
  isImageContentPart,
  isTextPart,
  type AttachmentContentPart,
  type ContentPart,
} from "@/lib/contentParts";
import { formatStreamingTps, formatTokenCount } from "@/lib/tps";
import clsx from "clsx";

const STICKY_BOTTOM_THRESHOLD_PX = 4;

/**
 * Preprocess content to convert various LaTeX-style delimiters to markdown math syntax.
 * Supports:
 * - \[...\] → $$...$$ (display math)
 * - \(...\) → $...$ (inline math)
 * - [ ... ] with LaTeX commands → $$...$$ (common AI output format)
 */
function preprocessLatex(content: string): string {
  if (!content) return content;

  // Convert \[...\] to $$...$$ for display math
  let processed = content.replace(/\\\[([\s\S]*?)\\\]/g, (_, math) => `$$${math}$$`);

  // Convert \(...\) to $...$ for inline math
  processed = processed.replace(/\\\(([\s\S]*?)\\\)/g, (_, math) => `$${math}$`);

  // Convert [ ... ] when it starts with a LaTeX command (common AI output format)
  // This handles multiline content like [ \begin{aligned} ... \end{aligned} ]
  // Match a standalone [ followed by whitespace and backslash, capture until
  // closing ]. Do not treat brackets that are part of LaTeX commands (for
  // example \left[ ... \right]) as markdown math delimiters.
  processed = processed.replace(
    /(^|[^\w\\])\[\s*(\\[\s\S]*?)\s*\]/g,
    (match, prefix, math) => {
      // Only convert if it looks like LaTeX (contains common LaTeX commands)
      if (/\\(?:begin|end|frac|sum|int|prod|lim|nabla|partial|sqrt|text|mathbf|mathrm|left|right|aligned|equation|matrix|cases)/.test(math)) {
        return `${prefix}$$${math}$$`;
      }
      return match; // Keep original if not LaTeX
    }
  );

  return processed;
}

function toolStreamSavingText(name: string, frames: number = 0): string {
  const framesList = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
  const glyph = framesList[Math.max(0, frames) % framesList.length] || "…";
  return `${glyph} tool${name ? ` ${name}` : ""}: preview limit reached; saving output only`;
}

function toolTimeoutCountdown(timeout: StreamingToolTimeout | undefined, nowMs: number): string | null {
  if (!timeout) return null;
  const limit = Number(timeout.timeoutSec);
  const startedAtMs = Number(timeout.startedAtMs);
  if (!Number.isFinite(limit) || limit <= 0 || !Number.isFinite(startedAtMs) || startedAtMs <= 0) return null;
  const elapsedSec = Math.max(0, (nowMs - startedAtMs) / 1000);
  const remainingSec = Math.max(0, limit - elapsedSec);
  return `timeout in ${remainingSec.toFixed(0)}s (limit ${limit.toFixed(0)}s)`;
}

type HiddenDetailKind = "reasoning" | "tool_calls" | "tool_results";

interface HiddenDetail {
  kind: HiddenDetailKind;
  header: string;
  name?: string;
  tokens?: number;
}

function oneLinePreview(value: unknown, maxChars = 160): string {
  let raw: string;
  if (typeof value === "string") {
    raw = value;
  } else {
    try {
      raw = JSON.stringify(value);
    } catch {
      raw = String(value ?? "");
    }
  }
  const preview = raw.replace(/\s+/g, " ").trim();
  return preview.length > maxChars ? `${preview.slice(0, maxChars - 3).trimEnd()}...` : preview;
}

function stringRecordEntries(value: Record<string, unknown> | undefined): Array<[string, string]> {
  if (!value || typeof value !== "object") return [];
  return Object.entries(value)
    .map(([key, raw]) => {
      let text: string;
      if (typeof raw === "string") {
        text = raw;
      } else {
        try {
          text = JSON.stringify(raw, null, 2);
        } catch {
          text = String(raw ?? "");
        }
      }
      return [key, text] as [string, string];
    })
    .filter(([, text]) => text.length > 0);
}

function streamedMetadataHiddenHeader(message: Message, label: string, text: string): string {
  return `${messageMetadataText(message, label)} | ${text.length.toLocaleString()} chars`;
}

function toolCallName(tc: any): string {
  return tc?.name || tc?.function?.name || "unknown";
}

function toolCallArgs(tc: any): unknown {
  const args = tc?.arguments ?? tc?.function?.arguments;
  if (typeof args === "string") {
    try {
      return JSON.parse(args);
    } catch {
      return args;
    }
  }
  return args;
}

function messageTimestampText(timestamp?: string): string | null {
  if (!timestamp) return null;
  try {
    return new Date(timestamp).toLocaleString(undefined, {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return timestamp;
  }
}

function messageMetadataText(message: Message, label: string): string {
  const parts = [label];
  if (message.model_key) parts.push(`model: ${message.model_key}`);
  const tokenText = formatTokenCount(message.tokens);
  if (tokenText) parts.push(tokenText);
  const tpsText = formatStreamingTps(message.tps);
  if (tpsText) parts.push(tpsText);
  const tsText = messageTimestampText(message.timestamp);
  if (tsText) parts.push(tsText);
  if (message.id && !message.id.startsWith('temp-')) parts.push(`msg_id: ${message.id}`);
  if (message.tool_call_id) parts.push(`tool_call_id: ${message.tool_call_id}`);
  return parts.join(" | ");
}

function isImportantSystemMessage(message: Message): boolean {
  if (message.role !== "system") return false;
  if (message.recovery_notice) return true;
  if (message.id?.startsWith("cmd-")) return true;
  const content = contentToPlainText(message.content, message.content_text || "").trim().toLowerCase();
  return content.startsWith("llm error:") ||
    content.startsWith("error:") ||
    content.startsWith("usage:") ||
    content.startsWith("unknown command:") ||
    content.startsWith("/");
}

function plural(count: number, singular: string, pluralText?: string): string {
  return `${count} ${count === 1 ? singular : (pluralText || `${singular}s`)}`;
}

function hiddenSummaryText(details: HiddenDetail[]): string {
  const counts: Record<HiddenDetailKind, number> = { reasoning: 0, tool_calls: 0, tool_results: 0 };
  details.forEach((detail) => { counts[detail.kind] += 1; });
  const parts: string[] = [];
  if (counts.tool_calls > 0) parts.push(`Executed ${plural(counts.tool_calls, "tool")}`);
  if (counts.tool_results > 0) parts.push(`got ${plural(counts.tool_results, "tool result")}`);
  if (counts.reasoning > 0) parts.push(plural(counts.reasoning, "reasoning block"));
  const tokenTotal = details.reduce((total, detail) => total + (Number.isFinite(detail.tokens || 0) ? Math.max(0, Math.trunc(detail.tokens || 0)) : 0), 0);
  if (tokenTotal > 0) parts.push(`total tokens ${tokenTotal.toLocaleString()}`);
  const toolNames = details
    .map((detail) => detail.name)
    .filter((name): name is string => Boolean(name));
  const uniqueToolNames = Array.from(new Set(toolNames));
  const summary = parts.join(", ") || "Hidden details";
  return uniqueToolNames.length ? `${summary}. Tools: ${uniqueToolNames.join(", ")}.` : `${summary}.`;
}

function HiddenDetailsBlock({ details, showBorders = true }: { details: HiddenDetail[]; showBorders?: boolean }) {
  if (!details.length) return null;
  return (
    <div
      className={`rounded p-3 mb-3 ${showBorders ? 'border' : ''}`}
      style={{ background: "var(--tool-msg-bg)", borderColor: "var(--tool-msg-border)", color: "var(--tool-msg-text, var(--foreground))" }}
      data-testid="hidden-details"
    >
      <div className="text-sm font-medium mb-2">{hiddenSummaryText(details)}</div>
      <div className="space-y-1 text-xs font-mono" style={{ color: "var(--muted)" }}>
        {details.map((detail, idx) => (
          <div key={`${detail.kind}-${idx}`}>{detail.header}</div>
        ))}
      </div>
    </div>
  );
}

function ContentPartsView({
  parts,
  showBorders = true,
  onStageAttachment,
}: {
  parts: ContentPart[];
  showBorders?: boolean;
  onStageAttachment?: (attachment: AttachmentContentPart) => void;
}) {
  const currentThreadId = useAppStore((state) => state.currentThreadId);
  const addSystemLog = useAppStore((state) => state.addSystemLog);
  const [promotingArtifactIds, setPromotingArtifactIds] = useState<Record<string, boolean>>({});

  const handleUseAsAttachment = useCallback(async (part: Extract<ContentPart, { type: "artifact" }>) => {
    if (!currentThreadId || !part.artifact_id || !onStageAttachment) return;
    const descendantThreadId = part.owner_thread_id && part.owner_thread_id !== currentThreadId
      ? part.owner_thread_id
      : undefined;
    setPromotingArtifactIds((prev) => ({ ...prev, [part.artifact_id]: true }));
    try {
      const promoted = await promoteProviderOutput(currentThreadId, part.artifact_id, { descendantThreadId });
      onStageAttachment(promoted.content_part);
      addSystemLog(`Staged provider output ${part.artifact_id} as attachment ${promoted.input_id}`, "success");
    } catch (error) {
      addSystemLog(error instanceof Error ? error.message : "Failed to use provider output as attachment", "error");
    } finally {
      setPromotingArtifactIds((prev) => {
        const next = { ...prev };
        delete next[part.artifact_id];
        return next;
      });
    }
  }, [addSystemLog, currentThreadId, onStageAttachment]);

  return (
    <div className="space-y-2">
      {parts.map((part, idx) => {
        if (isTextPart(part)) {
          return (
            <div key={`text-${idx}`} className="whitespace-pre-wrap text-sm">
              {part.text}
            </div>
          );
        }
        if (isAttachmentPart(part)) {
          const canLink = Boolean(currentThreadId && part.input_id);
          const descendantThreadId = canLink && part.owner_thread_id && part.owner_thread_id !== currentThreadId
            ? part.owner_thread_id
            : undefined;
          const openUrl = canLink
            ? attachmentUrl(currentThreadId!, part.input_id, { descendantThreadId })
            : null;
          const downloadUrl = canLink
            ? attachmentUrl(currentThreadId!, part.input_id, { descendantThreadId, download: true })
            : null;
          return (
            <div
              key={`${part.input_id || "attachment"}-${idx}`}
              className={`rounded p-3 text-sm ${showBorders ? "border" : ""}`}
              style={{ background: "var(--code-bg)", borderColor: "var(--panel-border)", color: "var(--foreground)" }}
              title={attachmentPlaceholder(part)}
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">Attachment</span>
                <span>{attachmentFilename(part)}</span>
                <span className="rounded px-1.5 py-0.5 text-xs" style={{ background: "var(--panel-bg)", color: "var(--muted)" }}>
                  {part.presentation || "file"}
                </span>
                <span className="text-xs" style={{ color: "var(--muted)" }}>{part.mime_type || "application/octet-stream"}</span>
                <span className="text-xs" style={{ color: "var(--muted)" }}>{formatBytes(part.size_bytes)}</span>
              </div>
              <div className="mt-1 font-mono text-xs" style={{ color: "var(--muted)" }}>
                {attachmentPlaceholder(part)}
              </div>
              {openUrl && isImageContentPart(part) && (
                <a href={openUrl} target="_blank" rel="noreferrer" className="mt-2 block w-fit" aria-label={`Open preview of ${attachmentFilename(part)}`}>
                  <img
                    src={openUrl}
                    alt={`Preview of ${attachmentFilename(part)}`}
                    loading="lazy"
                    decoding="async"
                    data-testid="attachment-preview"
                    className={`max-h-48 max-w-full rounded object-contain ${showBorders ? "border" : ""}`}
                    style={{ borderColor: "var(--panel-border)", background: "var(--panel-bg)" }}
                    onError={(event) => {
                      event.currentTarget.style.display = "none";
                    }}
                  />
                </a>
              )}
              {openUrl && downloadUrl && (
                <div className="mt-2 flex flex-wrap gap-3 text-xs">
                  <a href={openUrl} target="_blank" rel="noreferrer" className="underline" style={{ color: "var(--accent)" }}>
                    Open
                  </a>
                  <a href={downloadUrl} className="underline" style={{ color: "var(--accent)" }}>
                    Download
                  </a>
                </div>
              )}
            </div>
          );
        }
        if (isArtifactPart(part)) {
          const canLink = Boolean(currentThreadId && part.artifact_id);
          const canPromote = Boolean(canLink && onStageAttachment);
          const descendantThreadId = canLink && part.owner_thread_id && part.owner_thread_id !== currentThreadId
            ? part.owner_thread_id
            : undefined;
          const openUrl = canLink
            ? providerOutputUrl(currentThreadId!, part.artifact_id, { descendantThreadId })
            : null;
          const downloadUrl = canLink
            ? providerOutputUrl(currentThreadId!, part.artifact_id, { descendantThreadId, download: true })
            : null;
          return (
            <div
              key={`${part.artifact_id || "artifact"}-${idx}`}
              className={`rounded p-3 text-sm ${showBorders ? "border" : ""}`}
              style={{ background: "var(--code-bg)", borderColor: "var(--panel-border)", color: "var(--foreground)" }}
              title={artifactPlaceholder(part)}
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">Provider artifact</span>
                <span>{artifactFilename(part)}</span>
                <span className="rounded px-1.5 py-0.5 text-xs" style={{ background: "var(--panel-bg)", color: "var(--muted)" }}>
                  {part.presentation || "file"}
                </span>
                <span className="text-xs" style={{ color: "var(--muted)" }}>{part.mime_type || "application/octet-stream"}</span>
                <span className="text-xs" style={{ color: "var(--muted)" }}>{formatBytes(part.size_bytes)}</span>
              </div>
              <div className="mt-1 font-mono text-xs" style={{ color: "var(--muted)" }}>
                {artifactPlaceholder(part)}
              </div>
              {openUrl && isImageContentPart(part) && (
                <a href={openUrl} target="_blank" rel="noreferrer" className="mt-2 block w-fit" aria-label={`Open preview of ${artifactFilename(part)}`}>
                  <img
                    src={openUrl}
                    alt={`Preview of ${artifactFilename(part)}`}
                    loading="lazy"
                    decoding="async"
                    data-testid="provider-artifact-preview"
                    className={`max-h-48 max-w-full rounded object-contain ${showBorders ? "border" : ""}`}
                    style={{ borderColor: "var(--panel-border)", background: "var(--panel-bg)" }}
                    onError={(event) => {
                      event.currentTarget.style.display = "none";
                    }}
                  />
                </a>
              )}
              {openUrl && downloadUrl && (
                <div className="mt-2 flex flex-wrap gap-3 text-xs">
                  <a href={openUrl} target="_blank" rel="noreferrer" className="underline" style={{ color: "var(--accent)" }}>
                    Open
                  </a>
                  <a href={downloadUrl} className="underline" style={{ color: "var(--accent)" }}>
                    Download
                  </a>
                  {canPromote && (
                    <button
                      type="button"
                      onClick={() => handleUseAsAttachment(part)}
                      disabled={Boolean(promotingArtifactIds[part.artifact_id])}
                      className="underline disabled:cursor-not-allowed disabled:opacity-50"
                      style={{ color: "var(--accent)" }}
                    >
                      {promotingArtifactIds[part.artifact_id] ? "Staging…" : "Use as attachment"}
                    </button>
                  )}
                </div>
              )}
            </div>
          );
        }
        return (
          <pre key={`unknown-${idx}`} className="text-xs p-2 rounded overflow-auto" style={{ background: "var(--code-bg)", color: "var(--foreground)" }}>
            {JSON.stringify(part, null, 2)}
          </pre>
        );
      })}
    </div>
  );
}

interface MessageBlockProps {
  message: Message;
  showBorders?: boolean;
  displayVerbosity?: DisplayVerbosity;
  onStageAttachment?: (attachment: AttachmentContentPart) => void;
}

function CompactionMarker({ message }: { message: Message }) {
  const startId = message.start_msg_id || "";
  const startShort = startId.length >= 8 ? startId.slice(-8) : startId;
  const markerColor = "#ef4444";
  const details = [
    message.marker_event_seq ? `marker #${message.marker_event_seq}` : null,
    message.start_event_seq ? `start event #${message.start_event_seq}` : null,
    message.selector ? `selector ${message.selector}` : null,
    message.created_by ? `by ${message.created_by}` : null,
  ].filter(Boolean).join(" · ");

  return (
    <div className="my-4 flex items-center gap-3" data-testid="compaction-marker">
      <div className="h-px flex-1" style={{ background: markerColor }} />
      <div
        className="rounded-full px-3 py-1 text-xs font-medium text-center"
        style={{
          color: markerColor,
          border: `1px solid ${markerColor}`,
          background: "rgba(239, 68, 68, 0.10)",
        }}
        title={contentToPlainText(message.content, message.content_text || "") || undefined}
      >
        Compaction boundary: API context now starts at {startShort ? `msg_${startShort}` : "the selected message"}
        {details && <span className="ml-2 font-normal" style={{ color: "var(--muted)" }}>({details})</span>}
      </div>
      <div className="h-px flex-1" style={{ background: markerColor }} />
    </div>
  );
}

function MessageBlock({ message, showBorders = true, displayVerbosity = "max", onStageAttachment }: MessageBlockProps) {
  if (message.kind === "compaction_marker" || message.role === "compaction_marker") {
    return <CompactionMarker message={message} />;
  }

  // Use CSS variables for theme-aware colors
  // Text colors use fallback to --foreground for themes that don't define *-text vars
  const roleStyles: Record<string, React.CSSProperties> = {
    user: { background: "var(--user-msg-bg)", borderColor: "var(--user-msg-border)", color: "var(--user-msg-text, var(--foreground))" },
    assistant: { background: "var(--assistant-msg-bg)", borderColor: "var(--assistant-msg-border)", color: "var(--assistant-msg-text, var(--foreground))" },
    assistant_note: { background: "var(--assistant-msg-bg)", borderColor: "#d946ef", color: "#f0abfc" },
    system: { background: "var(--system-msg-bg)", borderColor: "var(--system-msg-border)", color: "var(--system-msg-text, var(--foreground))" },
    tool: { background: "var(--tool-msg-bg)", borderColor: "var(--tool-msg-border)", color: "var(--tool-msg-text, var(--foreground))" },
  };

  const roleLabels: Record<string, string> = {
    user: "User",
    assistant: "Assistant",
    assistant_note: "Assistant Note",
    system: "Command",
    tool: "Tool Result",
  };

  const displayRole = message.answer_user_preserve_turn && message.role === "assistant" ? "assistant_note" : message.role;
  const baseRoleLabel = message.recovery_notice && message.role === "system"
    ? "Continue Status"
    : roleLabels[displayRole] || displayRole;
  const roleLabel = message.role === "tool" && message.name ? `${baseRoleLabel}: ${message.name}` : baseRoleLabel;

  // Check if this is a shell command (starts with $ or $$)
  // Handle cases: "$ cmd", "$$ cmd", "$cmd" (no space)
  const contentText = contentToPlainText(message.content, message.content_text || "");
  const stringContent = typeof message.content === "string" ? message.content : contentText;
  const isShellCommand = message.role === "user" &&
    typeof message.content === "string" && message.content.match(/^\$\$?\s*\S/);

  // Check if this is a system/command message (should render as monospace)
  const isCommandOutput = message.role === "system";

  // For tool messages, check if content is long
  const isLongToolOutput = message.role === "tool" && contentText.length > 500;

  const shellStyle: React.CSSProperties = { background: "var(--code-bg)", borderColor: "var(--panel-border)" };

  const messageTps = formatStreamingTps(message.tps);
  const tokenText = formatTokenCount(message.tokens);
  const toolCalls = message.tool_calls || [];
  const toolStreamEntries = stringRecordEntries(message.tool_stream);
  const toolCallStreamEntries = stringRecordEntries(message.tool_calls_stream);
  const showReasoningBlock = Boolean(message.reasoning) && displayVerbosity !== "min";
  const hideReasoningBody = displayVerbosity === "medium";
  const hideToolBody = (displayVerbosity === "medium" || displayVerbosity === "min") && message.role === "tool";
  const showContent = Boolean(contentText) && !hideToolBody;
  const showToolCalls = toolCalls.length > 0 && displayVerbosity !== "min";
  const showStreamedMetadata = displayVerbosity !== "min" && (toolStreamEntries.length > 0 || toolCallStreamEntries.length > 0);

  return (
    <div
      className={`rounded p-3 mb-3 ${showBorders ? 'border' : ''}`}
      style={isShellCommand ? shellStyle : (roleStyles[displayRole] || shellStyle)}
    >
      {/* Header */}
      <div className="flex items-center gap-2 mb-2 text-xs flex-wrap" style={{ color: "var(--muted)" }}>
        <span className="font-medium" style={roleStyles[displayRole] ? { color: roleStyles[displayRole].color } : { color: "var(--foreground)" }}>
          {isShellCommand ? "Shell" : roleLabel}
        </span>
        {message.model_key && (
          <span style={{ color: "var(--muted)" }}>({message.model_key})</span>
        )}
        {tokenText && (
          <span style={{ color: "var(--muted)" }}>({tokenText})</span>
        )}
        {messageTps && (
          <span style={{ color: "var(--muted)" }}>({messageTps})</span>
        )}
        {message.timestamp && (
          <span className="font-mono" style={{ color: "var(--muted)" }}>
            {new Date(message.timestamp).toLocaleString(undefined, {
              year: 'numeric',
              month: '2-digit',
              day: '2-digit',
              hour: '2-digit',
              minute: '2-digit',
              second: '2-digit',
            })}
          </span>
        )}
        {message.id && message.id.length >= 8 && !message.id.startsWith('temp-') && (
          <span
            className="font-mono cursor-pointer hover:underline"
            style={{ color: "var(--muted)" }}
            title={`Click to copy: ${message.id}`}
            aria-label={`Message id ${message.id}; click to copy`}
            data-testid="message-id"
            onClick={() => {
              navigator.clipboard.writeText(message.id);
            }}
          >
            [msg_id: {message.id}]
          </span>
        )}
        {message.tool_call_id && (
          <span className="font-mono" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>
            ← {message.tool_call_id.slice(-8)}
          </span>
        )}
      </div>

      {/* Reasoning (collapsible) */}
      {showReasoningBlock && (
        <details
          open={hideReasoningBody ? false : undefined}
          className={`mb-2 rounded p-2 ${showBorders ? 'border' : ''}`}
          style={{ background: "var(--reasoning-bg)", borderColor: "var(--reasoning-border)" }}
        >
          <summary className="cursor-pointer text-sm" style={{ color: "var(--reasoning-text, var(--reasoning-border))" }}>
            Reasoning
            {hideReasoningBody && (
              <span className="ml-2 text-xs font-mono" style={{ color: "var(--muted)" }}>
                {messageMetadataText(message, "Reasoning")}
              </span>
            )}
          </summary>
          {!hideReasoningBody && (
            <div className="mt-2 text-sm whitespace-pre-wrap" style={{ color: "var(--reasoning-text, var(--foreground))", opacity: 0.9 }}>
              {message.reasoning}
            </div>
          )}
        </details>
      )}

      {/* Content */}
      {showContent && (
        <>
          {/* Shell command display */}
          {isShellCommand ? (
            <pre className="text-sm font-mono p-2 rounded overflow-auto" style={{ background: "var(--code-bg)", color: "var(--accent)" }}>
              {stringContent}
            </pre>
          ) : isCommandOutput ? (
            /* Command output (system messages) - monospace for tree/list formatting */
            <pre className="text-sm font-mono p-2 rounded overflow-auto whitespace-pre-wrap" style={{ background: "var(--code-bg)", color: "var(--system-msg-text, var(--foreground))" }}>
              {contentText}
            </pre>
          ) : isContentPartArray(message.content) ? (
            <ContentPartsView parts={message.content} showBorders={showBorders} onStageAttachment={onStageAttachment} />
          ) : message.role === "tool" ? (
            /* Tool output - collapsible if long */
            isLongToolOutput ? (
              <details className={`rounded ${showBorders ? 'border' : ''}`} style={{ background: "var(--code-bg)", borderColor: "var(--tool-msg-border)" }}>
                <summary className="cursor-pointer p-2 text-sm" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>
                  Output ({contentText.length.toLocaleString()} chars) - click to expand
                </summary>
                <pre className="p-2 text-xs overflow-auto max-h-96 whitespace-pre-wrap" style={{ color: "var(--tool-msg-text, var(--foreground))" }}>
                  {contentText}
                </pre>
              </details>
            ) : (
              <pre className="text-xs p-2 rounded overflow-auto max-h-64 whitespace-pre-wrap" style={{ background: "var(--code-bg)", color: "var(--tool-msg-text, var(--foreground))" }}>
                {contentText}
              </pre>
            )
          ) : (
            /* Regular markdown content with GFM tables and LaTeX support */
            <div className="prose prose-sm max-w-none" style={{ color: "inherit" }}>
              <ReactMarkdown
                remarkPlugins={[remarkGfm, remarkMath]}
                rehypePlugins={[rehypeRaw, rehypeKatex]}
                components={{
                  code({ node, className, children, ...props }) {
                    const match = /language-(\w+)/.exec(className || "");
                    const inline = !match;
                    return !inline ? (
                      <SyntaxHighlighter
                        style={oneDark}
                        language={match[1]}
                        PreTag="div"
                      >
                        {String(children).replace(/\n$/, "")}
                      </SyntaxHighlighter>
                    ) : (
                      <code className={className} {...props}>
                        {children}
                      </code>
                    );
                  },
                  table({ children }) {
                    return (
                      <div className="overflow-x-auto my-4">
                        <table className="min-w-full border-collapse border" style={{ borderColor: "var(--panel-border)" }}>
                          {children}
                        </table>
                      </div>
                    );
                  },
                  thead({ children }) {
                    return <thead style={{ background: "var(--panel-bg)" }}>{children}</thead>;
                  },
                  th({ children }) {
                    return (
                      <th className="px-4 py-2 text-left border font-semibold" style={{ borderColor: "var(--panel-border)", color: "var(--heading-color)" }}>
                        {children}
                      </th>
                    );
                  },
                  td({ children }) {
                    return (
                      <td className="px-4 py-2 border" style={{ borderColor: "var(--panel-border)" }}>
                        {children}
                      </td>
                    );
                  },
                }}
              >
                {preprocessLatex(contentText)}
              </ReactMarkdown>
            </div>
          )}
        </>
      )}

      {/* Tool calls */}
      {showToolCalls && (
        <div className="mt-2 space-y-2">
          {displayVerbosity === "medium" && (
            <div className="text-xs font-mono" style={{ color: "var(--muted)" }}>
              Tool Calls | {messageMetadataText(message, "Assistant")}
            </div>
          )}
          {toolCalls.map((tc: any, idx: number) => {
            const toolName = toolCallName(tc);
            const args = toolCallArgs(tc);
            const isBash = toolName === "bash";
            const script = isBash && typeof args === "object" && args !== null && "script" in args
              ? (args as any).script
              : null;
            const toolCallId = tc.id || tc.tool_call_id || "";

            return (
              <div
                key={toolCallId || idx}
                className={`rounded p-2 ${showBorders ? 'border' : ''}`}
                style={{ background: "var(--tool-call-bg)", borderColor: "var(--tool-call-border)" }}
              >
                <div className="flex items-center gap-2 text-sm flex-wrap">
                  <span className="font-medium" style={{ color: "var(--tool-call-text, var(--tool-call-border))" }}>{toolName}</span>
                  {toolCallId && (
                    <span className="text-xs font-mono" style={{ color: "var(--muted)" }}>
                      {displayVerbosity === "medium" ? `tool_call_id: ${toolCallId}` : toolCallId.slice(-8)}
                    </span>
                  )}
                  {displayVerbosity === "medium" && (
                    <span className="text-xs font-mono" style={{ color: "var(--foreground)" }}>
                      {oneLinePreview(args)}
                    </span>
                  )}
                </div>
                {displayVerbosity !== "medium" && (
                  <>
                    {/* Special display for bash scripts */}
                    {isBash && script ? (
                      <pre className="mt-1 text-sm font-mono p-2 rounded overflow-auto whitespace-pre-wrap break-all" style={{ background: "var(--code-bg)", color: "var(--accent)" }}>
                        $ {String(script)}
                      </pre>
                    ) : (
                      <pre className="mt-1 text-xs p-1 rounded overflow-auto max-h-40 whitespace-pre-wrap break-words" style={{ background: "var(--code-bg)", color: "var(--foreground)" }}>
                        {typeof args === "string"
                          ? args
                          : JSON.stringify(args, null, 2)}
                      </pre>
                    )}
                  </>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Persisted streamed tool metadata (historical/reloaded transcript) */}
      {showStreamedMetadata && (
        <div className="mt-2 space-y-2">
          {toolStreamEntries.map(([name, text]) => (
            <div
              key={`tool-stream-${name}`}
              className={`rounded p-2 ${showBorders ? 'border' : ''}`}
              style={{ background: "var(--tool-msg-bg)", borderColor: "var(--tool-msg-border)" }}
            >
              <div className="text-sm font-medium font-mono" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>
                {displayVerbosity === "medium"
                  ? messageMetadataText(message, `Tool Output: ${name}`)
                  : `Tool Output: ${name}`}
              </div>
              {displayVerbosity === "max" && (
                <pre className="mt-1 text-xs p-2 rounded overflow-auto max-h-64 whitespace-pre-wrap" style={{ background: "var(--code-bg)", color: "var(--tool-msg-text, var(--foreground))" }}>
                  {text}
                </pre>
              )}
            </div>
          ))}

          {toolCallStreamEntries.map(([streamKey, text]) => (
            <div
              key={`tool-call-stream-${streamKey}`}
              className={`rounded p-2 ${showBorders ? 'border' : ''}`}
              style={{ background: "var(--tool-call-bg)", borderColor: "var(--tool-call-border)" }}
            >
              <div className="text-sm font-medium font-mono" style={{ color: "var(--tool-call-text, var(--tool-call-border))" }}>
                {displayVerbosity === "medium"
                  ? messageMetadataText(message, `Tool Call Args: ${streamKey}`)
                  : `Tool Call Args: ${streamKey}`}
              </div>
              {displayVerbosity === "max" ? (
                <pre className="mt-1 text-xs p-2 rounded overflow-auto max-h-40 whitespace-pre-wrap break-words" style={{ background: "var(--code-bg)", color: "var(--foreground)" }}>
                  {text}
                </pre>
              ) : (
                <div className="mt-1 text-xs font-mono" style={{ color: "var(--foreground)" }}>
                  {oneLinePreview(text)}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function collectHiddenDetailsForMessage(message: Message): HiddenDetail[] {
  const details: HiddenDetail[] = [];
  let availableTokens = typeof message.tokens === "number" && Number.isFinite(message.tokens) ? message.tokens : undefined;
  const takeTokens = () => {
    const tokens = availableTokens;
    availableTokens = undefined;
    return tokens;
  };
  if (message.reasoning) {
    details.push({ kind: "reasoning", header: messageMetadataText(message, "Reasoning"), tokens: takeTokens() });
  }
  if (message.tool_calls?.length) {
    message.tool_calls.forEach((tc: any) => {
      const tcId = tc?.id || tc?.tool_call_id || "";
      const idText = tcId ? ` | tool_call_id: ${tcId}` : "";
      const name = toolCallName(tc);
      details.push({
        kind: "tool_calls",
        name,
        tokens: takeTokens(),
        header: `${messageMetadataText(message, "ToolCall")}${idText} | ${name} | ${oneLinePreview(toolCallArgs(tc))}`,
      });
    });
  }
  if (message.role === "tool") {
    const contentText = contentToPlainText(message.content, message.content_text || "");
    const label = contentText ? `Tool Result (${contentText.length.toLocaleString()} chars)` : "Tool Result";
    const name = message.name || "tool";
    details.push({ kind: "tool_results", name, tokens: takeTokens(), header: messageMetadataText(message, `${label}: ${name}`) });
  }
  stringRecordEntries(message.tool_stream).forEach(([name, text]) => {
    details.push({
      kind: "tool_results",
      name,
      tokens: takeTokens(),
      header: streamedMetadataHiddenHeader(message, `Tool Output: ${name}`, text),
    });
  });
  stringRecordEntries(message.tool_calls_stream).forEach(([streamKey, text]) => {
    details.push({
      kind: "tool_calls",
      name: streamKey,
      tokens: takeTokens(),
      header: streamedMetadataHiddenHeader(message, `Tool Call Args: ${streamKey}`, text),
    });
  });
  return details;
}

function renderMessagesForVerbosity(
  messages: Message[],
  displayVerbosity: DisplayVerbosity,
  showBorders: boolean,
  onStageAttachment?: (attachment: AttachmentContentPart) => void,
): ReactNode[] {
  if (displayVerbosity !== "min") {
    return messages.map((msg, idx) => (
      <MessageBlock key={msg.id || idx} message={msg} showBorders={showBorders} displayVerbosity={displayVerbosity} onStageAttachment={onStageAttachment} />
    ));
  }

  const nodes: ReactNode[] = [];
  let hidden: HiddenDetail[] = [];
  const flushHidden = (key: string) => {
    if (!hidden.length) return;
    const details = hidden;
    hidden = [];
    nodes.push(<HiddenDetailsBlock key={`hidden-${key}-${nodes.length}`} details={details} showBorders={showBorders} />);
  };

  messages.forEach((msg, idx) => {
    if (msg.kind === "compaction_marker" || msg.role === "compaction_marker") {
      flushHidden(`marker-${idx}`);
      nodes.push(<CompactionMarker key={msg.id || `marker-${idx}`} message={msg} />);
      return;
    }

    const hiddenDetails = collectHiddenDetailsForMessage(msg);
    const hasVisibleConversationBody = (msg.role === "user" || msg.role === "assistant") && Boolean(contentToPlainText(msg.content, msg.content_text || "").trim());
    if (hasVisibleConversationBody || isImportantSystemMessage(msg)) {
      const beforeVisibleDetails = msg.role === "assistant" ? hiddenDetails.filter((detail) => detail.kind === "reasoning") : [];
      const afterVisibleDetails = msg.role === "assistant" ? hiddenDetails.filter((detail) => detail.kind !== "reasoning") : hiddenDetails;
      hidden.push(...beforeVisibleDetails);
      flushHidden(`before-${msg.id || idx}`);
      nodes.push(<MessageBlock key={msg.id || idx} message={msg} showBorders={showBorders} displayVerbosity="min" onStageAttachment={onStageAttachment} />);
      hidden.push(...afterVisibleDetails);
      return;
    }

    hidden.push(...hiddenDetails);
  });

  flushHidden("end");
  return nodes;
}

interface ChatPanelProps {
  showBorders?: boolean;
  streamingTps?: number | null;
  onStageAttachment?: (attachment: AttachmentContentPart) => void;
}

export function ChatPanel({ showBorders = true, streamingTps = null, onStageAttachment }: ChatPanelProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const streamingContentRef = useRef<HTMLDivElement>(null);
  const streamingReasoningRef = useRef<HTMLDivElement>(null);
  const streamingReasoningSummaryRef = useRef<HTMLDivElement>(null);
  const streamingToolOutputRefs = useRef<Record<string, HTMLPreElement | null>>({});
  const lastContentIndexRef = useRef(0);
  const lastReasoningIndexRef = useRef(0);
  const lastReasoningSummaryIndexRef = useRef(0);
  const lastToolOutputIndexRef = useRef<Record<string, number>>({});
  const [nowMs, setNowMs] = useState(() => Date.now());

  const {
    currentThreadId,
    messages,
    setMessages,
    streamingToolCalls,
    streamingToolOutputs,
    streamingModelKey,
    streamingKind,
    isStreaming,
    scrollTrigger,
    displayVerbosity,
  } = useAppStore();
  const hasActiveToolTimeout = Object.values(streamingToolOutputs).some((tool) => Boolean(tool.timeout));
  const primaryToolTimeoutText = Object.values(streamingToolOutputs)
    .map((tool) => toolTimeoutCountdown(tool.timeout, nowMs))
    .find((text): text is string => Boolean(text));

  useEffect(() => {
    if (!hasActiveToolTimeout) return;
    setNowMs(Date.now());
    const intervalId = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(intervalId);
  }, [hasActiveToolTimeout]);

  // Stick-to-bottom scrolling: track if user intentionally scrolled away
  const stickToBottomRef = useRef(true);
  const rafIdRef = useRef<number | null>(null);
  const isAutoScrollingRef = useRef(false);
  const autoScrollGenerationRef = useRef(0);

  const distanceFromBottom = useCallback(() => {
    if (!scrollRef.current) return 0;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    return Math.max(0, scrollHeight - scrollTop - clientHeight);
  }, []);

  const isAtBottom = useCallback(() => {
    return distanceFromBottom() <= STICKY_BOTTOM_THRESHOLD_PX;
  }, [distanceFromBottom]);

  const finishAutoScrollSoon = useCallback((generation: number) => {
    window.setTimeout(() => {
      if (autoScrollGenerationRef.current === generation) {
        isAutoScrollingRef.current = false;
      }
    }, 50);
  }, []);

  // In sticky mode every output append must keep following the tail.  Use
  // clientHeight as the target rather than a captured scrollHeight value so
  // fast-growing content cannot leave us one frame behind.
  const scrollToBottomNow = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const generation = autoScrollGenerationRef.current + 1;
    autoScrollGenerationRef.current = generation;
    isAutoScrollingRef.current = true;
    const target = Math.max(0, el.scrollHeight - el.clientHeight);
    el.scrollTop = target;
    stickToBottomRef.current = true;
    finishAutoScrollSoon(generation);
  }, [finishAutoScrollSoon]);

  // Handle user scroll - scrolling up disables sticky mode; scrolling back to
  // the bottom reenables it.  Programmatic scroll events should not disable it.
  const handleScroll = useCallback(() => {
    if (isAutoScrollingRef.current) return;
    stickToBottomRef.current = isAtBottom();
  }, [isAtBottom]);

  const updateStickyAfterUserScroll = useCallback((forceStickyTail = false, exactBottomOnly = false) => {
    isAutoScrollingRef.current = false;
    autoScrollGenerationRef.current += 1;
    requestAnimationFrame(() => {
      if (forceStickyTail) {
        scrollToBottomNow();
        return;
      }
      stickToBottomRef.current = exactBottomOnly
        ? distanceFromBottom() <= 1
        : isAtBottom();
    });
  }, [distanceFromBottom, isAtBottom, scrollToBottomNow]);

  const handleWheel = useCallback((event: WheelEvent<HTMLDivElement>) => {
    if (event.deltaY < 0) {
      stickToBottomRef.current = false;
      updateStickyAfterUserScroll(false, true);
      return;
    }
    const scrollingDownToTail =
      event.deltaY > 0 &&
      distanceFromBottom() <= Math.abs(event.deltaY) + STICKY_BOTTOM_THRESHOLD_PX;
    updateStickyAfterUserScroll(scrollingDownToTail);
  }, [distanceFromBottom, updateStickyAfterUserScroll]);

  const handleTouchMove = useCallback(() => {
    // Direction is not exposed here, so let the post-scroll position decide.
    // Touch scrolling away disables sticky; touch scrolling back to the exact
    // bottom reenables it via the normal bottom check.
    updateStickyAfterUserScroll(false, true);
  }, [updateStickyAfterUserScroll]);


  const handlePointerUp = useCallback(() => {
    updateStickyAfterUserScroll();
  }, [updateStickyAfterUserScroll]);

  // Scroll to bottom using requestAnimationFrame for smooth, reliable scrolling.
  const scrollToBottom = useCallback(() => {
    if (!stickToBottomRef.current) return;
    if (rafIdRef.current !== null) return; // Already scheduled

    rafIdRef.current = requestAnimationFrame(() => {
      rafIdRef.current = null;
      if (!stickToBottomRef.current) return;

      scrollToBottomNow();

      // For fast streams, content can grow again in the same frame.  Re-check
      // for a second frame and keep sticky mode enabled if another adjustment
      // is needed.
      requestAnimationFrame(() => {
        if (stickToBottomRef.current && distanceFromBottom() > 1) {
          scrollToBottomNow();
        }
      });
    });
  }, [distanceFromBottom, scrollToBottomNow]);

  const flushStreamingText = useCallback(() => {
    // Import here to avoid SSR issues
    const { streamingBuffer } = require("@/lib/streamingBuffer") as typeof import("@/lib/streamingBuffer");
    let appended = false;

    if (streamingContentRef.current) {
      const chunks = streamingBuffer.contentChunks;
      for (let i = lastContentIndexRef.current; i < chunks.length; i++) {
        streamingContentRef.current.appendChild(document.createTextNode(chunks[i]));
        appended = true;
      }
      lastContentIndexRef.current = chunks.length;
    }

    if (streamingReasoningRef.current) {
      const chunks = streamingBuffer.reasoningChunks;
      if (chunks.length > 0) {
        const container = document.getElementById('streaming-reasoning-container');
        if (container) container.style.display = 'block';
      }
      for (let i = lastReasoningIndexRef.current; i < chunks.length; i++) {
        streamingReasoningRef.current.appendChild(document.createTextNode(chunks[i]));
        appended = true;
      }
      lastReasoningIndexRef.current = chunks.length;
    }

    if (streamingReasoningSummaryRef.current) {
      const chunks = streamingBuffer.reasoningSummaryChunks;
      if (chunks.length > 0) {
        const container = document.getElementById('streaming-reasoning-summary-container');
        if (container) container.style.display = 'block';
      }
      for (let i = lastReasoningSummaryIndexRef.current; i < chunks.length; i++) {
        streamingReasoningSummaryRef.current.appendChild(document.createTextNode(chunks[i]));
        appended = true;
      }
      lastReasoningSummaryIndexRef.current = chunks.length;
    }

    if (appended) scrollToBottom();
  }, [scrollToBottom]);

  const flushStreamingToolOutput = useCallback(() => {
    const { streamingBuffer } = require("@/lib/streamingBuffer") as typeof import("@/lib/streamingBuffer");
    let appended = false;

    streamingBuffer.toolOutputChunks.forEach((chunks, toolId) => {
      const el = streamingToolOutputRefs.current[toolId];
      if (!el) return;
      const lastIndex = lastToolOutputIndexRef.current[toolId] || 0;
      for (let i = lastIndex; i < chunks.length; i++) {
        el.appendChild(document.createTextNode(chunks[i]));
        appended = true;
      }
      if (stickToBottomRef.current) {
        el.scrollTop = el.scrollHeight;
      }
      lastToolOutputIndexRef.current[toolId] = chunks.length;
    });

    if (appended) scrollToBottom();
  }, [scrollToBottom]);

  const scheduleStreamingFlush = useCallback((flush: () => void) => {
    const timeoutId = window.setTimeout(() => {
      requestAnimationFrame(() => {
        requestAnimationFrame(flush);
      });
    }, 0);
    return timeoutId;
  }, []);

  // Subscribe to streaming buffer updates - bypasses React entirely
  // This is O(1) per chunk with direct DOM manipulation
  // Re-runs when isStreaming changes to catch up with buffered content when refs become available
  useEffect(() => {
    const { streamingBuffer } = require("@/lib/streamingBuffer") as typeof import("@/lib/streamingBuffer");

    const unsubContent = streamingBuffer.subscribeContent(flushStreamingText);
    const unsubReasoning = streamingBuffer.subscribeReasoning(flushStreamingText);

    // Render any existing buffer content (catches up when joining mid-stream).
    // When isStreaming changes to true, refs should be available after render.
    const timeoutId = isStreaming ? scheduleStreamingFlush(flushStreamingText) : null;

    return () => {
      if (timeoutId !== null) clearTimeout(timeoutId);
      unsubContent();
      unsubReasoning();
      if (rafIdRef.current !== null) {
        cancelAnimationFrame(rafIdRef.current);
        rafIdRef.current = null;
      }
    };
  }, [isStreaming, flushStreamingText, scheduleStreamingFlush]);

  // Subscribe to streaming tool-output preview updates. Like text streaming,
  // this writes chunks directly to DOM so large/fast tool output does not
  // trigger a React render per chunk.
  useEffect(() => {
    const { streamingBuffer } = require("@/lib/streamingBuffer") as typeof import("@/lib/streamingBuffer");

    const unsubToolOutput = streamingBuffer.subscribeToolOutput(flushStreamingToolOutput);
    const timeoutId = isStreaming ? scheduleStreamingFlush(flushStreamingToolOutput) : null;

    return () => {
      if (timeoutId !== null) clearTimeout(timeoutId);
      unsubToolOutput();
    };
  }, [isStreaming, streamingToolOutputs, flushStreamingToolOutput, scheduleStreamingFlush]);

  // Reset DOM state when streaming stops
  useEffect(() => {
    if (!isStreaming) {
      lastContentIndexRef.current = 0;
      lastReasoningIndexRef.current = 0;
      lastReasoningSummaryIndexRef.current = 0;
      lastToolOutputIndexRef.current = {};
      if (streamingContentRef.current) {
        streamingContentRef.current.textContent = '';
      }
      if (streamingReasoningRef.current) {
        streamingReasoningRef.current.textContent = '';
      }
      if (streamingReasoningSummaryRef.current) {
        streamingReasoningSummaryRef.current.textContent = '';
      }
      Object.values(streamingToolOutputRefs.current).forEach((el) => {
        if (el) el.textContent = '';
      });
    }
  }, [isStreaming]);

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["messages", currentThreadId],
    queryFn: () => fetchMessages(currentThreadId!),
    enabled: !!currentThreadId,
    retry: 3,
    retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 5000),
  });

  // Sync fetched messages to store and scroll to bottom if we were sticky
  useEffect(() => {
    if (data) {
      // Capture sticky state BEFORE DOM update.  Do not derive this from the
      // current distance here: replacing the streaming block with the final
      // message can shrink content and make an intentionally scrolled-up view
      // look "at bottom" for one render.
      const wasSticky = stickToBottomRef.current;
      setMessages(data);
      // Scroll to bottom after DOM update if we were at bottom before
      if (wasSticky) {
        // Double RAF: first waits for React render, second waits for paint
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            scrollToBottomNow();
          });
        });
      }
    }
  }, [data, scrollToBottomNow, setMessages]);

  // Reset scroll state and scroll to bottom when thread changes
  useEffect(() => {
    stickToBottomRef.current = true;
    requestAnimationFrame(() => {
      scrollToBottomNow();
    });
  }, [currentThreadId, scrollToBottomNow]);

  // Scroll to bottom when streaming starts (assistant header appears)
  useEffect(() => {
    if (isStreaming) {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          scrollToBottom();
        });
      });
    }
  }, [isStreaming, scrollToBottom]);

  // Scroll to bottom when new streaming tool calls or tool outputs appear (tool headers)
  useEffect(() => {
    if ((Object.keys(streamingToolCalls).length > 0 || Object.keys(streamingToolOutputs).length > 0) && stickToBottomRef.current) {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          scrollToBottomNow();
        });
      });
    }
  }, [scrollToBottomNow, streamingToolCalls, streamingToolOutputs]);

  // Scroll to bottom when UI-only messages are added (e.g., /cost, /help)
  useEffect(() => {
    if (scrollTrigger > 0 && stickToBottomRef.current) {
      requestAnimationFrame(() => {
        scrollToBottomNow();
      });
    }
  }, [scrollToBottomNow, scrollTrigger]);

  useEffect(() => {
    if (!scrollRef.current) return;
    if (typeof ResizeObserver === "undefined") return;

    const observer = new ResizeObserver(() => {
      scrollToBottom();
    });
    observer.observe(scrollRef.current);
    if (scrollRef.current.firstElementChild) {
      observer.observe(scrollRef.current.firstElementChild);
    }

    return () => observer.disconnect();
  }, [currentThreadId, isStreaming, messages.length, scrollToBottom]);

  if (!currentThreadId) {
    return (
      <div className="flex-1 flex items-center justify-center" style={{ color: "var(--muted)" }}>
        Select a thread to view messages
      </div>
    );
  }

  const lastMessageWithTps = [...messages].reverse().find(
    (msg) => (msg.role === "assistant" || msg.role === "tool") && typeof msg.tps === "number" && Number.isFinite(msg.tps) && (msg.tps || 0) > 0
  );
  const formattedStreamingTps =
    isStreaming && streamingKind === "llm"
      ? formatStreamingTps(streamingTps)
      : formatStreamingTps(lastMessageWithTps?.tps ?? null);
  const streamingRoleLabel = streamingKind === "tool" ? "Tool" : "Assistant";

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className={`px-3 py-2 text-xs flex items-center justify-between flex-shrink-0 ${showBorders ? 'border-b border-[var(--panel-border)]' : ''}`} style={{ color: "var(--muted)", background: "var(--panel-bg)" }}>
        <span>Chat Messages{formattedStreamingTps ? ` | ${formattedStreamingTps}` : ""}</span>
      </div>
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        onWheel={handleWheel}
        onTouchMove={handleTouchMove}
        onPointerUp={handlePointerUp}
        className="flex-1 overflow-auto p-4"
        data-testid="chat-panel"
      >
        <div data-testid="chat-panel-content">
          {isLoading ? (
            <div className="text-center" style={{ color: "var(--muted)" }}>Loading messages...</div>
          ) : isError ? (
            <div className="text-center space-y-2">
              <div style={{ color: "var(--error, #ef4444)" }}>Failed to load messages</div>
              <button
                onClick={() => refetch()}
                className="px-3 py-1 rounded text-sm"
                style={{ background: "var(--accent)", color: "var(--background)" }}
              >
                Retry
              </button>
            </div>
          ) : messages.length === 0 ? (
            <div className="text-center" style={{ color: "var(--muted)" }}>
              No messages yet. Start a conversation!
            </div>
          ) : (
            <>
              {renderMessagesForVerbosity(messages, displayVerbosity, showBorders, onStageAttachment)}

              {/* Streaming content */}
              {isStreaming && (
                <div
                  className={`rounded p-3 mb-3 ${showBorders ? 'border' : ''}`}
                  style={{ background: "var(--assistant-msg-bg)", borderColor: "var(--assistant-msg-border)", color: "var(--assistant-msg-text, var(--foreground))" }}
                >
                  <div className="text-xs mb-2" style={{ color: "var(--muted)" }}>
                    <span className="font-medium" style={{ color: "var(--assistant-msg-text, var(--foreground))" }}>{streamingRoleLabel}</span>
                    {streamingModelKey && (
                      <span style={{ color: "var(--muted)" }}> ({streamingModelKey})</span>
                    )}
                    <span className="ml-2 animate-pulse" style={{ color: "var(--accent)" }}>streaming...</span>
                    {streamingKind === "tool" && primaryToolTimeoutText && (
                      <span data-testid="streaming-tool-timeout-header" className="ml-2" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>
                        {primaryToolTimeoutText}
                      </span>
                    )}
                  </div>

                  {displayVerbosity === "min" ? (
                    <div className="space-y-1 text-sm animate-pulse" style={{ color: "var(--accent)" }}>
                      {streamingKind === "tool" ? (
                        <>
                          {Object.keys(streamingToolCalls).length > 0 && <div>Tool call streaming…</div>}
                          {Object.keys(streamingToolOutputs).length > 0 && <div>Tool output streaming…</div>}
                          {Object.keys(streamingToolCalls).length === 0 && Object.keys(streamingToolOutputs).length === 0 && <div>Tool output streaming…</div>}
                          {primaryToolTimeoutText && <div data-testid="streaming-tool-timeout-min">{primaryToolTimeoutText}</div>}
                        </>
                      ) : (
                        <>
                          <div>Reasoning streaming…</div>
                          <div>Content streaming…</div>
                        </>
                      )}
                    </div>
                  ) : (
                    <>
                      {/* Streaming reasoning - direct DOM updates via ref */}
                      <details
                      open
                      className={`mb-2 rounded p-2 ${showBorders ? 'border' : ''}`}
                      style={{ background: "var(--reasoning-bg)", borderColor: "var(--reasoning-border)", display: "none" }}
                      id="streaming-reasoning-container"
                      >
                      <summary className="cursor-pointer text-sm" style={{ color: "var(--reasoning-text, var(--reasoning-border))" }}>
                        Reasoning <span className="text-xs animate-pulse">(streaming...)</span>
                      </summary>
                      <div
                        ref={streamingReasoningRef}
                        className="mt-2 text-sm whitespace-pre-wrap"
                        style={{ color: "var(--reasoning-text, var(--foreground))", opacity: 0.9 }}
                      />
                      </details>

                      {/* Streaming reasoning summary - display-only, not persisted as reasoning */}
                      <details
                      open
                      className={`mb-2 rounded p-2 ${showBorders ? 'border' : ''}`}
                      style={{ background: "var(--reasoning-bg)", borderColor: "var(--reasoning-border)", display: "none" }}
                      id="streaming-reasoning-summary-container"
                      >
                      <summary className="cursor-pointer text-sm" style={{ color: "var(--reasoning-text, var(--reasoning-border))" }}>
                        Reasoning Summary <span className="text-xs animate-pulse">(streaming...)</span>
                      </summary>
                      <div
                        ref={streamingReasoningSummaryRef}
                        className="mt-2 text-sm whitespace-pre-wrap"
                        style={{ color: "var(--reasoning-text, var(--foreground))", opacity: 0.9 }}
                      />
                      </details>

                      {/* Streaming content - direct DOM updates via ref for O(1) performance */}
                      <div
                      ref={streamingContentRef}
                      className="text-sm"
                      style={{
                        color: "var(--assistant-msg-text, var(--foreground))",
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-word",
                      }}
                      />

                      {/* Streaming tool output preview */}
                      {Object.keys(streamingToolOutputs).length > 0 && (
                      <div className="mt-2 space-y-2">
                        {Object.entries(streamingToolOutputs).map(([toolId, tool]) => {
                          const timeoutText = toolTimeoutCountdown(tool.timeout, nowMs);
                          return (
                            <details
                              key={toolId}
                              open
                              className={`rounded ${showBorders ? 'border' : ''}`}
                              style={{ background: "var(--tool-msg-bg)", borderColor: "var(--tool-msg-border)" }}
                            >
                              <summary className="cursor-pointer p-2 flex items-center gap-2 text-sm flex-wrap">
                                <span className="font-medium" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>{tool.name || "tool"}</span>
                                <span className="text-xs font-mono" style={{ color: "var(--muted)" }}>
                                  {toolId.slice(-8)}
                                </span>
                                <span className="text-xs animate-pulse" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>streaming output...</span>
                                {timeoutText && (
                                  <span data-testid="streaming-tool-timeout-summary" className="text-xs" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>
                                    {timeoutText}
                                  </span>
                                )}
                              </summary>
                              <div className="px-2 pb-2">
                                {timeoutText && (
                                  <div
                                    data-testid="streaming-tool-timeout"
                                    className="mb-2 text-xs"
                                    style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}
                                  >
                                    {timeoutText}
                                  </div>
                                )}
                                {tool.summary && (
                                  <div
                                    data-testid="streaming-tool-summary"
                                    className="mb-2 text-xs animate-pulse"
                                    style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}
                                  >
                                    {tool.summary}
                                  </div>
                                )}
                                <pre
                                  ref={(el) => {
                                    streamingToolOutputRefs.current[toolId] = el;
                                  }}
                                  data-testid="streaming-tool-output"
                                  className="text-xs p-2 rounded overflow-auto max-h-64 whitespace-pre-wrap break-words"
                                  style={{ background: "var(--code-bg)", color: "var(--tool-msg-text, var(--foreground))" }}
                                />
                                {tool.suppressed && (
                                  <div
                                    data-testid="streaming-tool-output-suppressed"
                                    className="mt-2 text-xs animate-pulse"
                                    style={{ color: "var(--muted)" }}
                                  >
                                    {toolStreamSavingText(tool.name, tool.suppressedFrames)}
                                  </div>
                                )}
                              </div>
                            </details>
                          );
                        })}
                      </div>
                      )}

                      {/* Streaming tool calls */}
                      {Object.keys(streamingToolCalls).length > 0 && (
                      <div className="mt-2 space-y-2">
                        {Object.entries(streamingToolCalls).map(([tcId, tc]) => {
                          const isBash = tc.name === "bash";
                          let parsedArgs: any = tc.arguments;
                          try {
                            parsedArgs = JSON.parse(tc.arguments);
                          } catch {
                            // Keep as string
                          }
                          const script = isBash && parsedArgs?.script;

                          return (
                            <details
                              key={tcId}
                              open
                              className={`rounded ${showBorders ? 'border' : ''}`}
                              style={{ background: "var(--tool-call-bg)", borderColor: "var(--tool-call-border)" }}
                            >
                              <summary className="cursor-pointer p-2 flex items-center gap-2 text-sm">
                                <span className="font-medium" style={{ color: "var(--tool-call-text, var(--tool-call-border))" }}>{tc.name || "tool"}</span>
                                <span className="text-xs font-mono" style={{ color: "var(--muted)" }}>
                                  {tcId.slice(-8)}
                                </span>
                                <span className="text-xs animate-pulse" style={{ color: "var(--tool-call-text, var(--tool-call-border))" }}>streaming...</span>
                              </summary>
                              <div className="px-2 pb-2">
                                {isBash && script ? (
                                  <pre className="text-sm font-mono p-2 rounded overflow-auto whitespace-pre-wrap break-all" style={{ background: "var(--code-bg)", color: "var(--accent)" }}>
                                    $ {script}
                                  </pre>
                                ) : (
                                  <pre className="text-xs p-2 rounded overflow-auto whitespace-pre-wrap break-all" style={{ background: "var(--code-bg)", color: "var(--foreground)" }}>
                                    {tc.arguments || "..."}
                                  </pre>
                                )}
                              </div>
                            </details>
                          );
                        })}
                      </div>
                      )}
                    </>
                  )}
              </div>
            )}
            </>
          )}
          {/* Scroll anchor for auto-scroll */}
          <div ref={bottomRef} />
        </div>
      </div>
    </div>
  );
}
