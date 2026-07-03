from __future__ import annotations

"""Built-in output publication policies matching current behavior."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..output_policy import OUTPUT_CHANNELS, OutputPolicyRequest, OutputPublicationDecision
from ..plugins import PluginContext


@dataclass(frozen=True)
class DefaultOutputPolicy:
    """Apply terminal-safety plus current long-output artifact/preview defaults."""

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
                original_char_count=request.metadata.get("original_char_count") if isinstance(request.metadata, dict) else None,
                output_capped=bool(request.metadata.get("output_capped")) if isinstance(request.metadata, dict) else False,
            )
            reason = (
                f"Auto: output too long ({line_count} lines, {char_count} chars) — stored as artifact"
                if saved
                else f"Auto: output too long ({line_count} lines, {char_count} chars); artifact write failed, sending preview only"
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


@dataclass(frozen=True)
class NativeOptimizerOutputPolicy:
    """Optionally replace the default publication preview with an optimized one."""

    name: str = "native_output_optimizer"

    def decide(self, request: OutputPolicyRequest) -> OutputPublicationDecision:
        return self.decide_with_current(request, None)

    def decide_with_current(
        self,
        request: OutputPolicyRequest,
        current: OutputPublicationDecision | None,
    ) -> OutputPublicationDecision:
        from ..output_optimizer import OptimizeRequest, create_default_output_optimizer, output_optimizer_enabled

        if not output_optimizer_enabled(request.thread_config):
            return OutputPublicationDecision("abstain", "", reason="Native output optimizer disabled")

        try:
            output = request.output if isinstance(request.output, str) else str(request.output or "")
            metadata = self._optimizer_request_metadata(request, current)
            opt_request = OptimizeRequest(
                tool_name=request.tool_name,
                tool_args=request.tool_args,
                output=output,
                finished_reason=request.finished_reason,
                thread_id=request.thread_id,
                tool_call_id=request.tool_call_id,
                origin=request.origin,
                user_tool_call=request.user_tool_call,
                metadata=metadata,
            )
            min_size_chars = self._min_size_chars(request)
            optimizer = create_default_output_optimizer(
                min_size_chars=min_size_chars,
                min_confidence=0.5,
            )
            optimization = optimizer.optimize(opt_request)
        except Exception as exc:
            exception_metadata = self._exception_optimizer_metadata(request, exc)
            if current is None:
                return OutputPublicationDecision("abstain", "", reason=f"Native output optimizer failed: {type(exc).__name__}: {exc}")
            return self._current_with_optimizer_metadata(current, exception_metadata)
        if not optimization.optimized:
            if current is None:
                return OutputPublicationDecision(
                    "abstain",
                    "",
                    reason=optimization.reason,
                    channels={
                        "optimizer": self._optimizer_metadata(optimization, fallback=True),
                    },
                )
            return self._current_with_optimizer_metadata(
                current,
                self._optimizer_metadata(optimization, fallback=True),
            )

        published_preview = self._append_artifact_recovery_note(optimization.output, current)
        if current is not None and current.preview and len(published_preview) >= len(str(current.preview)):
            optimizer_metadata = dict(self._optimizer_metadata(optimization, fallback=True, published_output=published_preview))
            optimizer_metadata["published"] = False
            optimizer_metadata["fallback_reason"] = "not_smaller_than_current_preview"
            return self._current_with_optimizer_metadata(current, optimizer_metadata)

        base_channels = dict(current.channels) if current is not None and current.channels else {}
        channels = {
            **base_channels,
            "optimizer": self._optimizer_metadata(optimization, fallback=False, published_output=published_preview),
        }
        channels[OUTPUT_CHANNELS.ui_preview] = published_preview
        channels[OUTPUT_CHANNELS.llm_message] = published_preview
        decision = current.decision if current is not None else "whole"
        artifact_path = current.artifact_path if current is not None else ""
        reason = f"Native output optimizer: {optimization.reason}"
        if current is not None and current.reason:
            reason = f"{reason}; default={current.reason}"
        return OutputPublicationDecision(
            decision,
            published_preview,
            reason=reason,
            artifact_path=artifact_path,
            channels=channels,
        )

    @staticmethod
    def _current_with_optimizer_metadata(
        current: OutputPublicationDecision,
        optimizer_metadata: Mapping[str, Any],
    ) -> OutputPublicationDecision:
        return OutputPublicationDecision(
            current.decision,
            current.preview,
            reason=current.reason,
            artifact_path=current.artifact_path,
            channels={**dict(current.channels or {}), "optimizer": dict(optimizer_metadata)},
        )

    @staticmethod
    def _append_artifact_recovery_note(
        optimized_output: str,
        current: OutputPublicationDecision | None,
    ) -> str:
        note = NativeOptimizerOutputPolicy._artifact_recovery_note(current)
        if not note:
            return optimized_output
        return f"{optimized_output.rstrip()}\n\n{note}" if optimized_output else note

    @staticmethod
    def _artifact_recovery_note(current: OutputPublicationDecision | None) -> str:
        if current is None or not current.artifact_path:
            return ""
        preview = str(current.preview or "")
        for marker in ("\n\n[Preview only", "\n\n[Output truncated"):
            idx = preview.rfind(marker)
            if idx >= 0:
                return preview[idx + 2 :].strip()
        if preview.startswith("[Preview only") or preview.startswith("[Output truncated"):
            return preview.strip()
        artifact_id = Path(current.artifact_path).name
        return (
            "[Raw output stored as artifact. "
            f"Artifact id: {artifact_id}. "
            f"Read chunks with read_long_tool_output('{artifact_id}', chunk_number).]"
        )

    @staticmethod
    def _optimizer_request_metadata(
        request: OutputPolicyRequest,
        current: OutputPublicationDecision | None,
    ) -> Mapping[str, Any]:
        metadata: dict[str, Any] = {}
        metadata.update(dict(request.metadata or {}))
        metadata["tool_metadata"] = dict(request.tool_metadata or {})
        metadata["origin"] = request.origin
        metadata["user_tool_call"] = request.user_tool_call
        metadata["finished_reason"] = request.finished_reason
        metadata["line_count"] = len(str(request.output or "").splitlines())
        metadata["char_count"] = len(str(request.output or ""))
        if current is not None:
            metadata["default_decision"] = current.decision
            metadata["default_reason"] = current.reason
            metadata["default_artifact_path"] = current.artifact_path
        return metadata

    @staticmethod
    def _optimizer_metadata(
        optimization: Any,
        *,
        fallback: bool,
        published_output: str | None = None,
    ) -> Mapping[str, Any]:
        metadata = {
            "name": optimization.optimizer_name,
            "filter_name": optimization.filter_name,
            "optimized": bool(optimization.optimized),
            "fallback": bool(fallback),
            "raw_chars": optimization.raw_chars,
            "optimized_chars": optimization.optimized_chars,
            "savings_pct": optimization.savings_pct,
            "reason": optimization.reason,
            "confidence": optimization.confidence,
            "metadata": NativeOptimizerOutputPolicy._plain_metadata(optimization.metadata or {}),
        }
        if published_output is not None:
            raw_chars = int(optimization.raw_chars or 0)
            published_chars = len(published_output)
            metadata["published_chars"] = published_chars
            metadata["published_savings_pct"] = ((raw_chars - published_chars) / raw_chars * 100.0) if raw_chars else 0.0
        return metadata

    @staticmethod
    def _exception_optimizer_metadata(request: OutputPolicyRequest, exc: Exception) -> Mapping[str, Any]:
        raw_chars = len(str(request.output or ""))
        return {
            "name": "egg_native_output_optimizer",
            "filter_name": None,
            "optimized": False,
            "fallback": True,
            "raw_chars": raw_chars,
            "optimized_chars": raw_chars,
            "savings_pct": 0.0,
            "reason": "exception",
            "confidence": 0.0,
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "metadata": {},
        }

    @staticmethod
    def _plain_metadata(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): NativeOptimizerOutputPolicy._plain_metadata(val) for key, val in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [NativeOptimizerOutputPolicy._plain_metadata(item) for item in value]
        return value

    @staticmethod
    def _min_size_chars(request: OutputPolicyRequest) -> int:
        for key in ("optimizer_min_size_chars", "native_output_optimizer_min_size_chars"):
            value = request.thread_config.get(key) if request.thread_config else None
            try:
                if value is not None:
                    return max(0, int(value))
            except (TypeError, ValueError):
                pass
        limits = request.limits or {}
        value = limits.get("optimizer_min_size_chars")
        try:
            return max(0, int(value)) if value is not None else 0
        except (TypeError, ValueError):
            return 0


def register_output_policies(registry: Any) -> None:
    registry.register(DefaultOutputPolicy())
    registry.register(NativeOptimizerOutputPolicy())


@dataclass(frozen=True)
class OutputPoliciesPlugin:
    name: str = "output_policies"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.output_policy_registry is not None:
            register_output_policies(context.output_policy_registry)


__all__ = ["DefaultOutputPolicy", "NativeOptimizerOutputPolicy", "OutputPoliciesPlugin", "register_output_policies"]
