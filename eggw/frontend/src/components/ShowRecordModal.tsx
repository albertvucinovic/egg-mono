"use client";

import { useMemo } from "react";
import { useAppStore, type Message, type ShowRecordTarget } from "@/lib/store";
import { contentToPlainText } from "@/lib/contentParts";
import { OverlayPanel } from "@/components/ui/OverlayPanel";
import { Button } from "@/components/ui/primitives";
import { formatStreamingTps, formatTokenCount } from "@/lib/tps";

function readableValue(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value ?? "");
  }
}

function toolCallParts(toolCall: Record<string, any> | null | undefined) {
  const fn = toolCall?.function && typeof toolCall.function === "object" ? toolCall.function : {};
  const name = String(fn.name || toolCall?.name || "tool");
  const args = fn.arguments ?? toolCall?.arguments ?? "";
  return { name, args: readableValue(args) };
}

function metadata(target: ShowRecordTarget, message: Message): string[] {
  const fields = [
    `record_id: ${target.record_id}`,
    `kind: ${target.kind}`,
    `message_id: ${target.message_id}`,
    target.tool_call_id ? `tool_call_id: ${target.tool_call_id}` : "",
    message.model_key ? `model: ${message.model_key}` : "",
    formatTokenCount(message.tokens),
    message.token_stats?.content_tokens ? `content: ${formatTokenCount(message.token_stats.content_tokens)}` : "",
    message.token_stats?.reasoning_tokens ? `reasoning: ${formatTokenCount(message.token_stats.reasoning_tokens)}` : "",
    message.token_stats?.tool_calls_tokens ? `tool calls: ${formatTokenCount(message.token_stats.tool_calls_tokens)}` : "",
    formatStreamingTps(message.tps),
    message.timestamp ? `time: ${new Date(message.timestamp).toLocaleString()}` : "",
    message.user_tool_call ? "origin: user tool call" : "",
    message.incomplete ? `incomplete: ${message.incomplete_reason || "yes"}` : "",
    message.runner_error ? `runner error: ${message.runner_error}` : "",
    `event: #${target.event_seq}`,
  ];
  return fields.filter((field): field is string => Boolean(field));
}

function optimizerText(message: Message): string | null {
  const value = message.output_optimizer;
  if (!value || value.optimized !== true) return null;
  const summary = String(value.summary_with_artifact || value.summary || "").trim();
  const hint = String(value.raw_hint || "").trim();
  return [summary, hint && `Raw output: ${hint}`].filter(Boolean).join("\n") || null;
}

export function ShowRecordModal({ threadId }: { threadId: string }) {
  const target = useAppStore((state) => state.showRecordTargetByThread[threadId]);
  const setShowRecordTarget = useAppStore((state) => state.setShowRecordTarget);
  const message = target?.message;
  const content = useMemo(
    () => (message ? contentToPlainText(message.content, message.content_text || "") : ""),
    [message],
  );
  const toolCall = target?.kind === "tool_declaration" ? toolCallParts(target.tool_call) : null;
  const close = () => setShowRecordTarget(threadId, null);

  return (
    <OverlayPanel
      open={Boolean(target && message)}
      onClose={close}
      title={target ? `Inspect ${target.label}` : "Inspect record"}
      description="Read-only current-thread record. Global transcript verbosity is unchanged."
      closeLabel="Close inspected record"
      testId="show-record-modal"
      returnFocusSelector="[data-testid='message-input']"
      panelClassName="max-w-5xl"
      portal
      footer={<Button variant="secondary" onClick={close}>Close</Button>}
    >
      {target && message && (
        <article className="space-y-4" data-testid="show-record-content">
          <div className="eggw-message-meta flex flex-wrap gap-x-3 gap-y-1 font-mono">
            {metadata(target, message).map((field) => <span key={field}>{field}</span>)}
          </div>
          {target.paired_message_ids.length > 0 && (
            <div className="eggw-message-meta font-mono">
              Exact paired message IDs: {target.paired_message_ids.join(", ")}
            </div>
          )}
          {message.reasoning && (
            <section>
              <h3 className="font-semibold">Reasoning</h3>
              <pre className="eggw-code-block max-h-72 whitespace-pre-wrap">{message.reasoning}</pre>
            </section>
          )}
          {toolCall && (
            <section>
              <h3 className="font-semibold">Tool declaration: {toolCall.name}</h3>
              <pre className="eggw-code-block max-h-72 whitespace-pre-wrap break-words">{toolCall.args}</pre>
            </section>
          )}
          {!toolCall && content && (
            <section>
              <h3 className="font-semibold">Content</h3>
              <pre className="eggw-code-block max-h-[55vh] whitespace-pre-wrap break-words">{content}</pre>
            </section>
          )}
          {optimizerText(message) && (
            <section>
              <h3 className="font-semibold">Output recovery</h3>
              <pre className="eggw-code-block whitespace-pre-wrap">{optimizerText(message)}</pre>
            </section>
          )}
          {target.kind === "message" && Array.isArray(message.tool_calls) && message.tool_calls.length > 0 && (
            <section>
              <h3 className="font-semibold">Tool declarations</h3>
              <pre className="eggw-code-block max-h-72 whitespace-pre-wrap break-words">{readableValue(message.tool_calls)}</pre>
            </section>
          )}
        </article>
      )}
    </OverlayPanel>
  );
}
