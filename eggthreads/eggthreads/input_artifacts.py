from __future__ import annotations

"""Durable storage helpers for provider input artifacts.

Input artifacts are thread-owned records under ``.egg/egg_inputs`` with raw
bytes deduplicated through a global content-addressed blob store.  The blob
store is not an authorization boundary: callers must resolve bytes through an
owned or explicitly-authorized input metadata record.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .owned_byte_artifacts import (
    ARTIFACT_HASH_ALGORITHM,
    OwnedArtifactStorageSpec,
    artifact_record_dir,
    artifact_root_dir,
    artifact_root_relative_dir,
    blob_path_for_sha256,
    resolve_owned_artifact_bytes,
    resolve_owned_artifact_metadata,
    resolve_owned_artifact_owner_thread_id,
    save_owned_artifact_bytes,
    thread_artifact_dir,
    thread_artifact_relative_dir,
    validate_artifact_id,
)


INPUT_ID_LENGTH = 8
INPUT_METADATA_SCHEMA_VERSION = 1
INPUT_HASH_ALGORITHM = ARTIFACT_HASH_ALGORITHM


class InputArtifactError(RuntimeError):
    """Base class for input artifact storage/metadata errors."""


class InputArtifactAccessError(PermissionError):
    """Raised when a caller is not authorized for an input artifact namespace."""


class InputArtifactNotFoundError(FileNotFoundError):
    """Raised when an authorized input artifact record or blob is missing."""


_INPUT_SPEC = OwnedArtifactStorageSpec(
    root_dir_name="egg_inputs",
    id_field="input_id",
    id_label="input_id",
    artifact_label="input artifact",
    metadata_schema_version=INPUT_METADATA_SCHEMA_VERSION,
    default_provenance_kind="bytes",
    error_cls=InputArtifactError,
    access_error_cls=InputArtifactAccessError,
    not_found_error_cls=InputArtifactNotFoundError,
)


@dataclass(frozen=True)
class SavedInputArtifact:
    input_id: str
    metadata: dict[str, Any]
    record_dir: Path
    metadata_path: Path
    blob_path: Path


def input_root_relative_dir() -> Path:
    return artifact_root_relative_dir(_INPUT_SPEC)


def input_root_dir(workspace: Path | str | None = None) -> Path:
    return artifact_root_dir(_INPUT_SPEC, workspace)


def thread_input_relative_dir(thread_id: str) -> Path:
    return thread_artifact_relative_dir(_INPUT_SPEC, thread_id)


def thread_input_dir(workspace: Path | str | None, thread_id: str) -> Path:
    return thread_artifact_dir(_INPUT_SPEC, workspace, thread_id)


def validate_input_id(input_id: str) -> str:
    return validate_artifact_id(_INPUT_SPEC, input_id)


def input_record_dir(workspace: Path | str | None, thread_id: str, input_id: str) -> Path:
    return artifact_record_dir(_INPUT_SPEC, workspace, thread_id, input_id)


def _random_input_id() -> str:
    # Kept as a monkeypatch seam for focused collision tests.
    from .owned_byte_artifacts import default_random_artifact_id

    return default_random_artifact_id()


def _blob_path_for_sha256(workspace: Path | str | None, sha256: str) -> Path:
    return blob_path_for_sha256(_INPUT_SPEC, workspace, sha256)


def save_input_bytes(
    workspace: Path | str | None,
    thread_id: str,
    data: bytes | bytearray | memoryview,
    *,
    filename: str | None = None,
    mime_type: str | None = None,
    presentation: str | None = None,
    provenance: Mapping[str, Any] | None = None,
    derived: Mapping[str, Any] | None = None,
    provider_refs: Mapping[str, Any] | None = None,
) -> SavedInputArtifact:
    """Save bytes as a thread-owned input artifact.

    Raw bytes are deduplicated into ``.egg/egg_inputs/_blobs/sha256/...``.
    Authorization is represented by the per-thread metadata record created under
    ``.egg/egg_inputs/<safe_thread_id>/<input_id>/metadata.json``.
    """

    saved = save_owned_artifact_bytes(
        _INPUT_SPEC,
        workspace,
        thread_id,
        data,
        filename=filename,
        mime_type=mime_type,
        presentation=presentation,
        provenance=provenance,
        derived=derived,
        provider_refs=provider_refs,
        random_id_func=_random_input_id,
    )
    return SavedInputArtifact(
        input_id=saved.artifact_id,
        metadata=saved.metadata,
        record_dir=saved.record_dir,
        metadata_path=saved.metadata_path,
        blob_path=saved.blob_path,
    )


def resolve_input_owner_thread_id(
    db: Any,
    calling_thread_id: str,
    *,
    descendant_thread_id: str | None = None,
) -> str:
    """Resolve the input namespace a caller may access.

    Without ``descendant_thread_id`` the caller can only access its own thread
    namespace.  With ``descendant_thread_id``, the selected thread must be a
    strict descendant of the caller according to the thread DB.
    """

    return resolve_owned_artifact_owner_thread_id(
        _INPUT_SPEC,
        db,
        calling_thread_id,
        descendant_thread_id=descendant_thread_id,
    )


def resolve_input_metadata(
    workspace: Path | str | None,
    db: Any,
    calling_thread_id: str,
    input_id: str,
    *,
    descendant_thread_id: str | None = None,
) -> dict[str, Any]:
    """Resolve metadata for an owned or explicitly-authorized input artifact."""

    return resolve_owned_artifact_metadata(
        _INPUT_SPEC,
        workspace,
        db,
        calling_thread_id,
        input_id,
        descendant_thread_id=descendant_thread_id,
    )


def resolve_input_bytes(
    workspace: Path | str | None,
    db: Any,
    calling_thread_id: str,
    input_id: str,
    *,
    descendant_thread_id: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Resolve metadata and bytes for an owned or authorized input artifact."""

    return resolve_owned_artifact_bytes(
        _INPUT_SPEC,
        workspace,
        db,
        calling_thread_id,
        input_id,
        descendant_thread_id=descendant_thread_id,
    )


__all__ = [
    "INPUT_HASH_ALGORITHM",
    "INPUT_ID_LENGTH",
    "INPUT_METADATA_SCHEMA_VERSION",
    "InputArtifactAccessError",
    "InputArtifactError",
    "InputArtifactNotFoundError",
    "SavedInputArtifact",
    "input_record_dir",
    "input_root_dir",
    "input_root_relative_dir",
    "resolve_input_bytes",
    "resolve_input_metadata",
    "resolve_input_owner_thread_id",
    "save_input_bytes",
    "thread_input_dir",
    "thread_input_relative_dir",
    "validate_input_id",
]
