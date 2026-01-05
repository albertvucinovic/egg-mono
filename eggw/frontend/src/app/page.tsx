"use client";

import { useState } from "react";
import { ThreadTree } from "@/components/ThreadTree";
import { ChatPanel } from "@/components/ChatPanel";
import { MessageInput } from "@/components/MessageInput";
import { SystemPanel } from "@/components/SystemPanel";
import { ApprovalPanel } from "@/components/ApprovalPanel";
import { useAppStore } from "@/lib/store";
import { useSSE } from "@/hooks/useSSE";

export default function Home() {
  const { currentThreadId } = useAppStore();
  const [leftPanelWidth, setLeftPanelWidth] = useState(280);

  // Connect to SSE for real-time streaming
  useSSE(currentThreadId);

  return (
    <main className="h-screen flex flex-col">
      {/* Header */}
      <header className="h-12 border-b border-[var(--panel-border)] flex items-center px-4 bg-[var(--panel-bg)]">
        <h1 className="text-lg font-semibold">eggw</h1>
        <span className="ml-4 text-sm text-gray-400">
          {currentThreadId ? `Thread: ${currentThreadId.slice(-8)}` : "No thread selected"}
        </span>
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
