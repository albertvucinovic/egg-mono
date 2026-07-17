"use client";

import {
  memo,
  Profiler,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
  type TouchEvent,
  type WheelEvent,
} from "react";
import Link from "next/link";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import rehypeRaw from "rehype-raw";
import "katex/dist/katex.min.css";
import { attachmentUrl, createEditAnswerDraft, promoteProviderOutput, providerOutputUrl } from "@/lib/api";
import { useAppStore, type Message, type DisplayVerbosity, type StreamingProviderRequest, type StreamingToolOutput } from "@/lib/store";
import { ProtectedFileLink, ProtectedImage } from "@/components/ProtectedFileLink";
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
import { liveTimingSnapshot, shouldUpdateLiveTiming, type LiveTimingSnapshot } from "@/lib/liveTiming";
import {
  getUserAnswerToolCallId,
  getUserToolCallIds,
  isGetUserMessageTool,
  resolveToolResultNames,
  toolCallId,
  toolCallName,
  toolDisplayName,
  shortToolCallId,
  type HiddenToolDetail,
  type HiddenDetailKind,
} from "@/lib/toolPresentation";
import {
  cancelTranscriptRequests,
  fetchOlderTranscriptPage,
  refreshTranscriptTail,
  transcriptInfiniteQueryOptions,
} from "@/lib/transcript";
import { AnimationFrameCoalescer, IntervalCoalescer, streamingBufferForThread } from "@/lib/streamingBuffer";
import { recordReactCommit, recordStreamingFlush } from "@/lib/performanceInstrumentation";
import {
  nextTranscriptStartIndex,
  TRANSCRIPT_WINDOW_MESSAGES,
} from "@/lib/transcriptWindow";
import { expandedTranscriptStartIndex, oldestTranscriptFrontier, transcriptIndex, transcriptRenderWindow } from "@/lib/transcriptIndex";
import {
  IDLE_HISTORY_DEMAND,
  reduceHistoryDemand,
  reduceLiveEdgeState,
  type HistoryDemandState,
  type LiveEdgeState,
} from "@/lib/chatScrollState";
import clsx from "clsx";
import { eggwSyntaxTheme } from "@/lib/syntaxTheme";
import { Button, IconButton, StatusChip } from "@/components/ui/primitives";
import { OverlayPanel } from "@/components/ui/OverlayPanel";

const STICKY_BOTTOM_THRESHOLD_PX = 16;
const MESSAGE_IMAGE_PREVIEW_MAX_HEIGHT = "min(70vh, 720px)";
const TRANSCRIPT_SCROLLBACK_THRESHOLD_PX = 240;
const THREAD_LINK_SUFFIX_LENGTH = 8;
const TOOL_ARGUMENT_PREVIEW_INTERVAL_MS = 100;

/**
 * Preprocess content to convert various LaTeX-style delimiters to markdown math syntax.
 * Supports:
 * - \[...\] → $$...$$ (display math)
 * - \(...\) → $...$ (inline math)
 * - [ ... ] with LaTeX commands → $$...$$ (common AI output format)
 */
function preprocessLatex(content: string): string {
  if (!content) return content;

  const normalizeDisplayMathFences = (value: string): string => {
    // micromark/remark-math treats text after an opening `$$` fence as fence
    // metadata, not as math body.  LLMs often emit compact blocks such as
    // `$$\begin{aligned}` and `\end{aligned}$$`; normalize those to canonical
    // display-math fences so KaTeX receives the full aligned environment.
    let normalized = value.replace(/^([ \t]*)\$\$([^\n]*)$/gm, (line, indent, rest) => {
      if (!String(rest).trim()) return line;
      const body = String(rest);
      const withoutClosingFence = body.replace(/\$\$[ \t]*$/, "").trimEnd();
      if (withoutClosingFence !== body.trimEnd()) {
        return withoutClosingFence.trim() ? `${indent}$$\n${indent}${withoutClosingFence.trimStart()}\n${indent}$$` : `${indent}$$`;
      }
      return `${indent}$$\n${indent}${body.trimStart()}`;
    });

    normalized = normalized.replace(/^([ \t]*)(\\[^\n]*?)\$\$[ \t]*$/gm, (line, indent, body) => {
      const mathBody = String(body).trimEnd();
      return mathBody.trim() ? `${indent}${mathBody}\n${indent}$$` : line;
    });

    return normalized;
  };

  const decodeDisplayMathEntities = (value: string): string =>
    value.replace(/\$\$([\s\S]*?)\$\$/g, (_match, math) => {
      const decoded = String(math)
        .replace(/&amp;/g, "&")
        .replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">")
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'");
      return `$$${decoded}$$`;
    });

  // Convert \[...\] to $$...$$ for display math
  let processed = content.replace(/\\\[([\s\S]*?)\\\]/g, (_, math) => `$$${math}$$`);

  processed = normalizeDisplayMathFences(processed);
  processed = decodeDisplayMathEntities(processed);

  // Convert \(...\) to $...$ for inline math
  processed = processed.replace(/\\\(([\s\S]*?)\\\)/g, (_, math) => `$${math}$`);

  // Convert [ ... ] when it starts with a LaTeX command (common AI output format)
  // This handles multiline content like [ \begin{aligned} ... \end{aligned} ]
  // Match a standalone [ followed by whitespace and backslash, capture until
  // closing ]. Do not treat brackets that are part of LaTeX commands (for
  // example \left[ ... \right]) as markdown math delimiters.
  processed = processed.replace(/(^|[^\w\\])\[\s*(\\[\s\S]*?)\s*\]/g, (match, prefix, math) => {
    // Only convert if it looks like LaTeX (contains common LaTeX commands)
    if (/\\(?:begin|end|frac|sum|int|prod|lim|nabla|partial|sqrt|text|mathbf|mathrm|left|right|aligned|equation|matrix|cases)/.test(math)) {
      return `${prefix}$$${math}$$`;
    }
    return match; // Keep original if not LaTeX
  });

  return processed;
}

function toolStreamSavingText(name: string, frames: number = 0): string {
  const framesList = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
  const glyph = framesList[Math.max(0, frames) % framesList.length] || "…";
  return `${glyph} tool${name ? ` ${name}` : ""}: preview limit reached; saving output only`;
}

const LiveTimingText = memo(function LiveTimingText({
  kind,
  toolId,
  isStreaming,
  streamingKind,
  streamingStartedAtMs,
  providerRequest,
  toolOutputs,
  testId,
  className = "eggw-message-meta",
}: {
  kind: "provider" | "generic" | "toolElapsed" | "toolTimeout";
  toolId?: string;
  isStreaming: boolean;
  streamingKind: string | null;
  streamingStartedAtMs: number | null;
  providerRequest: StreamingProviderRequest | null;
  toolOutputs: Record<string, StreamingToolOutput>;
  testId?: string;
  className?: string;
}) {
  const read = useCallback(
    (snapshot: LiveTimingSnapshot) => {
      if (kind === "provider") return snapshot.provider;
      if (kind === "generic") return snapshot.generic;
      if (kind === "toolElapsed") return toolId ? snapshot.tools[toolId]?.elapsed || null : null;
      return toolId ? snapshot.tools[toolId]?.timeout || null : null;
    },
    [kind, toolId]
  );
  const initialText = read(liveTimingSnapshot(Date.now(), isStreaming, streamingKind, streamingStartedAtMs, providerRequest, toolOutputs));
  const [text, setText] = useState<string | null>(initialText);
  useEffect(() => {
    const update = () =>
      setText(read(liveTimingSnapshot(Date.now(), isStreaming, streamingKind, streamingStartedAtMs, providerRequest, toolOutputs)));
    update();
    if (!shouldUpdateLiveTiming(isStreaming, toolOutputs, providerRequest, streamingKind)) return;
    const intervalId = window.setInterval(update, 1000);
    return () => window.clearInterval(intervalId);
  }, [read, isStreaming, streamingKind, streamingStartedAtMs, providerRequest, toolOutputs]);
  // Mount the slot with fixed geometry even when this timing kind currently has
  // no text. The first effect/tick therefore cannot create or remove layout.
  const content = (
    <span
      data-testid={testId}
      className={clsx(className, "eggw-live-timing", `eggw-live-timing-${kind}`)}
      aria-hidden={text ? undefined : true}
    >
      {text || "\u00a0"}
    </span>
  );
  if (process.env.NODE_ENV === "production") return content;
  return (
    <Profiler id="LiveTiming" onRender={(id, _phase, duration) => recordReactCommit(id as "LiveTiming", duration)}>
      {content}
    </Profiler>
  );
});

type HiddenDetail = HiddenToolDetail;

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

function formatHiddenDetailBody(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value ?? "");
  }
}

function messageTimestampText(timestamp?: string): string | null {
  if (!timestamp) return null;
  try {
    return new Date(timestamp).toLocaleString(undefined, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
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
  if (message.id && !message.id.startsWith("temp-")) parts.push(`msg_id: ${message.id}`);
  if (message.tool_call_id) parts.push(`tool_call_id: ${message.tool_call_id}`);
  return parts.join(" | ");
}

function outputOptimizerSummaryText(message: Message, includeArtifactId = false): string | null {
  const metadata = message.output_optimizer;
  if (!metadata || metadata.optimized !== true) return null;
  const summaryWithArtifact = typeof metadata.summary_with_artifact === "string" ? metadata.summary_with_artifact.trim() : "";
  const summary = typeof metadata.summary === "string" ? metadata.summary.trim() : "";
  const text = includeArtifactId && summaryWithArtifact ? summaryWithArtifact : summary;
  return text || null;
}

function outputOptimizerRawHint(message: Message): string | null {
  const metadata = message.output_optimizer;
  if (!metadata || metadata.optimized !== true) return null;
  if (typeof metadata.raw_hint === "string" && metadata.raw_hint.trim()) return metadata.raw_hint.trim();
  if (typeof metadata.artifact_id === "string" && metadata.artifact_id.trim()) {
    return `read_long_tool_output('${metadata.artifact_id.trim()}', chunk_number=1)`;
  }
  return null;
}

function isImportantSystemMessage(message: Message): boolean {
  // System messages are part of the chronological transcript skeleton at every
  // verbosity. In particular, the root system prompt must remain reachable
  // after loading and revealing the oldest page.
  return message.role === "system" && Boolean(contentToPlainText(message.content, message.content_text || "").trim());
}

function plural(count: number, singular: string, pluralText?: string): string {
  return `${count} ${count === 1 ? singular : pluralText || `${singular}s`}`;
}

function hiddenSummaryCountsText(details: HiddenDetail[]): string {
  const counts: Record<HiddenDetailKind, number> = {
    reasoning: 0,
    tool_calls: 0,
    tool_results: 0,
  };
  details.forEach((detail) => {
    counts[detail.kind] += 1;
  });
  const parts: string[] = [];
  if (counts.tool_calls > 0) parts.push(`Executed ${plural(counts.tool_calls, "tool")}`);
  if (counts.tool_results > 0) parts.push(`got ${plural(counts.tool_results, "tool result")}`);
  if (counts.reasoning > 0) parts.push(plural(counts.reasoning, "reasoning block"));
  const tokenTotal = details.reduce(
    (total, detail) => total + (Number.isFinite(detail.tokens || 0) ? Math.max(0, Math.trunc(detail.tokens || 0)) : 0),
    0
  );
  if (tokenTotal > 0) parts.push(`total tokens ${tokenTotal.toLocaleString()}`);
  return parts.join(", ") || "Hidden details";
}

function HiddenDetailsBlock({
  details,
  sourceMessage,
  showBorders = true,
}: {
  details: HiddenDetail[];
  sourceMessage?: Message;
  showBorders?: boolean;
}) {
  const [selectedDetail, setSelectedDetail] = useState<HiddenDetail | null>(null);
  if (!details.length) return null;
  const toolDetails = details
    .filter((detail) => (detail.kind === "tool_calls" || detail.kind === "tool_results") && Boolean(detail.name))
    .map((detail) => {
      if (detail.source !== "tool_result") return detail;
      const header = [
        `Tool result: ${detail.name}`,
        detail.tool_call_id ? `tool_call_id: ${detail.tool_call_id}` : "",
      ].filter(Boolean).join("\n");
      return {
        ...detail,
        body: [header, "", "Result:", detail.body || "(empty)"].join("\n"),
      };
    });
  return (
    <div
      className={clsx("eggw-message-card eggw-role-card eggw-role-tool", !showBorders && "eggw-role-card-borderless")}
      data-testid="hidden-details"
      data-source-message-id={sourceMessage?.id || undefined}
      data-source-event-seq={sourceMessage?.event_seq ?? undefined}
    >
      <div className="eggw-hidden-summary">{hiddenSummaryCountsText(details)}</div>
      {toolDetails.length > 0 && (
        <div className="eggw-hidden-tools">
          <span>Tools: </span>
          {toolDetails.map((detail, index) => (
            <span key={`${detail.kind}-${index}-${detail.name || "tool"}`}>
              <button
                type="button"
                className="eggw-link"
                title={detail.body ? `Show ${detail.header}` : detail.header}
                onClick={() => setSelectedDetail(detail)}
              >
                {detail.name}
              </button>
              {detail.tool_call_id && (
                <span className="eggw-message-meta ml-1 font-mono" title={detail.tool_call_id}>
                  {shortToolCallId(detail.tool_call_id)}
                </span>
              )}
              {index < toolDetails.length - 1 ? ", " : null}
            </span>
          ))}
        </div>
      )}
      {selectedDetail && (
        <OverlayPanel
          open
          onClose={() => setSelectedDetail(null)}
          title={selectedDetail.header}
          description={selectedDetail.name || "Hidden transcript detail"}
          closeLabel="Close hidden detail"
          testId="hidden-detail-dialog"
          portal
        >
          {selectedDetail.body ? (
            <pre className="eggw-code-block max-h-[70vh] whitespace-pre-wrap break-words">{selectedDetail.body}</pre>
          ) : (
            <div className="eggw-transcript-state eggw-status-neutral">No detail body is available.</div>
          )}
        </OverlayPanel>
      )}
    </div>
  );
}

function uniqueThreadSuffixMap(threadIds: unknown): Map<string, string> {
  if (!Array.isArray(threadIds)) return new Map();
  const counts = new Map<string, number>();
  const ids: string[] = [];
  for (const raw of threadIds) {
    const threadId = typeof raw === "string" ? raw : "";
    if (threadId.length < THREAD_LINK_SUFFIX_LENGTH) continue;
    ids.push(threadId);
    const suffix = threadId.slice(-THREAD_LINK_SUFFIX_LENGTH).toUpperCase();
    counts.set(suffix, (counts.get(suffix) || 0) + 1);
  }

  const suffixMap = new Map<string, string>();
  for (const threadId of ids) {
    const suffix = threadId.slice(-THREAD_LINK_SUFFIX_LENGTH).toUpperCase();
    if (counts.get(suffix) === 1) suffixMap.set(suffix, threadId);
  }
  return suffixMap;
}

function ThreadCommandOutput({ content, threadIds }: { content: string; threadIds: unknown }) {
  const suffixMap = uniqueThreadSuffixMap(threadIds);
  const tokenPattern = /\b[A-Za-z0-9]{8}\b/g;

  const renderLine = (line: string, lineIndex: number): ReactNode[] => {
    const nodes: ReactNode[] = [];
    let lastIndex = 0;
    let match: RegExpExecArray | null;
    tokenPattern.lastIndex = 0;
    while ((match = tokenPattern.exec(line)) !== null) {
      const token = match[0];
      const fullThreadId = suffixMap.get(token.toUpperCase());
      if (!fullThreadId) continue;
      if (match.index > lastIndex) {
        nodes.push(line.slice(lastIndex, match.index));
      }
      nodes.push(
        <Link
          key={`${lineIndex}-${match.index}-${token}`}
          href={`/${fullThreadId}`}
          className="eggw-link font-semibold"
          title={`Open thread ${fullThreadId}`}
        >
          {token}
        </Link>
      );
      lastIndex = match.index + token.length;
    }
    if (lastIndex < line.length) nodes.push(line.slice(lastIndex));
    return nodes.length ? nodes : [line];
  };

  return (
    <pre className="eggw-code-block whitespace-pre-wrap">
      {content.split("\n").map((line, lineIndex, lines) => (
        <span key={lineIndex}>
          {renderLine(line, lineIndex)}
          {lineIndex < lines.length - 1 ? "\n" : null}
        </span>
      ))}
    </pre>
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
  const imagePreviewClassName = "eggw-attachment-preview";
  const hasImagePart = parts.some((part) => (isAttachmentPart(part) || isArtifactPart(part)) && isImageContentPart(part));
  const imagePreviewStyle = { maxHeight: MESSAGE_IMAGE_PREVIEW_MAX_HEIGHT };

  const handleUseAsAttachment = useCallback(
    async (part: Extract<ContentPart, { type: "artifact" }>) => {
      if (!currentThreadId || !part.artifact_id || !onStageAttachment) return;
      const descendantThreadId = part.owner_thread_id && part.owner_thread_id !== currentThreadId ? part.owner_thread_id : undefined;
      setPromotingArtifactIds((prev) => ({
        ...prev,
        [part.artifact_id]: true,
      }));
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
    },
    [addSystemLog, currentThreadId, onStageAttachment]
  );

  return (
    <div className="space-y-2">
      {parts.map((part, idx) => {
        if (isTextPart(part)) {
          return (
            <div key={`text-${idx}`} className={clsx("whitespace-pre-wrap text-sm", hasImagePart && "text-center")}>
              {part.text}
            </div>
          );
        }
        if (isAttachmentPart(part)) {
          const isImage = isImageContentPart(part);
          const canLink = Boolean(currentThreadId && part.input_id);
          const descendantThreadId =
            canLink && part.owner_thread_id && part.owner_thread_id !== currentThreadId ? part.owner_thread_id : undefined;
          const openUrl = canLink
            ? attachmentUrl(currentThreadId!, part.input_id, {
                descendantThreadId,
              })
            : null;
          const downloadUrl = canLink
            ? attachmentUrl(currentThreadId!, part.input_id, {
                descendantThreadId,
                download: true,
              })
            : null;
          return (
            <div
              key={`${part.input_id || "attachment"}-${idx}`}
              className={clsx("eggw-attachment-card", isImage && "text-center", !showBorders && "eggw-attachment-borderless")}
              title={attachmentPlaceholder(part)}
            >
              <div className={clsx("flex flex-wrap items-center gap-2", isImage && "justify-center")}>
                <span className="font-medium">Attachment</span>
                <span>{attachmentFilename(part)}</span>
                <span className="eggw-attachment-kind">{part.presentation || "file"}</span>
                <span className="eggw-attachment-meta">{part.mime_type || "application/octet-stream"}</span>
                <span className="eggw-attachment-meta">{formatBytes(part.size_bytes)}</span>
              </div>
              <div className="eggw-attachment-description">{attachmentPlaceholder(part)}</div>
              {openUrl && isImage && (
                <ProtectedFileLink
                  url={openUrl}
                  newWindow
                  className="mx-auto mt-3 block w-fit"
                  aria-label={`Open preview of ${attachmentFilename(part)}`}
                >
                  <ProtectedImage
                    url={openUrl}
                    alt={`Preview of ${attachmentFilename(part)}`}
                    loading="lazy"
                    decoding="async"
                    data-testid="attachment-preview"
                    className={imagePreviewClassName}
                    style={imagePreviewStyle}
                    onError={(event) => {
                      event.currentTarget.style.display = "none";
                    }}
                  />
                </ProtectedFileLink>
              )}
              {openUrl && downloadUrl && (
                <div className={clsx("mt-2 flex flex-wrap gap-3 text-xs", isImage && "justify-center")}>
                  <ProtectedFileLink url={openUrl} newWindow className="eggw-link">
                    Open
                  </ProtectedFileLink>
                  <ProtectedFileLink url={downloadUrl} filename={attachmentFilename(part)} className="eggw-link">
                    Download
                  </ProtectedFileLink>
                </div>
              )}
            </div>
          );
        }
        if (isArtifactPart(part)) {
          const isImage = isImageContentPart(part);
          const canLink = Boolean(currentThreadId && part.artifact_id);
          const canPromote = Boolean(canLink && onStageAttachment);
          const descendantThreadId =
            canLink && part.owner_thread_id && part.owner_thread_id !== currentThreadId ? part.owner_thread_id : undefined;
          const openUrl = canLink
            ? providerOutputUrl(currentThreadId!, part.artifact_id, {
                descendantThreadId,
              })
            : null;
          const downloadUrl = canLink
            ? providerOutputUrl(currentThreadId!, part.artifact_id, {
                descendantThreadId,
                download: true,
              })
            : null;
          return (
            <div
              key={`${part.artifact_id || "artifact"}-${idx}`}
              className={clsx("eggw-attachment-card", isImage && "text-center", !showBorders && "eggw-attachment-borderless")}
              title={artifactPlaceholder(part)}
            >
              <div className={clsx("flex flex-wrap items-center gap-2", isImage && "justify-center")}>
                <span className="font-medium">Provider artifact</span>
                <span>{artifactFilename(part)}</span>
                <span className="eggw-attachment-kind">{part.presentation || "file"}</span>
                <span className="eggw-attachment-meta">{part.mime_type || "application/octet-stream"}</span>
                <span className="eggw-attachment-meta">{formatBytes(part.size_bytes)}</span>
              </div>
              <div className="eggw-attachment-description">{artifactPlaceholder(part)}</div>
              {openUrl && isImage && (
                <ProtectedFileLink
                  url={openUrl}
                  newWindow
                  className="mx-auto mt-3 block w-fit"
                  aria-label={`Open preview of ${artifactFilename(part)}`}
                >
                  <ProtectedImage
                    url={openUrl}
                    alt={`Preview of ${artifactFilename(part)}`}
                    loading="lazy"
                    decoding="async"
                    data-testid="provider-artifact-preview"
                    className={imagePreviewClassName}
                    style={imagePreviewStyle}
                    onError={(event) => {
                      event.currentTarget.style.display = "none";
                    }}
                  />
                </ProtectedFileLink>
              )}
              {openUrl && downloadUrl && (
                <div className={clsx("mt-2 flex flex-wrap gap-3 text-xs", isImage && "justify-center")}>
                  <ProtectedFileLink url={openUrl} newWindow className="eggw-link">
                    Open
                  </ProtectedFileLink>
                  <ProtectedFileLink url={downloadUrl} filename={artifactFilename(part)} className="eggw-link">
                    Download
                  </ProtectedFileLink>
                  {canPromote && (
                    <button
                      type="button"
                      onClick={() => handleUseAsAttachment(part)}
                      disabled={Boolean(promotingArtifactIds[part.artifact_id])}
                      className="eggw-link disabled:cursor-not-allowed disabled:opacity-50"
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
          <pre key={`unknown-${idx}`} className="eggw-code-block">
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
  const details = [
    message.marker_event_seq ? `marker #${message.marker_event_seq}` : null,
    message.start_event_seq ? `start event #${message.start_event_seq}` : null,
    message.selector ? `selector ${message.selector}` : null,
    message.created_by ? `by ${message.created_by}` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="eggw-compaction-marker" data-testid="compaction-marker" role="separator" aria-label="Compaction boundary">
      <div className="eggw-compaction-line" />
      <div className="eggw-compaction-label" title={contentToPlainText(message.content, message.content_text || "") || undefined}>
        Compaction boundary: API context now starts at {startShort ? `msg_${startShort}` : "the selected message"}
        {details && <span className="eggw-compaction-detail">({details})</span>}
      </div>
      <div className="eggw-compaction-line" />
    </div>
  );
}

function appendBufferedTextChunks(element: HTMLElement, chunks: string[], startIndex: number): boolean {
  if (startIndex >= chunks.length) return false;
  const text = chunks.slice(startIndex).join("");
  if (!text) return false;
  element.appendChild(document.createTextNode(text));
  return true;
}

function nestedScrollportConsumesWheel(target: EventTarget | null, outer: HTMLElement, deltaY: number): boolean {
  let element = target instanceof HTMLElement ? target : null;
  while (element && element !== outer) {
    const { overflowY } = window.getComputedStyle(element);
    const canScroll = /(auto|scroll)/.test(overflowY) && element.scrollHeight > element.clientHeight;
    if (canScroll) {
      const distanceFromEnd = element.scrollHeight - element.scrollTop - element.clientHeight;
      if ((deltaY < 0 && element.scrollTop > 0) || (deltaY > 0 && distanceFromEnd > 1)) {
        return true;
      }
    }
    element = element.parentElement;
  }
  return false;
}

const MessageBlock = memo(function MessageBlock({
  message,
  showBorders = true,
  displayVerbosity = "max",
  onStageAttachment,
}: MessageBlockProps) {
  const currentThreadId = useAppStore((state) => state.currentThreadId);
  const openEditAnswerModal = useAppStore((state) => state.openEditAnswerModal);
  const addSystemLog = useAppStore((state) => state.addSystemLog);
  const [isPreparingEditAnswer, setIsPreparingEditAnswer] = useState(false);

  const handleQuoteEdit = useCallback(async () => {
    if (!currentThreadId || !message.id) return;
    setIsPreparingEditAnswer(true);
    try {
      const draft = await createEditAnswerDraft(currentThreadId, {
        source_msg_id: message.id,
      });
      openEditAnswerModal({
        threadId: currentThreadId,
        draft: draft.draft,
        sourceMsgId: draft.source_msg_id,
        sourceKind: draft.source_kind,
        sourceSuffix: draft.source_suffix || "",
        sourceLabel: draft.source_label || "",
        origin: "quote_button",
      });
      addSystemLog(draft.message || "Prepared quoted assistant answer", "success");
    } catch (error) {
      addSystemLog(error instanceof Error ? error.message : "Failed to prepare edit-answer draft", "error");
    } finally {
      setIsPreparingEditAnswer(false);
    }
  }, [addSystemLog, currentThreadId, message.id, openEditAnswerModal]);

  if (message.kind === "compaction_marker" || message.role === "compaction_marker") {
    return <CompactionMarker message={message} />;
  }

  const roleLabels: Record<string, string> = {
    user: "User",
    assistant: "Assistant",
    assistant_note: "Assistant Note",
    system: "Command",
    tool: "Tool Result",
  };

  const displayRole = message.answer_user_preserve_turn && message.role === "assistant" ? "assistant_note" : message.role;
  const baseRoleLabel =
    message.recovery_notice && message.role === "system"
      ? "Continue Status"
      : message.role === "system" && !message.command_name && !message.id?.startsWith("cmd-")
      ? "System"
      : roleLabels[displayRole] || displayRole;
  const roleLabel =
    message.role === "tool"
      ? message.name
        ? `${baseRoleLabel}: ${message.name}`
        : toolDisplayName("", message.tool_call_id, "Tool result")
      : baseRoleLabel;

  // Check if this is a shell command (starts with $ or $$)
  // Handle cases: "$ cmd", "$$ cmd", "$cmd" (no space)
  const contentText = contentToPlainText(message.content, message.content_text || "");
  const stringContent = typeof message.content === "string" ? message.content : contentText;
  const isShellCommand = message.role === "user" && typeof message.content === "string" && message.content.match(/^\$\$?\s*\S/);

  // Check if this is a system/command message (should render as monospace)
  const isCommandOutput = message.role === "system";
  const isThreadsCommandOutput = isCommandOutput && message.command_name === "threads";

  const roleClass = displayRole === "assistant_note" ? "eggw-role-assistant-note" : `eggw-role-${displayRole}`;

  const messageTps = formatStreamingTps(message.tps);
  const tokenText = formatTokenCount(message.tokens);
  const toolCalls = message.tool_calls || [];
  const toolStreamEntries = stringRecordEntries(message.tool_stream);
  const toolCallStreamEntries = stringRecordEntries(message.tool_calls_stream);
  const optimizerSummary = outputOptimizerSummaryText(message, Boolean(message.output_optimizer?.artifact_available));
  const optimizerRawHint = outputOptimizerRawHint(message);
  const showReasoningBlock = Boolean(message.reasoning) && displayVerbosity !== "min";
  const hideToolBody = displayVerbosity === "min" && message.role === "tool";
  const showContent = Boolean(contentText) && !hideToolBody;
  const showToolCalls = toolCalls.length > 0 && displayVerbosity !== "min";
  const showStreamedMetadata = displayVerbosity !== "min" && (toolStreamEntries.length > 0 || toolCallStreamEntries.length > 0);
  const canQuoteEdit = Boolean(
    currentThreadId &&
      message.id &&
      !message.id.startsWith("temp-") &&
      contentText.trim() &&
      (displayRole === "assistant" || displayRole === "assistant_note")
  );

  return (
    <div
      className={clsx(
        "eggw-message-card eggw-role-card",
        isShellCommand ? "eggw-role-shell" : roleClass,
        !showBorders && "eggw-role-card-borderless"
      )}
      data-message-role={displayRole}
      data-message-id={message.id || undefined}
      data-consumed-by-tool-call-id={message.consumed_by_tool_call_id || undefined}
    >
      {/* Header */}
      <div className="eggw-message-header">
        <span className="eggw-role-marker" aria-hidden="true" />
        <span className="eggw-role-label">{isShellCommand ? "Shell" : roleLabel}</span>
        {displayVerbosity !== "min" && message.model_key && <span className="eggw-message-meta">{message.model_key}</span>}
        {displayVerbosity !== "min" && tokenText && <span className="eggw-message-meta">{tokenText}</span>}
        {displayVerbosity !== "min" && messageTps && <span className="eggw-message-meta">{messageTps}</span>}
        {displayVerbosity === "max" && message.timestamp && (
          <span className="eggw-message-meta font-mono">
            {new Date(message.timestamp).toLocaleString(undefined, {
              year: "numeric",
              month: "2-digit",
              day: "2-digit",
              hour: "2-digit",
              minute: "2-digit",
              second: "2-digit",
            })}
          </span>
        )}
        {displayVerbosity === "max" && message.id && message.id.length >= 8 && !message.id.startsWith("temp-") && (
          <button
            type="button"
            className="eggw-message-id font-mono"
            title={`Click to copy: ${message.id}`}
            aria-label={`Message id ${message.id}; click to copy`}
            data-testid="message-id"
            onClick={() => {
              navigator.clipboard.writeText(message.id);
            }}
          >
            [msg_id: {message.id}]
          </button>
        )}
        {displayVerbosity === "max" && message.tool_call_id && (
          <span className="eggw-message-meta font-mono">← {message.tool_call_id.slice(-8)}</span>
        )}
        {displayVerbosity !== "min" && optimizerSummary && (
          <StatusChip
            tone="info"
            title={optimizerRawHint ? `Raw output: ${optimizerRawHint}` : optimizerSummary}
            data-testid="output-optimizer-badge"
          >
            {optimizerSummary}
          </StatusChip>
        )}
        {canQuoteEdit && (
          <Button
            variant="ghost"
            onClick={handleQuoteEdit}
            disabled={isPreparingEditAnswer}
            className="eggw-message-action"
            aria-label={`Quote/Edit ${roleLabel} ${message.id}`}
            title={`Quote/Edit ${roleLabel}${message.id ? ` ${message.id.slice(-8)}` : ""}`}
            data-testid="quote-edit-button"
          >
            {isPreparingEditAnswer ? "Preparing…" : "Quote/Edit"}
          </Button>
        )}
      </div>

      {optimizerRawHint && displayVerbosity !== "min" && (
        <div className="eggw-message-meta mb-2 font-mono" data-testid="raw-output-affordance">
          Raw output: {optimizerRawHint}
        </div>
      )}

      {/* Reasoning (collapsible) */}
      {showReasoningBlock && (
        <details
          open={displayVerbosity === "max" ? true : undefined}
          className={clsx("eggw-detail-block eggw-role-reasoning", !showBorders && "eggw-detail-borderless")}
        >
          <summary className="eggw-detail-summary">
            Reasoning
            {displayVerbosity === "medium" && message.reasoning && (
              <span className="eggw-message-meta ml-2 font-mono">{message.reasoning.length.toLocaleString()} chars</span>
            )}
          </summary>
          <div className="eggw-detail-content whitespace-pre-wrap">{message.reasoning}</div>
        </details>
      )}

      {/* Content */}
      {showContent && (
        <>
          {/* Shell command display */}
          {isShellCommand ? (
            <pre className="eggw-code-block eggw-shell-command">{stringContent}</pre>
          ) : isCommandOutput ? (
            /* Command output (system messages) - monospace for tree/list formatting */
            isThreadsCommandOutput ? (
              <ThreadCommandOutput content={contentText} threadIds={message.command_data?.thread_ids} />
            ) : (
              <pre className="eggw-code-block whitespace-pre-wrap">{contentText}</pre>
            )
          ) : isContentPartArray(message.content) ? (
            <ContentPartsView parts={message.content} showBorders={showBorders} onStageAttachment={onStageAttachment} />
          ) : message.role === "tool" ? (
            displayVerbosity === "medium" ? (
              <details className={clsx("eggw-detail-block eggw-role-tool", !showBorders && "eggw-detail-borderless")}>
                <summary className="eggw-detail-summary">
                  {oneLinePreview(contentText) || `Output (${contentText.length.toLocaleString()} chars)`}
                </summary>
                <pre className="eggw-code-block max-h-96 whitespace-pre-wrap">{contentText}</pre>
              </details>
            ) : (
              <pre className="eggw-code-block max-h-96 whitespace-pre-wrap">{contentText}</pre>
            )
          ) : (
            /* Regular markdown content with GFM tables and LaTeX support */
            <div className="prose eggw-prose max-w-none">
              <ReactMarkdown
                remarkPlugins={[remarkGfm, remarkMath]}
                rehypePlugins={[rehypeRaw, rehypeKatex]}
                components={{
                  code({ node, className, children, ...props }) {
                    const match = /language-(\w+)/.exec(className || "");
                    const inline = !match;
                    return !inline ? (
                      <SyntaxHighlighter style={eggwSyntaxTheme} language={match[1]} PreTag="div">
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
                        <table className="min-w-full border-collapse border">{children}</table>
                      </div>
                    );
                  },
                  thead({ children }) {
                    return <thead>{children}</thead>;
                  },
                  th({ children }) {
                    return <th className="px-4 py-2 text-left border font-semibold">{children}</th>;
                  },
                  td({ children }) {
                    return <td className="px-4 py-2 border">{children}</td>;
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
          {toolCalls.map((tc: any, idx: number) => {
            const toolCallIdText = toolCallId(tc);
            const toolName = toolDisplayName(toolCallName(tc), toolCallIdText, "Tool call");
            const args = toolCallArgs(tc);
            const isBash = toolName === "bash";
            const script = isBash && typeof args === "object" && args !== null && "script" in args ? (args as any).script : null;

            return (
              <details
                key={toolCallIdText || idx}
                open={displayVerbosity === "max" ? true : undefined}
                className={clsx("eggw-detail-block eggw-role-tool-call", !showBorders && "eggw-detail-borderless")}
              >
                <summary className="eggw-detail-summary flex-wrap">
                  <span className="font-medium">{toolName}</span>
                  {toolCallIdText && <span className="eggw-message-meta font-mono">{toolCallIdText.slice(-8)}</span>}
                  {displayVerbosity === "medium" && <span className="eggw-message-meta font-mono">{oneLinePreview(args)}</span>}
                </summary>
                {/* Special display for bash scripts */}
                {isBash && script ? (
                  <pre className="eggw-code-block eggw-shell-command whitespace-pre-wrap break-all">$ {String(script)}</pre>
                ) : (
                  <pre className="eggw-code-block max-h-40 whitespace-pre-wrap break-words">
                    {typeof args === "string" ? args : JSON.stringify(args, null, 2)}
                  </pre>
                )}
              </details>
            );
          })}
        </div>
      )}

      {/* Persisted streamed tool metadata (historical/reloaded transcript) */}
      {showStreamedMetadata && (
        <div className="mt-2 space-y-2">
          {toolStreamEntries.map(([name, text]) => (
            <details
              key={`tool-stream-${name}`}
              open={displayVerbosity === "max" ? true : undefined}
              className={clsx("eggw-detail-block eggw-role-tool", !showBorders && "eggw-detail-borderless")}
            >
              <summary className="eggw-detail-summary font-mono">
                {displayVerbosity === "medium" ? `Tool Output: ${name} · ${text.length.toLocaleString()} chars` : `Tool Output: ${name}`}
              </summary>
              {displayVerbosity === "medium" && <div className="mt-1 text-xs font-mono">{oneLinePreview(text)}</div>}
              <pre className="eggw-code-block max-h-64 whitespace-pre-wrap">{text}</pre>
            </details>
          ))}

          {toolCallStreamEntries.map(([streamKey, text]) => (
            <details
              key={`tool-call-stream-${streamKey}`}
              open={displayVerbosity === "max" ? true : undefined}
              className={clsx("eggw-detail-block eggw-role-tool-call", !showBorders && "eggw-detail-borderless")}
            >
              <summary className="eggw-detail-summary font-mono">
                {displayVerbosity === "medium"
                  ? `Tool Call Args: ${streamKey} · ${text.length.toLocaleString()} chars`
                  : `Tool Call Args: ${streamKey}`}
              </summary>
              {displayVerbosity === "medium" && <div className="eggw-detail-content font-mono">{oneLinePreview(text)}</div>}
              <pre className="eggw-code-block max-h-40 whitespace-pre-wrap break-words">{text}</pre>
            </details>
          ))}
        </div>
      )}
    </div>
  );
});

function collectHiddenDetailsForMessage(message: Message): HiddenDetail[] {
  const details: HiddenDetail[] = [];
  const toolCallNameById = new Map<string, string>();
  (message.tool_calls || []).forEach((toolCall: any) => {
    const toolCallIdText = toolCallId(toolCall);
    const explicitName = toolCallName(toolCall);
    if (toolCallIdText && explicitName) toolCallNameById.set(toolCallIdText, explicitName);
  });
  let availableTokens = typeof message.tokens === "number" && Number.isFinite(message.tokens) ? message.tokens : undefined;
  const takeTokens = () => {
    const tokens = availableTokens;
    availableTokens = undefined;
    return tokens;
  };
  if (message.reasoning) {
    details.push({
      kind: "reasoning",
      header: messageMetadataText(message, "Reasoning"),
      tokens: takeTokens(),
      source: "reasoning",
    });
  }
  if (message.tool_calls?.length) {
    message.tool_calls.forEach((tc: any) => {
      const toolCallIdText = toolCallId(tc);
      const name = toolDisplayName(toolCallName(tc), toolCallIdText, "Tool call");
      const args = toolCallArgs(tc);
      details.push({
        kind: "tool_calls",
        name,
        tool_call_id: toolCallIdText,
        tokens: takeTokens(),
        header: name ? `ToolCall: ${name}` : "ToolCall",
        body: formatHiddenDetailBody({
          ...(toolCallIdText ? { id: toolCallIdText } : {}),
          name,
          arguments: args,
        }),
        source: "tool_call",
      });
    });
  }
  if (message.role === "tool") {
    const contentText = contentToPlainText(message.content, message.content_text || "");
    const name = toolDisplayName(message.name, message.tool_call_id, "Tool result");
    details.push({
      kind: "tool_results",
      name,
      tool_call_id: message.tool_call_id,
      tokens: takeTokens(),
      header: name ? `Tool Result: ${name}` : "Tool Result",
      body: contentText,
      source: "tool_result",
    });
  }
  stringRecordEntries(message.tool_stream).forEach(([name, text]) => {
    details.push({
      kind: "tool_results",
      name,
      tokens: takeTokens(),
      header: name ? `Tool Output: ${name}` : "Tool Output",
      body: text,
      source: "tool_stream",
    });
  });
  stringRecordEntries(message.tool_calls_stream).forEach(([streamKey, text]) => {
    const structuredName = toolCallNameById.get(streamKey);
    details.push({
      kind: "tool_calls",
      name: structuredName || streamKey,
      // Historical snapshots sometimes keyed this dictionary by tool name
      // rather than ID. Only claim identity when the same message proves it.
      tool_call_id: structuredName ? streamKey : undefined,
      tokens: takeTokens(),
      header: streamKey ? `Tool Call Args: ${streamKey}` : "Tool Call Args",
      body: text,
      source: "tool_call_stream",
    });
  });
  return details;
}

function renderMessagesForVerbosity(
  messages: Message[],
  displayVerbosity: DisplayVerbosity,
  showBorders: boolean,
  onStageAttachment?: (attachment: AttachmentContentPart) => void
): ReactNode[] {
  const displayMessages = resolveToolResultNames(messages);
  if (displayVerbosity !== "min") {
    return displayMessages.map((msg, idx) => (
      <MessageBlock
        key={msg.id || idx}
        message={msg}
        showBorders={showBorders}
        displayVerbosity={displayVerbosity}
        onStageAttachment={onStageAttachment}
      />
    ));
  }

  // Keep compact details message-local. The ordered transcript is the sole
  // chronology authority; tool identity is for inspection, never relocation.
  const nodes: ReactNode[] = [];
  const answeredGetUserIds = new Set(displayMessages.map(getUserAnswerToolCallId).filter(Boolean));

  displayMessages.forEach((msg, idx) => {
    if (msg.kind === "compaction_marker" || msg.role === "compaction_marker") {
      nodes.push(<CompactionMarker key={msg.id || `marker-${idx}`} message={msg} />);
      return;
    }

    const getUserCallIds = new Set(getUserToolCallIds(msg));
    const hiddenDetails = collectHiddenDetailsForMessage(msg).filter(
      (detail) =>
        !(
          detail.tool_call_id &&
          answeredGetUserIds.has(detail.tool_call_id) &&
          (getUserCallIds.has(detail.tool_call_id) || detail.source === "tool_result" || detail.source === "tool_call_stream")
        )
    );
    const reasoningDetails = hiddenDetails.filter((detail) => detail.kind === "reasoning");
    const operationalDetails = hiddenDetails.filter((detail) => detail.kind !== "reasoning");
    const hasVisibleConversationBody =
      (msg.role === "user" || msg.role === "assistant") && Boolean(contentToPlainText(msg.content, msg.content_text || "").trim());

    if (reasoningDetails.length) {
      nodes.push(
        <HiddenDetailsBlock
          key={`reasoning-${msg.id || idx}`}
          details={reasoningDetails}
          sourceMessage={msg}
          showBorders={showBorders}
        />
      );
    }
    if (hasVisibleConversationBody || isImportantSystemMessage(msg)) {
      nodes.push(
        <MessageBlock
          key={msg.id || idx}
          message={msg}
          showBorders={showBorders}
          displayVerbosity="min"
          onStageAttachment={onStageAttachment}
        />
      );
    }
    if (operationalDetails.length) {
      nodes.push(
        <HiddenDetailsBlock
          key={`details-${msg.id || idx}`}
          details={operationalDetails}
          sourceMessage={msg}
          showBorders={showBorders}
        />
      );
    }
  });

  return nodes;
}

function StaticTranscriptImpl({
  messages,
  displayVerbosity,
  showBorders,
  onStageAttachment,
}: {
  messages: Message[];
  displayVerbosity: DisplayVerbosity;
  showBorders: boolean;
  onStageAttachment?: (attachment: AttachmentContentPart) => void;
}) {
  const content = <div data-testid="static-transcript-owner">{renderMessagesForVerbosity(
    messages,
    displayVerbosity,
    showBorders,
    onStageAttachment,
  )}</div>;
  if (process.env.NODE_ENV === "production") return content;
  return (
    <Profiler id="StaticTranscript" onRender={(id, _phase, duration) => recordReactCommit(id as "StaticTranscript", duration)}>
      {content}
    </Profiler>
  );
}

const StaticTranscript = memo(
  StaticTranscriptImpl,
  (previous, next) =>
    previous.messages === next.messages &&
    previous.displayVerbosity === next.displayVerbosity &&
    previous.showBorders === next.showBorders &&
    previous.onStageAttachment === next.onStageAttachment,
);

interface ChatPanelProps {
  threadId: string;
  showBorders?: boolean;
  streamingTps?: number | null;
  onStageAttachment?: (attachment: AttachmentContentPart) => void;
}

function ChatPanelImpl({ threadId, showBorders = true, streamingTps = null, onStageAttachment }: ChatPanelProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const streamingContentRef = useRef<HTMLDivElement>(null);
  const streamingReasoningRef = useRef<HTMLDivElement>(null);
  const streamingReasoningSummaryRef = useRef<HTMLDivElement>(null);
  const streamingToolOutputRefs = useRef<Record<string, HTMLPreElement | null>>({});
  const streamingToolCallArgRefs = useRef<Record<string, HTMLPreElement | null>>({});
  const streamingToolCallPreviewRefs = useRef<Record<string, HTMLSpanElement | null>>({});
  const lastContentIndexRef = useRef(0);
  const lastReasoningIndexRef = useRef(0);
  const lastReasoningSummaryIndexRef = useRef(0);
  const lastToolOutputIndexRef = useRef<Record<string, number>>({});
  const lastToolCallArgIndexRef = useRef<Record<string, number>>({});
  const lastToolCallChunksRef = useRef<Record<string, string[]>>({});
  const streamingTextFlushRafRef = useRef<number | null>(null);
  const streamingToolFlushRafRef = useRef<number | null>(null);
  const streamingToolCallFlushRef = useRef<AnimationFrameCoalescer | null>(null);
  const streamingToolPreviewFlushRef = useRef<IntervalCoalescer<number> | null>(null);
  const loadingOlderRef = useRef(false);
  const revealingLoadedRef = useRef(false);
  const historyDemandRef = useRef<HistoryDemandState>(IDLE_HISTORY_DEMAND);
  const historyDemandRunRef = useRef<(() => void) | null>(null);
  const historyBoundaryRafRef = useRef<number | null>(null);
  const pendingHistoryBoundaryRef = useRef<{
    token: number;
    scrollport: HTMLDivElement;
  } | null>(null);
  const historyBoundaryTokenRef = useRef(0);
  const historyOperationRef = useRef(0);
  const pendingHistoryAnchorRef = useRef<{
    anchor: { id: string; offset: number } | null;
    fallbackTop: number;
    fallbackHeight: number;
    operation: number;
  } | null>(null);
  const [isLoadingOlder, setIsLoadingOlder] = useState(false);
  const [renderStartIndex, setRenderStartIndex] = useState<number | null>(null);
  const routeIdentityRef = useRef(threadId);

  const currentThreadId = threadId;
  const queryClient = useQueryClient();
  const transcriptQuery = useInfiniteQuery(transcriptInfiniteQueryOptions(threadId, queryClient));
  const transcriptMetadata = useMemo(() => transcriptIndex(transcriptQuery.data), [transcriptQuery.data]);
  const totalMessages = transcriptMetadata.totalMessages;
  const hasOlderTranscript = Boolean(oldestTranscriptFrontier(transcriptQuery.data));
  const displayVerbosity = useAppStore((state) => state.displayVerbosity);
  const renderedTranscript = useMemo(
    () => transcriptRenderWindow(transcriptQuery.data, renderStartIndex),
    [transcriptQuery.data, renderStartIndex]
  );
  const historyAvailabilityRef = useRef({ canReveal: false, canFetch: false });
  historyAvailabilityRef.current = {
    canReveal: renderedTranscript.hiddenCount > 0,
    canFetch: renderedTranscript.hiddenCount === 0 && hasOlderTranscript,
  };
  const streamingToolCalls = useAppStore((state) => state.streamingByThread[threadId]?.streamingToolCalls);
  const streamingToolOutputs = useAppStore((state) => state.streamingByThread[threadId]?.streamingToolOutputs);
  const streamingModelKey = useAppStore((state) => state.streamingByThread[threadId]?.streamingModelKey || null);
  const streamingKind = useAppStore((state) => state.streamingByThread[threadId]?.streamingKind || null);
  const streamingStartedAtMs = useAppStore((state) => state.streamingByThread[threadId]?.streamingStartedAtMs || null);
  const streamingProviderRequest = useAppStore((state) => state.streamingByThread[threadId]?.streamingProviderRequest || null);
  const isStreaming = useAppStore((state) => state.streamingByThread[threadId]?.isStreaming || false);
  const visibleStreamingToolCalls = streamingToolCalls || {};
  const visibleStreamingToolOutputs = streamingToolOutputs || {};
  const hasLiveTools = Object.keys(visibleStreamingToolCalls).length > 0 || Object.keys(visibleStreamingToolOutputs).length > 0;
  const showLiveCard = renderedTranscript.atLiveTail && (isStreaming || hasLiveTools);
  // Ordinary live tools remain expanded at every verbosity. Timing owns its
  // one-second updates in memoized leaves, so transcript/layout ownership never
  // re-renders merely because countdown text changed.

  // Live-edge following is owned by explicit user intent. Scroll/layout events
  // never infer provenance from a coordinate that can become stale mid-commit.
  const liveEdgeStateRef = useRef<LiveEdgeState>("following");
  const rafIdRef = useRef<number | null>(null);
  if (routeIdentityRef.current !== threadId) {
    // Render-time refs make the upcoming layout commit route-local; the layout
    // effect below resets React-owned history state before paint.
    routeIdentityRef.current = threadId;
    liveEdgeStateRef.current = "following";
    historyDemandRef.current = IDLE_HISTORY_DEMAND;
    loadingOlderRef.current = false;
    revealingLoadedRef.current = false;
    pendingHistoryAnchorRef.current = null;
  }

  const distanceFromBottom = useCallback(() => {
    if (!scrollRef.current) return 0;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    return Math.max(0, scrollHeight - scrollTop - clientHeight);
  }, []);

  const isAtBottom = useCallback(() => {
    return distanceFromBottom() <= STICKY_BOTTOM_THRESHOLD_PX;
  }, [distanceFromBottom]);

  const scrollToBottomNow = useCallback(() => {
    const el = scrollRef.current;
    if (!el || liveEdgeStateRef.current !== "following") return;
    el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight);
  }, []);

  // Coalesce follow requests into one frame. React-owned geometry is corrected
  // synchronously by the layout effect below; this frame covers direct DOM
  // streaming writes and later image/font ResizeObserver callbacks.
  const scrollToBottom = useCallback(() => {
    if (liveEdgeStateRef.current !== "following" || rafIdRef.current !== null) return;
    rafIdRef.current = requestAnimationFrame(() => {
      rafIdRef.current = null;
      scrollToBottomNow();
    });
  }, [scrollToBottomNow]);

  const detachFromBottom = useCallback(() => {
    liveEdgeStateRef.current = reduceLiveEdgeState(liveEdgeStateRef.current, {
      type: "user_toward_history",
    });
    if (rafIdRef.current !== null) {
      cancelAnimationFrame(rafIdRef.current);
      rafIdRef.current = null;
    }
  }, []);

  const followBottom = useCallback(() => {
    // Re-entering live mode is explicit. A historical window's local bottom is
    // never evidence that the user reached the true transcript live edge.
    setRenderStartIndex(null);
    liveEdgeStateRef.current = reduceLiveEdgeState(liveEdgeStateRef.current, {
      type: "user_reached_live_edge",
    });
    requestAnimationFrame(scrollToBottom);
  }, [scrollToBottom]);

  const followLocalLiveEdge = useCallback(() => {
    if (!renderedTranscript.atLiveTail) return;
    liveEdgeStateRef.current = reduceLiveEdgeState(liveEdgeStateRef.current, {
      type: "user_reached_live_edge",
    });
    scrollToBottom();
  }, [renderedTranscript.atLiveTail, scrollToBottom]);

  const captureHistoryAnchor = useCallback(() => {
    const scrollport = scrollRef.current;
    if (!scrollport) return null;
    const scrollportTop = scrollport.getBoundingClientRect().top;
    const candidates = Array.from(scrollport.querySelectorAll<HTMLElement>("[data-message-id]"));
    for (const candidate of candidates) {
      const rect = candidate.getBoundingClientRect();
      if (rect.bottom > scrollportTop) {
        return {
          id: candidate.dataset.messageId || "",
          offset: rect.top - scrollportTop,
        };
      }
    }
    return null;
  }, []);

  const restoreHistoryAnchor = useCallback((anchor: { id: string; offset: number } | null, fallbackTop: number, fallbackHeight: number) => {
    const scrollport = scrollRef.current;
    if (!scrollport) return;
    const anchored = anchor
      ? Array.from(scrollport.querySelectorAll<HTMLElement>("[data-message-id]")).find(
          (candidate) => candidate.dataset.messageId === anchor.id
        ) || null
      : null;
    if (anchored && anchor) {
      const scrollportTop = scrollport.getBoundingClientRect().top;
      scrollport.scrollTop += anchored.getBoundingClientRect().top - scrollportTop - anchor.offset;
      return;
    }
    scrollport.scrollTop = fallbackTop + (scrollport.scrollHeight - fallbackHeight);
  }, []);

  const settleHistoryDemand = useCallback(
    (operation = historyOperationRef.current) => {
      if (operation !== historyOperationRef.current) return;
      if (pendingHistoryAnchorRef.current?.operation === operation) pendingHistoryAnchorRef.current = null;
      revealingLoadedRef.current = false;
      loadingOlderRef.current = false;
      setIsLoadingOlder(false);
      historyAvailabilityRef.current = {
        canReveal: renderedTranscript.hiddenCount > 0,
        canFetch: renderedTranscript.hiddenCount === 0 && hasOlderTranscript,
      };
      historyDemandRef.current = reduceHistoryDemand(historyDemandRef.current, { type: "settled" }, historyAvailabilityRef.current);
      if (historyDemandRef.current.phase !== "idle") queueMicrotask(() => historyDemandRunRef.current?.());
    },
    [renderedTranscript.hiddenCount, hasOlderTranscript]
  );

  const revealFromIndex = useCallback(
    (startIndex: number) => {
      const el = scrollRef.current;
      const anchor = captureHistoryAnchor();
      const previousScrollHeight = el?.scrollHeight ?? 0;
      const previousScrollTop = el?.scrollTop ?? 0;
      revealingLoadedRef.current = true;
      const operation = historyOperationRef.current + 1;
      historyOperationRef.current = operation;
      pendingHistoryAnchorRef.current = {
        anchor,
        fallbackTop: previousScrollTop,
        fallbackHeight: previousScrollHeight,
        operation,
      };
      setRenderStartIndex(startIndex);
    },
    [captureHistoryAnchor]
  );

  const expandLoadedTranscript = useCallback(() => {
    revealFromIndex(expandedTranscriptStartIndex(renderedTranscript.startIndex));
  }, [renderedTranscript.startIndex, revealFromIndex]);

  const showNewerTranscript = useCallback(() => {
    const nextStart = nextTranscriptStartIndex(
      renderedTranscript.startIndex,
      totalMessages,
    );
    if (nextStart === null) followBottom();
    else revealFromIndex(nextStart);
  }, [followBottom, renderedTranscript.startIndex, revealFromIndex, totalMessages]);

  const showLiveTranscript = useCallback(() => {
    followBottom();
  }, [followBottom]);

  const loadOlderMessages = useCallback(async () => {
    if (!hasOlderTranscript) {
      settleHistoryDemand();
      return;
    }
    const el = scrollRef.current;
    const anchor = captureHistoryAnchor();
    const previousScrollHeight = el?.scrollHeight ?? 0;
    const previousScrollTop = el?.scrollTop ?? 0;
    loadingOlderRef.current = true;
    setIsLoadingOlder(true);
    detachFromBottom();
    const operation = historyOperationRef.current + 1;
    historyOperationRef.current = operation;
    try {
      const updated = await fetchOlderTranscriptPage(queryClient, threadId);
      if (!updated) {
        restoreHistoryAnchor(anchor, previousScrollTop, previousScrollHeight);
        requestAnimationFrame(() => settleHistoryDemand(operation));
        return;
      }
      const updatedMetadata = transcriptIndex(updated);
      const addedCount = Math.max(0, updatedMetadata.totalMessages - totalMessages);
      const nextStartIndex = Math.max(
        0,
        renderedTranscript.startIndex + addedCount - TRANSCRIPT_WINDOW_MESSAGES,
      );
      pendingHistoryAnchorRef.current = {
        anchor,
        fallbackTop: previousScrollTop,
        fallbackHeight: previousScrollHeight,
        operation,
      };
      setRenderStartIndex(nextStartIndex);
    } catch (error) {
      console.error("Failed to load older messages:", error);
      settleHistoryDemand(operation);
    }
  }, [
    captureHistoryAnchor,
    detachFromBottom,
    renderedTranscript.startIndex,
    restoreHistoryAnchor,
    settleHistoryDemand,
    queryClient,
    threadId,
    hasOlderTranscript,
    totalMessages,
  ]);

  const runHistoryDemand = useCallback(() => {
    if (historyDemandRef.current.phase === "revealing") {
      expandLoadedTranscript();
    } else if (historyDemandRef.current.phase === "fetching") {
      void loadOlderMessages();
    }
  }, [expandLoadedTranscript, loadOlderMessages]);
  historyDemandRunRef.current = runHistoryDemand;

  const demandOlderHistory = useCallback(() => {
    detachFromBottom();
    const previous = historyDemandRef.current;
    const next = reduceHistoryDemand(
      previous,
      { type: "older_intent" },
      {
        canReveal: renderedTranscript.hiddenCount > 0,
        canFetch: renderedTranscript.hiddenCount === 0 && hasOlderTranscript,
      }
    );
    historyDemandRef.current = next;
    if (previous.phase === "idle" && next.phase !== "idle") runHistoryDemand();
  }, [detachFromBottom, renderedTranscript.hiddenCount, runHistoryDemand, hasOlderTranscript]);

  useEffect(() => {
    const pending = pendingHistoryAnchorRef.current;
    if (!pending) return;
    pendingHistoryAnchorRef.current = null;
    restoreHistoryAnchor(pending.anchor, pending.fallbackTop, pending.fallbackHeight);
    requestAnimationFrame(() => settleHistoryDemand(pending.operation));
  }, [renderStartIndex, renderedTranscript.startIndex, restoreHistoryAnchor, settleHistoryDemand]);

  const consumeHistoryBoundary = useCallback(
    (token: number, scrollport: HTMLDivElement) => {
      const pending = pendingHistoryBoundaryRef.current;
      if (!pending || pending.token !== token || pending.scrollport !== scrollport) return;
      pendingHistoryBoundaryRef.current = null;
      historyBoundaryTokenRef.current += 1;
      if (historyBoundaryRafRef.current !== null) {
        cancelAnimationFrame(historyBoundaryRafRef.current);
        historyBoundaryRafRef.current = null;
      }
      demandOlderHistory();
    },
    [demandOlderHistory]
  );

  const scheduleHistoryBoundaryCheck = useCallback(
    (scrollport: HTMLDivElement) => {
      const token = historyBoundaryTokenRef.current + 1;
      historyBoundaryTokenRef.current = token;
      pendingHistoryBoundaryRef.current = { token, scrollport };
      if (historyBoundaryRafRef.current !== null) cancelAnimationFrame(historyBoundaryRafRef.current);
      // The scroll event services a moving input. This single input-owned frame is
      // solely the fallback for an already-clamped gesture that emits no scroll.
      historyBoundaryRafRef.current = requestAnimationFrame(() => {
        historyBoundaryRafRef.current = null;
        if (scrollRef.current === scrollport && scrollport.scrollTop <= TRANSCRIPT_SCROLLBACK_THRESHOLD_PX) {
          consumeHistoryBoundary(token, scrollport);
        }
      });
    },
    [consumeHistoryBoundary]
  );

  const handleScroll = useCallback(() => {
    const pending = pendingHistoryBoundaryRef.current;
    if (!pending || scrollRef.current !== pending.scrollport) return;
    if (pending.scrollport.scrollTop <= TRANSCRIPT_SCROLLBACK_THRESHOLD_PX) {
      consumeHistoryBoundary(pending.token, pending.scrollport);
    }
  }, [consumeHistoryBoundary]);

  const handleWheel = useCallback(
    (event: WheelEvent<HTMLDivElement>) => {
      if (nestedScrollportConsumesWheel(event.target, event.currentTarget, event.deltaY)) return;
      if (event.deltaY < 0) {
        detachFromBottom();
        scheduleHistoryBoundaryCheck(event.currentTarget);
        return;
      }
      if (event.deltaY > 0)
        requestAnimationFrame(() => {
          if (renderedTranscript.atLiveTail && isAtBottom()) followLocalLiveEdge();
        });
    },
    [detachFromBottom, followLocalLiveEdge, isAtBottom, renderedTranscript.atLiveTail, scheduleHistoryBoundaryCheck]
  );

  const handlePointerDown = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      const rect = event.currentTarget.getBoundingClientRect();
      if (event.clientX >= rect.right - 20) detachFromBottom();
    },
    [detachFromBottom]
  );

  const handlePointerUp = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      const rect = event.currentTarget.getBoundingClientRect();
      if (event.clientX < rect.right - 20) return;
      if (event.currentTarget.scrollTop <= TRANSCRIPT_SCROLLBACK_THRESHOLD_PX) demandOlderHistory();
      else if (renderedTranscript.atLiveTail && isAtBottom()) followLocalLiveEdge();
    },
    [demandOlderHistory, followLocalLiveEdge, isAtBottom, renderedTranscript.atLiveTail]
  );

  const lastTouchYRef = useRef<number | null>(null);
  const handleTouchStart = useCallback((event: TouchEvent<HTMLDivElement>) => {
    lastTouchYRef.current = event.touches[0]?.clientY ?? null;
  }, []);

  const handleTouchMove = useCallback(
    (event: TouchEvent<HTMLDivElement>) => {
      const nextY = event.touches[0]?.clientY;
      const previousY = lastTouchYRef.current;
      lastTouchYRef.current = nextY ?? null;
      if (nextY !== undefined && previousY !== null && nextY > previousY) {
        detachFromBottom();
        scheduleHistoryBoundaryCheck(event.currentTarget);
      } else if (nextY !== undefined && previousY !== null && nextY < previousY) {
        requestAnimationFrame(() => {
          if (renderedTranscript.atLiveTail && isAtBottom()) followLocalLiveEdge();
        });
      }
    },
    [detachFromBottom, followLocalLiveEdge, isAtBottom, renderedTranscript.atLiveTail, scheduleHistoryBoundaryCheck]
  );

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "End") {
        followBottom();
        return;
      }
      if (["ArrowUp", "PageUp", "Home"].includes(event.key) || (event.key === " " && event.shiftKey)) {
        detachFromBottom();
        if (event.key === "Home") event.currentTarget.scrollTop = 0;
        scheduleHistoryBoundaryCheck(event.currentTarget);
        return;
      }
      if (["ArrowDown", "PageDown"].includes(event.key) || (event.key === " " && !event.shiftKey)) {
        requestAnimationFrame(() => {
          if (renderedTranscript.atLiveTail && isAtBottom()) followLocalLiveEdge();
        });
      }
    },
    [detachFromBottom, followBottom, followLocalLiveEdge, isAtBottom, renderedTranscript.atLiveTail, scheduleHistoryBoundaryCheck]
  );

  const flushStreamingText = useCallback(() => {
    recordStreamingFlush("text");
    const streamingBuffer = streamingBufferForThread(threadId);
    let appended = false;

    if (streamingContentRef.current) {
      const chunks = streamingBuffer.contentChunks;
      appended = appendBufferedTextChunks(streamingContentRef.current, chunks, lastContentIndexRef.current) || appended;
      lastContentIndexRef.current = chunks.length;
    }

    if (streamingReasoningRef.current) {
      const chunks = streamingBuffer.reasoningChunks;
      if (chunks.length > 0) {
        const container = document.getElementById("streaming-reasoning-container");
        if (container) container.style.display = displayVerbosity === "min" ? "none" : "block";
      }
      appended = appendBufferedTextChunks(streamingReasoningRef.current, chunks, lastReasoningIndexRef.current) || appended;
      lastReasoningIndexRef.current = chunks.length;
    }

    if (streamingReasoningSummaryRef.current) {
      const chunks = streamingBuffer.reasoningSummaryChunks;
      if (chunks.length > 0) {
        const container = document.getElementById("streaming-reasoning-summary-container");
        if (container) container.style.display = displayVerbosity === "min" ? "none" : "block";
      }
      appended = appendBufferedTextChunks(streamingReasoningSummaryRef.current, chunks, lastReasoningSummaryIndexRef.current) || appended;
      lastReasoningSummaryIndexRef.current = chunks.length;
    }

    if (appended) scrollToBottom();
  }, [displayVerbosity, scrollToBottom, threadId]);

  const flushStreamingToolOutput = useCallback(() => {
    recordStreamingFlush("toolOutput");
    const streamingBuffer = streamingBufferForThread(threadId);
    let appended = false;

    streamingBuffer.toolOutputChunks.forEach((chunks, toolId) => {
      const el = streamingToolOutputRefs.current[toolId];
      if (!el) return;
      const lastIndex = lastToolOutputIndexRef.current[toolId] || 0;
      appended = appendBufferedTextChunks(el, chunks, lastIndex) || appended;
      if (liveEdgeStateRef.current === "following") {
        el.scrollTop = el.scrollHeight;
      }
      lastToolOutputIndexRef.current[toolId] = chunks.length;
    });

    if (appended) scrollToBottom();
  }, [scrollToBottom, threadId]);

  const flushStreamingToolCalls = useCallback(() => {
    recordStreamingFlush("toolArguments");
    const streamingBuffer = streamingBufferForThread(threadId);
    streamingBuffer.toolCalls.forEach((toolCall, tcId) => {
      const chunks = toolCall.argumentChunks;
      const argsElement = streamingToolCallArgRefs.current[tcId];
      if (argsElement) {
        if (lastToolCallChunksRef.current[tcId] !== chunks) {
          lastToolCallChunksRef.current[tcId] = chunks;
          lastToolCallArgIndexRef.current[tcId] = 0;
          argsElement.textContent = "";
        }
        let renderedAuthoritativeBash = false;
        if (toolCall.name === "bash" && chunks.length === 1) {
          try {
            const parsed = JSON.parse(chunks[0]);
            if (typeof parsed?.script === "string") {
              argsElement.textContent = `$ ${parsed.script}`;
              renderedAuthoritativeBash = true;
            }
          } catch {
            // Streamed partial JSON is appended verbatim until complete.
          }
        }
        if (!renderedAuthoritativeBash) {
          const lastIndex = lastToolCallArgIndexRef.current[tcId] || 0;
          appendBufferedTextChunks(argsElement, chunks, lastIndex);
        }
        lastToolCallArgIndexRef.current[tcId] = chunks.length;
        if (!argsElement.textContent) argsElement.textContent = "...";
      }
    });
    scrollToBottom();
  }, [scrollToBottom, threadId]);

  const flushStreamingToolCallPreviews = useCallback(() => {
    recordStreamingFlush("toolPreview");
    const streamingBuffer = streamingBufferForThread(threadId);
    streamingBuffer.toolCalls.forEach((toolCall, tcId) => {
      const previewElement = streamingToolCallPreviewRefs.current[tcId];
      if (!previewElement) return;
      const argumentPrefix = streamingBuffer.getToolCallArgumentPrefix(tcId);
      previewElement.textContent = oneLinePreview(
        toolCall.name === "bash" ? argumentPrefix.replace(/^\s*\{?\s*"script"\s*:\s*"?/, "$ ") : argumentPrefix
      );
    });
  }, [threadId]);

  const scheduleStreamingTextFlush = useCallback(() => {
    if (streamingTextFlushRafRef.current !== null) return;
    streamingTextFlushRafRef.current = requestAnimationFrame(() => {
      streamingTextFlushRafRef.current = null;
      flushStreamingText();
    });
  }, [flushStreamingText]);

  const scheduleStreamingToolFlush = useCallback(() => {
    if (streamingToolFlushRafRef.current !== null) return;
    streamingToolFlushRafRef.current = requestAnimationFrame(() => {
      streamingToolFlushRafRef.current = null;
      flushStreamingToolOutput();
    });
  }, [flushStreamingToolOutput]);

  const scheduleInitialStreamingFlush = useCallback((flush: () => void) => {
    const timeoutId = window.setTimeout(() => {
      requestAnimationFrame(flush);
    }, 0);
    return timeoutId;
  }, []);

  const attachStreamingToolCallArgs = useCallback(
    (toolCallId: string, element: HTMLPreElement | null) => {
      const previous = streamingToolCallArgRefs.current[toolCallId];
      streamingToolCallArgRefs.current[toolCallId] = element;
      if (!element || previous === element) return;
      lastToolCallArgIndexRef.current[toolCallId] = 0;
      delete lastToolCallChunksRef.current[toolCallId];
      element.textContent = "";
      requestAnimationFrame(flushStreamingToolCalls);
    },
    [flushStreamingToolCalls]
  );

  const attachStreamingToolOutput = useCallback(
    (toolCallId: string, element: HTMLPreElement | null) => {
      const previous = streamingToolOutputRefs.current[toolCallId];
      streamingToolOutputRefs.current[toolCallId] = element;
      if (!element || previous === element) return;
      lastToolOutputIndexRef.current[toolCallId] = 0;
      element.textContent = "";
      requestAnimationFrame(flushStreamingToolOutput);
    },
    [flushStreamingToolOutput]
  );

  // Subscribe to streaming buffer updates - bypasses React entirely
  // This is O(1) per chunk with direct DOM manipulation
  // Re-runs when isStreaming changes to catch up with buffered content when refs become available
  useEffect(() => {
    const streamingBuffer = streamingBufferForThread(threadId);

    const unsubContent = streamingBuffer.subscribeContent(scheduleStreamingTextFlush);
    const unsubReasoning = streamingBuffer.subscribeReasoning(scheduleStreamingTextFlush);

    // Render any existing buffer content (catches up when joining mid-stream).
    // When isStreaming changes to true, refs should be available after render.
    const timeoutId = isStreaming && renderedTranscript.atLiveTail
      ? scheduleInitialStreamingFlush(flushStreamingText)
      : null;

    return () => {
      if (timeoutId !== null) clearTimeout(timeoutId);
      unsubContent();
      unsubReasoning();
      if (streamingTextFlushRafRef.current !== null) {
        cancelAnimationFrame(streamingTextFlushRafRef.current);
        streamingTextFlushRafRef.current = null;
      }
      if (rafIdRef.current !== null) {
        cancelAnimationFrame(rafIdRef.current);
        rafIdRef.current = null;
      }
    };
  }, [
    isStreaming,
    renderedTranscript.atLiveTail,
    flushStreamingText,
    scheduleInitialStreamingFlush,
    scheduleStreamingTextFlush,
    threadId,
  ]);

  // Tool-call arguments use the same mutable/RAF architecture as text and
  // tool output. A burst schedules one imperative preview flush per frame and
  // never publishes its growing argument body through React or Zustand.
  useEffect(() => {
    const streamingBuffer = streamingBufferForThread(threadId);
    const coalescer = new AnimationFrameCoalescer(
      (callback) => window.requestAnimationFrame(callback),
      (id) => window.cancelAnimationFrame(id)
    );
    const previewCoalescer = new IntervalCoalescer<number>(
      TOOL_ARGUMENT_PREVIEW_INTERVAL_MS,
      () => performance.now(),
      (callback, delayMs) => window.setTimeout(callback, delayMs),
      (id) => window.clearTimeout(id)
    );
    streamingToolCallFlushRef.current = coalescer;
    streamingToolPreviewFlushRef.current = previewCoalescer;
    const schedule = () => {
      coalescer.schedule(flushStreamingToolCalls);
      previewCoalescer.schedule(flushStreamingToolCallPreviews);
    };
    const unsubscribe = streamingBuffer.subscribeToolCalls(schedule);
    schedule();
    return () => {
      unsubscribe();
      coalescer.cancel();
      previewCoalescer.cancel();
      if (streamingToolCallFlushRef.current === coalescer) streamingToolCallFlushRef.current = null;
      if (streamingToolPreviewFlushRef.current === previewCoalescer) streamingToolPreviewFlushRef.current = null;
    };
  }, [flushStreamingToolCallPreviews, flushStreamingToolCalls, streamingToolCalls, threadId]);

  // Subscribe to streaming tool-output preview updates. Like text streaming,
  // this writes chunks directly to DOM so large/fast tool output does not
  // trigger a React render per chunk.
  useEffect(() => {
    const streamingBuffer = streamingBufferForThread(threadId);

    const unsubToolOutput = streamingBuffer.subscribeToolOutput(scheduleStreamingToolFlush);
    const timeoutId = isStreaming ? scheduleInitialStreamingFlush(flushStreamingToolOutput) : null;

    return () => {
      if (timeoutId !== null) clearTimeout(timeoutId);
      unsubToolOutput();
      if (streamingToolFlushRafRef.current !== null) {
        cancelAnimationFrame(streamingToolFlushRafRef.current);
        streamingToolFlushRafRef.current = null;
      }
    };
  }, [isStreaming, streamingToolOutputs, flushStreamingToolOutput, scheduleInitialStreamingFlush, scheduleStreamingToolFlush, threadId]);

  // Reset DOM state only after assistant and retained tool state are both gone.
  useEffect(() => {
    if (!showLiveCard) {
      lastContentIndexRef.current = 0;
      lastReasoningIndexRef.current = 0;
      lastReasoningSummaryIndexRef.current = 0;
      lastToolOutputIndexRef.current = {};
      lastToolCallArgIndexRef.current = {};
      lastToolCallChunksRef.current = {};
      if (streamingContentRef.current) {
        streamingContentRef.current.textContent = "";
      }
      if (streamingReasoningRef.current) {
        streamingReasoningRef.current.textContent = "";
      }
      if (streamingReasoningSummaryRef.current) {
        streamingReasoningSummaryRef.current.textContent = "";
      }
      Object.values(streamingToolOutputRefs.current).forEach((el) => {
        if (el) el.textContent = "";
      });
      Object.values(streamingToolCallArgRefs.current).forEach((el) => {
        if (el) el.textContent = "";
      });
      Object.values(streamingToolCallPreviewRefs.current).forEach((el) => {
        if (el) el.textContent = "";
      });
    }
  }, [showLiveCard]);

  const { isLoading, isError } = transcriptQuery;
  useEffect(() => {
    const ownedThreadId = threadId;
    return () => {
      // React Strict Mode immediately remounts the same route. Defer the test so
      // only a genuine route-owner change aborts pending retry waits.
      queueMicrotask(() => {
        if (useAppStore.getState().currentThreadId !== ownedThreadId) {
          cancelTranscriptRequests(ownedThreadId);
        }
      });
    };
  }, [threadId]);

  const retryTranscript = useCallback(() => {
    void refreshTranscriptTail(queryClient, threadId).catch((error) => {
      console.error("Failed to retry transcript tail:", error);
    });
  }, [queryClient, threadId]);

  // Reset route intent/history and correct the new route before paint.
  useLayoutEffect(() => {
    liveEdgeStateRef.current = reduceLiveEdgeState(liveEdgeStateRef.current, {
      type: "thread_changed",
    });
    historyDemandRef.current = reduceHistoryDemand(historyDemandRef.current, { type: "reset" }, { canReveal: false, canFetch: false });
    loadingOlderRef.current = false;
    revealingLoadedRef.current = false;
    pendingHistoryAnchorRef.current = null;
    historyOperationRef.current += 1;
    historyBoundaryTokenRef.current += 1;
    pendingHistoryBoundaryRef.current = null;
    if (historyBoundaryRafRef.current !== null) {
      cancelAnimationFrame(historyBoundaryRafRef.current);
      historyBoundaryRafRef.current = null;
    }
    setRenderStartIndex(null);
    setIsLoadingOlder(false);
    scrollToBottomNow();
  }, [currentThreadId, scrollToBottomNow]);

  // React can remove live cards and install several durable results in one
  // commit. Correct the following viewport in that commit's layout phase so no
  // transient scroll-up frame is painted before the eventual bottom follow.
  useLayoutEffect(() => {
    if (pendingHistoryAnchorRef.current || loadingOlderRef.current || revealingLoadedRef.current) return;
    scrollToBottomNow();
  }, [
    currentThreadId,
    isStreaming,
    transcriptMetadata.revision,
    renderedTranscript.startIndex,
    showLiveCard,
    streamingToolCalls,
    streamingToolOutputs,
    scrollToBottomNow,
  ]);

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
  }, [currentThreadId, isStreaming, transcriptMetadata.revision, scrollToBottom]);

  if (!currentThreadId) {
    return <div className="eggw-transcript-state m-auto max-w-xl">Select a thread to view messages</div>;
  }

  const formattedStreamingTps =
    isStreaming && streamingKind === "llm" ? formatStreamingTps(streamingTps) : formatStreamingTps(transcriptMetadata.tailTps);
  const streamingRoleLabel = streamingKind === "tool" ? "Tool" : "Assistant";

  const panel = (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div
        className={clsx(
          "eggw-section-header px-4 py-2 text-xs flex items-center justify-between flex-shrink-0",
          showBorders && "border-b border-[var(--border-default)]"
        )}
      >
        <span>
          Chat Messages · {totalMessages.toLocaleString()} loaded
          {hasOlderTranscript ? " · scroll up for older" : ""}
          {formattedStreamingTps ? ` | ${formattedStreamingTps}` : ""}
        </span>
      </div>
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        onWheel={handleWheel}
        onTouchStart={handleTouchStart}
        onTouchMove={handleTouchMove}
        onPointerDown={handlePointerDown}
        onPointerUp={handlePointerUp}
        onKeyDown={handleKeyDown}
        aria-label="Conversation transcript"
        aria-busy={isLoading || isStreaming}
        tabIndex={0}
        className="eggw-transcript-scroll flex-1 overflow-auto px-4 py-6 md:px-8"
        data-testid="chat-panel"
      >
        <div className="eggw-transcript-inner" data-testid="chat-panel-content">
          {isLoading ? (
            <div className="eggw-transcript-state" role="status" aria-live="polite">
              Loading messages…
            </div>
          ) : isError ? (
            <div className="eggw-transcript-state eggw-transcript-state-error space-y-2" role="alert">
              <div>Failed to load messages</div>
              <Button variant="danger" onClick={retryTranscript}>
                Retry
              </Button>
            </div>
          ) : totalMessages === 0 ? (
            <div className="eggw-transcript-state">No messages yet. Start a conversation!</div>
          ) : (
            <>
              {renderedTranscript.hiddenCount > 0 && (
                <div className="mb-4 flex justify-center">
                  <Button variant="secondary" onClick={demandOlderHistory} data-testid="show-more-loaded-messages">
                    Show {TRANSCRIPT_WINDOW_MESSAGES} older loaded messages ({renderedTranscript.hiddenCount.toLocaleString()} earlier)
                  </Button>
                </div>
              )}
              {renderedTranscript.newerHiddenCount > 0 && (
                <div className="mb-4 flex justify-center gap-2">
                  <Button variant="secondary" onClick={showNewerTranscript} data-testid="show-newer-loaded-messages">
                    Show newer ({renderedTranscript.newerHiddenCount.toLocaleString()} later)
                  </Button>
                  <Button variant="secondary" onClick={showLiveTranscript} data-testid="return-to-live-tail">
                    Return to live tail
                  </Button>
                </div>
              )}
              <StaticTranscript
                messages={renderedTranscript.messages}
                displayVerbosity={displayVerbosity}
                showBorders={showBorders}
                onStageAttachment={onStageAttachment}
              />

              {/* Streaming content */}
              {showLiveCard && (
                <div
                  className={clsx("eggw-message-card eggw-role-card eggw-role-assistant", !showBorders && "eggw-role-card-borderless")}
                  role="status"
                  aria-live="polite"
                  aria-label={`${streamingRoleLabel} streaming`}
                >
                  <div className="eggw-message-header">
                    <span className="eggw-role-marker" aria-hidden="true" />
                    <span className="eggw-role-label">{streamingRoleLabel}</span>
                    {displayVerbosity !== "min" && streamingModelKey && <span className="eggw-message-meta">{streamingModelKey}</span>}
                    {isStreaming && streamingKind !== "tool" && <StatusChip tone="info">Streaming…</StatusChip>}
                    <LiveTimingText
                      kind={streamingKind === "llm" ? "provider" : "generic"}
                      isStreaming={isStreaming}
                      streamingKind={streamingKind}
                      streamingStartedAtMs={streamingStartedAtMs}
                      providerRequest={streamingProviderRequest}
                      toolOutputs={visibleStreamingToolOutputs}
                    />
                  </div>

                  <>
                    {/* Streaming reasoning - direct DOM updates via ref */}
                    <details
                      open={displayVerbosity === "max" ? true : undefined}
                      className={clsx(
                        "eggw-detail-block eggw-role-reasoning",
                        !showBorders && "eggw-detail-borderless",
                        displayVerbosity === "min" && "hidden"
                      )}
                      id="streaming-reasoning-container"
                    >
                      <summary className="eggw-detail-summary">
                        Reasoning <span className="text-xs animate-pulse">(streaming...)</span>
                      </summary>
                      <div ref={streamingReasoningRef} className="eggw-detail-content whitespace-pre-wrap" />
                    </details>

                    {/* Streaming reasoning summary - display-only, not persisted as reasoning */}
                    <details
                      open={displayVerbosity === "max" ? true : undefined}
                      className={clsx("eggw-detail-block eggw-role-reasoning hidden", !showBorders && "eggw-detail-borderless")}
                      id="streaming-reasoning-summary-container"
                    >
                      <summary className="eggw-detail-summary">
                        Reasoning Summary <span className="text-xs animate-pulse">(streaming...)</span>
                      </summary>
                      <div ref={streamingReasoningSummaryRef} className="eggw-detail-content whitespace-pre-wrap" />
                    </details>

                    {/* Streaming content - direct DOM updates via ref for O(1) performance */}
                    <div ref={streamingContentRef} data-testid="streaming-content" className="eggw-streaming-content" />

                    {/* Streaming tool calls */}
                    {Object.keys(visibleStreamingToolCalls).length > 0 && (
                      <div className="mt-2 space-y-2">
                        {Object.entries(visibleStreamingToolCalls).map(([tcId, tc]) => {
                          return (
                            <details
                              key={tcId}
                              open={isGetUserMessageTool(tc.name) ? undefined : true}
                              data-testid={isGetUserMessageTool(tc.name) ? "get-user-wait-call" : undefined}
                              className={clsx("eggw-detail-block eggw-role-tool-call", !showBorders && "eggw-detail-borderless")}
                            >
                              <summary className="eggw-detail-summary">
                                <span className="font-medium">{tc.name || "tool"}</span>
                                <span className="eggw-message-meta font-mono">{tcId.slice(-8)}</span>
                                <span className="eggw-streaming-label">
                                  {tc.finished ? "finished" : isGetUserMessageTool(tc.name) ? "waiting for reply" : "streaming..."}
                                </span>
                                {displayVerbosity === "medium" && (
                                  <span
                                    ref={(element) => {
                                      streamingToolCallPreviewRefs.current[tcId] = element;
                                    }}
                                    className="eggw-message-meta font-mono"
                                  />
                                )}
                                {displayVerbosity === "medium" && <span className="eggw-attachment-meta">expand to inspect args</span>}
                              </summary>
                              <div className="px-2 pb-2">
                                <pre
                                  ref={(element) => attachStreamingToolCallArgs(tcId, element)}
                                  data-testid="streaming-tool-arguments"
                                  className="eggw-code-block whitespace-pre-wrap break-all"
                                >
                                  ...
                                </pre>
                              </div>
                            </details>
                          );
                        })}
                      </div>
                    )}

                    {/* Streaming tool output preview */}
                    {Object.keys(visibleStreamingToolOutputs).length > 0 && (
                      <div className="mt-2 space-y-2">
                        {Object.entries(visibleStreamingToolOutputs).map(([toolId, tool]) => {
                          const isGetUserWait = isGetUserMessageTool(tool.name);
                          return (
                            <details
                              key={toolId}
                              open={isGetUserWait ? undefined : true}
                              data-testid={isGetUserWait ? "get-user-wait-output" : undefined}
                              className={clsx("eggw-detail-block eggw-role-tool", !showBorders && "eggw-detail-borderless")}
                            >
                              <summary className="eggw-detail-summary flex-wrap">
                                <span className="font-medium">{tool.name || "tool"}</span>
                                <span className="eggw-message-meta font-mono">{toolId.slice(-8)}</span>
                                <span className="eggw-streaming-label">
                                  {tool.finished ? "finished" : isGetUserWait ? "waiting for reply" : "streaming output..."}
                                </span>
                                {displayVerbosity === "medium" && (
                                  <span className="eggw-attachment-meta">
                                    {isGetUserWait ? "expand to inspect" : "expand to inspect output"}
                                  </span>
                                )}
                                {!isGetUserWait && !tool.finished && (
                                  <LiveTimingText
                                    kind="toolElapsed"
                                    toolId={toolId}
                                    isStreaming={isStreaming}
                                    streamingKind={streamingKind}
                                    streamingStartedAtMs={streamingStartedAtMs}
                                    providerRequest={streamingProviderRequest}
                                    toolOutputs={visibleStreamingToolOutputs}
                                    testId="streaming-tool-elapsed-summary"
                                  />
                                )}
                                {!isGetUserWait && !tool.finished && (
                                  <LiveTimingText
                                    kind="toolTimeout"
                                    toolId={toolId}
                                    isStreaming={isStreaming}
                                    streamingKind={streamingKind}
                                    streamingStartedAtMs={streamingStartedAtMs}
                                    providerRequest={streamingProviderRequest}
                                    toolOutputs={visibleStreamingToolOutputs}
                                    testId="streaming-tool-timeout-summary"
                                  />
                                )}
                              </summary>
                              <div className="px-2 pb-2">
                                {tool.summary && (
                                  <div data-testid="streaming-tool-summary" className="eggw-streaming-label mb-2">
                                    {tool.summary}
                                  </div>
                                )}
                                <pre
                                  ref={(element) => attachStreamingToolOutput(toolId, element)}
                                  data-testid="streaming-tool-output"
                                  className="eggw-code-block max-h-64 whitespace-pre-wrap break-words"
                                />
                                {tool.suppressed && (
                                  <div data-testid="streaming-tool-output-suppressed" className="eggw-message-meta mt-2">
                                    {toolStreamSavingText(tool.name, tool.suppressedFrames)}
                                  </div>
                                )}
                              </div>
                            </details>
                          );
                        })}
                      </div>
                    )}
                  </>
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
  if (process.env.NODE_ENV === "production") return panel;
  return (
    <Profiler id="ChatPanel" onRender={(id, _phase, duration) => recordReactCommit(id as "ChatPanel", duration)}>
      {panel}
    </Profiler>
  );
}


export const ChatPanel = memo(ChatPanelImpl);
