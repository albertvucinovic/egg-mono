from __future__ import annotations

"""Helpers for staging local input attachments before a user message append."""

import mimetypes
from pathlib import Path
from typing import Any, Iterable, Mapping

from .content_parts import attachment_part_from_input_metadata, format_attachment_placeholder, validate_content_part
from .input_artifacts import SavedInputArtifact, save_input_bytes
from .sandbox import authorize_thread_path_read
from .terminal_safety import sanitize_terminal_text


def safe_display_filename(filename: str | Path | None, *, default: str = "attachment") -> str:
    """Return a safe basename for user-visible attachment metadata."""

    text = sanitize_terminal_text(str(filename or "")).replace("\\", "/")
    text = text.rsplit("/", 1)[-1]
    text = " ".join(text.split())
    if not text or text in {".", ".."}:
        text = default
    return text


_IMAGE_MAGIC_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"RIFF", "image/webp"),
)

_GENERIC_MAGIC_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
)


def _looks_like_webp(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def infer_attachment_mime_and_presentation(filename: str | Path, data: bytes) -> tuple[str, str]:
    """Infer a conservative MIME type and Egg presentation for attachment data.

    Common raster image formats are recognized by magic bytes and staged with
    ``presentation='image'``.  Everything else stays a generic file, with a
    best-effort MIME type from the display filename or
    ``application/octet-stream``.
    """

    data_bytes = bytes(data or b"")
    for prefix, mime_type in _IMAGE_MAGIC_PREFIXES:
        if data_bytes.startswith(prefix) and (mime_type != "image/webp" or _looks_like_webp(data_bytes)):
            return mime_type, "image"

    for prefix, mime_type in _GENERIC_MAGIC_PREFIXES:
        if data_bytes.startswith(prefix):
            return mime_type, "file"

    if data_bytes:
        try:
            decoded = data_bytes.decode("utf-8")
            if all(ch in "\n\r\t" or ord(ch) >= 0x20 for ch in decoded):
                guessed, _encoding = mimetypes.guess_type(str(filename or ""))
                if guessed and (guessed.startswith("text/") or guessed in {"application/json", "application/xml"}):
                    return guessed.lower(), "file"
                return "text/plain", "file"
        except UnicodeDecodeError:
            pass

    guessed, _encoding = mimetypes.guess_type(str(filename or ""))
    mime_type = (guessed or "application/octet-stream").strip().lower() or "application/octet-stream"
    # Be conservative: extension-derived image/* is not enough to choose image
    # presentation or MIME because provider lowering will treat image
    # attachments as bytes that must match image metadata.
    if mime_type.startswith("image/"):
        mime_type = "application/octet-stream"
    return mime_type, "file"


def save_local_attachment_for_thread(
    db: Any,
    thread_id: str,
    source_path: str | Path,
    *,
    workspace: str | Path | None = None,
) -> tuple[SavedInputArtifact, dict[str, Any]]:
    """Authorize, ingest, and return an attachment part for a local path."""

    resolved = authorize_thread_path_read(db, thread_id, source_path)
    data = resolved.read_bytes()
    return save_attachment_bytes_for_thread(
        Path.cwd() if workspace is None else workspace,
        thread_id,
        data,
        filename=resolved.name,
        provenance={"kind": "local_path"},
    )


def save_attachment_bytes_for_thread(
    workspace: str | Path,
    thread_id: str,
    data: bytes | bytearray | memoryview,
    *,
    filename: str | Path | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> tuple[SavedInputArtifact, dict[str, Any]]:
    """Save uploaded/staged bytes and return a canonical attachment part."""

    data_bytes = bytes(data)
    display_name = safe_display_filename(filename)
    mime_type, presentation = infer_attachment_mime_and_presentation(display_name, data_bytes)
    provenance_data = dict(provenance or {})
    provenance_data.setdefault("kind", "bytes")
    provenance_data["display_name"] = display_name
    saved = save_input_bytes(
        workspace,
        thread_id,
        data_bytes,
        filename=display_name,
        mime_type=mime_type,
        presentation=presentation,
        provenance=provenance_data,
    )
    return saved, attachment_part_from_input_metadata(saved.metadata)


def build_message_content_with_attachments(
    text: str,
    attachments: Iterable[Mapping[str, Any]],
) -> str | list[dict[str, Any]]:
    """Return historical string content or ordered text+attachment parts."""

    staged = [validate_content_part(part) for part in attachments]
    if not staged:
        return text
    parts: list[dict[str, Any]] = []
    if isinstance(text, str) and text:
        parts.append({"type": "text", "text": text})
    parts.extend(staged)
    return parts


def format_staged_attachments(attachments: Iterable[Mapping[str, Any]]) -> str:
    """Render staged attachments as a readable numbered list."""

    parts = [validate_content_part(part) for part in attachments]
    if not parts:
        return "No attachments staged."
    lines = [f"{index}. {format_attachment_placeholder(part, validate=False)}" for index, part in enumerate(parts, start=1)]
    return "Staged attachments:\n" + "\n".join(lines)


__all__ = [
    "build_message_content_with_attachments",
    "format_staged_attachments",
    "infer_attachment_mime_and_presentation",
    "safe_display_filename",
    "save_attachment_bytes_for_thread",
    "save_local_attachment_for_thread",
]
