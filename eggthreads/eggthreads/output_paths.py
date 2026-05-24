from __future__ import annotations

"""Paths for long tool output artifacts."""

import os
from pathlib import Path


ARTIFACT_ID_LENGTH = 8
_ARTIFACT_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def safe_thread_dir_name(thread_id: str) -> str:
    safe = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '-' for ch in str(thread_id or 'thread'))
    return safe or 'thread'


def thread_artifact_relative_dir(thread_id: str) -> Path:
    """Return ``.egg/egg_outputs/<thread_id>`` for long-output artifacts."""

    return Path(".egg", "egg_outputs", safe_thread_dir_name(thread_id))


def thread_artifact_dir(workspace: Path, thread_id: str) -> Path:
    return workspace.resolve() / thread_artifact_relative_dir(thread_id)


def _random_artifact_id() -> str:
    data = os.urandom(ARTIFACT_ID_LENGTH)
    alphabet = _ARTIFACT_ID_ALPHABET
    return "".join(alphabet[b % len(alphabet)] for b in data)


def create_thread_artifact_dir(workspace: Path, thread_id: str) -> tuple[str, Path]:
    """Create and return ``(artifact_id, artifact_dir)`` for a thread.

    Artifact ids are short lower-case alphanumeric strings.  The directory is
    created with ``exist_ok=False`` so an id collision is retried instead of
    reusing an existing artifact.
    """

    base_dir = thread_artifact_dir(workspace, thread_id)
    base_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base_dir, 0o700)
    except Exception:
        pass

    for _ in range(128):
        artifact_id = _random_artifact_id()
        artifact_dir = base_dir / artifact_id
        try:
            artifact_dir.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            continue
        try:
            os.chmod(artifact_dir, 0o700)
        except Exception:
            pass
        return artifact_id, artifact_dir

    raise FileExistsError(f"could not allocate a unique long-output artifact id under {base_dir}")
