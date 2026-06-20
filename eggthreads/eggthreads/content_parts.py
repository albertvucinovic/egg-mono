from __future__ import annotations

"""Egg-native message content-part helpers.

Existing string message content remains the canonical simple case.  New
multimodal-capable messages may store an ordered list of provider-neutral Egg
content parts.  This module validates that durable shape and provides safe text
previews/placeholders for UI, token counting, REPL context files, and interim
provider compatibility until provider-native lowering is implemented.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .input_artifacts import validate_input_id
from .provider_output_artifacts import validate_provider_output_artifact_id


TEXT_PART_TYPE = "text"
ATTACHMENT_PART_TYPE = "attachment"
ARTIFACT_PART_TYPE = "artifact"
CONTENT_PART_TYPES = {TEXT_PART_TYPE, ATTACHMENT_PART_TYPE, ARTIFACT_PART_TYPE}

_ATTACHMENT_REQUIRED_FIELDS = (
    "input_id",
    "owner_thread_id",
    "presentation",
    "mime_type",
    "filename",
    "size_bytes",
    "sha256",
)
_ATTACHMENT_ALLOWED_FIELDS = {"type", *_ATTACHMENT_REQUIRED_FIELDS, "options"}
_ARTIFACT_REQUIRED_FIELDS = (
    "artifact_id",
    "owner_thread_id",
    "presentation",
    "mime_type",
    "filename",
    "size_bytes",
    "sha256",
)
_ARTIFACT_ALLOWED_FIELDS = {"type", *_ARTIFACT_REQUIRED_FIELDS, "provenance", "options"}
_TEXT_ALLOWED_FIELDS = {"type", "text"}
_HEX = set("0123456789abcdef")

MessageContent = str | list[dict[str, Any]]


class ContentPartError(ValueError):
    """Raised when Egg message content parts are invalid."""


def _ensure_no_unknown_fields(part: Mapping[str, Any], allowed: set[str], *, part_type: str) -> None:
    unknown = sorted(str(k) for k in part.keys() if k not in allowed)
    if unknown:
        joined = ", ".join(unknown)
        raise ContentPartError(f"{part_type} content part has unsupported field(s): {joined}.")


def _require_string(value: Any, *, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContentPartError(f"{field} must be a string.")
    text = value.strip()
    if not allow_empty and not text:
        raise ContentPartError(f"{field} must be a non-empty string.")
    return text


def _validate_sha256(value: Any) -> str:
    if not isinstance(value, str):
        raise ContentPartError("content part sha256 must be a string.")
    text = value.strip().lower()
    if len(text) != 64 or any(ch not in _HEX for ch in text):
        raise ContentPartError("content part sha256 must be a 64-character lower-case hexadecimal string.")
    return text


def _validate_filename(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContentPartError("content part filename must be a string or null.")
    text = value.strip()
    if not text:
        return None
    if "\x00" in text or "/" in text or "\\" in text:
        raise ContentPartError("content part filename must be a display filename, not a path.")
    if text in {".", ".."}:
        raise ContentPartError("content part filename must be a display filename, not a path.")
    return text


def _validate_size_bytes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContentPartError("content part size_bytes must be a non-negative integer.")
    if value < 0:
        raise ContentPartError("content part size_bytes must be a non-negative integer.")
    return value


def _validate_object(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContentPartError(f"{field} must be an object.")
    data = dict(value)
    try:
        json.dumps(data, ensure_ascii=False)
    except Exception as e:
        raise ContentPartError(f"{field} must be JSON-serializable: {e}") from e
    return data


def validate_content_part(part: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a canonical copy of one Egg content part."""

    if not isinstance(part, Mapping):
        raise ContentPartError("content parts must be objects.")
    part_type = part.get("type")
    if part_type == TEXT_PART_TYPE:
        _ensure_no_unknown_fields(part, _TEXT_ALLOWED_FIELDS, part_type="text")
        text = part.get("text")
        if not isinstance(text, str):
            raise ContentPartError("text content part requires a string text field.")
        return {"type": TEXT_PART_TYPE, "text": text}

    if part_type == ATTACHMENT_PART_TYPE:
        _ensure_no_unknown_fields(part, _ATTACHMENT_ALLOWED_FIELDS, part_type="attachment")
        missing = [field for field in _ATTACHMENT_REQUIRED_FIELDS if field not in part]
        if missing:
            joined = ", ".join(missing)
            raise ContentPartError(f"attachment content part is missing required field(s): {joined}.")
        try:
            input_id = validate_input_id(str(part.get("input_id") or ""))
        except ValueError as e:
            raise ContentPartError(str(e)) from e
        canonical = {
            "type": ATTACHMENT_PART_TYPE,
            "input_id": input_id,
            "owner_thread_id": _require_string(part.get("owner_thread_id"), field="attachment owner_thread_id"),
            "presentation": _require_string(part.get("presentation"), field="attachment presentation").lower(),
            "mime_type": _require_string(part.get("mime_type"), field="attachment mime_type").lower(),
            "filename": _validate_filename(part.get("filename")),
            "size_bytes": _validate_size_bytes(part.get("size_bytes")),
            "sha256": _validate_sha256(part.get("sha256")),
            "options": _validate_object(part.get("options", {}), field="attachment options"),
        }
        return canonical

    if part_type == ARTIFACT_PART_TYPE:
        _ensure_no_unknown_fields(part, _ARTIFACT_ALLOWED_FIELDS, part_type="artifact")
        missing = [field for field in _ARTIFACT_REQUIRED_FIELDS if field not in part]
        if missing:
            joined = ", ".join(missing)
            raise ContentPartError(f"artifact content part is missing required field(s): {joined}.")
        try:
            artifact_id = validate_provider_output_artifact_id(str(part.get("artifact_id") or ""))
        except ValueError as e:
            raise ContentPartError(str(e)) from e
        canonical = {
            "type": ARTIFACT_PART_TYPE,
            "artifact_id": artifact_id,
            "owner_thread_id": _require_string(part.get("owner_thread_id"), field="artifact owner_thread_id"),
            "presentation": _require_string(part.get("presentation"), field="artifact presentation").lower(),
            "mime_type": _require_string(part.get("mime_type"), field="artifact mime_type").lower(),
            "filename": _validate_filename(part.get("filename")),
            "size_bytes": _validate_size_bytes(part.get("size_bytes")),
            "sha256": _validate_sha256(part.get("sha256")),
            "provenance": _validate_object(part.get("provenance", {}), field="artifact provenance"),
            "options": _validate_object(part.get("options", {}), field="artifact options"),
        }
        return canonical

    raise ContentPartError("content part type must be 'text', 'attachment', or 'artifact'.")


def validate_content_parts(parts: Any) -> list[dict[str, Any]]:
    """Validate and canonicalize an ordered Egg content-part array."""

    if not isinstance(parts, list):
        raise ContentPartError("content parts must be a list.")
    if not parts:
        raise ContentPartError("content parts must not be empty.")
    canonical = [validate_content_part(part) for part in parts]
    try:
        json.dumps(canonical, ensure_ascii=False)
    except Exception as e:
        raise ContentPartError(f"content parts must be JSON-serializable: {e}") from e
    return canonical


def validate_message_content(content: Any) -> MessageContent:
    """Validate message content accepted by ``append_message``.

    Strings are returned unchanged for backwards compatibility.  Lists are
    treated as Egg content-part arrays and returned as canonical dict copies.
    """

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return validate_content_parts(content)
    raise ContentPartError("message content must be a string or a list of Egg content parts.")


def attachment_part_from_input_metadata(
    metadata: Mapping[str, Any],
    *,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a canonical attachment content part from saved input metadata.

    ``save_input_bytes`` metadata is Egg's durable source of truth for an input
    artifact.  UI/API staging paths should derive message attachment parts from
    that metadata instead of hand-copying field lists in each frontend.
    """

    if not isinstance(metadata, Mapping):
        raise ContentPartError("input metadata must be an object.")
    part = {
        "type": ATTACHMENT_PART_TYPE,
        "input_id": metadata.get("input_id"),
        "owner_thread_id": metadata.get("owner_thread_id"),
        "presentation": metadata.get("presentation"),
        "mime_type": metadata.get("mime_type"),
        "filename": metadata.get("filename"),
        "size_bytes": metadata.get("size_bytes"),
        "sha256": metadata.get("sha256"),
        "options": dict(options or {}),
    }
    return validate_content_part(part)


def artifact_part_from_provider_output_metadata(
    metadata: Mapping[str, Any],
    *,
    provenance: Mapping[str, Any] | None = None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a canonical provider-output artifact content part from metadata.

    Provider-output artifacts are references to bytes produced by a provider and
    stored under ``.egg/egg_provider_output``.  Message content stores only the
    small durable reference/metadata summary; bytes and base64 never appear in
    transcript content.
    """

    if not isinstance(metadata, Mapping):
        raise ContentPartError("provider output metadata must be an object.")
    part = {
        "type": ARTIFACT_PART_TYPE,
        "artifact_id": metadata.get("artifact_id"),
        "owner_thread_id": metadata.get("owner_thread_id"),
        "presentation": metadata.get("presentation"),
        "mime_type": metadata.get("mime_type"),
        "filename": metadata.get("filename"),
        "size_bytes": metadata.get("size_bytes"),
        "sha256": metadata.get("sha256"),
        "provenance": dict(provenance or {}),
        "options": dict(options or {}),
    }
    return validate_content_part(part)


def normalize_content_to_parts(content: Any) -> list[dict[str, Any]]:
    """Return Egg content parts for a string or validated content array.

    This is useful for future provider lowering and for preview code that wants
    one uniform representation.  String content becomes one ``text`` part while
    durable string messages themselves remain stored as strings.
    """

    if isinstance(content, str):
        return [{"type": TEXT_PART_TYPE, "text": content}]
    return validate_content_parts(content)


def is_content_part_array(content: Any) -> bool:
    """Return True when *content* has the Egg content-array shape."""

    return isinstance(content, list)


def _format_size(size_bytes: Any) -> str:
    try:
        size = int(size_bytes)
    except Exception:
        return "unknown size"
    if size < 0:
        return "unknown size"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)} B"
    if value >= 100:
        rendered = f"{value:.0f}"
    elif value >= 10:
        rendered = f"{value:.1f}".rstrip("0").rstrip(".")
    else:
        rendered = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{rendered} {units[unit_index]}"


def format_attachment_placeholder(part: Mapping[str, Any], *, validate: bool = True) -> str:
    """Format one attachment part as a plain transcript/provider placeholder."""

    attachment = validate_content_part(part) if validate else dict(part)
    if attachment.get("type") != ATTACHMENT_PART_TYPE:
        raise ContentPartError("attachment placeholder requires an attachment content part.")
    filename = attachment.get("filename") or "(unnamed)"
    sha = str(attachment.get("sha256") or "")
    sha_short = sha[:8] if sha else "unknown"
    presentation = attachment.get("presentation") or "file"
    mime_type = attachment.get("mime_type") or "application/octet-stream"
    size = _format_size(attachment.get("size_bytes"))
    return f"[Attachment: {presentation} {filename} {mime_type} {size} sha256:{sha_short}]"


def format_provider_artifact_placeholder(part: Mapping[str, Any], *, validate: bool = True) -> str:
    """Format one provider-output artifact part as a plain readable placeholder."""

    artifact = validate_content_part(part) if validate else dict(part)
    if artifact.get("type") != ARTIFACT_PART_TYPE:
        raise ContentPartError("provider artifact placeholder requires an artifact content part.")
    filename = artifact.get("filename") or "(unnamed)"
    sha = str(artifact.get("sha256") or "")
    sha_short = sha[:8] if sha else "unknown"
    artifact_id = str(artifact.get("artifact_id") or "unknown")
    presentation = artifact.get("presentation") or "file"
    mime_type = artifact.get("mime_type") or "application/octet-stream"
    size = _format_size(artifact.get("size_bytes"))
    return f"[Provider artifact: {presentation} {filename} {mime_type} {size} sha256:{sha_short} artifact_id:{artifact_id}]"


def _fallback_part_text(part: Any) -> str:
    try:
        return json.dumps(part, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(part)


def content_to_plain_text(content: Any, *, validate: bool = False) -> str:
    """Render string or Egg content-array content as plain readable text.

    With ``validate=False`` (the default), this function is deliberately
    tolerant so UI/status paths can never crash on older or malformed snapshot
    payloads.  Use :func:`validate_message_content` when accepting new content.
    """

    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if not isinstance(content, list):
        return str(content)

    parts: Sequence[Any]
    if validate:
        parts = validate_content_parts(content)
    else:
        parts = content

    rendered: list[str] = []
    for raw_part in parts:
        if not isinstance(raw_part, Mapping):
            rendered.append(_fallback_part_text(raw_part))
            continue
        part_type = raw_part.get("type")
        if part_type == TEXT_PART_TYPE:
            text = raw_part.get("text")
            rendered.append(text if isinstance(text, str) else _fallback_part_text(raw_part))
        elif part_type == ATTACHMENT_PART_TYPE:
            try:
                rendered.append(format_attachment_placeholder(raw_part, validate=validate))
            except Exception:
                rendered.append(_fallback_part_text(raw_part))
        elif part_type == ARTIFACT_PART_TYPE:
            try:
                rendered.append(format_provider_artifact_placeholder(raw_part, validate=validate))
            except Exception:
                rendered.append(_fallback_part_text(raw_part))
        else:
            rendered.append(_fallback_part_text(raw_part))
    return "\n".join(piece for piece in rendered if piece is not None)


def extract_attachment_refs(content: Any, *, validate: bool = True) -> list[dict[str, Any]]:
    """Return canonical attachment parts referenced by message content."""

    if isinstance(content, str) or content is None:
        return []
    if not isinstance(content, list):
        if validate:
            validate_message_content(content)
        return []
    parts = validate_content_parts(content) if validate else [dict(part) for part in content if isinstance(part, Mapping)]
    return [part for part in parts if part.get("type") == ATTACHMENT_PART_TYPE]


def extract_artifact_refs(content: Any, *, validate: bool = True) -> list[dict[str, Any]]:
    """Return canonical provider-output artifact parts referenced by content."""

    if isinstance(content, str) or content is None:
        return []
    if not isinstance(content, list):
        if validate:
            validate_message_content(content)
        return []
    parts = validate_content_parts(content) if validate else [dict(part) for part in content if isinstance(part, Mapping)]
    return [part for part in parts if part.get("type") == ARTIFACT_PART_TYPE]


def content_has_attachments(content: Any, *, validate: bool = True) -> bool:
    """Return True if message content contains one or more attachment parts."""

    return bool(extract_attachment_refs(content, validate=validate))


def content_has_artifacts(content: Any, *, validate: bool = True) -> bool:
    """Return True if message content contains provider-output artifact parts."""

    return bool(extract_artifact_refs(content, validate=validate))


__all__ = [
    "ATTACHMENT_PART_TYPE",
    "ARTIFACT_PART_TYPE",
    "CONTENT_PART_TYPES",
    "ContentPartError",
    "MessageContent",
    "TEXT_PART_TYPE",
    "attachment_part_from_input_metadata",
    "artifact_part_from_provider_output_metadata",
    "content_has_artifacts",
    "content_has_attachments",
    "content_to_plain_text",
    "extract_artifact_refs",
    "extract_attachment_refs",
    "format_attachment_placeholder",
    "format_provider_artifact_placeholder",
    "is_content_part_array",
    "normalize_content_to_parts",
    "validate_content_part",
    "validate_content_parts",
    "validate_message_content",
]
