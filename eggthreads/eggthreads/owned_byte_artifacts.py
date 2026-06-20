from __future__ import annotations

"""Shared helpers for thread-owned durable binary artifact namespaces.

This internal module backs namespaces such as ``.egg/egg_inputs`` and
``.egg/egg_provider_output``.  Authorization is represented by per-thread
metadata records; content-addressed blob paths are deduplicated storage only and
must never be treated as authority to read bytes.
"""

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .output_paths import ARTIFACT_ID_LENGTH, _ARTIFACT_ID_ALPHABET, _random_artifact_id, safe_thread_dir_name


ARTIFACT_HASH_ALGORITHM = "sha256"


@dataclass(frozen=True)
class OwnedArtifactStorageSpec:
    root_dir_name: str
    id_field: str
    id_label: str
    artifact_label: str
    metadata_schema_version: int
    default_provenance_kind: str
    error_cls: type[Exception]
    access_error_cls: type[Exception]
    not_found_error_cls: type[Exception]


@dataclass(frozen=True)
class SavedOwnedArtifact:
    artifact_id: str
    metadata: dict[str, Any]
    record_dir: Path
    metadata_path: Path
    blob_path: Path


def _raise(error_cls: type[Exception], message: str) -> None:
    raise error_cls(message)


def artifact_root_relative_dir(spec: OwnedArtifactStorageSpec) -> Path:
    return Path(".egg", spec.root_dir_name)


def artifact_root_dir(spec: OwnedArtifactStorageSpec, workspace: Path | str | None = None) -> Path:
    base = Path.cwd() if workspace is None else Path(workspace)
    return base.resolve() / artifact_root_relative_dir(spec)


def thread_artifact_relative_dir(spec: OwnedArtifactStorageSpec, thread_id: str) -> Path:
    return artifact_root_relative_dir(spec) / safe_thread_dir_name(thread_id)


def thread_artifact_dir(spec: OwnedArtifactStorageSpec, workspace: Path | str | None, thread_id: str) -> Path:
    base = Path.cwd() if workspace is None else Path(workspace)
    return base.resolve() / thread_artifact_relative_dir(spec, thread_id)


def validate_artifact_id(spec: OwnedArtifactStorageSpec, artifact_id: str) -> str:
    text = str(artifact_id or "").strip()
    if len(text) != ARTIFACT_ID_LENGTH or any(ch not in _ARTIFACT_ID_ALPHABET for ch in text):
        raise ValueError(f"{spec.id_label} must be a {ARTIFACT_ID_LENGTH}-character lower-case alphanumeric id.")
    return text


def artifact_record_dir(
    spec: OwnedArtifactStorageSpec,
    workspace: Path | str | None,
    thread_id: str,
    artifact_id: str,
) -> Path:
    return thread_artifact_dir(spec, workspace, thread_id) / validate_artifact_id(spec, artifact_id)


def default_random_artifact_id() -> str:
    return _random_artifact_id()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _chmod_best_effort(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except Exception:
        pass


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _chmod_best_effort(path, 0o700)


def _ensure_artifact_root(spec: OwnedArtifactStorageSpec, workspace: Path | str | None) -> Path:
    root = artifact_root_dir(spec, workspace)
    _mkdir_private(root)
    return root


def _validate_thread_id(thread_id: str) -> str:
    text = str(thread_id or "").strip()
    if not text:
        raise ValueError("thread_id is required.")
    return text


def _validate_sha256(spec: OwnedArtifactStorageSpec, sha256: str) -> str:
    text = str(sha256 or "").strip().lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        _raise(spec.error_cls, f"{spec.artifact_label} metadata has an invalid sha256.")
    return text


def _blob_prefix_dir(spec: OwnedArtifactStorageSpec, root: Path, sha256: str) -> Path:
    sha256 = _validate_sha256(spec, sha256)
    return root / "_blobs" / ARTIFACT_HASH_ALGORITHM / sha256[:2]


def blob_path_for_sha256(spec: OwnedArtifactStorageSpec, workspace: Path | str | None, sha256: str) -> Path:
    root = artifact_root_dir(spec, workspace)
    sha256 = _validate_sha256(spec, sha256)
    return _blob_prefix_dir(spec, root, sha256) / sha256


def _hash_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private_bytes_exclusive(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except Exception:
                pass
        try:
            path.unlink()
        except Exception:
            pass
        raise
    _chmod_best_effort(path, 0o600)


def _ensure_blob(spec: OwnedArtifactStorageSpec, workspace: Path | str | None, sha256: str, data: bytes) -> Path:
    root = _ensure_artifact_root(spec, workspace)
    prefix_dir = _blob_prefix_dir(spec, root, sha256)
    _mkdir_private(root / "_blobs")
    _mkdir_private(root / "_blobs" / ARTIFACT_HASH_ALGORITHM)
    _mkdir_private(prefix_dir)
    blob_path = prefix_dir / sha256

    def verify_existing() -> None:
        if not blob_path.is_file():
            _raise(spec.error_cls, f"{spec.artifact_label} blob path is not a regular file: {blob_path}")
        try:
            size = blob_path.stat().st_size
        except OSError as e:
            raise spec.error_cls(f"could not stat {spec.artifact_label} blob: {e}") from e
        if size != len(data) or _hash_file_sha256(blob_path) != sha256:
            _raise(spec.error_cls, f"existing {spec.artifact_label} blob does not match its sha256 path.")
        _chmod_best_effort(blob_path, 0o600)

    if blob_path.exists():
        verify_existing()
        return blob_path

    try:
        _write_private_bytes_exclusive(blob_path, data)
    except FileExistsError:
        verify_existing()
    return blob_path


def _create_thread_record_dir(
    spec: OwnedArtifactStorageSpec,
    workspace: Path | str | None,
    thread_id: str,
    *,
    random_id_func: Callable[[], str] | None = None,
) -> tuple[str, Path]:
    thread_id = _validate_thread_id(thread_id)
    _ensure_artifact_root(spec, workspace)
    base_dir = thread_artifact_dir(spec, workspace, thread_id)
    _mkdir_private(base_dir)
    make_id = random_id_func or default_random_artifact_id

    for _ in range(128):
        artifact_id = validate_artifact_id(spec, make_id())
        record_dir = base_dir / artifact_id
        try:
            record_dir.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            continue
        _chmod_best_effort(record_dir, 0o700)
        return artifact_id, record_dir

    raise FileExistsError(f"could not allocate a unique {spec.id_label} under {base_dir}")


def _display_filename(filename: str | None) -> str | None:
    if filename is None:
        return None
    text = str(filename).strip()
    if not text:
        return None
    return text.replace("\\", "/").rsplit("/", 1)[-1] or None


def _mapping_or_empty(value: Mapping[str, Any] | None, *, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping.")
    return dict(value)


def _metadata_blob_relpath(record_dir: Path, blob_path: Path) -> str:
    return os.path.relpath(blob_path, start=record_dir).replace(os.sep, "/")


def _write_metadata(metadata_path: Path, metadata: Mapping[str, Any]) -> None:
    data = (json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _write_private_bytes_exclusive(metadata_path, data)


def save_owned_artifact_bytes(
    spec: OwnedArtifactStorageSpec,
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
    random_id_func: Callable[[], str] | None = None,
) -> SavedOwnedArtifact:
    """Save bytes in a thread-owned artifact namespace."""

    thread_id = _validate_thread_id(thread_id)
    try:
        data_bytes = bytes(data)
    except Exception as e:
        raise TypeError("data must be bytes-like.") from e

    sha256 = hashlib.sha256(data_bytes).hexdigest()
    blob_path = _ensure_blob(spec, workspace, sha256, data_bytes)
    artifact_id, record_dir = _create_thread_record_dir(
        spec,
        workspace,
        thread_id,
        random_id_func=random_id_func,
    )

    metadata = {
        "schema_version": spec.metadata_schema_version,
        spec.id_field: artifact_id,
        "owner_thread_id": thread_id,
        "created_at": _utcnow_iso(),
        "filename": _display_filename(filename),
        "mime_type": str(mime_type or "application/octet-stream").strip().lower() or "application/octet-stream",
        "presentation": str(presentation or "file").strip().lower() or "file",
        "size_bytes": len(data_bytes),
        "sha256": sha256,
        "blob_relpath": _metadata_blob_relpath(record_dir, blob_path),
        "provenance": _mapping_or_empty(provenance, name="provenance") or {"kind": spec.default_provenance_kind},
        "derived": _mapping_or_empty(derived, name="derived"),
        "provider_refs": _mapping_or_empty(provider_refs, name="provider_refs"),
    }
    metadata_path = record_dir / "metadata.json"
    try:
        _write_metadata(metadata_path, metadata)
    except Exception:
        try:
            record_dir.rmdir()
        except Exception:
            pass
        raise
    _chmod_best_effort(record_dir, 0o700)
    return SavedOwnedArtifact(
        artifact_id=artifact_id,
        metadata=metadata,
        record_dir=record_dir,
        metadata_path=metadata_path,
        blob_path=blob_path,
    )


def resolve_owned_artifact_owner_thread_id(
    spec: OwnedArtifactStorageSpec,
    db: Any,
    calling_thread_id: str,
    *,
    descendant_thread_id: str | None = None,
) -> str:
    """Resolve the artifact namespace a caller may access."""

    caller = _validate_thread_id(calling_thread_id)
    descendant = str(descendant_thread_id or "").strip()
    if not descendant:
        return caller
    if db is None:
        _raise(spec.access_error_cls, "access denied: descendant_thread_id requires a thread database.")
    try:
        from .api import is_descendant_thread

        allowed = is_descendant_thread(db, caller, descendant)
    except Exception:
        allowed = False
    if not allowed:
        _raise(spec.access_error_cls, "access denied: descendant_thread_id is not a descendant of the calling thread.")
    return descendant


def _load_metadata(spec: OwnedArtifactStorageSpec, metadata_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise spec.not_found_error_cls(f"{spec.artifact_label} not found.") from e
    except Exception as e:
        raise spec.error_cls(f"could not read {spec.artifact_label} metadata: {e}") from e
    if not isinstance(data, dict):
        _raise(spec.error_cls, f"{spec.artifact_label} metadata is invalid.")
    return data


def _validate_resolved_metadata(
    spec: OwnedArtifactStorageSpec,
    metadata: dict[str, Any],
    *,
    expected_artifact_id: str,
    expected_owner_thread_id: str,
    record_dir: Path,
    workspace: Path | str | None,
) -> dict[str, Any]:
    if metadata.get("schema_version") != spec.metadata_schema_version:
        _raise(spec.error_cls, f"{spec.artifact_label} metadata has an unsupported schema_version.")
    if metadata.get(spec.id_field) != expected_artifact_id:
        _raise(spec.error_cls, f"{spec.artifact_label} metadata {spec.id_field} does not match the requested id.")
    if metadata.get("owner_thread_id") != expected_owner_thread_id:
        _raise(spec.access_error_cls, f"access denied: {spec.artifact_label} owner does not match the selected thread.")

    created_at = metadata.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        _raise(spec.error_cls, f"{spec.artifact_label} metadata has an invalid created_at.")
    size = metadata.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        _raise(spec.error_cls, f"{spec.artifact_label} metadata has an invalid size_bytes.")
    sha256 = _validate_sha256(spec, str(metadata.get("sha256") or ""))
    metadata["sha256"] = sha256

    blob_path = blob_path_for_sha256(spec, workspace, sha256)
    expected_relpath = _metadata_blob_relpath(record_dir, blob_path)
    if metadata.get("blob_relpath") != expected_relpath:
        _raise(spec.error_cls, f"{spec.artifact_label} metadata blob_relpath does not match its sha256 path.")

    for key in ("filename", "mime_type", "presentation", "provenance", "derived", "provider_refs"):
        if key not in metadata:
            _raise(spec.error_cls, f"{spec.artifact_label} metadata is missing {key}.")
    if metadata.get("filename") is not None and not isinstance(metadata.get("filename"), str):
        _raise(spec.error_cls, f"{spec.artifact_label} metadata has an invalid filename.")
    if not isinstance(metadata.get("mime_type"), str) or not metadata.get("mime_type"):
        _raise(spec.error_cls, f"{spec.artifact_label} metadata has an invalid mime_type.")
    if not isinstance(metadata.get("presentation"), str) or not metadata.get("presentation"):
        _raise(spec.error_cls, f"{spec.artifact_label} metadata has an invalid presentation.")
    for key in ("provenance", "derived", "provider_refs"):
        if not isinstance(metadata.get(key), dict):
            _raise(spec.error_cls, f"{spec.artifact_label} metadata has an invalid {key}.")
    return metadata


def resolve_owned_artifact_metadata(
    spec: OwnedArtifactStorageSpec,
    workspace: Path | str | None,
    db: Any,
    calling_thread_id: str,
    artifact_id: str,
    *,
    descendant_thread_id: str | None = None,
) -> dict[str, Any]:
    """Resolve metadata for an owned or explicitly-authorized artifact."""

    safe_artifact_id = validate_artifact_id(spec, artifact_id)
    owner_thread_id = resolve_owned_artifact_owner_thread_id(
        spec,
        db,
        calling_thread_id,
        descendant_thread_id=descendant_thread_id,
    )
    record_dir = artifact_record_dir(spec, workspace, owner_thread_id, safe_artifact_id)
    metadata = _load_metadata(spec, record_dir / "metadata.json")
    return _validate_resolved_metadata(
        spec,
        metadata,
        expected_artifact_id=safe_artifact_id,
        expected_owner_thread_id=owner_thread_id,
        record_dir=record_dir,
        workspace=workspace,
    )


def resolve_owned_artifact_bytes(
    spec: OwnedArtifactStorageSpec,
    workspace: Path | str | None,
    db: Any,
    calling_thread_id: str,
    artifact_id: str,
    *,
    descendant_thread_id: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Resolve metadata and bytes for an owned or authorized artifact."""

    metadata = resolve_owned_artifact_metadata(
        spec,
        workspace,
        db,
        calling_thread_id,
        artifact_id,
        descendant_thread_id=descendant_thread_id,
    )
    blob_path = blob_path_for_sha256(spec, workspace, metadata["sha256"])
    try:
        data = blob_path.read_bytes()
    except FileNotFoundError as e:
        raise spec.not_found_error_cls(f"{spec.artifact_label} blob not found.") from e
    except Exception as e:
        raise spec.error_cls(f"could not read {spec.artifact_label} blob: {e}") from e
    if len(data) != metadata["size_bytes"] or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
        _raise(spec.error_cls, f"{spec.artifact_label} blob content does not match metadata.")
    return metadata, data


__all__ = [
    "ARTIFACT_HASH_ALGORITHM",
    "OwnedArtifactStorageSpec",
    "SavedOwnedArtifact",
    "artifact_record_dir",
    "artifact_root_dir",
    "artifact_root_relative_dir",
    "blob_path_for_sha256",
    "default_random_artifact_id",
    "resolve_owned_artifact_bytes",
    "resolve_owned_artifact_metadata",
    "resolve_owned_artifact_owner_thread_id",
    "save_owned_artifact_bytes",
    "thread_artifact_dir",
    "thread_artifact_relative_dir",
    "validate_artifact_id",
]
