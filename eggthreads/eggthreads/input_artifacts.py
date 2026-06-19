from __future__ import annotations

"""Durable storage helpers for provider input artifacts.

Input artifacts are thread-owned records under ``.egg/egg_inputs`` with raw
bytes deduplicated through a global content-addressed blob store.  The blob
store is not an authorization boundary: callers must resolve bytes through an
owned or explicitly-authorized input metadata record.
"""

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .output_paths import ARTIFACT_ID_LENGTH, _ARTIFACT_ID_ALPHABET, _random_artifact_id, safe_thread_dir_name


INPUT_ID_LENGTH = ARTIFACT_ID_LENGTH
_INPUT_ID_ALPHABET = _ARTIFACT_ID_ALPHABET
INPUT_METADATA_SCHEMA_VERSION = 1
INPUT_HASH_ALGORITHM = "sha256"


class InputArtifactError(RuntimeError):
    """Base class for input artifact storage/metadata errors."""


class InputArtifactAccessError(PermissionError):
    """Raised when a caller is not authorized for an input artifact namespace."""


class InputArtifactNotFoundError(FileNotFoundError):
    """Raised when an authorized input artifact record or blob is missing."""


@dataclass(frozen=True)
class SavedInputArtifact:
    input_id: str
    metadata: dict[str, Any]
    record_dir: Path
    metadata_path: Path
    blob_path: Path


def input_root_relative_dir() -> Path:
    return Path(".egg", "egg_inputs")


def input_root_dir(workspace: Path | str | None = None) -> Path:
    base = Path.cwd() if workspace is None else Path(workspace)
    return base.resolve() / input_root_relative_dir()


def thread_input_relative_dir(thread_id: str) -> Path:
    return input_root_relative_dir() / safe_thread_dir_name(thread_id)


def thread_input_dir(workspace: Path | str | None, thread_id: str) -> Path:
    base = Path.cwd() if workspace is None else Path(workspace)
    return base.resolve() / thread_input_relative_dir(thread_id)


def validate_input_id(input_id: str) -> str:
    text = str(input_id or "").strip()
    if len(text) != INPUT_ID_LENGTH or any(ch not in _INPUT_ID_ALPHABET for ch in text):
        raise ValueError(f"input_id must be a {INPUT_ID_LENGTH}-character lower-case alphanumeric id.")
    return text


def input_record_dir(workspace: Path | str | None, thread_id: str, input_id: str) -> Path:
    return thread_input_dir(workspace, thread_id) / validate_input_id(input_id)


def _random_input_id() -> str:
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


def _ensure_input_root(workspace: Path | str | None) -> Path:
    root = input_root_dir(workspace)
    _mkdir_private(root)
    return root


def _validate_thread_id(thread_id: str) -> str:
    text = str(thread_id or "").strip()
    if not text:
        raise ValueError("thread_id is required.")
    return text


def _validate_sha256(sha256: str) -> str:
    text = str(sha256 or "").strip().lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise InputArtifactError("input artifact metadata has an invalid sha256.")
    return text


def _blob_prefix_dir(root: Path, sha256: str) -> Path:
    sha256 = _validate_sha256(sha256)
    return root / "_blobs" / INPUT_HASH_ALGORITHM / sha256[:2]


def _blob_path_for_sha256(workspace: Path | str | None, sha256: str) -> Path:
    root = input_root_dir(workspace)
    sha256 = _validate_sha256(sha256)
    return _blob_prefix_dir(root, sha256) / sha256


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


def _ensure_blob(workspace: Path | str | None, sha256: str, data: bytes) -> Path:
    root = _ensure_input_root(workspace)
    prefix_dir = _blob_prefix_dir(root, sha256)
    _mkdir_private(root / "_blobs")
    _mkdir_private(root / "_blobs" / INPUT_HASH_ALGORITHM)
    _mkdir_private(prefix_dir)
    blob_path = prefix_dir / sha256

    def verify_existing() -> None:
        if not blob_path.is_file():
            raise InputArtifactError(f"input artifact blob path is not a regular file: {blob_path}")
        try:
            size = blob_path.stat().st_size
        except OSError as e:
            raise InputArtifactError(f"could not stat input artifact blob: {e}") from e
        if size != len(data) or _hash_file_sha256(blob_path) != sha256:
            raise InputArtifactError("existing input artifact blob does not match its sha256 path.")
        _chmod_best_effort(blob_path, 0o600)

    if blob_path.exists():
        verify_existing()
        return blob_path

    try:
        _write_private_bytes_exclusive(blob_path, data)
    except FileExistsError:
        verify_existing()
    return blob_path


def _create_thread_input_dir(workspace: Path | str | None, thread_id: str) -> tuple[str, Path]:
    thread_id = _validate_thread_id(thread_id)
    _ensure_input_root(workspace)
    base_dir = thread_input_dir(workspace, thread_id)
    _mkdir_private(base_dir)

    for _ in range(128):
        input_id = validate_input_id(_random_input_id())
        record_dir = base_dir / input_id
        try:
            record_dir.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            continue
        _chmod_best_effort(record_dir, 0o700)
        return input_id, record_dir

    raise FileExistsError(f"could not allocate a unique input artifact id under {base_dir}")


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

    thread_id = _validate_thread_id(thread_id)
    try:
        data_bytes = bytes(data)
    except Exception as e:
        raise TypeError("data must be bytes-like.") from e

    sha256 = hashlib.sha256(data_bytes).hexdigest()
    blob_path = _ensure_blob(workspace, sha256, data_bytes)
    input_id, record_dir = _create_thread_input_dir(workspace, thread_id)

    metadata = {
        "schema_version": INPUT_METADATA_SCHEMA_VERSION,
        "input_id": input_id,
        "owner_thread_id": thread_id,
        "created_at": _utcnow_iso(),
        "filename": _display_filename(filename),
        "mime_type": str(mime_type or "application/octet-stream").strip().lower() or "application/octet-stream",
        "presentation": str(presentation or "file").strip().lower() or "file",
        "size_bytes": len(data_bytes),
        "sha256": sha256,
        "blob_relpath": _metadata_blob_relpath(record_dir, blob_path),
        "provenance": _mapping_or_empty(provenance, name="provenance") or {"kind": "bytes"},
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
    return SavedInputArtifact(
        input_id=input_id,
        metadata=metadata,
        record_dir=record_dir,
        metadata_path=metadata_path,
        blob_path=blob_path,
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

    caller = _validate_thread_id(calling_thread_id)
    descendant = str(descendant_thread_id or "").strip()
    if not descendant:
        return caller
    if db is None:
        raise InputArtifactAccessError("access denied: descendant_thread_id requires a thread database.")
    try:
        from .api import is_descendant_thread

        allowed = is_descendant_thread(db, caller, descendant)
    except Exception:
        allowed = False
    if not allowed:
        raise InputArtifactAccessError("access denied: descendant_thread_id is not a descendant of the calling thread.")
    return descendant


def _load_metadata(metadata_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise InputArtifactNotFoundError("input artifact not found.") from e
    except Exception as e:
        raise InputArtifactError(f"could not read input artifact metadata: {e}") from e
    if not isinstance(data, dict):
        raise InputArtifactError("input artifact metadata is invalid.")
    return data


def _validate_resolved_metadata(
    metadata: dict[str, Any],
    *,
    expected_input_id: str,
    expected_owner_thread_id: str,
    record_dir: Path,
    workspace: Path | str | None,
) -> dict[str, Any]:
    if metadata.get("schema_version") != INPUT_METADATA_SCHEMA_VERSION:
        raise InputArtifactError("input artifact metadata has an unsupported schema_version.")
    if metadata.get("input_id") != expected_input_id:
        raise InputArtifactError("input artifact metadata input_id does not match the requested id.")
    if metadata.get("owner_thread_id") != expected_owner_thread_id:
        raise InputArtifactAccessError("access denied: input artifact owner does not match the selected thread.")

    size = metadata.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise InputArtifactError("input artifact metadata has an invalid size_bytes.")
    sha256 = _validate_sha256(str(metadata.get("sha256") or ""))
    metadata["sha256"] = sha256

    blob_path = _blob_path_for_sha256(workspace, sha256)
    expected_relpath = _metadata_blob_relpath(record_dir, blob_path)
    if metadata.get("blob_relpath") != expected_relpath:
        raise InputArtifactError("input artifact metadata blob_relpath does not match its sha256 path.")

    for key in ("filename", "mime_type", "presentation", "provenance", "derived", "provider_refs"):
        if key not in metadata:
            raise InputArtifactError(f"input artifact metadata is missing {key}.")
    if metadata.get("filename") is not None and not isinstance(metadata.get("filename"), str):
        raise InputArtifactError("input artifact metadata has an invalid filename.")
    if not isinstance(metadata.get("mime_type"), str) or not metadata.get("mime_type"):
        raise InputArtifactError("input artifact metadata has an invalid mime_type.")
    if not isinstance(metadata.get("presentation"), str) or not metadata.get("presentation"):
        raise InputArtifactError("input artifact metadata has an invalid presentation.")
    for key in ("provenance", "derived", "provider_refs"):
        if not isinstance(metadata.get(key), dict):
            raise InputArtifactError(f"input artifact metadata has an invalid {key}.")
    return metadata


def resolve_input_metadata(
    workspace: Path | str | None,
    db: Any,
    calling_thread_id: str,
    input_id: str,
    *,
    descendant_thread_id: str | None = None,
) -> dict[str, Any]:
    """Resolve metadata for an owned or explicitly-authorized input artifact."""

    safe_input_id = validate_input_id(input_id)
    owner_thread_id = resolve_input_owner_thread_id(db, calling_thread_id, descendant_thread_id=descendant_thread_id)
    record_dir = input_record_dir(workspace, owner_thread_id, safe_input_id)
    metadata = _load_metadata(record_dir / "metadata.json")
    return _validate_resolved_metadata(
        metadata,
        expected_input_id=safe_input_id,
        expected_owner_thread_id=owner_thread_id,
        record_dir=record_dir,
        workspace=workspace,
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

    metadata = resolve_input_metadata(
        workspace,
        db,
        calling_thread_id,
        input_id,
        descendant_thread_id=descendant_thread_id,
    )
    blob_path = _blob_path_for_sha256(workspace, metadata["sha256"])
    try:
        data = blob_path.read_bytes()
    except FileNotFoundError as e:
        raise InputArtifactNotFoundError("input artifact blob not found.") from e
    except Exception as e:
        raise InputArtifactError(f"could not read input artifact blob: {e}") from e
    if len(data) != metadata["size_bytes"] or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
        raise InputArtifactError("input artifact blob content does not match metadata.")
    return metadata, data


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
