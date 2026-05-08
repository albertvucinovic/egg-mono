from __future__ import annotations

"""Built-in output publication policies matching current behavior."""

from dataclasses import dataclass
from typing import Any

from ..output_policy import OUTPUT_CHANNELS, OutputPolicyRequest, OutputPublicationDecision
from ..plugins import PluginContext


@dataclass(frozen=True)
class DefaultOutputPolicy:
    """Apply terminal-safety plus current long-output stash/preview defaults."""

    name: str = "default_output"

    def decide(self, request: OutputPolicyRequest) -> OutputPublicationDecision:
        from ..runner import LONG_OUTPUT_CHAR_THRESHOLD, LONG_OUTPUT_LINE_THRESHOLD, stash_tool_output_and_build_preview
        from ..terminal_safety import sanitize_terminal_text

        output = request.output if isinstance(request.output, str) else str(request.output or "")
        safe_output = sanitize_terminal_text(output)
        line_count = len(safe_output.splitlines())
        char_count = len(safe_output)
        channels = {
            OUTPUT_CHANNELS.raw: {"stored_in_finished_event": True},
            OUTPUT_CHANNELS.audit: {"line_count": line_count, "char_count": char_count},
        }
        is_long = line_count > LONG_OUTPUT_LINE_THRESHOLD or char_count > LONG_OUTPUT_CHAR_THRESHOLD
        if is_long:
            preview, saved = stash_tool_output_and_build_preview(
                request.db,
                request.thread_id,
                request.tool_call_id,
                safe_output,
            )
            reason = (
                f"Auto: output too long ({line_count} lines, {char_count} chars) — stashed to {saved}"
                if saved
                else f"Auto: output too long ({line_count} lines, {char_count} chars); stash failed, sending preview only"
            )
            return OutputPublicationDecision(
                "partial",
                preview,
                reason=reason,
                artifact_path=saved,
                channels={**channels, OUTPUT_CHANNELS.artifact: saved, OUTPUT_CHANNELS.ui_preview: preview, OUTPUT_CHANNELS.llm_message: preview},
            )
        return OutputPublicationDecision(
            "whole",
            safe_output,
            reason="Auto: output below size thresholds",
            channels={**channels, OUTPUT_CHANNELS.ui_preview: safe_output, OUTPUT_CHANNELS.llm_message: safe_output},
        )


def register_output_policies(registry: Any) -> None:
    registry.register(DefaultOutputPolicy())


@dataclass(frozen=True)
class OutputPoliciesPlugin:
    name: str = "output_policies"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.output_policy_registry is not None:
            register_output_policies(context.output_policy_registry)


__all__ = ["DefaultOutputPolicy", "OutputPoliciesPlugin", "register_output_policies"]
