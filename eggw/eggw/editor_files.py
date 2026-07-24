from __future__ import annotations

"""Human-authority host file operations for EggW's existing Monaco editor."""

import hashlib
import os
import stat
import tempfile
import threading
import time
import uuid
from pathlib import Path

from eggthreads.editor_sources import EDITOR_DRAFT_MAX_BYTES

EDITOR_FILE_MAX_BYTES = EDITOR_DRAFT_MAX_BYTES
EDITOR_FILE_SESSION_TTL_SEC = 24 * 60 * 60


class EditorFileConflict(RuntimeError):
    """Raised when a browser save no longer targets the file that was opened."""


_open_files: dict[str, tuple[str, str, str, bool, float]] = {}
_open_files_lock = threading.Lock()


def _prune_editor_file_sessions(now: float) -> None:
    expired = [
        handle
        for handle, (*_binding, opened_at) in _open_files.items()
        if now - opened_at > EDITOR_FILE_SESSION_TTL_SEC
    ]
    for handle in expired:
        _open_files.pop(handle, None)


def _resolved_existing_file(path: Path) -> tuple[Path, os.stat_result]:
    """Open-path validation that rejects symlink targets before reading."""

    if path.is_symlink():
        raise EditorFileConflict("file path became a symbolic link; reopen the file")
    stat_result = path.stat()
    if not stat.S_ISREG(stat_result.st_mode):
        raise ValueError(f"editor target is not a regular file: {path}")
    return path, stat_result


def _assert_stable_resolved_path(path: Path) -> None:
    """Fail if an existing resolved target became a symlink or moved."""

    if path.is_symlink() or not path.exists() or path.resolve(strict=True) != path:
        raise EditorFileConflict("file path changed while it was being opened; try again")


def _fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def register_editor_file(
    thread_id: str,
    path: Path,
    *,
    fingerprint: str,
    existed: bool,
) -> str:
    """Create an opaque, process-local save capability for one opened file."""

    handle = uuid.uuid4().hex
    with _open_files_lock:
        now = time.monotonic()
        _prune_editor_file_sessions(now)
        for stale_handle in [
            stale_handle
            for stale_handle, (owner_thread, raw_path, *_rest) in _open_files.items()
            if owner_thread == str(thread_id) and raw_path == str(path)
        ]:
            _open_files.pop(stale_handle, None)
        _open_files[handle] = (str(thread_id), str(path), str(fingerprint), bool(existed), now)
    return handle


def save_registered_editor_file(thread_id: str, handle: str, text: str) -> Path:
    """Consume an opened-file capability and save exactly its bound path."""

    key = str(handle or "")
    with _open_files_lock:
        _prune_editor_file_sessions(time.monotonic())
        opened = _open_files.get(key)
        if opened is None:
            raise EditorFileConflict("editor file session expired or was already saved; reopen the file")
        owner_thread, raw_path, fingerprint, existed, _opened_at = opened
        if owner_thread != str(thread_id):
            raise EditorFileConflict("editor file session does not belong to this thread; reopen the file")
        path = Path(raw_path)
        try:
            save_editor_file(
                path,
                text,
                expected_fingerprint=fingerprint,
                expected_exists=existed,
            )
        except EditorFileConflict:
            _open_files.pop(key, None)
            raise
        _open_files.pop(key, None)
        return path


def discard_registered_editor_file(thread_id: str, handle: str) -> bool:
    """Discard an opened-file capability owned by ``thread_id``."""

    key = str(handle or "")
    with _open_files_lock:
        opened = _open_files.get(key)
        if opened is None:
            return False
        if opened[0] != str(thread_id):
            raise EditorFileConflict("editor file session does not belong to this thread")
        _open_files.pop(key, None)
        return True


def clear_registered_editor_files() -> None:
    """Clear process-local editor capabilities during backend shutdown."""

    with _open_files_lock:
        _open_files.clear()


def read_editor_file(path: Path) -> tuple[str, str, bool]:
    if not path.exists():
        return "", "", False
    path, before = _resolved_existing_file(path)
    with path.open("rb") as handle:
        data = handle.read(EDITOR_FILE_MAX_BYTES + 1)
    if len(data) > EDITOR_FILE_MAX_BYTES:
        raise ValueError(f"file is too large for the browser editor ({len(data)} bytes; limit {EDITOR_FILE_MAX_BYTES})")
    if b"\x00" in data:
        raise ValueError("binary files cannot be opened in the browser editor")
    after = path.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or after.st_size != len(data):
        raise RuntimeError("file changed while it was being opened; try again")
    _assert_stable_resolved_path(path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("file is not valid UTF-8") from exc
    return text, _fingerprint(data), True


def save_editor_file(path: Path, text: str, *, expected_fingerprint: str, expected_exists: bool) -> str:
    data = str(text).encode("utf-8")
    if len(data) > EDITOR_FILE_MAX_BYTES:
        raise ValueError(f"edited file is too large ({len(data)} bytes; limit {EDITOR_FILE_MAX_BYTES})")
    if path.is_symlink():
        raise EditorFileConflict("file path became a symbolic link after it was opened; reload it before saving")
    if path.exists():
        if not path.is_file():
            raise ValueError(f"editor target is not a regular file: {path}")
        path, current_stat = _resolved_existing_file(path)
        with path.open("rb") as handle:
            current = handle.read(EDITOR_FILE_MAX_BYTES + 1)
        if len(current) > EDITOR_FILE_MAX_BYTES:
            raise EditorFileConflict("file grew beyond the browser editor limit after it was opened; reload it before saving")
        if not expected_exists or _fingerprint(current) != str(expected_fingerprint or ""):
            raise EditorFileConflict("file changed on disk after it was opened; reload it before saving")
        mode = stat.S_IMODE(current_stat.st_mode)
    else:
        if expected_exists:
            raise EditorFileConflict("file was removed after it was opened; reload it before saving")
        if str(expected_fingerprint or ""):
            raise ValueError("new-file save must not provide an existing-file fingerprint")
        mode = None
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".egg-save", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        if expected_exists:
            if not path.exists():
                raise EditorFileConflict("file was removed while it was being saved; reload it before saving")
            if path.is_symlink():
                raise EditorFileConflict("file path became a symbolic link while it was being saved; reload it before saving")
            _assert_stable_resolved_path(path)
            latest_stat = path.stat()
            with path.open("rb") as handle:
                latest = handle.read(EDITOR_FILE_MAX_BYTES + 1)
            if _fingerprint(latest) != str(expected_fingerprint or "") or (
                latest_stat.st_dev,
                latest_stat.st_ino,
            ) != (
                current_stat.st_dev,
                current_stat.st_ino,
            ):
                raise EditorFileConflict("file changed on disk while it was being saved; reload it before saving")
        elif path.exists():
            raise EditorFileConflict("file was created by another process after it was opened; reload it before saving")
        os.replace(temp_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temp_path.unlink(missing_ok=True)
    return _fingerprint(data)


__all__ = [
    "EditorFileConflict",
    "EDITOR_FILE_MAX_BYTES",
    "EDITOR_FILE_SESSION_TTL_SEC",
    "discard_registered_editor_file",
    "clear_registered_editor_files",
    "read_editor_file",
    "register_editor_file",
    "save_editor_file",
    "save_registered_editor_file",
]
