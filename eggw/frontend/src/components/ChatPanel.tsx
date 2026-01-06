"use client";

import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { fetchMessages } from "@/lib/api";
import { useAppStore, Message } from "@/lib/store";
import clsx from "clsx";

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
  // Match [ followed by whitespace and backslash, capture until closing ]
  processed = processed.replace(
    /\[\s*(\\[\s\S]*?)\s*\]/g,
    (match, math) => {
      // Only convert if it looks like LaTeX (contains common LaTeX commands)
      if (/\\(?:begin|end|frac|sum|int|prod|lim|nabla|partial|sqrt|text|mathbf|mathrm|left|right|aligned|equation|matrix|cases)/.test(math)) {
        return `$$${math}$$`;
      }
      return match; // Keep original if not LaTeX
    }
  );

  return processed;
}

interface MessageBlockProps {
  message: Message;
}

function MessageBlock({ message }: MessageBlockProps) {
  const roleColors: Record<string, string> = {
    user: "bg-blue-900/50 border-blue-700",
    assistant: "bg-slate-800 border-slate-600",
    system: "bg-gray-800 border-gray-600",
    tool: "bg-emerald-900/30 border-emerald-700",
  };

  const roleLabels: Record<string, string> = {
    user: "User",
    assistant: "Assistant",
    system: "System",
    tool: "Tool Result",
  };

  // Check if this is a shell command (starts with $ or $$)
  // Handle cases: "$ cmd", "$$ cmd", "$cmd" (no space)
  const isShellCommand = message.role === "user" &&
    message.content?.match(/^\$\$?\s*\S/);

  // For tool messages, check if content is long
  const isLongToolOutput = message.role === "tool" &&
    message.content && message.content.length > 500;

  return (
    <div
      className={clsx(
        "rounded border p-3 mb-3",
        isShellCommand ? "bg-gray-900 border-gray-600" : roleColors[message.role] || "bg-gray-800 border-gray-600"
      )}
    >
      {/* Header */}
      <div className="flex items-center gap-2 mb-2 text-xs text-gray-400">
        <span className="font-medium text-gray-300">
          {isShellCommand ? "Shell" : roleLabels[message.role] || message.role}
        </span>
        {message.model_key && (
          <span className="text-gray-500">({message.model_key})</span>
        )}
        {message.tool_call_id && (
          <span className="text-emerald-500 font-mono">
            ← {message.tool_call_id.slice(-8)}
          </span>
        )}
      </div>

      {/* Reasoning (collapsible) */}
      {message.reasoning && (
        <details className="mb-2 bg-purple-900/30 rounded p-2 border border-purple-700">
          <summary className="cursor-pointer text-sm text-purple-300">
            Reasoning
          </summary>
          <div className="mt-2 text-sm text-purple-200 whitespace-pre-wrap">
            {message.reasoning}
          </div>
        </details>
      )}

      {/* Content */}
      {message.content && (
        <>
          {/* Shell command display */}
          {isShellCommand ? (
            <pre className="text-sm text-green-400 font-mono bg-black/30 p-2 rounded overflow-auto">
              {message.content}
            </pre>
          ) : message.role === "tool" ? (
            /* Tool output - collapsible if long */
            isLongToolOutput ? (
              <details className="bg-black/30 rounded border border-emerald-800">
                <summary className="cursor-pointer p-2 text-sm text-emerald-300">
                  Output ({message.content.length.toLocaleString()} chars) - click to expand
                </summary>
                <pre className="p-2 text-xs text-gray-300 overflow-auto max-h-96 whitespace-pre-wrap">
                  {message.content}
                </pre>
              </details>
            ) : (
              <pre className="text-xs text-gray-300 bg-black/30 p-2 rounded overflow-auto max-h-64 whitespace-pre-wrap">
                {message.content}
              </pre>
            )
          ) : (
            /* Regular markdown content with GFM tables and LaTeX support */
            <div className="prose prose-invert prose-sm max-w-none">
              <ReactMarkdown
                remarkPlugins={[remarkGfm, remarkMath]}
                rehypePlugins={[rehypeKatex]}
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
                }}
              >
                {preprocessLatex(message.content)}
              </ReactMarkdown>
            </div>
          )}
        </>
      )}

      {/* Tool calls */}
      {message.tool_calls && message.tool_calls.length > 0 && (
        <div className="mt-2 space-y-2">
          {message.tool_calls.map((tc: any, idx: number) => {
            // Extract the tool name - handle both formats
            const toolName = tc.name || tc.function?.name || "unknown";
            // Extract arguments - handle both formats
            let args = tc.arguments || tc.function?.arguments;
            if (typeof args === "string") {
              try {
                args = JSON.parse(args);
              } catch {
                // Keep as string
              }
            }
            // For bash commands, extract the script
            const isBash = toolName === "bash";
            const script = isBash && args?.script;

            return (
              <div
                key={tc.id || idx}
                className="bg-amber-900/30 rounded p-2 border border-amber-700"
              >
                <div className="flex items-center gap-2 text-sm">
                  <span className="text-amber-400 font-medium">{toolName}</span>
                  <span className="text-xs text-gray-500 font-mono">
                    {tc.id?.slice(-8)}
                  </span>
                </div>
                {/* Special display for bash scripts */}
                {isBash && script ? (
                  <pre className="mt-1 text-sm text-green-400 font-mono bg-black/40 p-2 rounded overflow-auto whitespace-pre-wrap break-all">
                    $ {script}
                  </pre>
                ) : (
                  <pre className="mt-1 text-xs text-gray-200 bg-black/30 p-1 rounded overflow-auto max-h-40 whitespace-pre-wrap break-words">
                    {typeof args === "string"
                      ? args
                      : JSON.stringify(args, null, 2)}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function ChatPanel() {
  const scrollRef = useRef<HTMLDivElement>(null);
  const {
    currentThreadId,
    messages,
    setMessages,
    streamingContent,
    streamingReasoning,
    streamingToolCalls,
  } = useAppStore();

  const { data, isLoading } = useQuery({
    queryKey: ["messages", currentThreadId],
    queryFn: () => fetchMessages(currentThreadId!),
    enabled: !!currentThreadId,
  });

  // Sync fetched messages to store (but don't overwrite optimistic updates)
  useEffect(() => {
    if (data) {
      setMessages(data);
    }
  }, [data, setMessages]);

  // Auto-scroll to bottom
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, streamingContent, streamingReasoning, streamingToolCalls]);

  if (!currentThreadId) {
    return (
      <div className="flex-1 flex items-center justify-center text-gray-500">
        Select a thread to view messages
      </div>
    );
  }

  return (
    <div ref={scrollRef} className="flex-1 overflow-auto p-4">
      {isLoading ? (
        <div className="text-center text-gray-500">Loading messages...</div>
      ) : messages.length === 0 ? (
        <div className="text-center text-gray-500">
          No messages yet. Start a conversation!
        </div>
      ) : (
        <>
          {messages.map((msg, idx) => (
            <MessageBlock key={msg.id || idx} message={msg} />
          ))}

          {/* Streaming content */}
          {(streamingContent || streamingReasoning || Object.keys(streamingToolCalls).length > 0) && (
            <div className="rounded border p-3 mb-3 bg-slate-800 border-slate-600">
              <div className="text-xs text-gray-400 mb-2">
                <span className="font-medium text-gray-300">Assistant</span>
                <span className="ml-2 text-blue-400 animate-pulse">streaming...</span>
              </div>

              {/* Streaming reasoning */}
              {streamingReasoning && (
                <details open className="mb-2 bg-purple-900/30 rounded p-2 border border-purple-700">
                  <summary className="cursor-pointer text-sm text-purple-300">
                    Reasoning <span className="text-xs text-purple-400 animate-pulse">(streaming...)</span>
                  </summary>
                  <div className="mt-2 text-sm text-purple-200 whitespace-pre-wrap">
                    {streamingReasoning}
                  </div>
                </details>
              )}

              {/* Streaming content with GFM tables and LaTeX support */}
              {streamingContent && (
                <div className="prose prose-invert prose-sm max-w-none">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm, remarkMath]}
                    rehypePlugins={[rehypeKatex]}
                  >
                    {preprocessLatex(streamingContent)}
                  </ReactMarkdown>
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
                        className="bg-amber-900/30 rounded border border-amber-700"
                      >
                        <summary className="cursor-pointer p-2 flex items-center gap-2 text-sm">
                          <span className="text-amber-400 font-medium">{tc.name || "tool"}</span>
                          <span className="text-xs text-gray-500 font-mono">
                            {tcId.slice(-8)}
                          </span>
                          <span className="text-xs text-amber-300 animate-pulse">streaming...</span>
                        </summary>
                        <div className="px-2 pb-2">
                          {isBash && script ? (
                            <pre className="text-sm text-green-400 font-mono bg-black/40 p-2 rounded overflow-auto whitespace-pre-wrap break-all">
                              $ {script}
                            </pre>
                          ) : (
                            <pre className="text-xs text-gray-200 bg-black/30 p-2 rounded overflow-auto max-h-64 whitespace-pre-wrap break-all">
                              {tc.arguments || "..."}
                            </pre>
                          )}
                        </div>
                      </details>
                    );
                  })}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
