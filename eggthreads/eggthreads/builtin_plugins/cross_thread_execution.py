from __future__ import annotations

"""Execute an enabled tool with a strict descendant's thread context."""

import asyncio
from dataclasses import dataclass, replace
from typing import Any, Dict

from ..plugins import PluginContext
from ..tools import ToolContext, ToolExecutionResult, ToolRegistry, resolve_tool_timeout_arg


TOOL_NAME = "execute_tool_in_other_thread"

# Context identity and registry control are supplied by Egg, never by the model
# through the nested argument object.  Some legacy non-context-aware tools still
# understand the public-looking aliases, so reject those too rather than relying
# on each implementation to get precedence right.
_RESERVED_NESTED_ARGUMENTS = {
    "_thread_id",
    "_initial_model_key",
    "initial_model_key",
    "_cancel_check",
    "_tool_timeout_sec",
    "_egg_tool_timeout_sec",
    "_tool_call_id",
    "_msg_id",
}

_THREAD_IDENTITY_ARGUMENTS = {"parent_thread_id", "manager_thread_id"}

# These tools depend on the currently executing tool call being represented in
# the contextual thread's own TC lifecycle.  A direct registry dispatch cannot
# truthfully manufacture that authority in a descendant, and recursive wrapper
# dispatch would be both surprising and unbounded.
_UNSUPPORTED_TOOLS = {
    TOOL_NAME,
    "get_user_message_while_preserving_llm_turn",
    "extract_tool_output",
}

# These tools produce validated Egg content parts in a JSON envelope.  Preserve
# the underlying name so the outer runner can render those parts while keeping
# the protocol-visible tool call/result paired to this wrapper.
_STRUCTURED_CONTENT_TOOLS = {
    "generate_image",
    "add_local_file_to_model_context",
    "add_provider_artifact_to_model_context",
}


def _error(message: str, *, reason: str = "error") -> ToolExecutionResult:
    return ToolExecutionResult(f"Error: {message}", reason=reason)


def _thread_db(ctx: ToolContext):
    """Return a DB connection safe for the current execution thread."""

    from .execution import _thread_db as resolve_thread_db

    return resolve_thread_db(ctx.db)


def _nested_arguments(value: Any) -> Dict[str, Any] | ToolExecutionResult:
    if not isinstance(value, dict):
        return _error("arguments must be an object.")
    arguments = dict(value)
    forbidden = sorted(key for key in arguments if key in _RESERVED_NESTED_ARGUMENTS)
    if forbidden:
        return _error(
            "arguments may not set reserved tool context field(s): " + ", ".join(forbidden) + "."
        )
    return arguments


def _bind_legacy_thread_identity(arguments: Dict[str, Any], target_thread_id: str) -> None:
    """Retarget non-context-aware built-ins without accepting caller redirection."""

    for key in sorted(_THREAD_IDENTITY_ARGUMENTS):
        if key in arguments:
            supplied = str(arguments.get(key) or "").strip()
            if supplied and supplied != target_thread_id:
                raise ValueError(f"arguments may not redirect {key} away from the target thread")
            arguments.pop(key, None)


async def execute_tool_in_other_thread_tool(
    args: Dict[str, Any],
    ctx: ToolContext,
) -> ToolExecutionResult:
    """Dispatch a tool directly with a strict descendant as ToolContext.thread_id."""

    caller_thread_id = str(ctx.thread_id or "").strip()
    target_thread_id = str(args.get("thread_id") or "").strip()
    requested_name = str(args.get("tool_name") or "").strip()
    if not caller_thread_id:
        return _error(f"{TOOL_NAME} requires a calling thread.")
    if not target_thread_id:
        return _error("thread_id is required.")
    if not requested_name:
        return _error("tool_name is required.")

    nested_args = _nested_arguments(args.get("arguments"))
    if isinstance(nested_args, ToolExecutionResult):
        return nested_args

    registry = ctx.raw.get("tool_registry")
    if not isinstance(registry, ToolRegistry):
        return _error("tool registry context is unavailable.")

    db = _thread_db(ctx)
    close_db = db is not ctx.db
    try:
        if db.get_thread(caller_thread_id) is None:
            return _error(f"calling thread not found: {caller_thread_id}.")
        if db.get_thread(target_thread_id) is None:
            return _error(f"target thread not found: {target_thread_id}.")

        from ..api import (
            _ensure_thread_working_directory,
            current_thread_model,
            is_descendant_thread,
        )

        if not is_descendant_thread(db, caller_thread_id, target_thread_id):
            return _error("target thread must be a strict descendant of the calling thread.", reason="denied")

        resolved_name = registry.resolve_name(requested_name)
        if not registry.is_registered(resolved_name):
            return _error(f"unknown tool: {requested_name}.", reason="unsupported")
        if registry.is_local_only(resolved_name):
            return _error(
                f"tool '{resolved_name}' is local-only and cannot be executed cross-thread.",
                reason="unsupported",
            )
        if resolved_name in _UNSUPPORTED_TOOLS:
            return _error(
                f"tool '{resolved_name}' cannot be executed cross-thread because it requires its own thread-local tool-call lifecycle.",
                reason="unsupported",
            )
        if not registry.capabilities(resolved_name).supports_cross_thread_execution:
            return _error(
                f"tool '{resolved_name}' has not opted in to cross-thread execution.",
                reason="unsupported",
            )

        from ..tools_config import get_thread_tools_config

        caller_tools = get_thread_tools_config(db, caller_thread_id)
        if caller_tools.policy_error:
            return _error(
                f"calling tool policy is unavailable; execution denied: {caller_tools.policy_error}",
                reason="policy_error",
            )
        if not caller_tools.llm_tools_enabled:
            return _error("LLM tools are disabled for the calling thread.", reason="disabled")
        if not caller_tools.is_tool_allowed(TOOL_NAME):
            return _error(f"tool '{TOOL_NAME}' is no longer allowed for the calling thread.", reason="disabled")
        if not caller_tools.is_tool_allowed(resolved_name):
            return _error(
                f"tool '{resolved_name}' is not allowed for the calling ancestor.",
                reason="disabled",
            )

        target_tools = get_thread_tools_config(db, target_thread_id)
        if target_tools.policy_error:
            return _error(
                f"target tool policy is unavailable; execution denied: {target_tools.policy_error}",
                reason="policy_error",
            )
        if not target_tools.llm_tools_enabled:
            return _error("LLM tools are disabled for the target thread.", reason="disabled")
        if not target_tools.is_tool_allowed(resolved_name):
            return _error(f"tool '{resolved_name}' is not allowed for the target thread.", reason="disabled")

        _bind_legacy_thread_identity(nested_args, target_thread_id)

        target_model = current_thread_model(db, target_thread_id)
        target_working_dir = _ensure_thread_working_directory(db, target_thread_id)

        # The outer tool deadline is authoritative.  A nested explicit timeout
        # may narrow it, never widen it, because ToolRegistry composes its own
        # deadline and cancellation controller around this dispatch.
        outer_timeout = ctx.timeout_sec
        nested_timeout = resolve_tool_timeout_arg(nested_args)
        if nested_timeout is None:
            nested_timeout = outer_timeout
        elif outer_timeout is not None:
            nested_timeout = min(nested_timeout, outer_timeout)
        if nested_timeout is not None:
            nested_args["timeout"] = nested_timeout

        nested_result = await registry.execute_in_thread_context(
            resolved_name,
            nested_args,
            thread_id=target_thread_id,
            origin="ancestor_cross_thread",
            initial_model_key=target_model,
            tool_timeout_sec=nested_timeout,
            cancel_check=ctx.cancel_check,
            db=db,
            working_dir=target_working_dir,
            models_path=ctx.raw.get("models_path"),
            all_models_path=ctx.raw.get("all_models_path"),
            image_generation_models_path=ctx.raw.get("image_generation_models_path"),
            preserve_tool_result=True,
        )
        target_tools_after = get_thread_tools_config(db, target_thread_id)
        caller_tools_after = get_thread_tools_config(db, caller_thread_id)
        target_allows_raw_output = bool(
            not target_tools_after.policy_error
            and target_tools_after.allow_raw_tool_output
            and not caller_tools_after.policy_error
            and caller_tools_after.allow_raw_tool_output
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _error(f"cross-thread execution failed: {type(exc).__name__}: {exc}")
    finally:
        if close_db:
            try:
                db.conn.close()
            except Exception:
                pass

    if isinstance(nested_result, ToolExecutionResult):
        result = nested_result
    else:
        result = ToolExecutionResult(str(nested_result))

    # Descendant output may only reach the ancestor provider unmasked when both
    # effective policies allow raw output.  The caller-side decision is made by
    # the outer runner's normal sanitizer; record the stricter target decision.
    return replace(
        result,
        streamed=False,
        force_provider_output_masking=(
            result.force_provider_output_masking
            or not target_allows_raw_output
        ),
        transcript_content_tool_name=(
            result.transcript_content_tool_name
            or (resolved_name if resolved_name in _STRUCTURED_CONTENT_TOOLS else None)
        ),
    )


def register_cross_thread_execution_tool(registry: ToolRegistry) -> None:
    registry.register(
        name=TOOL_NAME,
        description=(
            "Execute an enabled tool using a strict descendant thread's context, while returning the result "
            "to the calling ancestor. The target must be a descendant and the selected tool must be enabled "
            "there. Context-bound tools such as python_repl use the descendant's persistent session and "
            "hydrated thread history."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Registered tool to execute in the descendant context, for example python_repl.",
                },
                "arguments": {
                    "type": "object",
                    "description": "Arguments passed to the selected tool.",
                },
                "thread_id": {
                    "type": "string",
                    "description": "Strict descendant thread whose context, policy, working directory, and sessions should be used.",
                },
            },
            "required": ["tool_name", "arguments", "thread_id"],
            "additionalProperties": False,
        },
        impl=execute_tool_in_other_thread_tool,
        accepts_context=True,
        capabilities={"supports_cancellation": True},
    )


@dataclass(frozen=True)
class CrossThreadExecutionPlugin:
    name: str = "cross_thread_execution"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.tool_registry is not None:
            register_cross_thread_execution_tool(context.tool_registry)


__all__ = [
    "CrossThreadExecutionPlugin",
    "TOOL_NAME",
    "execute_tool_in_other_thread_tool",
    "register_cross_thread_execution_tool",
]