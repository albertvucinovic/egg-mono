"use client";

import { memo, Profiler, useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode, type TouchEvent, type WheelEvent } from "react";
import Link from "next/link";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import rehypeRaw from "rehype-raw";
import "katex/dist/katex.min.css";
import { attachmentUrl, createEditAnswerDraft, promoteProviderOutput, providerOutputUrl } from "@/lib/api";
import { useAppStore, type Message, type DisplayVerbosity, type StreamingToolTimeout } from "@/lib/store";
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
import { flattenTranscript, transcriptInfiniteQueryOptions } from "@/lib/transcript";
import { AnimationFrameCoalescer, IntervalCoalescer, streamingBufferForThread } from "@/lib/streamingBuffer";
import { recordReactCommit, recordStreamingFlush } from "@/lib/performanceInstrumentation";
import { expandedTranscriptStartId, transcriptWindow } from "@/lib/transcriptWindow";
import clsx from "clsx";

const STICKY_BOTTOM_THRESHOLD_PX = 16;
const MESSAGE_IMAGE_PREVIEW_MAX_HEIGHT = "min(70vh, 720px)";
const INITIAL_TRANSCRIPT_MESSAGE_LIMIT = 300;
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
        return withoutClosingFence.trim()
          ? `${indent}$$\n${indent}${withoutClosingFence.trimStart()}\n${indent}$$`
          : `${indent}$$`;
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

function elapsedSecondsText(startedAtMs: number | null | undefined, nowMs: number, label = "streaming"): string | null {
  const started = Number(startedAtMs);
  if (!Number.isFinite(started) || started <= 0) return null;
  const elapsedSec = Math.max(0, (nowMs - started) / 1000);
  return `${label} ${elapsedSec.toFixed(0)}s`;
}

function providerTimingText(
  request: { startedAtMs: number; timeoutSec?: number } | null | undefined,
  nowMs: number,
): string | null {
  if (!request) return null;
  const started = Number(request.startedAtMs);
  if (!Number.isFinite(started) || started <= 0) return null;
  const elapsedSec = Math.max(0, (nowMs - started) / 1000);
  const limit = Number(request.timeoutSec || 0);
  if (Number.isFinite(limit) && limit > 0) {
    return `streaming ${elapsedSec.toFixed(0)}s (limit ${limit.toFixed(0)}s)`;
  }
  return `streaming ${elapsedSec.toFixed(0)}s`;
}

type HiddenDetailKind = "reasoning" | "tool_calls" | "tool_results";

interface HiddenDetail {
  kind: HiddenDetailKind;
  header: string;
  name?: string;
  tool_call_id?: string;
  tokens?: number;
  body?: string;
  source?: "reasoning" | "tool_call" | "tool_result" | "tool_stream" | "tool_call_stream";
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
  if (message.role !== "system") return false;
  if (message.recovery_notice || message.command_name || message.id?.startsWith("cmd-")) return true;
  const content = contentToPlainText(message.content, message.content_text || "").trim().toLowerCase();
  return content.startsWith("llm error:")
    || content.startsWith("error:")
    || content.startsWith("usage:")
    || content.startsWith("unknown command:")
    || content.startsWith("/");
}

function plural(count: number, singular: string, pluralText?: string): string {
  return `${count} ${count === 1 ? singular : (pluralText || `${singular}s`)}`;
}

function hiddenSummaryCountsText(details: HiddenDetail[]): string {
  const counts: Record<HiddenDetailKind, number> = { reasoning: 0, tool_calls: 0, tool_results: 0 };
  details.forEach((detail) => { counts[detail.kind] += 1; });
  const parts: string[] = [];
  if (counts.tool_calls > 0) parts.push(`Executed ${plural(counts.tool_calls, "tool")}`);
  if (counts.tool_results > 0) parts.push(`got ${plural(counts.tool_results, "tool result")}`);
  if (counts.reasoning > 0) parts.push(plural(counts.reasoning, "reasoning block"));
  const tokenTotal = details.reduce((total, detail) => total + (Number.isFinite(detail.tokens || 0) ? Math.max(0, Math.trunc(detail.tokens || 0)) : 0), 0);
  if (tokenTotal > 0) parts.push(`total tokens ${tokenTotal.toLocaleString()}`);
  return parts.join(", ") || "Hidden details";
}

function hiddenToolDetails(details: HiddenDetail[]): HiddenDetail[] {
  const structuredCalls = details.filter((detail) => detail.source === "tool_call" && Boolean(detail.name));
  const structuredIds = new Set(structuredCalls.map((detail) => detail.tool_call_id).filter(Boolean));
  const calls = [
    ...structuredCalls,
    ...details.filter((detail) => (
      detail.source === "tool_call_stream"
      && Boolean(detail.name)
      && (!detail.tool_call_id || !structuredIds.has(detail.tool_call_id))
    )),
  ];
  const finalResults = details.filter((detail) => detail.source === "tool_result");
  const streamResults = details.filter((detail) => detail.source === "tool_stream");
  if (calls.length === 0) return [...finalResults, ...streamResults].filter((detail) => Boolean(detail.name));

  const usedFinalResults = new Set<number>();
  const usedStreamResults = new Set<number>();
  const resultByCall = new Map<number, HiddenDetail>();
  const exactResultQueues = new Map<string, number[]>();
  finalResults.forEach((result, resultIndex) => {
    if (!result.tool_call_id) return;
    const queue = exactResultQueues.get(result.tool_call_id) || [];
    queue.push(resultIndex);
    exactResultQueues.set(result.tool_call_id, queue);
  });

  // Identity is authoritative. Consume exact-ID results before considering any
  // legacy inference, and never let a call with an ID steal a differently
  // identified result merely because both tools have the same name.
  calls.forEach((call, callIndex) => {
    if (!call.tool_call_id) return;
    const queue = exactResultQueues.get(call.tool_call_id);
    const resultIndex = queue?.shift();
    if (resultIndex === undefined) return;
    usedFinalResults.add(resultIndex);
    resultByCall.set(callIndex, finalResults[resultIndex]);
  });

  // Old/imported transcripts can lack IDs. Pair only when the relationship is
  // unambiguous inside this hidden run: one unmatched ID-less call and one
  // unmatched ID-less final result with the same tool name.
  const unmatchedIdlessCallsByName = new Map<string, number[]>();
  calls.forEach((call, callIndex) => {
    if (resultByCall.has(callIndex) || call.tool_call_id || !call.name) return;
    const indexes = unmatchedIdlessCallsByName.get(call.name) || [];
    indexes.push(callIndex);
    unmatchedIdlessCallsByName.set(call.name, indexes);
  });
  const unmatchedIdlessResultsByName = new Map<string, number[]>();
  finalResults.forEach((result, resultIndex) => {
    if (usedFinalResults.has(resultIndex) || result.tool_call_id || !result.name) return;
    const indexes = unmatchedIdlessResultsByName.get(result.name) || [];
    indexes.push(resultIndex);
    unmatchedIdlessResultsByName.set(result.name, indexes);
  });
  unmatchedIdlessCallsByName.forEach((callIndexes, name) => {
    const resultIndexes = unmatchedIdlessResultsByName.get(name) || [];
    if (callIndexes.length !== 1 || resultIndexes.length !== 1) return;
    resultByCall.set(callIndexes[0], finalResults[resultIndexes[0]]);
    usedFinalResults.add(resultIndexes[0]);
  });

  // A persisted streamed preview has no stable result ID. Use it only when a
  // single still-unmatched call and a single preview share a name; otherwise
  // expose it separately instead of fabricating a lifecycle.
  const unmatchedCallsByName = new Map<string, number[]>();
  calls.forEach((call, callIndex) => {
    if (resultByCall.has(callIndex) || !call.name) return;
    const indexes = unmatchedCallsByName.get(call.name) || [];
    indexes.push(callIndex);
    unmatchedCallsByName.set(call.name, indexes);
  });
  const streamResultsByName = new Map<string, number[]>();
  streamResults.forEach((result, resultIndex) => {
    if (!result.name) return;
    const indexes = streamResultsByName.get(result.name) || [];
    indexes.push(resultIndex);
    streamResultsByName.set(result.name, indexes);
  });
  unmatchedCallsByName.forEach((callIndexes, name) => {
    const resultIndexes = streamResultsByName.get(name) || [];
    if (callIndexes.length !== 1 || resultIndexes.length !== 1) return;
    resultByCall.set(callIndexes[0], streamResults[resultIndexes[0]]);
    usedStreamResults.add(resultIndexes[0]);
  });

  const pairedCalls = calls.map((call, callIndex) => {
    const result = resultByCall.get(callIndex);
    const callHeader = [
      `Tool call: ${call.name || "tool"}`,
      call.tool_call_id ? `tool_call_id: ${call.tool_call_id}` : "",
    ].filter(Boolean).join("\n");
    const bodyParts = [callHeader, "", "Arguments:", call.body || "(none)"];
    bodyParts.push(
      "",
      "Result:",
      result?.body || (result ? "(empty)" : "(not found in the loaded transcript)"),
    );
    return { ...call, body: bodyParts.join("\n") };
  });

  const unmatchedResults = [
    ...finalResults.filter((_, index) => !usedFinalResults.has(index)),
    ...streamResults.filter((_, index) => !usedStreamResults.has(index)),
  ].filter((detail) => Boolean(detail.name));
  return [...pairedCalls, ...unmatchedResults];
}

function HiddenDetailsBlock({ details, showBorders = true }: { details: HiddenDetail[]; showBorders?: boolean }) {
  const [selectedDetail, setSelectedDetail] = useState<HiddenDetail | null>(null);
  if (!details.length) return null;
  const toolDetails = hiddenToolDetails(details);
  return (
    <div
      className={`eggw-message-card rounded p-4 mb-4 ${showBorders ? 'border' : ''}`}
      style={{ background: "var(--tool-msg-bg)", borderColor: "var(--tool-msg-border)", color: "var(--tool-msg-text, var(--foreground))" }}
      data-testid="hidden-details"
    >
      <div className="whitespace-pre-wrap text-sm font-medium">{hiddenSummaryCountsText(details)}</div>
      {toolDetails.length > 0 && (
        <div className="mt-1 text-sm font-mono" style={{ color: "var(--tool-msg-text, var(--foreground))" }}>
          <span>Tools: </span>
          {toolDetails.map((detail, index) => (
            <span key={`${detail.kind}-${index}-${detail.name || "tool"}`}>
              <button
                type="button"
                className="underline-offset-2 hover:underline"
                style={{ color: "var(--accent)" }}
                title={detail.body ? `Show ${detail.header}` : detail.header}
                onClick={() => setSelectedDetail(detail)}
              >
                {detail.name}
              </button>
              {index < toolDetails.length - 1 ? ", " : null}
            </span>
          ))}
        </div>
      )}
      {selectedDetail && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ background: "rgba(0, 0, 0, 0.45)" }}
          role="presentation"
          onClick={() => setSelectedDetail(null)}
        >
          <div
            className="w-full max-w-3xl rounded-lg border p-4 shadow-xl"
            style={{ background: "var(--panel-bg)", borderColor: "var(--panel-border)", color: "var(--foreground)" }}
            role="dialog"
            aria-modal="true"
            aria-label={selectedDetail.header}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="mb-3 flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="truncate text-sm font-semibold">{selectedDetail.header}</div>
                {selectedDetail.name && (
                  <div className="text-xs font-mono" style={{ color: "var(--muted)" }}>{selectedDetail.name}</div>
                )}
              </div>
              <button
                type="button"
                className="rounded px-2 py-1 text-xs"
                style={{ background: "var(--code-bg)", color: "var(--foreground)" }}
                onClick={() => setSelectedDetail(null)}
              >
                Close
              </button>
            </div>
            <pre
              className="max-h-[70vh] overflow-auto rounded p-3 text-xs whitespace-pre-wrap break-words"
              style={{ background: "var(--code-bg)", color: "var(--foreground)" }}
            >
              {selectedDetail.body || selectedDetail.header}
            </pre>
          </div>
        </div>
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
          className="font-semibold underline-offset-2 hover:underline"
          style={{ color: "var(--accent)" }}
          title={`Open thread ${fullThreadId}`}
        >
          {token}
        </Link>,
      );
      lastIndex = match.index + token.length;
    }
    if (lastIndex < line.length) nodes.push(line.slice(lastIndex));
    return nodes.length ? nodes : [line];
  };

  return (
    <pre className="text-sm font-mono p-2 rounded overflow-auto whitespace-pre-wrap" style={{ background: "var(--code-bg)", color: "var(--system-msg-text, var(--foreground))" }}>
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
  const imagePreviewClassName = clsx("mx-auto block max-w-full rounded object-contain", showBorders && "border");
  const hasImagePart = parts.some(
    (part) => (isAttachmentPart(part) || isArtifactPart(part)) && isImageContentPart(part),
  );
  const imagePreviewStyle = {
    maxHeight: MESSAGE_IMAGE_PREVIEW_MAX_HEIGHT,
    borderColor: "var(--panel-border)",
    background: "var(--panel-bg)",
  };

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
            <div key={`text-${idx}`} className={clsx("whitespace-pre-wrap text-sm", hasImagePart && "text-center")}>
              {part.text}
            </div>
          );
        }
        if (isAttachmentPart(part)) {
          const isImage = isImageContentPart(part);
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
              className={clsx("rounded p-3 text-sm", isImage && "text-center", showBorders && "border")}
              style={{ background: "var(--code-bg)", borderColor: "var(--panel-border)", color: "var(--foreground)" }}
              title={attachmentPlaceholder(part)}
            >
              <div className={clsx("flex flex-wrap items-center gap-2", isImage && "justify-center")}>
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
              {openUrl && isImage && (
                <ProtectedFileLink url={openUrl} newWindow className="mx-auto mt-3 block w-fit" aria-label={`Open preview of ${attachmentFilename(part)}`}>
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
                  <ProtectedFileLink url={openUrl} newWindow className="underline" style={{ color: "var(--accent)" }}>
                    Open
                  </ProtectedFileLink>
                  <ProtectedFileLink url={downloadUrl} filename={attachmentFilename(part)} className="underline" style={{ color: "var(--accent)" }}>
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
              className={clsx("rounded p-3 text-sm", isImage && "text-center", showBorders && "border")}
              style={{ background: "var(--code-bg)", borderColor: "var(--panel-border)", color: "var(--foreground)" }}
              title={artifactPlaceholder(part)}
            >
              <div className={clsx("flex flex-wrap items-center gap-2", isImage && "justify-center")}>
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
              {openUrl && isImage && (
                <ProtectedFileLink url={openUrl} newWindow className="mx-auto mt-3 block w-fit" aria-label={`Open preview of ${artifactFilename(part)}`}>
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
                  <ProtectedFileLink url={openUrl} newWindow className="underline" style={{ color: "var(--accent)" }}>
                    Open
                  </ProtectedFileLink>
                  <ProtectedFileLink url={downloadUrl} filename={artifactFilename(part)} className="underline" style={{ color: "var(--accent)" }}>
                    Download
                  </ProtectedFileLink>
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

const MessageBlock = memo(function MessageBlock({ message, showBorders = true, displayVerbosity = "max", onStageAttachment }: MessageBlockProps) {
  const currentThreadId = useAppStore((state) => state.currentThreadId);
  const openEditAnswerModal = useAppStore((state) => state.openEditAnswerModal);
  const addSystemLog = useAppStore((state) => state.addSystemLog);
  const [isPreparingEditAnswer, setIsPreparingEditAnswer] = useState(false);

  const handleQuoteEdit = useCallback(async () => {
    if (!currentThreadId || !message.id) return;
    setIsPreparingEditAnswer(true);
    try {
      const draft = await createEditAnswerDraft(currentThreadId, { source_msg_id: message.id });
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
  const isThreadsCommandOutput = isCommandOutput && message.command_name === "threads";

  const shellStyle: React.CSSProperties = { background: "var(--code-bg)", borderColor: "var(--panel-border)" };

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
      className={`eggw-message-card rounded p-4 mb-4 ${showBorders ? 'border' : ''}`}
      style={isShellCommand ? shellStyle : (roleStyles[displayRole] || shellStyle)}
    >
      {/* Header */}
      <div className="flex items-center gap-2 mb-2 text-xs flex-wrap" style={{ color: "var(--muted)" }}>
        <span className="font-medium" style={roleStyles[displayRole] ? { color: roleStyles[displayRole].color } : { color: "var(--foreground)" }}>
          {isShellCommand ? "Shell" : roleLabel}
        </span>
        {displayVerbosity !== "min" && message.model_key && (
          <span style={{ color: "var(--muted)" }}>({message.model_key})</span>
        )}
        {displayVerbosity !== "min" && tokenText && (
          <span style={{ color: "var(--muted)" }}>({tokenText})</span>
        )}
        {displayVerbosity !== "min" && messageTps && (
          <span style={{ color: "var(--muted)" }}>({messageTps})</span>
        )}
        {displayVerbosity === "max" && message.timestamp && (
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
        {displayVerbosity === "max" && message.id && message.id.length >= 8 && !message.id.startsWith('temp-') && (
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
        {displayVerbosity === "max" && message.tool_call_id && (
          <span className="font-mono" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>
            ← {message.tool_call_id.slice(-8)}
          </span>
        )}
        {displayVerbosity !== "min" && optimizerSummary && (
          <span
            className="rounded px-1.5 py-0.5 text-[11px] font-medium"
            style={{ background: "var(--code-bg)", color: "var(--accent)", border: "1px solid var(--panel-border)" }}
            title={optimizerRawHint ? `Raw output: ${optimizerRawHint}` : optimizerSummary}
            data-testid="output-optimizer-badge"
          >
            {optimizerSummary}
          </span>
        )}
        {canQuoteEdit && (
          <button
            type="button"
            onClick={handleQuoteEdit}
            disabled={isPreparingEditAnswer}
            className="rounded border px-1.5 py-0.5 text-[11px] font-medium disabled:cursor-wait disabled:opacity-60"
            style={{ borderColor: "var(--panel-border)", color: "var(--accent)", background: "var(--panel-bg)" }}
            aria-label={`Quote/Edit ${roleLabel} ${message.id}`}
            title={`Quote/Edit ${roleLabel}${message.id ? ` ${message.id.slice(-8)}` : ""}`}
            data-testid="quote-edit-button"
          >
            {isPreparingEditAnswer ? "Preparing…" : "Quote/Edit"}
          </button>
        )}
      </div>

      {optimizerRawHint && displayVerbosity !== "min" && (
        <div className="mb-2 text-xs font-mono" style={{ color: "var(--muted)" }} data-testid="raw-output-affordance">
          Raw output: {optimizerRawHint}
        </div>
      )}

      {/* Reasoning (collapsible) */}
      {showReasoningBlock && (
        <details
          open={displayVerbosity === "max" ? true : undefined}
          className={`mb-2 rounded p-2 ${showBorders ? 'border' : ''}`}
          style={{ background: "var(--reasoning-bg)", borderColor: "var(--reasoning-border)" }}
        >
          <summary className="cursor-pointer text-sm" style={{ color: "var(--reasoning-text, var(--reasoning-border))" }}>
            Reasoning
            {displayVerbosity === "medium" && message.reasoning && (
              <span className="ml-2 text-xs font-mono" style={{ color: "var(--muted)" }}>
                {message.reasoning.length.toLocaleString()} chars
              </span>
            )}
          </summary>
          <div className="mt-2 text-sm whitespace-pre-wrap" style={{ color: "var(--reasoning-text, var(--foreground))", opacity: 0.9 }}>
            {message.reasoning}
          </div>
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
            isThreadsCommandOutput ? (
              <ThreadCommandOutput content={contentText} threadIds={message.command_data?.thread_ids} />
            ) : (
              <pre className="text-sm font-mono p-2 rounded overflow-auto whitespace-pre-wrap" style={{ background: "var(--code-bg)", color: "var(--system-msg-text, var(--foreground))" }}>
                {contentText}
              </pre>
            )
          ) : isContentPartArray(message.content) ? (
            <ContentPartsView parts={message.content} showBorders={showBorders} onStageAttachment={onStageAttachment} />
          ) : message.role === "tool" ? (
            displayVerbosity === "medium" ? (
              <details className={`rounded ${showBorders ? 'border' : ''}`} style={{ background: "var(--code-bg)", borderColor: "var(--tool-msg-border)" }}>
                <summary className="cursor-pointer p-2 text-sm" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>
                  {oneLinePreview(contentText) || `Output (${contentText.length.toLocaleString()} chars)`}
                </summary>
                <pre className="p-2 text-xs overflow-auto max-h-96 whitespace-pre-wrap" style={{ color: "var(--tool-msg-text, var(--foreground))" }}>
                  {contentText}
                </pre>
              </details>
            ) : (
              <pre className="text-xs p-2 rounded overflow-auto max-h-96 whitespace-pre-wrap" style={{ background: "var(--code-bg)", color: "var(--tool-msg-text, var(--foreground))" }}>
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
          {toolCalls.map((tc: any, idx: number) => {
            const toolName = toolCallName(tc);
            const args = toolCallArgs(tc);
            const isBash = toolName === "bash";
            const script = isBash && typeof args === "object" && args !== null && "script" in args
              ? (args as any).script
              : null;
            const toolCallId = tc.id || tc.tool_call_id || "";

            return (
              <details
                key={toolCallId || idx}
                open={displayVerbosity === "max" ? true : undefined}
                className={`rounded p-2 ${showBorders ? 'border' : ''}`}
                style={{ background: "var(--tool-call-bg)", borderColor: "var(--tool-call-border)" }}
              >
                <summary className="flex cursor-pointer items-center gap-2 text-sm flex-wrap">
                  <span className="font-medium" style={{ color: "var(--tool-call-text, var(--tool-call-border))" }}>{toolName}</span>
                  {toolCallId && (
                    <span className="text-xs font-mono" style={{ color: "var(--muted)" }}>
                      {toolCallId.slice(-8)}
                    </span>
                  )}
                  {displayVerbosity === "medium" && (
                    <span className="text-xs font-mono" style={{ color: "var(--foreground)" }}>
                      {oneLinePreview(args)}
                    </span>
                  )}
                </summary>
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
              className={`rounded p-2 ${showBorders ? 'border' : ''}`}
              style={{ background: "var(--tool-msg-bg)", borderColor: "var(--tool-msg-border)" }}
            >
              <summary className="cursor-pointer text-sm font-medium font-mono" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>
                {displayVerbosity === "medium"
                  ? `Tool Output: ${name} · ${text.length.toLocaleString()} chars`
                  : `Tool Output: ${name}`}
              </summary>
              {displayVerbosity === "medium" && <div className="mt-1 text-xs font-mono">{oneLinePreview(text)}</div>}
              <pre className="mt-1 text-xs p-2 rounded overflow-auto max-h-64 whitespace-pre-wrap" style={{ background: "var(--code-bg)", color: "var(--tool-msg-text, var(--foreground))" }}>
                {text}
              </pre>
            </details>
          ))}

          {toolCallStreamEntries.map(([streamKey, text]) => (
            <details
              key={`tool-call-stream-${streamKey}`}
              open={displayVerbosity === "max" ? true : undefined}
              className={`rounded p-2 ${showBorders ? 'border' : ''}`}
              style={{ background: "var(--tool-call-bg)", borderColor: "var(--tool-call-border)" }}
            >
              <summary className="cursor-pointer text-sm font-medium font-mono" style={{ color: "var(--tool-call-text, var(--tool-call-border))" }}>
                {displayVerbosity === "medium"
                  ? `Tool Call Args: ${streamKey} · ${text.length.toLocaleString()} chars`
                  : `Tool Call Args: ${streamKey}`}
              </summary>
              {displayVerbosity === "medium" && (
                <div className="mt-1 text-xs font-mono" style={{ color: "var(--foreground)" }}>
                  {oneLinePreview(text)}
                </div>
              )}
              <pre className="mt-1 text-xs p-2 rounded overflow-auto max-h-40 whitespace-pre-wrap break-words" style={{ background: "var(--code-bg)", color: "var(--foreground)" }}>
                {text}
              </pre>
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
    const toolCallId = String(toolCall?.id || toolCall?.tool_call_id || "");
    if (toolCallId) toolCallNameById.set(toolCallId, toolCallName(toolCall));
  });
  let availableTokens = typeof message.tokens === "number" && Number.isFinite(message.tokens) ? message.tokens : undefined;
  const takeTokens = () => {
    const tokens = availableTokens;
    availableTokens = undefined;
    return tokens;
  };
  if (message.reasoning) {
    details.push({ kind: "reasoning", header: messageMetadataText(message, "Reasoning"), tokens: takeTokens(), source: "reasoning" });
  }
  if (message.tool_calls?.length) {
    message.tool_calls.forEach((tc: any) => {
      const name = toolCallName(tc);
      const args = toolCallArgs(tc);
      const toolCallId = tc?.id || tc?.tool_call_id || "";
      details.push({
        kind: "tool_calls",
        name,
        tool_call_id: toolCallId,
        tokens: takeTokens(),
        header: name ? `ToolCall: ${name}` : "ToolCall",
        body: formatHiddenDetailBody({
          ...(toolCallId ? { id: toolCallId } : {}),
          name,
          arguments: args,
        }),
        source: "tool_call",
      });
    });
  }
  if (message.role === "tool") {
    const contentText = contentToPlainText(message.content, message.content_text || "");
    const name = message.name || "tool";
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

function hasMinVisibleBody(message: Message): boolean {
  return ((message.role === "user" || message.role === "assistant")
    && Boolean(contentToPlainText(message.content, message.content_text || "").trim()))
    || isImportantSystemMessage(message)
    || message.kind === "compaction_marker"
    || message.role === "compaction_marker";
}

/** Collapse hidden details outside the mounted tail without losing min summaries. */
function PrefixHiddenDetails({ messages, showBorders }: { messages: Message[]; showBorders: boolean }) {
  const details = useMemo(() => messages.flatMap(collectHiddenDetailsForMessage), [messages]);
  if (!details.length) return null;
  return <HiddenDetailsBlock details={details} showBorders={showBorders} />;
}

const StaticTranscript = memo(function StaticTranscript({
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
  const content = renderMessagesForVerbosity(messages, displayVerbosity, showBorders, onStageAttachment);
  if (process.env.NODE_ENV === "production") return <>{content}</>;
  return (
    <Profiler id="StaticTranscript" onRender={(id, _phase, duration) => recordReactCommit(id as "StaticTranscript", duration)}>
      {content}
    </Profiler>
  );
});

interface ChatPanelProps {
  threadId: string;
  showBorders?: boolean;
  streamingTps?: number | null;
  onStageAttachment?: (attachment: AttachmentContentPart) => void;
}

export function ChatPanel({ threadId, showBorders = true, streamingTps = null, onStageAttachment }: ChatPanelProps) {
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
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [isLoadingOlder, setIsLoadingOlder] = useState(false);
  const [renderStartMessageId, setRenderStartMessageId] = useState<string | null>(null);

  const currentThreadId = threadId;
  const queryClient = useQueryClient();
  const transcriptQuery = useInfiniteQuery(transcriptInfiniteQueryOptions(threadId, queryClient));
  const messages = useMemo(() => flattenTranscript(transcriptQuery.data), [transcriptQuery.data]);
  const displayVerbosity = useAppStore((state) => state.displayVerbosity);
  const renderedTranscript = useMemo(() => {
    const window = transcriptWindow(messages, renderStartMessageId);
    if (displayVerbosity !== "min" || window.startIndex === 0) return window;
    const visiblePrefixIndex = messages
      .slice(0, window.startIndex)
      .findLastIndex(hasMinVisibleBody);
    if (visiblePrefixIndex < 0) return window;
    return transcriptWindow(messages, messages[visiblePrefixIndex].id);
  }, [displayVerbosity, messages, renderStartMessageId]);
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
  const showLiveCard = isStreaming || hasLiveTools;
  const hasActiveToolTiming = Object.values(visibleStreamingToolOutputs).some((tool) => Boolean(tool.startedAtMs || tool.timeout));
  const shouldUpdateTiming = isStreaming || hasActiveToolTiming || Boolean(streamingProviderRequest);
  const primaryToolTimeoutText = Object.values(visibleStreamingToolOutputs)
    .map((tool) => toolTimeoutCountdown(tool.timeout, nowMs))
    .find((text): text is string => Boolean(text));
  const providerTimeText = streamingKind === "llm"
    ? providerTimingText(streamingProviderRequest, nowMs) || elapsedSecondsText(streamingStartedAtMs, nowMs, "streaming")
    : null;
  const genericStreamingTimeText = streamingKind !== "llm"
    ? elapsedSecondsText(streamingStartedAtMs, nowMs, "streaming")
    : null;
  // Live execution is operational state, not historical transcript detail:
  // always expose streamed tool arguments and output at every verbosity.
  const streamingToolDetailsOpen = true;

  useEffect(() => {
    if (!shouldUpdateTiming) return;
    setNowMs(Date.now());
    const intervalId = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(intervalId);
  }, [shouldUpdateTiming]);

  // Stick-to-bottom scrolling: track if user intentionally scrolled away
  const stickToBottomRef = useRef(true);
  const rafIdRef = useRef<number | null>(null);
  const programmaticScrollTargetRef = useRef<number | null>(null);

  const distanceFromBottom = useCallback(() => {
    if (!scrollRef.current) return 0;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    return Math.max(0, scrollHeight - scrollTop - clientHeight);
  }, []);

  const isAtBottom = useCallback(() => {
    return distanceFromBottom() <= STICKY_BOTTOM_THRESHOLD_PX;
  }, [distanceFromBottom]);

  // In sticky mode every output append must keep following the tail. User
  // input detaches synchronously, so a previously queued frame cannot undo an
  // intentional scroll into history.
  const scrollToBottomNow = useCallback(() => {
    const el = scrollRef.current;
    if (!el || !stickToBottomRef.current) return;
    const target = Math.max(0, el.scrollHeight - el.clientHeight);
    programmaticScrollTargetRef.current = target;
    el.scrollTop = target;
  }, []);

  const revealFromMessage = useCallback((startMessageId: string | null) => {
    const el = scrollRef.current;
    const previousScrollHeight = el?.scrollHeight ?? 0;
    const previousScrollTop = el?.scrollTop ?? 0;
    setRenderStartMessageId(startMessageId);
    requestAnimationFrame(() => {
      const currentEl = scrollRef.current;
      if (!currentEl) return;
      currentEl.scrollTop = previousScrollTop + (currentEl.scrollHeight - previousScrollHeight);
    });
  }, []);

  const expandLoadedTranscript = useCallback(() => {
    revealFromMessage(expandedTranscriptStartId(messages, renderedTranscript.startIndex));
  }, [messages, renderedTranscript.startIndex, revealFromMessage]);

  const loadOlderMessages = useCallback(async () => {
    if (loadingOlderRef.current || isLoadingOlder || !transcriptQuery.hasNextPage) return;
    const el = scrollRef.current;
    const previousScrollHeight = el?.scrollHeight ?? 0;
    const previousScrollTop = el?.scrollTop ?? 0;
    loadingOlderRef.current = true;
    setIsLoadingOlder(true);
    stickToBottomRef.current = false;
    try {
      const result = await transcriptQuery.fetchNextPage();
      const updatedMessages = flattenTranscript(result.data);
      const previousStartId = renderedTranscript.messages[0]?.id || null;
      const previousStartIndex = previousStartId
        ? updatedMessages.findIndex((message) => message.id === previousStartId)
        : updatedMessages.length;
      const nextStartId = expandedTranscriptStartId(updatedMessages, previousStartIndex);
      setRenderStartMessageId(nextStartId);
      requestAnimationFrame(() => {
        const currentEl = scrollRef.current;
        if (!currentEl) return;
        currentEl.scrollTop = previousScrollTop + (currentEl.scrollHeight - previousScrollHeight);
      });
    } catch (error) {
      console.error("Failed to load older messages:", error);
    } finally {
      loadingOlderRef.current = false;
      setIsLoadingOlder(false);
    }
  }, [isLoadingOlder, renderedTranscript.messages, transcriptQuery.fetchNextPage, transcriptQuery.hasNextPage]);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    const programmaticTarget = programmaticScrollTargetRef.current;
    if (el && programmaticTarget !== null && Math.abs(el.scrollTop - programmaticTarget) <= 1) {
      programmaticScrollTargetRef.current = null;
      if (stickToBottomRef.current && !isAtBottom()) requestAnimationFrame(scrollToBottomNow);
    } else {
      programmaticScrollTargetRef.current = null;
      stickToBottomRef.current = isAtBottom();
    }
    if (el && el.scrollTop <= TRANSCRIPT_SCROLLBACK_THRESHOLD_PX) {
      void loadOlderMessages();
    }
  }, [isAtBottom, loadOlderMessages, scrollToBottomNow]);

  const detachFromBottom = useCallback(() => {
    stickToBottomRef.current = false;
    programmaticScrollTargetRef.current = null;
    if (rafIdRef.current !== null) {
      cancelAnimationFrame(rafIdRef.current);
      rafIdRef.current = null;
    }
  }, []);

  const handleWheel = useCallback((event: WheelEvent<HTMLDivElement>) => {
    if (event.deltaY >= 0 || nestedScrollportConsumesWheel(event.target, event.currentTarget, event.deltaY)) return;
    detachFromBottom();
  }, [detachFromBottom]);

  const handlePointerDown = useCallback((event: PointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (event.clientX >= rect.right - 20) detachFromBottom();
  }, [detachFromBottom]);

  const lastTouchYRef = useRef<number | null>(null);
  const handleTouchStart = useCallback((event: TouchEvent<HTMLDivElement>) => {
    lastTouchYRef.current = event.touches[0]?.clientY ?? null;
  }, []);

  const handleTouchMove = useCallback((event: TouchEvent<HTMLDivElement>) => {
    const nextY = event.touches[0]?.clientY;
    const previousY = lastTouchYRef.current;
    lastTouchYRef.current = nextY ?? null;
    if (nextY !== undefined && previousY !== null && nextY > previousY) detachFromBottom();
  }, [detachFromBottom]);

  const handleKeyDown = useCallback((event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "End") {
      stickToBottomRef.current = true;
      programmaticScrollTargetRef.current = null;
      requestAnimationFrame(scrollToBottomNow);
      return;
    }
    if (["ArrowUp", "PageUp", "Home"].includes(event.key) || (event.key === " " && event.shiftKey)) {
      detachFromBottom();
    }
  }, [detachFromBottom, scrollToBottomNow]);

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
        const container = document.getElementById('streaming-reasoning-container');
        if (container) container.style.display = displayVerbosity === "min" ? 'none' : 'block';
      }
      appended = appendBufferedTextChunks(streamingReasoningRef.current, chunks, lastReasoningIndexRef.current) || appended;
      lastReasoningIndexRef.current = chunks.length;
    }

    if (streamingReasoningSummaryRef.current) {
      const chunks = streamingBuffer.reasoningSummaryChunks;
      if (chunks.length > 0) {
        const container = document.getElementById('streaming-reasoning-summary-container');
        if (container) container.style.display = displayVerbosity === "min" ? 'none' : 'block';
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
      if (stickToBottomRef.current) {
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
        toolCall.name === "bash" ? argumentPrefix.replace(/^\s*\{?\s*"script"\s*:\s*"?/, "$ ") : argumentPrefix,
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

  const attachStreamingToolCallArgs = useCallback((toolCallId: string, element: HTMLPreElement | null) => {
    const previous = streamingToolCallArgRefs.current[toolCallId];
    streamingToolCallArgRefs.current[toolCallId] = element;
    if (!element || previous === element) return;
    lastToolCallArgIndexRef.current[toolCallId] = 0;
    delete lastToolCallChunksRef.current[toolCallId];
    element.textContent = "";
    requestAnimationFrame(flushStreamingToolCalls);
  }, [flushStreamingToolCalls]);

  const attachStreamingToolOutput = useCallback((toolCallId: string, element: HTMLPreElement | null) => {
    const previous = streamingToolOutputRefs.current[toolCallId];
    streamingToolOutputRefs.current[toolCallId] = element;
    if (!element || previous === element) return;
    lastToolOutputIndexRef.current[toolCallId] = 0;
    element.textContent = "";
    requestAnimationFrame(flushStreamingToolOutput);
  }, [flushStreamingToolOutput]);

  // Subscribe to streaming buffer updates - bypasses React entirely
  // This is O(1) per chunk with direct DOM manipulation
  // Re-runs when isStreaming changes to catch up with buffered content when refs become available
  useEffect(() => {
    const streamingBuffer = streamingBufferForThread(threadId);

    const unsubContent = streamingBuffer.subscribeContent(scheduleStreamingTextFlush);
    const unsubReasoning = streamingBuffer.subscribeReasoning(scheduleStreamingTextFlush);

    // Render any existing buffer content (catches up when joining mid-stream).
    // When isStreaming changes to true, refs should be available after render.
    const timeoutId = isStreaming ? scheduleInitialStreamingFlush(flushStreamingText) : null;

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
  }, [isStreaming, flushStreamingText, scheduleInitialStreamingFlush, scheduleStreamingTextFlush, threadId]);

  // Tool-call arguments use the same mutable/RAF architecture as text and
  // tool output. A burst schedules one imperative preview flush per frame and
  // never publishes its growing argument body through React or Zustand.
  useEffect(() => {
    const streamingBuffer = streamingBufferForThread(threadId);
    const coalescer = new AnimationFrameCoalescer(
      (callback) => window.requestAnimationFrame(callback),
      (id) => window.cancelAnimationFrame(id),
    );
    const previewCoalescer = new IntervalCoalescer<number>(
      TOOL_ARGUMENT_PREVIEW_INTERVAL_MS,
      () => performance.now(),
      (callback, delayMs) => window.setTimeout(callback, delayMs),
      (id) => window.clearTimeout(id),
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
      Object.values(streamingToolCallArgRefs.current).forEach((el) => {
        if (el) el.textContent = '';
      });
      Object.values(streamingToolCallPreviewRefs.current).forEach((el) => {
        if (el) el.textContent = '';
      });
    }
  }, [showLiveCard]);

  const { isLoading, isError, refetch } = transcriptQuery;

  // Reset scroll state and scroll to bottom when thread changes
  useEffect(() => {
    stickToBottomRef.current = true;
    loadingOlderRef.current = false;
    setRenderStartMessageId(null);
    setIsLoadingOlder(false);
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
    if ((Object.keys(visibleStreamingToolCalls).length > 0 || Object.keys(visibleStreamingToolOutputs).length > 0) && stickToBottomRef.current) {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          scrollToBottomNow();
        });
      });
    }
  }, [scrollToBottomNow, streamingToolCalls, streamingToolOutputs]);

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

  const panel = (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className={`eggw-section-header px-4 py-2 text-xs flex items-center justify-between flex-shrink-0 ${showBorders ? 'border-b border-[var(--panel-border)]' : ''}`} style={{ color: "var(--muted)" }}>
        <span>
              Chat Messages · {messages.length.toLocaleString()} loaded{transcriptQuery.hasNextPage ? " · scroll up for older" : ""}{formattedStreamingTps ? ` | ${formattedStreamingTps}` : ""}
          {isStreaming && providerTimeText ? ` | ${providerTimeText}` : ""}
          {isStreaming && !providerTimeText && genericStreamingTimeText ? ` | ${genericStreamingTimeText}` : ""}
        </span>
      </div>
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        onWheel={handleWheel}
        onTouchStart={handleTouchStart}
        onTouchMove={handleTouchMove}
        onPointerDown={handlePointerDown}
        onKeyDown={handleKeyDown}
        tabIndex={0}
        className="eggw-transcript-scroll flex-1 overflow-auto px-4 py-6 md:px-8"
        data-testid="chat-panel"
      >
        <div className="eggw-transcript-inner" data-testid="chat-panel-content">
          {isLoading ? (
            <div className="eggw-empty-state text-center" style={{ color: "var(--muted)" }}>Loading messages...</div>
          ) : isError ? (
            <div className="eggw-empty-state text-center space-y-2">
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
            <div className="eggw-empty-state text-center" style={{ color: "var(--muted)" }}>
              No messages yet. Start a conversation!
            </div>
          ) : (
            <>
              {messages.length > 0 && transcriptQuery.hasNextPage && (
                <div className="mb-4 flex justify-center">
                  <button
                    type="button"
                    onClick={() => void loadOlderMessages()}
                    disabled={isLoadingOlder}
                    className="rounded-full border px-3 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-60"
                    style={{ borderColor: "var(--panel-border)", background: "var(--panel-bg)", color: "var(--muted)" }}
                    data-testid="load-older-messages"
                  >
                    {isLoadingOlder ? "Loading older messages…" : "Load older messages"}
                  </button>
                </div>
              )}
              {renderedTranscript.hiddenCount > 0 && (
                <div className="mb-4 flex justify-center">
                  <button
                    type="button"
                    onClick={expandLoadedTranscript}
                    className="rounded-full border px-3 py-1 text-xs"
                    style={{ borderColor: "var(--panel-border)", background: "var(--panel-bg)", color: "var(--muted)" }}
                    data-testid="show-more-loaded-messages"
                  >
                    Show more loaded messages ({renderedTranscript.hiddenCount.toLocaleString()} hidden)
                  </button>
                </div>
              )}
              {displayVerbosity === "min" && renderedTranscript.hiddenCount > 0 && (
                <PrefixHiddenDetails
                  messages={messages.slice(0, renderedTranscript.startIndex)}
                  showBorders={showBorders}
                />
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
                  className={`eggw-message-card rounded p-4 mb-4 ${showBorders ? 'border' : ''}`}
                  style={{ background: "var(--assistant-msg-bg)", borderColor: "var(--assistant-msg-border)", color: "var(--assistant-msg-text, var(--foreground))" }}
                >
                  <div className="text-xs mb-2" style={{ color: "var(--muted)" }}>
                    <span className="font-medium" style={{ color: "var(--assistant-msg-text, var(--foreground))" }}>{streamingRoleLabel}</span>
                    {displayVerbosity !== "min" && streamingModelKey && (
                      <span style={{ color: "var(--muted)" }}> ({streamingModelKey})</span>
                    )}
                    {isStreaming && (
                      <span className="ml-2 animate-pulse" style={{ color: "var(--accent)" }}>streaming...</span>
                    )}
                    {providerTimeText && (
                      <span className="ml-2" style={{ color: "var(--accent)" }}>{providerTimeText}</span>
                    )}
                    {!providerTimeText && genericStreamingTimeText && (
                      <span className="ml-2" style={{ color: "var(--accent)" }}>{genericStreamingTimeText}</span>
                    )}
                    {streamingKind === "tool" && primaryToolTimeoutText && (
                      <span data-testid="streaming-tool-timeout-header" className="ml-2" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>
                        {primaryToolTimeoutText}
                      </span>
                    )}
                  </div>

                  <>
                      {/* Streaming reasoning - direct DOM updates via ref */}
                      <details
                      open={displayVerbosity === "max" ? true : undefined}
                      className={`mb-2 rounded p-2 ${showBorders ? 'border' : ''}`}
                      style={{ background: "var(--reasoning-bg)", borderColor: "var(--reasoning-border)", display: displayVerbosity === "min" ? "none" : undefined }}
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
                      open={displayVerbosity === "max" ? true : undefined}
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
                      data-testid="streaming-content"
                      className="text-sm"
                      style={{
                        color: "var(--assistant-msg-text, var(--foreground))",
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-word",
                      }}
                      />

                      {/* Streaming tool calls */}
                      {Object.keys(visibleStreamingToolCalls).length > 0 && (
                      <div className="mt-2 space-y-2">
                        {Object.entries(visibleStreamingToolCalls).map(([tcId, tc]) => {
                          return (
                            <details
                              key={tcId}
                              open={streamingToolDetailsOpen}
                              className={`rounded ${showBorders ? 'border' : ''}`}
                              style={{ background: "var(--tool-call-bg)", borderColor: "var(--tool-call-border)" }}
                            >
                              <summary className="cursor-pointer p-2 flex items-center gap-2 text-sm">
                                <span className="font-medium" style={{ color: "var(--tool-call-text, var(--tool-call-border))" }}>{tc.name || "tool"}</span>
                                <span className="text-xs font-mono" style={{ color: "var(--muted)" }}>
                                  {tcId.slice(-8)}
                                </span>
                                <span className="text-xs animate-pulse" style={{ color: "var(--tool-call-text, var(--tool-call-border))" }}>streaming...</span>
                                {displayVerbosity === "medium" && (
                                  <span
                                    ref={(element) => { streamingToolCallPreviewRefs.current[tcId] = element; }}
                                    className="text-xs font-mono"
                                    style={{ color: "var(--foreground)" }}
                                  />
                                )}
                                {displayVerbosity === "medium" && (
                                  <span className="text-xs" style={{ color: "var(--muted)" }}>
                                    expand to inspect args
                                  </span>
                                )}
                              </summary>
                              <div className="px-2 pb-2">
                                <pre
                                  ref={(element) => attachStreamingToolCallArgs(tcId, element)}
                                  data-testid="streaming-tool-arguments"
                                  className="text-xs p-2 rounded overflow-auto whitespace-pre-wrap break-all"
                                  style={{ background: "var(--code-bg)", color: tc.name === "bash" ? "var(--accent)" : "var(--foreground)" }}
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
                          const timeoutText = toolTimeoutCountdown(tool.timeout, nowMs);
                          const elapsedText = tool.startedAtMs ? elapsedSecondsText(tool.startedAtMs, nowMs, "running") : null;
                          return (
                            <details
                              key={toolId}
                              open={streamingToolDetailsOpen}
                              className={`rounded ${showBorders ? 'border' : ''}`}
                              style={{ background: "var(--tool-msg-bg)", borderColor: "var(--tool-msg-border)" }}
                            >
                              <summary className="cursor-pointer p-2 flex items-center gap-2 text-sm flex-wrap">
                                <span className="font-medium" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>{tool.name || "tool"}</span>
                                <span className="text-xs font-mono" style={{ color: "var(--muted)" }}>
                                  {toolId.slice(-8)}
                                </span>
                                <span className="text-xs animate-pulse" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>streaming output...</span>
                                {displayVerbosity === "medium" && (
                                  <span className="text-xs" style={{ color: "var(--muted)" }}>
                                    expand to inspect output
                                  </span>
                                )}
                                {elapsedText && (
                                  <span data-testid="streaming-tool-elapsed-summary" className="text-xs" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>
                                    {elapsedText}
                                  </span>
                                )}
                                {timeoutText && (
                                  <span data-testid="streaming-tool-timeout-summary" className="text-xs" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>
                                    {timeoutText}
                                  </span>
                                )}
                              </summary>
                              <div className="px-2 pb-2">
                                {elapsedText && (
                                  <div
                                    data-testid="streaming-tool-elapsed"
                                    className="mb-2 text-xs"
                                    style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}
                                  >
                                    {elapsedText}
                                  </div>
                                )}
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
                                  ref={(element) => attachStreamingToolOutput(toolId, element)}
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
