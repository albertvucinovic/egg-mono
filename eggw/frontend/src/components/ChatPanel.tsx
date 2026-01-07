"use client";

import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import rehypeRaw from "rehype-raw";
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
  showBorders?: boolean;
}

function MessageBlock({ message, showBorders = true }: MessageBlockProps) {
  // Use CSS variables for theme-aware colors
  // Text colors use fallback to --foreground for themes that don't define *-text vars
  const roleStyles: Record<string, React.CSSProperties> = {
    user: { background: "var(--user-msg-bg)", borderColor: "var(--user-msg-border)", color: "var(--user-msg-text, var(--foreground))" },
    assistant: { background: "var(--assistant-msg-bg)", borderColor: "var(--assistant-msg-border)", color: "var(--assistant-msg-text, var(--foreground))" },
    system: { background: "var(--system-msg-bg)", borderColor: "var(--system-msg-border)", color: "var(--system-msg-text, var(--foreground))" },
    tool: { background: "var(--tool-msg-bg)", borderColor: "var(--tool-msg-border)", color: "var(--tool-msg-text, var(--foreground))" },
  };

  const roleLabels: Record<string, string> = {
    user: "User",
    assistant: "Assistant",
    system: "Command",
    tool: "Tool Result",
  };

  // Check if this is a shell command (starts with $ or $$)
  // Handle cases: "$ cmd", "$$ cmd", "$cmd" (no space)
  const isShellCommand = message.role === "user" &&
    message.content?.match(/^\$\$?\s*\S/);

  // Check if this is a system/command message (should render as monospace)
  const isCommandOutput = message.role === "system";

  // For tool messages, check if content is long
  const isLongToolOutput = message.role === "tool" &&
    message.content && message.content.length > 500;

  const shellStyle: React.CSSProperties = { background: "var(--code-bg)", borderColor: "var(--panel-border)" };

  return (
    <div
      className={`rounded p-3 mb-3 ${showBorders ? 'border' : ''}`}
      style={isShellCommand ? shellStyle : (roleStyles[message.role] || shellStyle)}
    >
      {/* Header */}
      <div className="flex items-center gap-2 mb-2 text-xs flex-wrap" style={{ color: "var(--muted)" }}>
        <span className="font-medium" style={roleStyles[message.role] ? { color: roleStyles[message.role].color } : { color: "var(--foreground)" }}>
          {isShellCommand ? "Shell" : roleLabels[message.role] || message.role}
        </span>
        {message.model_key && (
          <span style={{ color: "var(--muted)" }}>({message.model_key})</span>
        )}
        {message.tokens && message.tokens > 0 && (
          <span style={{ color: "var(--muted)" }}>(tok={message.tokens.toLocaleString()})</span>
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
          <span className="font-mono" style={{ color: "var(--muted)" }}>
            {message.id.slice(-8)}
          </span>
        )}
        {message.tool_call_id && (
          <span className="font-mono" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>
            ← {message.tool_call_id.slice(-8)}
          </span>
        )}
      </div>

      {/* Reasoning (collapsible) */}
      {message.reasoning && (
        <details
          className={`mb-2 rounded p-2 ${showBorders ? 'border' : ''}`}
          style={{ background: "var(--reasoning-bg)", borderColor: "var(--reasoning-border)" }}
        >
          <summary className="cursor-pointer text-sm" style={{ color: "var(--reasoning-text, var(--reasoning-border))" }}>
            Reasoning
          </summary>
          <div className="mt-2 text-sm whitespace-pre-wrap" style={{ color: "var(--reasoning-text, var(--foreground))", opacity: 0.9 }}>
            {message.reasoning}
          </div>
        </details>
      )}

      {/* Content */}
      {message.content && (
        <>
          {/* Shell command display */}
          {isShellCommand ? (
            <pre className="text-sm font-mono p-2 rounded overflow-auto" style={{ background: "var(--code-bg)", color: "var(--accent)" }}>
              {message.content}
            </pre>
          ) : isCommandOutput ? (
            /* Command output (system messages) - monospace for tree/list formatting */
            <pre className="text-sm font-mono p-2 rounded overflow-auto whitespace-pre-wrap" style={{ background: "var(--code-bg)", color: "var(--system-msg-text, var(--foreground))" }}>
              {message.content}
            </pre>
          ) : message.role === "tool" ? (
            /* Tool output - collapsible if long */
            isLongToolOutput ? (
              <details className={`rounded ${showBorders ? 'border' : ''}`} style={{ background: "var(--code-bg)", borderColor: "var(--tool-msg-border)" }}>
                <summary className="cursor-pointer p-2 text-sm" style={{ color: "var(--tool-msg-text, var(--tool-msg-border))" }}>
                  Output ({message.content.length.toLocaleString()} chars) - click to expand
                </summary>
                <pre className="p-2 text-xs overflow-auto max-h-96 whitespace-pre-wrap" style={{ color: "var(--tool-msg-text, var(--foreground))" }}>
                  {message.content}
                </pre>
              </details>
            ) : (
              <pre className="text-xs p-2 rounded overflow-auto max-h-64 whitespace-pre-wrap" style={{ background: "var(--code-bg)", color: "var(--tool-msg-text, var(--foreground))" }}>
                {message.content}
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
                className={`rounded p-2 ${showBorders ? 'border' : ''}`}
                style={{ background: "var(--tool-call-bg)", borderColor: "var(--tool-call-border)" }}
              >
                <div className="flex items-center gap-2 text-sm">
                  <span className="font-medium" style={{ color: "var(--tool-call-text, var(--tool-call-border))" }}>{toolName}</span>
                  <span className="text-xs font-mono" style={{ color: "var(--muted)" }}>
                    {tc.id?.slice(-8)}
                  </span>
                </div>
                {/* Special display for bash scripts */}
                {isBash && script ? (
                  <pre className="mt-1 text-sm font-mono p-2 rounded overflow-auto whitespace-pre-wrap break-all" style={{ background: "var(--code-bg)", color: "var(--accent)" }}>
                    $ {script}
                  </pre>
                ) : (
                  <pre className="mt-1 text-xs p-1 rounded overflow-auto max-h-40 whitespace-pre-wrap break-words" style={{ background: "var(--code-bg)", color: "var(--foreground)" }}>
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

interface ChatPanelProps {
  showBorders?: boolean;
}

export function ChatPanel({ showBorders = true }: ChatPanelProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const streamingContentRef = useRef<HTMLDivElement>(null);
  const streamingReasoningRef = useRef<HTMLDivElement>(null);
  const lastContentIndexRef = useRef(0);
  const lastReasoningIndexRef = useRef(0);

  // Smart auto-scroll: only scroll if user is at/near bottom
  // This allows users to scroll up to read while streaming continues
  const shouldAutoScrollRef = useRef(true);
  const SCROLL_THRESHOLD = 50; // pixels from bottom to consider "at bottom"

  const {
    currentThreadId,
    messages,
    setMessages,
    streamingToolCalls,
    isStreaming,
  } = useAppStore();

  // Check if scrolled to bottom (within threshold)
  const isAtBottom = () => {
    if (!scrollRef.current) return true;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    return scrollHeight - scrollTop - clientHeight < SCROLL_THRESHOLD;
  };

  // Handle user scroll - update shouldAutoScroll based on position
  const handleScroll = () => {
    shouldAutoScrollRef.current = isAtBottom();
  };

  // Auto-scroll helper that respects user scroll position
  const autoScroll = () => {
    if (shouldAutoScrollRef.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  };

  // Subscribe to streaming buffer updates - bypasses React entirely
  // This is O(1) per chunk with direct DOM manipulation
  useEffect(() => {
    // Import here to avoid SSR issues
    const { streamingBuffer } = require("@/lib/streamingBuffer");

    const handleContentUpdate = () => {
      if (!streamingContentRef.current) return;

      const chunks = streamingBuffer.contentChunks;
      // Only append new chunks since last update
      for (let i = lastContentIndexRef.current; i < chunks.length; i++) {
        streamingContentRef.current.appendChild(document.createTextNode(chunks[i]));
      }
      lastContentIndexRef.current = chunks.length;

      // Auto-scroll (respects user scroll position)
      autoScroll();
    };

    const handleReasoningUpdate = () => {
      if (!streamingReasoningRef.current) return;

      const chunks = streamingBuffer.reasoningChunks;
      // Show the reasoning container when first chunk arrives
      if (chunks.length > 0 && lastReasoningIndexRef.current === 0) {
        const container = document.getElementById('streaming-reasoning-container');
        if (container) container.style.display = 'block';
      }
      for (let i = lastReasoningIndexRef.current; i < chunks.length; i++) {
        streamingReasoningRef.current.appendChild(document.createTextNode(chunks[i]));
      }
      lastReasoningIndexRef.current = chunks.length;
    };

    const unsubContent = streamingBuffer.subscribeContent(handleContentUpdate);
    const unsubReasoning = streamingBuffer.subscribeReasoning(handleReasoningUpdate);

    return () => {
      unsubContent();
      unsubReasoning();
    };
  }, []);

  // Reset DOM when streaming stops
  useEffect(() => {
    if (!isStreaming) {
      lastContentIndexRef.current = 0;
      lastReasoningIndexRef.current = 0;
      if (streamingContentRef.current) {
        streamingContentRef.current.textContent = '';
      }
      if (streamingReasoningRef.current) {
        streamingReasoningRef.current.textContent = '';
      }
    }
  }, [isStreaming]);

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

  // Auto-scroll to bottom on new messages (respects user scroll position)
  useEffect(() => {
    autoScroll();
  }, [messages]);

  // Reset auto-scroll when streaming starts (scroll to bottom to see new content)
  useEffect(() => {
    if (isStreaming) {
      shouldAutoScrollRef.current = true;
      autoScroll();
    }
  }, [isStreaming]);

  if (!currentThreadId) {
    return (
      <div className="flex-1 flex items-center justify-center" style={{ color: "var(--muted)" }}>
        Select a thread to view messages
      </div>
    );
  }

  return (
    <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-auto p-4" data-testid="chat-panel">
      {isLoading ? (
        <div className="text-center" style={{ color: "var(--muted)" }}>Loading messages...</div>
      ) : messages.length === 0 ? (
        <div className="text-center" style={{ color: "var(--muted)" }}>
          No messages yet. Start a conversation!
        </div>
      ) : (
        <>
          {messages.map((msg, idx) => (
            <MessageBlock key={msg.id || idx} message={msg} showBorders={showBorders} />
          ))}

          {/* Streaming content */}
          {isStreaming && (
            <div
              className={`rounded p-3 mb-3 ${showBorders ? 'border' : ''}`}
              style={{ background: "var(--assistant-msg-bg)", borderColor: "var(--assistant-msg-border)", color: "var(--assistant-msg-text, var(--foreground))" }}
            >
              <div className="text-xs mb-2" style={{ color: "var(--muted)" }}>
                <span className="font-medium" style={{ color: "var(--assistant-msg-text, var(--foreground))" }}>Assistant</span>
                <span className="ml-2 animate-pulse" style={{ color: "var(--accent)" }}>streaming...</span>
              </div>

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
                            <pre className="text-xs p-2 rounded overflow-auto max-h-64 whitespace-pre-wrap break-all" style={{ background: "var(--code-bg)", color: "var(--foreground)" }}>
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
