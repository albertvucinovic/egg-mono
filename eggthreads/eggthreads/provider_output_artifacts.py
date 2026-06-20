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
from typing import TYPE_CHECKING, Any, Mapping

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

if TYPE_CHECKING:
    from .input_artifacts import SavedInputArtifact


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


def promote_provider_output_to_input(
    workspace: Path | str | None,
    db: Any,
    calling_thread_id: str,
    artifact_id: str,
    *,
    descendant_thread_id: str | None = None,
) -> tuple["SavedInputArtifact", dict[str, Any]]:
    """Promote a provider-output artifact into the caller's input namespace.

    Provider-output artifacts are not automatically reusable as model inputs.
    This helper performs the explicit, access-checked copy into
    ``.egg/egg_inputs/<calling_thread_id>/...`` and returns both the saved input
    record and its canonical ``attachment`` content part.

    The source artifact is resolved with the normal provider-output access
    rules: own thread directly, or a descendant only when selected with
    ``descendant_thread_id``.  The promoted input is always owned by the calling
    thread for this slice; this avoids implicit cross-thread writes.
    """

    source_metadata, data = resolve_provider_output_bytes(
        workspace,
        db,
        calling_thread_id,
        artifact_id,
        descendant_thread_id=descendant_thread_id,
    )
    source_artifact_id = str(source_metadata.get("artifact_id") or "")
    source_owner_thread_id = str(source_metadata.get("owner_thread_id") or "")
    source_sha256 = str(source_metadata.get("sha256") or "")
    source_provenance = dict(source_metadata.get("provenance") or {})
    source_provider_refs = dict(source_metadata.get("provider_refs") or {})

    provenance = {
        "kind": "provider_output_promotion",
        "source_artifact_id": source_artifact_id,
        "source_owner_thread_id": source_owner_thread_id,
        "source_sha256": source_sha256,
        "source_filename": source_metadata.get("filename"),
        "source_mime_type": source_metadata.get("mime_type"),
        "source_presentation": source_metadata.get("presentation"),
        "source_provenance": source_provenance,
        "source_provider_refs": source_provider_refs,
    }
    provider_refs = {
        "source_provider_output": {
            "artifact_id": source_artifact_id,
            "owner_thread_id": source_owner_thread_id,
            "sha256": source_sha256,
        },
        "source_provider_refs": source_provider_refs,
    }

    # Local imports avoid a module import cycle: content_parts imports this
    # module for artifact-id validation.
    from .content_parts import attachment_part_from_input_metadata
    from .input_artifacts import save_input_bytes

    saved = save_input_bytes(
        workspace,
        calling_thread_id,
        data,
        filename=source_metadata.get("filename"),
        mime_type=source_metadata.get("mime_type"),
        presentation=source_metadata.get("presentation"),
        provenance=provenance,
        derived=dict(source_metadata.get("derived") or {}),
        provider_refs=provider_refs,
    )
    if saved.metadata.get("sha256") != source_sha256 or saved.metadata.get("size_bytes") != source_metadata.get("size_bytes"):
        raise ProviderOutputArtifactError("promoted input metadata does not match source provider-output bytes.")
    return saved, attachment_part_from_input_metadata(saved.metadata)


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
    "promote_provider_output_to_input",
    "resolve_provider_output_bytes",
    "resolve_provider_output_metadata",
    "resolve_provider_output_owner_thread_id",
    "save_provider_output_bytes",
    "thread_provider_output_dir",
    "thread_provider_output_relative_dir",
    "validate_provider_output_artifact_id",
]
