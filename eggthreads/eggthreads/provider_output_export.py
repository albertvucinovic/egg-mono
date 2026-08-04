from __future__ import annotations

"""Explicit export helpers for provider-output artifacts.

Provider-output artifacts remain canonical under ``.egg/egg_provider_output``.
These helpers implement the user-facing convenience copy/export path used by
terminal Egg and EggW without exposing raw storage paths or bypassing the normal
thread access checks.
"""

from pathlib import Path
from typing import Any, Callable

from .attachment_staging import safe_display_filename
from .provider_output_artifacts import resolve_provider_output_bytes, resolve_provider_output_metadata


def safe_provider_artifact_export_filename(value: Any, *, default: str = "provider-artifact") -> str:
    """Return a safe cwd filename for an exported provider artifact."""

    return safe_display_filename(str(value or ""), default=default)


def resolve_provider_artifact_export_path(
    workspace: str | Path | None,
    raw_path: str | Path | None,
    metadata: dict[str, Any],
    *,
    authorize_target: Callable[[Path], Path] | None = None,
) -> Path:
    """Resolve and validate a user-requested provider artifact export path.

    The export path is intentionally constrained to the current workspace and
    must not target Egg-private ``.egg`` storage.  Canonical bytes stay in
    ``.egg/egg_provider_output``; this path is only a convenience copy.
    """

    root = Path.cwd().resolve() if workspace is None else Path(workspace).expanduser().resolve()
    default_name = safe_provider_artifact_export_filename(metadata.get("filename") or metadata.get("artifact_id"))
    explicit_directory = False
    if raw_path is None or not str(raw_path).strip():
        path = root / default_name
    else:
        raw_text = str(raw_path)
        path = Path(raw_text).expanduser()
        if not path.is_absolute():
            path = root / path
        explicit_directory = raw_text.endswith(("/", "\\"))
        if explicit_directory:
            path = path / default_name

    resolved = (
        Path(authorize_target(path)).resolve()
        if authorize_target is not None
        else path.resolve()
    )
    if not explicit_directory and resolved.exists() and resolved.is_dir():
        resolved = resolved / default_name
        if authorize_target is not None:
            resolved = Path(authorize_target(resolved))
        resolved = resolved.resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError as e:
        raise ValueError("export path must stay under the current working directory") from e
    if rel.parts and rel.parts[0] == ".egg":
        raise ValueError("export path must not be under Egg-private .egg storage")
    if resolved.exists() and resolved.is_dir():
        raise ValueError("export path is a directory")
    return resolved


def export_provider_output_artifact(
    workspace: str | Path | None,
    db: Any,
    thread_id: str,
    artifact_id: str,
    output_path: str | Path | None = None,
    *,
    descendant_thread_id: str | None = None,
    export_workspace: str | Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Copy one accessible provider-output artifact to a workspace path.

    Returns ``(target_path, source_metadata)``.  Existing files are not
    overwritten; callers should ask the user to pick another path instead.
    """

    metadata = resolve_provider_output_metadata(
        workspace,
        db,
        thread_id,
        artifact_id,
        descendant_thread_id=descendant_thread_id,
    )
    from .sandbox import authorize_thread_path_write

    target = resolve_provider_artifact_export_path(
        workspace if export_workspace is None else export_workspace,
        output_path,
        metadata,
        authorize_target=lambda path: authorize_thread_path_write(db, thread_id, path),
    )
    metadata, data = resolve_provider_output_bytes(
        workspace,
        db,
        thread_id,
        artifact_id,
        descendant_thread_id=descendant_thread_id,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {target}")
    target.write_bytes(data)
    return target, metadata


__all__ = [
    "export_provider_output_artifact",
    "resolve_provider_artifact_export_path",
    "safe_provider_artifact_export_filename",
]
