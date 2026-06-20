from __future__ import annotations

"""Durable storage helpers for provider-produced binary artifacts.

Provider output artifacts are thread-owned records under
``.egg/egg_provider_output``.  They are distinct from local tool outputs
(``.egg/egg_outputs``) and provider inputs (``.egg/egg_inputs``).  Raw bytes may
be content-address deduplicated, but only per-thread metadata records authorize
reads.
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


PROVIDER_OUTPUT_ARTIFACT_ID_LENGTH = 8
PROVIDER_OUTPUT_METADATA_SCHEMA_VERSION = 1
PROVIDER_OUTPUT_HASH_ALGORITHM = ARTIFACT_HASH_ALGORITHM


class ProviderOutputArtifactError(RuntimeError):
    """Base class for provider output artifact storage/metadata errors."""


class ProviderOutputArtifactAccessError(PermissionError):
    """Raised when a caller is not authorized for a provider output namespace."""


class ProviderOutputArtifactNotFoundError(FileNotFoundError):
    """Raised when an authorized provider output record or blob is missing."""


_PROVIDER_OUTPUT_SPEC = OwnedArtifactStorageSpec(
    root_dir_name="egg_provider_output",
    id_field="artifact_id",
    id_label="artifact_id",
    artifact_label="provider output artifact",
    metadata_schema_version=PROVIDER_OUTPUT_METADATA_SCHEMA_VERSION,
    default_provenance_kind="provider_output",
    error_cls=ProviderOutputArtifactError,
    access_error_cls=ProviderOutputArtifactAccessError,
    not_found_error_cls=ProviderOutputArtifactNotFoundError,
)


@dataclass(frozen=True)
class SavedProviderOutputArtifact:
    artifact_id: str
    metadata: dict[str, Any]
    record_dir: Path
    metadata_path: Path
    blob_path: Path


def provider_output_root_relative_dir() -> Path:
    return artifact_root_relative_dir(_PROVIDER_OUTPUT_SPEC)


def provider_output_root_dir(workspace: Path | str | None = None) -> Path:
    return artifact_root_dir(_PROVIDER_OUTPUT_SPEC, workspace)


def thread_provider_output_relative_dir(thread_id: str) -> Path:
    return thread_artifact_relative_dir(_PROVIDER_OUTPUT_SPEC, thread_id)


def thread_provider_output_dir(workspace: Path | str | None, thread_id: str) -> Path:
    return thread_artifact_dir(_PROVIDER_OUTPUT_SPEC, workspace, thread_id)


def validate_provider_output_artifact_id(artifact_id: str) -> str:
    return validate_artifact_id(_PROVIDER_OUTPUT_SPEC, artifact_id)


def provider_output_record_dir(workspace: Path | str | None, thread_id: str, artifact_id: str) -> Path:
    return artifact_record_dir(_PROVIDER_OUTPUT_SPEC, workspace, thread_id, artifact_id)


def _random_provider_output_artifact_id() -> str:
    # Kept as a monkeypatch seam for focused collision tests.
    from .owned_byte_artifacts import default_random_artifact_id

    return default_random_artifact_id()


def _provider_output_blob_path_for_sha256(workspace: Path | str | None, sha256: str) -> Path:
    return blob_path_for_sha256(_PROVIDER_OUTPUT_SPEC, workspace, sha256)


def save_provider_output_bytes(
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
) -> SavedProviderOutputArtifact:
    """Save provider-produced bytes as a thread-owned artifact.

    Raw bytes are deduplicated into
    ``.egg/egg_provider_output/_blobs/sha256/...``.  Authorization is represented
    by the per-thread metadata record under
    ``.egg/egg_provider_output/<safe_thread_id>/<artifact_id>/metadata.json``.
    """

    saved = save_owned_artifact_bytes(
        _PROVIDER_OUTPUT_SPEC,
        workspace,
        thread_id,
        data,
        filename=filename,
        mime_type=mime_type,
        presentation=presentation,
        provenance=provenance,
        derived=derived,
        provider_refs=provider_refs,
        random_id_func=_random_provider_output_artifact_id,
    )
    return SavedProviderOutputArtifact(
        artifact_id=saved.artifact_id,
        metadata=saved.metadata,
        record_dir=saved.record_dir,
        metadata_path=saved.metadata_path,
        blob_path=saved.blob_path,
    )


def resolve_provider_output_owner_thread_id(
    db: Any,
    calling_thread_id: str,
    *,
    descendant_thread_id: str | None = None,
) -> str:
    """Resolve the provider-output namespace a caller may access."""

    return resolve_owned_artifact_owner_thread_id(
        _PROVIDER_OUTPUT_SPEC,
        db,
        calling_thread_id,
        descendant_thread_id=descendant_thread_id,
    )


def resolve_provider_output_metadata(
    workspace: Path | str | None,
    db: Any,
    calling_thread_id: str,
    artifact_id: str,
    *,
    descendant_thread_id: str | None = None,
) -> dict[str, Any]:
    """Resolve metadata for an owned or explicitly-authorized provider output artifact."""

    return resolve_owned_artifact_metadata(
        _PROVIDER_OUTPUT_SPEC,
        workspace,
        db,
        calling_thread_id,
        artifact_id,
        descendant_thread_id=descendant_thread_id,
    )


def resolve_provider_output_bytes(
    workspace: Path | str | None,
    db: Any,
    calling_thread_id: str,
    artifact_id: str,
    *,
    descendant_thread_id: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Resolve metadata and bytes for an owned or authorized provider output artifact."""

    return resolve_owned_artifact_bytes(
        _PROVIDER_OUTPUT_SPEC,
        workspace,
        db,
        calling_thread_id,
        artifact_id,
        descendant_thread_id=descendant_thread_id,
    )


__all__ = [
    "PROVIDER_OUTPUT_ARTIFACT_ID_LENGTH",
    "PROVIDER_OUTPUT_HASH_ALGORITHM",
    "PROVIDER_OUTPUT_METADATA_SCHEMA_VERSION",
    "ProviderOutputArtifactAccessError",
    "ProviderOutputArtifactError",
    "ProviderOutputArtifactNotFoundError",
    "SavedProviderOutputArtifact",
    "provider_output_record_dir",
    "provider_output_root_dir",
    "provider_output_root_relative_dir",
    "resolve_provider_output_bytes",
    "resolve_provider_output_metadata",
    "resolve_provider_output_owner_thread_id",
    "save_provider_output_bytes",
    "thread_provider_output_dir",
    "thread_provider_output_relative_dir",
    "validate_provider_output_artifact_id",
]
