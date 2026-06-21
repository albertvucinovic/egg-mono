"""Attachment slash commands for eggw backend."""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Iterable, Mapping

from eggthreads import current_thread_model
from eggthreads.attachment_staging import (
    format_attachments_overview,
)
from eggthreads.attachment_tools import (
    artifact_workspace_from_db,
    attach_local_file_operation,
    attach_provider_output_operation,
    save_provider_artifact_operation,
    thread_working_directory,
)
from eggthreads.content_parts import content_to_plain_text, format_attachment_placeholder, validate_content_part

from .. import core
from ..models import CommandResponse


def _workspace() -> Path:
    """Return the workspace root used for EggW artifacts."""

    return artifact_workspace_from_db(core.db)


def _thread_working_directory(thread_id: str) -> Path:
    try:
        return thread_working_directory(core.db, thread_id, fallback=_workspace())
    except Exception:
        return _workspace()


def _public_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in dict(metadata).items() if key != "blob_relpath"}


def _parse_one_arg(arg: str, *, usage: str, description: str) -> str:
    text = (arg or "").strip()
    if not text:
        raise ValueError(usage)
    try:
        parts = shlex.split(text)
    except ValueError as e:
        raise ValueError(f"Could not parse {description}: {e}") from e
    if len(parts) != 1:
        raise ValueError(usage)
    return parts[0]


def _parse_attach_path(arg: str) -> str:
    return _parse_one_arg(arg, usage="Usage: /attach <path>", description="path")


def _parse_attach_output_artifact_id(arg: str) -> str:
    return _parse_one_arg(arg, usage="Usage: /attachOutput <artifact_id>", description="artifact id")


def _parse_save_provider_artifact_args(arg: str) -> tuple[str, str | None]:
    text = (arg or "").strip()
    if not text:
        raise ValueError("Usage: /saveProviderArtifact <artifact_id> [path]")
    try:
        parts = shlex.split(text)
    except ValueError as e:
        raise ValueError(f"Could not parse artifact id/path: {e}") from e
    if len(parts) not in {1, 2}:
        raise ValueError("Usage: /saveProviderArtifact <artifact_id> [path]")
    return parts[0], parts[1] if len(parts) == 2 else None


def _validate_current_model_attachment(thread_id: str, filename: str, mime_type: str, presentation: str) -> None:
    if str(presentation or "").lower() != "image":
        return
    try:
        from eggllm.capabilities import supports_attachment_presentation
    except Exception:
        return

    model_key: str | None = None
    try:
        model_key = current_thread_model(core.db, thread_id) if core.db is not None else None
    except Exception:
        model_key = None
    cfg: dict[str, Any] = {}
    if model_key:
        try:
            cfg = core.effective_model_config(model_key, core.models_config.get(model_key, {}), core.llm_client)
        except Exception:
            cfg = core.models_config.get(model_key, {}) if isinstance(core.models_config, dict) else {}
    if supports_attachment_presentation(cfg, "image", mime_type=mime_type):
        return
    model = model_key or "current model"
    raise ValueError(
        f"{model} is configured as not supporting image attachments ({mime_type}) for {filename}. "
        "Choose a vision-capable model or update the model/provider attachment capabilities."
    )


def _stage_response(message: str, *, saved: Any, content_part: dict[str, Any]) -> CommandResponse:
    return CommandResponse(
        success=True,
        message=message,
        data={
            "action": "stage_attachment",
            "input_id": getattr(saved, "input_id", None),
            "metadata": _public_metadata(getattr(saved, "metadata", {}) or {}),
            "content_part": content_part,
            "content_text": content_to_plain_text([content_part], validate=True),
        },
    )


def _validate_client_attachments(staged_attachments: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    if staged_attachments is None:
        return []
    out: list[dict[str, Any]] = []
    for item in staged_attachments:
        part = validate_content_part(item)
        if part.get("type") != "attachment":
            raise ValueError("staged_attachments may contain only attachment parts")
        out.append(part)
    return out


async def cmd_attach(thread_id: str, arg: str) -> CommandResponse:
    """Stage a local server-side path as an EggW attachment."""

    if not core.db:
        return CommandResponse(success=False, message="/attach failed: database not initialized")
    try:
        source_path = _parse_attach_path(arg)
        result = attach_local_file_operation(
            core.db,
            thread_id,
            source_path,
            workspace=_workspace(),
            validate_candidate=lambda filename, mime_type, presentation: _validate_current_model_attachment(
                thread_id,
                filename,
                mime_type,
                presentation,
            ),
        )
        saved = result.saved
        part = result.content_part
        message = f"Attached {part.get('filename') or '(unnamed)'} as {part.get('presentation')} ({part.get('mime_type')}); 1 staged."
        return _stage_response(message, saved=saved, content_part=part)
    except Exception as e:
        return CommandResponse(success=False, message=f"/attach failed: {e}")


def _thread_messages(thread_id: str) -> list[dict[str, Any]]:
    if core.db is None:
        return []
    try:
        from eggthreads import create_snapshot

        snap = create_snapshot(core.db, thread_id)
        messages = snap.get("messages") if isinstance(snap, dict) else None
        return [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []
    except Exception:
        return []


def cmd_attachments(thread_id: str, staged_attachments: Iterable[Mapping[str, Any]] | None = None) -> CommandResponse:
    """List current staged attachments and historical conversation attachments."""

    try:
        staged = _validate_client_attachments(staged_attachments)
        messages = _thread_messages(thread_id)
        return CommandResponse(
            success=True,
            message=format_attachments_overview(staged, messages),
            data={
                "action": "list_attachments",
                "staged_count": len(staged),
                "historical_count": sum(
                    1
                    for message in messages
                    if isinstance(message.get("content"), list)
                    for part in message.get("content", [])
                    if isinstance(part, Mapping) and part.get("type") == "attachment"
                ),
            },
        )
    except Exception as e:
        return CommandResponse(success=False, message=f"/attachments failed: {e}")


async def cmd_attach_output(thread_id: str, arg: str) -> CommandResponse:
    """Promote a provider-output artifact and stage it as an EggW attachment."""

    if not core.db:
        return CommandResponse(success=False, message="/attachOutput failed: database not initialized")
    try:
        artifact_id = _parse_attach_output_artifact_id(arg)
        result = attach_provider_output_operation(core.db, thread_id, artifact_id, workspace=_workspace())
        saved = result.saved
        part = result.content_part
        placeholder = format_attachment_placeholder(part, validate=False)
        message = f"Promoted provider output {artifact_id} to input {saved.input_id}; staged attachment.\n{placeholder}"
        return _stage_response(message, saved=saved, content_part=part)
    except Exception as e:
        return CommandResponse(success=False, message=f"/attachOutput failed: {e}")


def cmd_clear_attachments(staged_attachments: Iterable[Mapping[str, Any]] | None = None) -> CommandResponse:
    """Tell the EggW client to clear its local staged attachment list."""

    try:
        count = len(_validate_client_attachments(staged_attachments))
    except Exception:
        count = 0
    return CommandResponse(
        success=True,
        message=f"Cleared {count} staged attachment{'s' if count != 1 else ''}.",
        data={"action": "clear_staged_attachments", "count": count},
    )


async def cmd_save_provider_artifact(thread_id: str, arg: str) -> CommandResponse:
    """Copy a provider-output artifact into the current working directory."""

    if not core.db:
        return CommandResponse(success=False, message="/saveProviderArtifact failed: database not initialized")
    try:
        artifact_id, output_path = _parse_save_provider_artifact_args(arg)
        result = save_provider_artifact_operation(
            core.db,
            thread_id,
            artifact_id,
            output_path,
            workspace=_workspace(),
            export_workspace=_thread_working_directory(thread_id),
        )
        return CommandResponse(
            success=True,
            message=result.message,
            data=result.public_payload(),
        )
    except Exception as e:
        return CommandResponse(success=False, message=f"/saveProviderArtifact failed: {e}")


__all__ = [
    "cmd_attach",
    "cmd_attach_output",
    "cmd_attachments",
    "cmd_clear_attachments",
    "cmd_save_provider_artifact",
]
