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
    <div className="border-t border-yellow-700 bg-yellow-900/20 p-4">
      <div className="flex items-center gap-2 mb-3 text-yellow-400">
        <AlertTriangle className="w-5 h-5" />
        <span className="font-medium">Pending Approvals</span>
      </div>

      <div className="space-y-3">
        {pendingTools.map((tc) => (
          <div
            key={tc.id}
            className="bg-[#1a1a1a] border border-yellow-800 rounded p-3"
          >
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2">
                <span className="font-medium text-yellow-300">{tc.name}</span>
                <span className="text-xs text-gray-500 font-mono">
                  {tc.id.slice(-8)}
                </span>
                <span
                  className={clsx(
                    "text-xs px-2 py-0.5 rounded",
                    tc.state === "TC1" && "bg-yellow-800 text-yellow-200",
                    tc.state === "TC4" && "bg-purple-800 text-purple-200"
                  )}
                >
                  {tc.state === "TC1" ? "Exec Approval" : "Output Approval"}
                </span>
              </div>
            </div>

            {/* Arguments preview */}
            <pre className="text-xs text-gray-400 mb-3 max-h-32 overflow-auto bg-black/30 p-2 rounded">
              {typeof tc.arguments === "string"
                ? tc.arguments.slice(0, 500)
                : JSON.stringify(tc.arguments, null, 2).slice(0, 500)}
            </pre>

            {/* Output preview for TC4 */}
            {tc.state === "TC4" && tc.output && (
              <details className="mb-3">
                <summary className="cursor-pointer text-sm text-purple-300">
                  View Output ({tc.output.length} chars)
                </summary>
                <pre className="mt-2 text-xs text-gray-300 max-h-40 overflow-auto bg-black/30 p-2 rounded">
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
                    className="flex items-center gap-1 px-3 py-1 bg-green-700 hover:bg-green-600 rounded text-sm"
                  >
                    <Check className="w-4 h-4" /> Approve
                  </button>
                  <button
                    onClick={() =>
                      approveMutation.mutate({ toolCallId: tc.id, approved: false })
                    }
                    className="flex items-center gap-1 px-3 py-1 bg-red-700 hover:bg-red-600 rounded text-sm"
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
                    className="flex items-center gap-1 px-3 py-1 bg-green-700 hover:bg-green-600 rounded text-sm"
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
                    className="flex items-center gap-1 px-3 py-1 bg-red-700 hover:bg-red-600 rounded text-sm"
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
