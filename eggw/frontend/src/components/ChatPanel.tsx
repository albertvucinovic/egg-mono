"use client";

import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { fetchMessages } from "@/lib/api";
import { useAppStore, Message } from "@/lib/store";
import clsx from "clsx";

interface MessageBlockProps {
  message: Message;
}

function MessageBlock({ message }: MessageBlockProps) {
  const roleColors: Record<string, string> = {
    user: "bg-blue-900/50 border-blue-700",
    assistant: "bg-slate-800 border-slate-600",
    system: "bg-gray-800 border-gray-600",
    tool: "bg-amber-900/30 border-amber-700",
  };

  const roleLabels: Record<string, string> = {
    user: "User",
    assistant: "Assistant",
    system: "System",
    tool: "Tool",
  };

  return (
    <div
      className={clsx(
        "rounded border p-3 mb-3",
        roleColors[message.role] || "bg-gray-800 border-gray-600"
      )}
    >
      {/* Header */}
      <div className="flex items-center gap-2 mb-2 text-xs text-gray-400">
        <span className="font-medium text-gray-300">
          {roleLabels[message.role] || message.role}
        </span>
        {message.model_key && (
          <span className="text-gray-500">({message.model_key})</span>
        )}
        {message.tool_call_id && (
          <span className="text-amber-500 font-mono">
            {message.tool_call_id.slice(-8)}
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
        <div className="prose prose-invert prose-sm max-w-none">
          <ReactMarkdown
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
            {message.content}
          </ReactMarkdown>
        </div>
      )}

      {/* Tool calls */}
      {message.tool_calls && message.tool_calls.length > 0 && (
        <div className="mt-2 space-y-2">
          {message.tool_calls.map((tc: any, idx: number) => (
            <div
              key={tc.id || idx}
              className="bg-amber-900/20 rounded p-2 border border-amber-800"
            >
              <div className="flex items-center gap-2 text-sm">
                <span className="text-amber-400 font-medium">{tc.name}</span>
                <span className="text-xs text-gray-500 font-mono">
                  {tc.id?.slice(-8)}
                </span>
              </div>
              <pre className="mt-1 text-xs text-gray-300 overflow-auto">
                {typeof tc.arguments === "string"
                  ? tc.arguments
                  : JSON.stringify(tc.arguments, null, 2)}
              </pre>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function ChatPanel() {
  const scrollRef = useRef<HTMLDivElement>(null);
  const { currentThreadId, messages, setMessages, streamingContent } =
    useAppStore();

  const { data, isLoading } = useQuery({
    queryKey: ["messages", currentThreadId],
    queryFn: () => fetchMessages(currentThreadId!),
    enabled: !!currentThreadId,
  });

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
  }, [messages, streamingContent]);

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
          {streamingContent && (
            <div className="rounded border p-3 mb-3 bg-slate-800 border-slate-600 animate-pulse">
              <div className="text-xs text-gray-400 mb-2">
                <span className="font-medium text-gray-300">Assistant</span>
                <span className="ml-2 text-blue-400">streaming...</span>
              </div>
              <div className="prose prose-invert prose-sm max-w-none">
                <ReactMarkdown>{streamingContent}</ReactMarkdown>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
