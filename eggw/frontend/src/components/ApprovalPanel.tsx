"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, X, AlertTriangle } from "lucide-react";
import { fetchToolCalls, approveTool } from "@/lib/api";
import { useAppStore, ToolCall } from "@/lib/store";
import clsx from "clsx";

export function ApprovalPanel() {
  const queryClient = useQueryClient();
  const { currentThreadId, addSystemLog } = useAppStore();

  const { data: toolCalls } = useQuery({
    queryKey: ["toolCalls", currentThreadId],
    queryFn: () => fetchToolCalls(currentThreadId!),
    enabled: !!currentThreadId,
    refetchInterval: 2000, // Poll for updates
  });

  const approveMutation = useMutation({
    mutationFn: ({
      toolCallId,
      approved,
      outputDecision,
    }: {
      toolCallId: string;
      approved: boolean;
      outputDecision?: string;
    }) => approveTool(currentThreadId!, toolCallId, approved, outputDecision),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["toolCalls", currentThreadId] });
      // Also invalidate messages since tool will execute and produce output
      queryClient.invalidateQueries({ queryKey: ["messages", currentThreadId] });
      addSystemLog("Tool approval updated", "success");
    },
    onError: () => {
      addSystemLog("Failed to approve tool", "error");
    },
  });

  // Filter for pending tools (TC1 = exec approval, TC4 = output approval)
  const pendingTools: ToolCall[] = (toolCalls || []).filter(
    (tc: ToolCall) => tc.state === "TC1" || tc.state === "TC4"
  );

  if (pendingTools.length === 0) {
    return null;
  }

  return (
    <div
      className="border-t p-4"
      style={{ borderColor: "var(--tool-call-border)", background: "var(--tool-call-bg)" }}
    >
      <div className="flex items-center gap-2 mb-3" style={{ color: "var(--tool-call-border)" }}>
        <AlertTriangle className="w-5 h-5" />
        <span className="font-medium">Pending Approvals</span>
      </div>

      <div className="space-y-3">
        {pendingTools.map((tc) => (
          <div
            key={tc.id}
            className="border rounded p-3"
            style={{ background: "var(--panel-bg)", borderColor: "var(--tool-call-border)" }}
          >
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2">
                <span className="font-medium" style={{ color: "var(--tool-call-border)" }}>{tc.name}</span>
                <span className="text-xs font-mono" style={{ color: "var(--muted)" }}>
                  {tc.id.slice(-8)}
                </span>
                <span
                  className="text-xs px-2 py-0.5 rounded border"
                  style={{
                    borderColor: tc.state === "TC1" ? "var(--tool-call-border)" : "var(--reasoning-border)",
                    color: tc.state === "TC1" ? "var(--tool-call-border)" : "var(--reasoning-border)",
                  }}
                >
                  {tc.state === "TC1" ? "Exec Approval" : "Output Approval"}
                </span>
              </div>
            </div>

            {/* Arguments preview */}
            <pre
              className="text-xs mb-3 max-h-32 overflow-auto p-2 rounded"
              style={{ background: "var(--code-bg)", color: "var(--foreground)" }}
            >
              {typeof tc.arguments === "string"
                ? tc.arguments.slice(0, 500)
                : JSON.stringify(tc.arguments, null, 2).slice(0, 500)}
            </pre>

            {/* Output preview for TC4 */}
            {tc.state === "TC4" && tc.output && (
              <details className="mb-3">
                <summary
                  className="cursor-pointer text-sm"
                  style={{ color: "var(--reasoning-border)" }}
                >
                  View Output ({tc.output.length} chars)
                </summary>
                <pre
                  className="mt-2 text-xs max-h-40 overflow-auto p-2 rounded"
                  style={{ background: "var(--code-bg)", color: "var(--foreground)" }}
                >
                  {tc.output.slice(0, 2000)}
                  {tc.output.length > 2000 && "\n... (truncated)"}
                </pre>
              </details>
            )}

            {/* Approval buttons */}
            <div className="flex gap-2">
              {tc.state === "TC1" ? (
                <>
                  <button
                    onClick={() =>
                      approveMutation.mutate({ toolCallId: tc.id, approved: true })
                    }
                    className="flex items-center gap-1 px-3 py-1 rounded text-sm border font-medium"
                    style={{ borderColor: "var(--tool-msg-border)", color: "var(--tool-msg-border)" }}
                  >
                    <Check className="w-4 h-4" /> Approve
                  </button>
                  <button
                    onClick={() =>
                      approveMutation.mutate({ toolCallId: tc.id, approved: false })
                    }
                    className="flex items-center gap-1 px-3 py-1 rounded text-sm border font-medium"
                    style={{ borderColor: "var(--user-msg-border)", color: "var(--user-msg-border)" }}
                  >
                    <X className="w-4 h-4" /> Deny
                  </button>
                </>
              ) : (
                <>
                  <button
                    onClick={() =>
                      approveMutation.mutate({
                        toolCallId: tc.id,
                        approved: true,
                        outputDecision: "whole",
                      })
                    }
                    className="flex items-center gap-1 px-3 py-1 rounded text-sm border font-medium"
                    style={{ borderColor: "var(--tool-msg-border)", color: "var(--tool-msg-border)" }}
                  >
                    <Check className="w-4 h-4" /> Include Output
                  </button>
                  <button
                    onClick={() =>
                      approveMutation.mutate({
                        toolCallId: tc.id,
                        approved: false,
                        outputDecision: "omit",
                      })
                    }
                    className="flex items-center gap-1 px-3 py-1 rounded text-sm border font-medium"
                    style={{ borderColor: "var(--user-msg-border)", color: "var(--user-msg-border)" }}
                  >
                    <X className="w-4 h-4" /> Omit Output
                  </button>
                </>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
