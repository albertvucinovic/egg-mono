"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, X, AlertTriangle, CheckCheck, FileText } from "lucide-react";
import { fetchToolCalls, approveTool } from "@/lib/api";
import { useAppStore, ToolCall } from "@/lib/store";
import { createClientOperationId } from "@/lib/messageOperations";
import { Button, StatusChip } from "@/components/ui/primitives";
import clsx from "clsx";

interface ApprovalPanelProps {
  threadId: string;
  showBorders?: boolean;
}

export function ApprovalPanel({ threadId, showBorders = true }: ApprovalPanelProps) {
  const queryClient = useQueryClient();
  const addSystemLog = useAppStore((state) => state.addSystemLog);

  // Tool calls are updated via SSE events - no polling needed
  // SSE uses db.path to ensure cross-process events (e.g., TUI approvals) are seen
  const { data: toolCalls } = useQuery({
    queryKey: ["toolCalls", threadId],
    queryFn: () => fetchToolCalls(threadId),
    enabled: !!threadId,
  });

  const approveMutation = useMutation({
    mutationFn: ({
      threadId: sourceThreadId,
      toolCallId,
      approved,
      outputDecision,
      decision,
    }: {
      threadId: string;
      operationId: string;
      toolCallId: string;
      approved: boolean;
      outputDecision?: string;
      decision?: string;
    }) => approveTool(sourceThreadId, toolCallId, approved, outputDecision, decision),
    // Optimistic update - remove from list immediately on click
    onMutate: async ({ threadId: sourceThreadId, toolCallId }) => {
      await queryClient.cancelQueries({ queryKey: ["toolCalls", sourceThreadId] });
      const previous = queryClient.getQueryData(["toolCalls", sourceThreadId]);
      queryClient.setQueryData(["toolCalls", sourceThreadId], (old: ToolCall[] | undefined) =>
        old?.filter((tc) => tc.id !== toolCallId) ?? []
      );
      return { previous, threadId: sourceThreadId };
    },
    onSuccess: (_response, variables) => {
      queryClient.invalidateQueries({ queryKey: ["toolCalls", variables.threadId] });
      queryClient.invalidateQueries({ queryKey: ["threadState", variables.threadId] });
      addSystemLog("Tool approval updated", "success");
    },
    onError: (_err, _vars, context) => {
      if (context?.previous) {
        queryClient.setQueryData(["toolCalls", context.threadId], context.previous);
      }
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
      className={clsx("eggw-approval-panel", showBorders && "eggw-approval-panel-bordered")}
    >
      <div className="eggw-approval-heading">
        <AlertTriangle className="w-5 h-5" />
        <span className="font-medium">Pending Approvals</span>
      </div>

      <div className="space-y-3">
        {pendingTools.map((tc) => (
          <div
            key={tc.id}
            className={clsx("eggw-approval-card", !showBorders && "eggw-approval-card-borderless")}
          >
            <div className="eggw-approval-card-header">
              <div className="flex items-center gap-2">
                <span className="eggw-approval-tool-name">{tc.name}</span>
                <span className="eggw-ui-muted text-xs font-mono">
                  {tc.id.slice(-8)}
                </span>
                <StatusChip tone={tc.state === "TC1" ? "warning" : "special"}>
                  {tc.state === "TC1" ? "Exec Approval" : "Output Approval"}
                </StatusChip>
              </div>
            </div>

            {tc.summary && (
              <div className="eggw-approval-summary" role="status">
                {tc.summary}
              </div>
            )}

            {/* Arguments preview */}
            <pre className="eggw-code-block mb-3 max-h-32">
              {typeof tc.arguments === "string"
                ? tc.arguments.slice(0, 500)
                : JSON.stringify(tc.arguments, null, 2).slice(0, 500)}
            </pre>

            {/* Output preview for TC4 */}
            {tc.state === "TC4" && tc.output && (
              <details className="eggw-detail-block eggw-role-tool mb-3">
                <summary className="eggw-detail-summary">
                  View Output ({tc.output.length} chars)
                </summary>
                <pre className="eggw-code-block max-h-40">
                  {tc.output.slice(0, 2000)}
                  {tc.output.length > 2000 && "\n... (truncated)"}
                </pre>
              </details>
            )}

            {/* Approval buttons */}
            <div className="flex gap-2 flex-wrap">
              {tc.state === "TC1" ? (
                <>
                  <Button
                    onClick={() =>
                      approveMutation.mutate({ threadId, operationId: createClientOperationId("approval"), toolCallId: tc.id, approved: true })
                    }
                    variant="primary"
                    title="Approve this tool call (y)"
                  >
                    <Check className="w-4 h-4" /> Approve
                  </Button>
                  <Button
                    onClick={() =>
                      approveMutation.mutate({ threadId, operationId: createClientOperationId("approval"), toolCallId: tc.id, approved: false })
                    }
                    variant="danger"
                    title="Deny this tool call (n)"
                  >
                    <X className="w-4 h-4" /> Deny
                  </Button>
                  <Button
                    onClick={() =>
                      approveMutation.mutate({
                        threadId,
                        operationId: createClientOperationId("approval"),
                        toolCallId: tc.id,
                        approved: true,
                        decision: "all-in-turn",
                      })
                    }
                    variant="secondary"
                    title="Approve all tool calls in this turn (a)"
                  >
                    <CheckCheck className="w-4 h-4" /> Approve All
                  </Button>
                </>
              ) : (
                <>
                  <Button
                    onClick={() =>
                      approveMutation.mutate({
                        threadId,
                        operationId: createClientOperationId("approval"),
                        toolCallId: tc.id,
                        approved: true,
                        outputDecision: "whole",
                      })
                    }
                    variant="primary"
                    title="Include full output (y)"
                  >
                    <Check className="w-4 h-4" /> Whole
                  </Button>
                  <Button
                    onClick={() =>
                      approveMutation.mutate({
                        threadId,
                        operationId: createClientOperationId("approval"),
                        toolCallId: tc.id,
                        approved: true,
                        outputDecision: "partial",
                      })
                    }
                    variant="warning"
                    title="Include shortened preview (n)"
                  >
                    <FileText className="w-4 h-4" /> Partial
                  </Button>
                  <Button
                    onClick={() =>
                      approveMutation.mutate({
                        threadId,
                        operationId: createClientOperationId("approval"),
                        toolCallId: tc.id,
                        approved: false,
                        outputDecision: "omit",
                      })
                    }
                    variant="danger"
                    title="Omit output entirely (o)"
                  >
                    <X className="w-4 h-4" /> Omit
                  </Button>
                </>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
