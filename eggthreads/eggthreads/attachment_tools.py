from __future__ import annotations

"""Shared attachment/provider-artifact operations for commands and tools.

This module is intentionally UI-neutral: terminal Egg, EggW, and LLM-facing
Tools all call these helpers so path checks, provider-output authorization, and
result payloads stay consistent.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .attachment_staging import save_local_attachment_for_thread
from .content_parts import content_to_plain_text
from .provider_output_artifacts import promote_provider_output_to_input
from .provider_output_export import export_provider_output_artifact


@dataclass(frozen=True)
class AttachmentOperationResult:
    """Result of staging an input attachment."""

    message: str
    saved: Any
    content_part: dict[str, Any]

    def public_payload(self) -> dict[str, Any]:
        metadata = _public_metadata(getattr(self.saved, "metadata", {}) or {})
        content_parts = [
            {"type": "text", "text": self.message},
            self.content_part,
        ]
        content_text = content_to_plain_text(content_parts, validate=True)
        return {
            "action": "stage_attachment",
            "input_id": getattr(self.saved, "input_id", None),
            "metadata": metadata,
            "content_part": self.content_part,
            "content_parts": content_parts,
            "content_text": content_text,
        }


@dataclass(frozen=True)
class ProviderArtifactExportResult:
    """Result of exporting a provider-output artifact to the workspace."""

    message: str
    artifact_id: str
    path: Path
    display_path: str
    metadata: dict[str, Any]

    def public_payload(self) -> dict[str, Any]:
        return {
            "action": "save_provider_artifact",
            "artifact_id": self.artifact_id,
            "path": self.display_path,
            "metadata": _public_metadata(self.metadata),
        }


def artifact_workspace_from_db(db: Any, *, fallback: str | Path | None = None) -> Path:
    """Return the workspace root used for Egg artifact storage."""

    try:
        db_path = Path(getattr(db, "path")).expanduser().resolve()
        if db_path.parent.name == ".egg":
            return db_path.parent.parent
    except Exception:
        pass
    if fallback is not None:
        return Path(fallback).expanduser().resolve()
    return Path.cwd().resolve()


def thread_working_directory(db: Any, thread_id: str, *, fallback: str | Path | None = None) -> Path:
    """Return the effective thread working directory."""

    try:
        from .api import get_thread_working_directory

        if db is not None and str(thread_id or "").strip():
            return get_thread_working_directory(db, thread_id).resolve()
    except Exception:
        pass
    if fallback is not None:
        return Path(fallback).expanduser().resolve()
    return artifact_workspace_from_db(db).resolve()


def _public_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in dict(metadata).items() if key != "blob_relpath"}


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return str(path)


def attach_local_file_operation(
    db: Any,
    thread_id: str,
    source_path: str | Path,
    *,
    workspace: str | Path | None = None,
    validate_candidate: Any = None,
) -> AttachmentOperationResult:
    """Ingest a local path as an input artifact and return a stage payload."""

    tid = str(thread_id or "").strip()
    if db is None or not tid:
        raise ValueError("attach_file requires a current thread and database context.")
    storage_workspace = artifact_workspace_from_db(db) if workspace is None else Path(workspace).expanduser().resolve()
    saved, part = save_local_attachment_for_thread(
        db,
        tid,
        source_path,
        workspace=storage_workspace,
        validate_candidate=validate_candidate,
    )
    message = f"Attached {part.get('filename') or '(unnamed)'} as {part.get('presentation')} ({part.get('mime_type')})."
    return AttachmentOperationResult(message=message, saved=saved, content_part=part)


def attach_provider_output_operation(
    db: Any,
    thread_id: str,
    artifact_id: str,
    *,
    descendant_thread_id: str | None = None,
    workspace: str | Path | None = None,
) -> AttachmentOperationResult:
    """Promote a provider-output artifact into input storage for attachment."""

    tid = str(thread_id or "").strip()
    if db is None or not tid:
        raise ValueError("attach_output requires a current thread and database context.")
    storage_workspace = artifact_workspace_from_db(db) if workspace is None else Path(workspace).expanduser().resolve()
    saved, part = promote_provider_output_to_input(
        storage_workspace,
        db,
        tid,
        str(artifact_id or "").strip(),
        descendant_thread_id=(str(descendant_thread_id).strip() if descendant_thread_id else None),
    )
    message = f"Promoted provider output {artifact_id} to input {saved.input_id}."
    return AttachmentOperationResult(message=message, saved=saved, content_part=part)


def save_provider_artifact_operation(
    db: Any,
    thread_id: str,
    artifact_id: str,
    output_path: str | Path | None = None,
    *,
    descendant_thread_id: str | None = None,
    workspace: str | Path | None = None,
    export_workspace: str | Path | None = None,
) -> ProviderArtifactExportResult:
    """Export an accessible provider-output artifact to the thread workspace."""

    tid = str(thread_id or "").strip()
    if db is None or not tid:
        raise ValueError("save_provider_artifact requires a current thread and database context.")
    storage_workspace = artifact_workspace_from_db(db) if workspace is None else Path(workspace).expanduser().resolve()
    export_root = (
        thread_working_directory(db, tid)
        if export_workspace is None
        else Path(export_workspace).expanduser().resolve()
    )
    descendant = str(descendant_thread_id).strip() if descendant_thread_id else None
    target, metadata = export_provider_output_artifact(
        storage_workspace,
        db,
        tid,
        str(artifact_id or "").strip(),
        output_path,
        descendant_thread_id=descendant,
        export_workspace=export_root,
    )
    display_path = _display_path(target, export_root)
    message = (
        f"Saved provider artifact {artifact_id} to {display_path} "
        f"({metadata.get('mime_type') or 'application/octet-stream'}, {metadata.get('size_bytes')} bytes)."
    )
    return ProviderArtifactExportResult(
        message=message,
        artifact_id=str(artifact_id or "").strip(),
        path=target,
        display_path=display_path,
        metadata=metadata,
    )


__all__ = [
    "AttachmentOperationResult",
    "ProviderArtifactExportResult",
    "artifact_workspace_from_db",
    "attach_local_file_operation",
    "attach_provider_output_operation",
    "save_provider_artifact_operation",
    "thread_working_directory",
]
