from __future__ import annotations

"""Built-in LLM-facing attachment/provider-artifact tools."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from ..attachment_tools import (
    attach_local_file_operation,
    attach_provider_output_operation,
    save_provider_artifact_operation,
)
from ..plugins import PluginContext
from ..tools import ToolContext, ToolExecutionResult, ToolRegistry


ADD_LOCAL_FILE_TO_MODEL_CONTEXT_TOOL_NAME = "add_local_file_to_model_context"
ADD_PROVIDER_ARTIFACT_TO_MODEL_CONTEXT_TOOL_NAME = "add_provider_artifact_to_model_context"
SAVE_PROVIDER_ARTIFACT_TO_FILE_TOOL_NAME = "save_provider_artifact_to_file"


def _error(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(f"Error: {message}", reason="error")


def _json_result(payload: Dict[str, Any]) -> ToolExecutionResult:
    return ToolExecutionResult(json.dumps(payload, ensure_ascii=False, sort_keys=True), reason="success")


def _workspace(ctx: ToolContext) -> Path | None:
    db_path = getattr(ctx.db, "path", None)
    if db_path is not None:
        try:
            resolved = Path(db_path).expanduser().resolve()
            if resolved.parent.name == ".egg":
                return resolved.parent.parent
        except Exception:
            pass
    if ctx.working_dir:
        return Path(ctx.working_dir).expanduser().resolve()
    return None


def _thread_context(ctx: ToolContext, tool_name: str) -> tuple[Any, str] | ToolExecutionResult:
    thread_id = str(ctx.thread_id or "").strip()
    if ctx.db is None or not thread_id:
        return _error(f"{tool_name} requires a current thread and database context.")
    return ctx.db, thread_id


def add_local_file_to_model_context_tool(args: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    """Ingest a sandbox-authorized local file as an Egg input attachment."""

    resolved = _thread_context(ctx, ADD_LOCAL_FILE_TO_MODEL_CONTEXT_TOOL_NAME)
    if isinstance(resolved, ToolExecutionResult):
        return resolved
    db, thread_id = resolved
    path = str(args.get("path") or "").strip()
    if not path:
        return _error("path is required.")
    try:
        result = attach_local_file_operation(db, thread_id, path, workspace=_workspace(ctx))
    except Exception as e:
        return _error(str(e))
    payload = result.public_payload()
    payload["message"] = result.message
    return _json_result(payload)


def add_provider_artifact_to_model_context_tool(args: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    """Promote a provider-output artifact to an input attachment."""

    resolved = _thread_context(ctx, ADD_PROVIDER_ARTIFACT_TO_MODEL_CONTEXT_TOOL_NAME)
    if isinstance(resolved, ToolExecutionResult):
        return resolved
    db, thread_id = resolved
    artifact_id = str(args.get("artifact_id") or "").strip()
    if not artifact_id:
        return _error("artifact_id is required.")
    descendant_thread_id = args.get("descendant_thread_id")
    try:
        result = attach_provider_output_operation(
            db,
            thread_id,
            artifact_id,
            descendant_thread_id=str(descendant_thread_id).strip() if descendant_thread_id else None,
            workspace=_workspace(ctx),
        )
    except Exception as e:
        return _error(str(e))
    payload = result.public_payload()
    payload["message"] = result.message
    payload["artifact_id"] = artifact_id
    return _json_result(payload)


def save_provider_artifact_to_file_tool(args: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    """Export a provider-output artifact to the thread working directory."""

    resolved = _thread_context(ctx, SAVE_PROVIDER_ARTIFACT_TO_FILE_TOOL_NAME)
    if isinstance(resolved, ToolExecutionResult):
        return resolved
    db, thread_id = resolved
    artifact_id = str(args.get("artifact_id") or "").strip()
    if not artifact_id:
        return _error("artifact_id is required.")
    path = args.get("path")
    descendant_thread_id = args.get("descendant_thread_id")
    try:
        result = save_provider_artifact_operation(
            db,
            thread_id,
            artifact_id,
            path if path is not None else None,
            descendant_thread_id=str(descendant_thread_id).strip() if descendant_thread_id else None,
            workspace=_workspace(ctx),
        )
    except Exception as e:
        return _error(str(e))
    payload = result.public_payload()
    payload["message"] = result.message
    return _json_result(payload)


def register_attachment_tools(registry: ToolRegistry) -> None:
    registry.register(
        name=ADD_LOCAL_FILE_TO_MODEL_CONTEXT_TOOL_NAME,
        description=(
            "Add a local file to the current thread's model context. "
            "The path is authorized through the thread's effective sandbox/filesystem read policy, "
            "then bytes are copied into .egg/egg_inputs. If the file is an image, the next image-capable "
            "model call receives it as visual input; other files/documents are sent through the appropriate "
            "provider attachment mechanism when supported. The result contains metadata and attachment content parts, "
            "never inline bytes or base64."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Local file path to add to model context. Relative paths are resolved against the current thread working directory.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        impl=add_local_file_to_model_context_tool,
        accepts_context=True,
        capabilities={"supports_cross_thread_execution": True},
    )
    registry.register(
        name=ADD_PROVIDER_ARTIFACT_TO_MODEL_CONTEXT_TOOL_NAME,
        description=(
            "Add an accessible provider-output artifact to the current thread's model context. "
            "This is the LLM-facing equivalent of /attachOutput: it copies authorized provider-output bytes into "
            ".egg/egg_inputs and returns reusable attachment content parts. If the artifact is an image, the next "
            "image-capable model call receives it as visual input; other files/documents are sent through the "
            "appropriate provider attachment mechanism when supported."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "Provider-output artifact id to add to model context, for example one returned by generate_image.",
                },
                "descendant_thread_id": {
                    "type": "string",
                    "description": "Optional explicit descendant thread id whose provider-output namespace to read. Only ancestors may use this selector.",
                },
            },
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
        impl=add_provider_artifact_to_model_context_tool,
        accepts_context=True,
        capabilities={"supports_cross_thread_execution": True},
    )
    registry.register(
        name=SAVE_PROVIDER_ARTIFACT_TO_FILE_TOOL_NAME,
        description=(
            "Save an accessible provider-output artifact to a user-visible file under the current thread working directory. "
            "This is the LLM-facing equivalent of /saveProviderArtifact and honors provider-output access checks, "
            "sandbox/filesystem write policy, .egg protection, and no-overwrite safety. This saves a file for the "
            "user/project; it does not add the artifact to model context."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "Provider-output artifact id to save to a file.",
                },
                "path": {
                    "type": "string",
                    "description": "Optional output file path or directory under the current thread working directory. Omit to use the artifact filename.",
                },
                "descendant_thread_id": {
                    "type": "string",
                    "description": "Optional explicit descendant thread id whose provider-output namespace to read. Only ancestors may use this selector.",
                },
            },
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
        impl=save_provider_artifact_to_file_tool,
        accepts_context=True,
        capabilities={"supports_cross_thread_execution": True},
    )


@dataclass(frozen=True)
class AttachmentToolsPlugin:
    name: str = "attachment_tools"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.tool_registry is not None:
            register_attachment_tools(context.tool_registry)


__all__ = [
    "ADD_PROVIDER_ARTIFACT_TO_MODEL_CONTEXT_TOOL_NAME",
    "ADD_LOCAL_FILE_TO_MODEL_CONTEXT_TOOL_NAME",
    "SAVE_PROVIDER_ARTIFACT_TO_FILE_TOOL_NAME",
    "AttachmentToolsPlugin",
    "add_provider_artifact_to_model_context_tool",
    "add_local_file_to_model_context_tool",
    "register_attachment_tools",
    "save_provider_artifact_to_file_tool",
]
