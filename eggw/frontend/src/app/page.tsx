"use client";

import { useState, useEffect, useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ThreadTree } from "@/components/ThreadTree";
import { ChatPanel } from "@/components/ChatPanel";
import { MessageInput } from "@/components/MessageInput";
import { SystemPanel } from "@/components/SystemPanel";
import { ApprovalPanel } from "@/components/ApprovalPanel";
import { useAppStore } from "@/lib/store";
import { useSSE } from "@/hooks/useSSE";
import { createThread, openThread } from "@/lib/api";

export default function Home() {
  const queryClient = useQueryClient();
  const { currentThreadId, setCurrentThreadId, addSystemLog } = useAppStore();
  const [leftPanelWidth, setLeftPanelWidth] = useState(280);
  const [showHelp, setShowHelp] = useState(false);

  // Connect to SSE for real-time streaming
  useSSE(currentThreadId);

  // Keyboard shortcuts
  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    // Don't trigger shortcuts when typing in input fields
    const target = e.target as HTMLElement;
    if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT") {
      // Allow Escape to blur
      if (e.key === "Escape") {
        target.blur();
      }
      return;
    }

    // Ctrl/Cmd + N - New thread
    if ((e.ctrlKey || e.metaKey) && e.key === "n") {
      e.preventDefault();
      createThread({}).then((thread) => {
        queryClient.invalidateQueries({ queryKey: ["rootThreads"] });
        setCurrentThreadId(thread.id);
        openThread(thread.id);
        addSystemLog(`Created thread ${thread.id.slice(-8)}`, "success");
      });
    }

    // / - Focus input with slash
    if (e.key === "/" && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      const input = document.querySelector("textarea") as HTMLTextAreaElement;
      if (input) {
        input.focus();
        input.value = "/";
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }

    // ? - Show help
    if (e.key === "?" && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      setShowHelp(!showHelp);
    }

    // i - Focus input
    if (e.key === "i" && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      const input = document.querySelector("textarea") as HTMLTextAreaElement;
      if (input) input.focus();
    }
  }, [queryClient, setCurrentThreadId, addSystemLog, showHelp]);

  useEffect(() => {
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  return (
    <main className="h-screen flex flex-col">
      {/* Help Modal */}
      {showHelp && (
        <div
          className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
          onClick={() => setShowHelp(false)}
        >
          <div
            className="bg-[#1a1a1a] border border-[var(--panel-border)] rounded-lg p-6 max-w-md"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="text-lg font-semibold mb-4">Keyboard Shortcuts</h2>
            <div className="space-y-2 text-sm">
              <div className="flex justify-between">
                <span className="text-gray-400">New thread</span>
                <kbd className="px-2 py-0.5 bg-[#333] rounded text-xs">Ctrl+N</kbd>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-400">Focus input</span>
                <kbd className="px-2 py-0.5 bg-[#333] rounded text-xs">i</kbd>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-400">Start command</span>
                <kbd className="px-2 py-0.5 bg-[#333] rounded text-xs">/</kbd>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-400">Show this help</span>
                <kbd className="px-2 py-0.5 bg-[#333] rounded text-xs">?</kbd>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-400">Unfocus input</span>
                <kbd className="px-2 py-0.5 bg-[#333] rounded text-xs">Esc</kbd>
              </div>
            </div>
            <div className="mt-4 pt-4 border-t border-[var(--panel-border)] text-sm text-gray-400">
              <p className="font-medium text-gray-300 mb-2">Commands:</p>
              <p>/model [name] - Change model</p>
              <p>/spawn &lt;context&gt; - Spawn child thread</p>
              <p>/newThread - Create new thread</p>
              <p>/help - Show command help</p>
              <p>$ cmd - Run shell command</p>
              <p>$$ cmd - Run hidden shell command</p>
            </div>
            <button
              onClick={() => setShowHelp(false)}
              className="mt-4 w-full py-2 bg-blue-600 hover:bg-blue-500 rounded"
            >
              Close
            </button>
          </div>
        </div>
      )}

      {/* Header */}
      <header className="h-12 border-b border-[var(--panel-border)] flex items-center px-4 bg-[var(--panel-bg)]">
        <h1 className="text-lg font-semibold">eggw</h1>
        <span className="ml-4 text-sm text-gray-400">
          {currentThreadId ? `Thread: ${currentThreadId.slice(-8)}` : "No thread selected"}
        </span>
        <div className="ml-auto">
          <button
            onClick={() => setShowHelp(true)}
            className="text-xs text-gray-500 hover:text-gray-300 px-2 py-1"
          >
            Press ? for help
          </button>
        </div>
      </header>

      {/* Main content */}
      <div className="flex-1 flex overflow-hidden">
        {/* Left sidebar - Thread tree */}
        <div
          className="border-r border-[var(--panel-border)] overflow-auto"
          style={{ width: leftPanelWidth }}
        >
          <ThreadTree />
        </div>

        {/* Resize handle */}
        <div
          className="w-1 cursor-col-resize hover:bg-blue-500 transition-colors"
          onMouseDown={(e) => {
            const startX = e.clientX;
            const startWidth = leftPanelWidth;

            const onMouseMove = (e: MouseEvent) => {
              const newWidth = startWidth + (e.clientX - startX);
              setLeftPanelWidth(Math.max(200, Math.min(500, newWidth)));
            };

            const onMouseUp = () => {
              document.removeEventListener("mousemove", onMouseMove);
              document.removeEventListener("mouseup", onMouseUp);
            };

            document.addEventListener("mousemove", onMouseMove);
            document.addEventListener("mouseup", onMouseUp);
          }}
        />

        {/* Center - Chat */}
        <div className="flex-1 flex flex-col overflow-hidden">
          <ChatPanel />
          <ApprovalPanel />
          <MessageInput />
        </div>

        {/* Right sidebar - System log */}
        <div className="w-80 border-l border-[var(--panel-border)] overflow-auto">
          <SystemPanel />
        </div>
      </div>
    </main>
  );
}
