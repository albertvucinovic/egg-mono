from __future__ import annotations

"""Provider-bound lowering for Egg attachment content parts.

This module is deliberately narrow for Phase 3: attachments lower at the
provider boundary only when the selected chat model explicitly advertises the
corresponding presentation/MIME support.  Raw bytes become base64/data URLs
here, never in stored thread history.
"""

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from eggllm.capabilities import supports_attachment_presentation

from .content_parts import (
    ARTIFACT_PART_TYPE,
    ATTACHMENT_PART_TYPE,
    TEXT_PART_TYPE,
    content_has_attachments,
    content_to_plain_text,
    format_attachment_placeholder,
    validate_content_parts,
)
from .input_artifacts import resolve_input_bytes


class AttachmentLoweringError(ValueError):
    """Raised when current-turn attachments cannot be lowered safely."""


_OPENAI_ATTACHMENT_API_TYPES = {"chat_completions", "responses"}
_ANTHROPIC_ATTACHMENT_API_TYPES = {"anthropic", "anthropic_messages"}
_OPENAI_FILE_PRESENTATIONS = {"document", "file"}
_ANTHROPIC_DOCUMENT_MIME_PREFIXES = ("text/", "application/vnd.")
_ANTHROPIC_DOCUMENT_MIME_TYPES = {"application/pdf"}
_NATIVE_ATTACHMENT_PART_TYPES = {"image_url", "input_image", "file", "input_file", "image", "document"}


@dataclass(frozen=True)
class AttachmentLoweringContext:
    workspace: Path
    db: Any
    calling_thread_id: str
    model_key: str | None
    model_config: Mapping[str, Any]
    provider_api_type: str


def message_has_attachments(message: Mapping[str, Any]) -> bool:
    return content_has_attachments(message.get("content"), validate=False)


def _is_image_attachment(part: Mapping[str, Any]) -> bool:
    return str(part.get("type") or "") == ATTACHMENT_PART_TYPE and str(part.get("presentation") or "").lower() == "image"


def _is_openai_file_attachment(part: Mapping[str, Any]) -> bool:
    return (
        str(part.get("type") or "") == ATTACHMENT_PART_TYPE
        and str(part.get("presentation") or "").strip().lower() in _OPENAI_FILE_PRESENTATIONS
    )


def _is_anthropic_document_attachment(part: Mapping[str, Any]) -> bool:
    if str(part.get("type") or "") != ATTACHMENT_PART_TYPE:
        return False
    presentation = str(part.get("presentation") or "").strip().lower()
    if presentation == "document":
        return True
    if presentation != "file":
        return False
    mime_type = str(part.get("mime_type") or "").strip().lower()
    return mime_type in _ANTHROPIC_DOCUMENT_MIME_TYPES or any(mime_type.startswith(prefix) for prefix in _ANTHROPIC_DOCUMENT_MIME_PREFIXES)


def _data_url(part: Mapping[str, Any], data: bytes) -> str:
    mime_type = str(part.get("mime_type") or "application/octet-stream").strip().lower() or "application/octet-stream"
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _raw_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _owner_selector(ctx: AttachmentLoweringContext, part: Mapping[str, Any]) -> str | None:
    owner = str(part.get("owner_thread_id") or "").strip()
    if not owner or owner == ctx.calling_thread_id:
        return None
    # Phase 3 provider lowering should normally resolve current thread inputs.
    # If history legitimately includes descendant-owned records in an ancestor
    # context, use the same explicit descendant selector as Phase 1 helpers.
    return owner


def _metadata_mismatch(field: str, part_value: Any, metadata_value: Any) -> AttachmentLoweringError:
    return AttachmentLoweringError(
        f"Attachment metadata mismatch for {field}: content part has {part_value!r}, "
        f"stored input metadata has {metadata_value!r}."
    )


def _resolve_attachment_bytes(ctx: AttachmentLoweringContext, part: Mapping[str, Any]) -> bytes:
    descendant = _owner_selector(ctx, part)
    metadata, data = resolve_input_bytes(
        ctx.workspace,
        ctx.db,
        ctx.calling_thread_id,
        str(part.get("input_id") or ""),
        descendant_thread_id=descendant,
    )
    # Content parts are durable references to input metadata, not independent
    # authority about the stored bytes.  Verify the provider-bound presentation
    # still matches the access-checked input record before using it to choose a
    # MIME/type-specific provider shape.
    for field in ("owner_thread_id", "presentation", "mime_type", "filename", "size_bytes", "sha256"):
        if part.get(field) != metadata.get(field):
            raise _metadata_mismatch(field, part.get(field), metadata.get(field))
    return data


def _unsupported_message(part: Mapping[str, Any], ctx: AttachmentLoweringContext, reason: str) -> str:
    filename = part.get("filename") or "(unnamed)"
    presentation = part.get("presentation") or "file"
    mime_type = part.get("mime_type") or "application/octet-stream"
    model = ctx.model_key or "current model"
    return f"Attachment {filename} ({presentation} {mime_type}) cannot be sent to {model}: {reason}."


def _can_lower_image(ctx: AttachmentLoweringContext, part: Mapping[str, Any]) -> bool:
    return supports_attachment_presentation(
        ctx.model_config,
        "image",
        mime_type=str(part.get("mime_type") or ""),
    )


def _can_lower_openai_file(ctx: AttachmentLoweringContext, part: Mapping[str, Any]) -> bool:
    presentation = str(part.get("presentation") or "").strip().lower()
    if presentation not in _OPENAI_FILE_PRESENTATIONS:
        return False
    return supports_attachment_presentation(
        ctx.model_config,
        presentation,
        mime_type=str(part.get("mime_type") or ""),
    )


def _can_lower_anthropic_document(ctx: AttachmentLoweringContext, part: Mapping[str, Any]) -> bool:
    if not _is_anthropic_document_attachment(part):
        return False
    return supports_attachment_presentation(
        ctx.model_config,
        "document",
        mime_type=str(part.get("mime_type") or ""),
    )


def _placeholder(part: Mapping[str, Any]) -> str:
    try:
        return format_attachment_placeholder(part, validate=False)
    except Exception:
        return str(part)


def _lower_text_part(part: Mapping[str, Any], provider_api_type: str) -> Dict[str, Any] | str:
    text = part.get("text") if isinstance(part.get("text"), str) else ""
    if provider_api_type == "responses":
        return {"type": "input_text", "text": text}
    return {"type": "text", "text": text}


def _lower_image_part(ctx: AttachmentLoweringContext, part: Mapping[str, Any]) -> Dict[str, Any]:
    data = _resolve_attachment_bytes(ctx, part)
    if ctx.provider_api_type in _ANTHROPIC_ATTACHMENT_API_TYPES:
        mime_type = str(part.get("mime_type") or "application/octet-stream").strip().lower() or "application/octet-stream"
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": _raw_base64(data),
            },
        }
    data_url = _data_url(part, data)
    if ctx.provider_api_type == "responses":
        out: Dict[str, Any] = {"type": "input_image", "image_url": data_url}
    else:
        out = {"type": "image_url", "image_url": {"url": data_url}}
    options = part.get("options")
    if isinstance(options, Mapping):
        detail = options.get("detail")
        if isinstance(detail, str) and detail.strip():
            if ctx.provider_api_type == "responses":
                out["detail"] = detail.strip()
            else:
                out["image_url"]["detail"] = detail.strip()  # type: ignore[index]
    return out


def _attachment_filename(part: Mapping[str, Any]) -> str:
    filename = str(part.get("filename") or "").strip()
    if filename:
        return filename
    input_id = str(part.get("input_id") or "").strip()
    return input_id or "attachment"


def _lower_openai_file_part(ctx: AttachmentLoweringContext, part: Mapping[str, Any]) -> Dict[str, Any]:
    data_url = _data_url(part, _resolve_attachment_bytes(ctx, part))
    filename = _attachment_filename(part)
    if ctx.provider_api_type == "responses":
        return {"type": "input_file", "filename": filename, "file_data": data_url}
    return {"type": "file", "file": {"filename": filename, "file_data": data_url}}


def _lower_anthropic_document_part(ctx: AttachmentLoweringContext, part: Mapping[str, Any]) -> Dict[str, Any]:
    data = _resolve_attachment_bytes(ctx, part)
    mime_type = str(part.get("mime_type") or "application/octet-stream").strip().lower() or "application/octet-stream"
    out: Dict[str, Any] = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": mime_type,
            "data": _raw_base64(data),
        },
    }
    filename = _attachment_filename(part)
    if filename:
        out["title"] = filename
    return out


def _lower_content_array(
    ctx: AttachmentLoweringContext,
    content: list[dict[str, Any]],
    *,
    current_message: bool,
) -> list[Any] | str:
    parts = validate_content_parts(content)
    if not any(part.get("type") == ATTACHMENT_PART_TYPE for part in parts):
        return content_to_plain_text(parts)

    lowered: List[Any] = []
    for part in parts:
        part_type = part.get("type")
        if part_type == TEXT_PART_TYPE:
            lowered.append(_lower_text_part(part, ctx.provider_api_type))
            continue
        if part_type == ARTIFACT_PART_TYPE:
            # Provider-output artifacts are not provider inputs.  Until an
            # explicit promotion-to-input flow exists, keep them as readable
            # placeholders in provider context for both current and historical
            # messages.
            lowered.append(_lower_text_part({"type": TEXT_PART_TYPE, "text": content_to_plain_text([part])}, ctx.provider_api_type))
            continue
        if part_type != ATTACHMENT_PART_TYPE:
            if current_message:
                raise AttachmentLoweringError(f"Unsupported content part type: {part_type}")
            lowered.append({"type": "text", "text": content_to_plain_text([part])})
            continue
        if _is_image_attachment(part) and ctx.provider_api_type in (_OPENAI_ATTACHMENT_API_TYPES | _ANTHROPIC_ATTACHMENT_API_TYPES) and _can_lower_image(ctx, part):
            try:
                lowered.append(_lower_image_part(ctx, part))
            except Exception as e:
                if current_message:
                    if isinstance(e, AttachmentLoweringError):
                        raise
                    raise AttachmentLoweringError(_unsupported_message(part, ctx, str(e))) from e
                lowered.append(_lower_text_part({"type": TEXT_PART_TYPE, "text": _placeholder(part)}, ctx.provider_api_type))
            continue
        if _is_openai_file_attachment(part) and ctx.provider_api_type in _OPENAI_ATTACHMENT_API_TYPES and _can_lower_openai_file(ctx, part):
            try:
                lowered.append(_lower_openai_file_part(ctx, part))
            except Exception as e:
                if current_message:
                    if isinstance(e, AttachmentLoweringError):
                        raise
                    raise AttachmentLoweringError(_unsupported_message(part, ctx, str(e))) from e
                lowered.append(_lower_text_part({"type": TEXT_PART_TYPE, "text": _placeholder(part)}, ctx.provider_api_type))
            continue
        if _is_anthropic_document_attachment(part) and ctx.provider_api_type in _ANTHROPIC_ATTACHMENT_API_TYPES and _can_lower_anthropic_document(ctx, part):
            try:
                lowered.append(_lower_anthropic_document_part(ctx, part))
            except Exception as e:
                if current_message:
                    if isinstance(e, AttachmentLoweringError):
                        raise
                    raise AttachmentLoweringError(_unsupported_message(part, ctx, str(e))) from e
                lowered.append(_lower_text_part({"type": TEXT_PART_TYPE, "text": _placeholder(part)}, ctx.provider_api_type))
            continue
        reason = "unsupported attachment type or missing attachment capability"
        if current_message:
            raise AttachmentLoweringError(_unsupported_message(part, ctx, reason))
        lowered.append(_lower_text_part({"type": TEXT_PART_TYPE, "text": _placeholder(part)}, ctx.provider_api_type))

    if (
        not current_message
        and not any(isinstance(item, dict) and item.get("type") in _NATIVE_ATTACHMENT_PART_TYPES for item in lowered)
    ):
        return "\n".join(str(item.get("text") or "") for item in lowered)
    return lowered


def lower_message_attachments_for_provider(
    message: Mapping[str, Any],
    ctx: AttachmentLoweringContext,
    *,
    current_message: bool,
) -> Dict[str, Any]:
    """Return a provider-safe copy of one message.

    ``current_message=True`` means attachment lowering failures are user-facing
    fail-fast errors.  Older context messages may fall back to textual
    placeholders instead.
    """

    out = dict(message)
    content = out.get("content")
    if isinstance(content, list):
        if str(out.get("role") or "") != "user" and content_has_attachments(content, validate=False):
            # Attachments are provider inputs, not structured tool/assistant
            # outputs.  Non-user messages that contain attachment references
            # should reach providers as readable metadata/placeholders rather
            # than native input_image/input_file blocks in an invalid role.
            out["content"] = content_to_plain_text(content)
        else:
            out["content"] = _lower_content_array(ctx, content, current_message=current_message)
    return out


def expand_tool_attachment_messages_for_provider(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add provider-visible user input messages for attachment-producing tools.

    The OpenAI-style tools protocol requires an assistant ``tool_calls`` message
    to be answered by a ``role='tool'`` message.  Most providers do not accept
    multimodal input blocks directly on that tool message, so a tool such as
    ``attach`` or ``attach_output`` would otherwise give the model only a text
    placeholder.  Preserve the protocol tool result as text, then append a
    synthetic user-role message carrying the same Egg attachment content parts;
    normal provider-bound lowering can turn that user message into image/file
    input blocks for the current model.
    """

    out: List[Dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            out.append(message)
            continue
        content = message.get("content")
        if str(message.get("role") or "") != "tool" or not isinstance(content, list):
            out.append(message)
            continue
        if not content_has_attachments(content, validate=False):
            out.append(message)
            continue

        tool_message = dict(message)
        tool_message["content"] = content_to_plain_text(content)
        out.append(tool_message)

        user_message: Dict[str, Any] = {"role": "user", "content": content}
        # Keep msg_id/event_seq so current-turn detection treats the synthetic
        # visual-context message as the current tool result when appropriate.
        if message.get("msg_id"):
            user_message["msg_id"] = message.get("msg_id")
        if message.get("event_seq") is not None:
            user_message["event_seq"] = message.get("event_seq")
        out.append(user_message)
    return out


def lower_messages_for_provider(
    messages: List[Dict[str, Any]],
    ctx: AttachmentLoweringContext,
    *,
    current_msg_id: str | None = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for index, message in enumerate(messages):
        msg_id = message.get("msg_id")
        current = bool(current_msg_id and msg_id == current_msg_id)
        if current_msg_id is None:
            current = index == len(messages) - 1
        out.append(lower_message_attachments_for_provider(message, ctx, current_message=current))
    return out


__all__ = [
    "AttachmentLoweringContext",
    "AttachmentLoweringError",
    "expand_tool_attachment_messages_for_provider",
    "lower_message_attachments_for_provider",
    "lower_messages_for_provider",
    "message_has_attachments",
]
