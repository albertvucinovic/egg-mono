from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from eggthreads.attachment_tools import artifact_workspace_from_db
from eggthreads.provider_output_artifacts import (
    resolve_provider_output_bytes,
    save_provider_output_bytes,
)
from eggthreads.sandbox import authorize_thread_path_read

from eggflow import Task
from eggthreads import (
    ToolRegistry,
    build_tool_call_states,
    get_thread_working_directory,
    record_synthetic_user_tool_call,
)

from .context import _current_evaluation, _evaluation_runtime
from .identity import canonical_json, digest_payload


@dataclass(frozen=True)
class ThreadToolFile:
    """One sandbox-written file snapshotted by :class:`ThreadTool`."""

    path: str
    artifact_id: str
    sha256: str
    size_bytes: int
    mime_type: str


@dataclass(frozen=True)
class ThreadToolResult:
    """Compact tool receipt plus durable file snapshots."""

    output: str
    files: tuple[ThreadToolFile, ...] = ()


@dataclass
class ThreadTool(Task):
    """Run one durable synthetic tool call on an assigned Eggthreads thread.

    ``output_files`` are workspace-relative regular files produced by the tool.
    On first execution they are authorized, read, and snapshotted into Eggthreads'
    content-addressed provider-output store. Recovery verifies the workspace copy
    and atomically rematerializes it from the snapshot when absent or corrupt.
    The transcript stores only a compact receipt, never the file bytes.
    """

    tools: ToolRegistry = field(repr=False, compare=False)
    thread_id: str
    name: str
    arguments: Any
    occurrence: int | None = None
    origin: str = "eggopt"
    output_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.output_files = _output_paths(self.output_files)

    def get_cache_key(self) -> str:
        version = "eggopt.thread-tool.v2" if self.output_files else "eggopt.thread-tool.v1"
        return digest_payload(
            version,
            {
                "thread": self.thread_id,
                "name": self.name,
                "arguments": json.loads(
                    canonical_json(self.arguments, what="tool arguments")
                ),
                "occurrence": self.occurrence,
                "origin": self.origin,
                **({"output_files": self.output_files} if self.output_files else {}),
            },
        )

    def _call_id(self) -> str:
        return self.get_cache_key().rsplit(":", 1)[-1]

    async def recover(self) -> bool:
        if not self.output_files:
            return True
        runtime_key = str(_current_evaluation()["_runtime_key"])
        db = _evaluation_runtime(runtime_key)
        call = build_tool_call_states(db, self.thread_id).get(self._call_id())
        if call is None or call.finished_output is None:
            return True
        try:
            result = _parse_receipt(call.finished_output, self.output_files)
            for file in result.files:
                _materialize_output_file(db, self.thread_id, file)
        except (FileNotFoundError, RuntimeError, TypeError, ValueError):
            return False
        return True

    async def run(self) -> str | ThreadToolResult:
        runtime_key = str(_current_evaluation()["_runtime_key"])
        db = _evaluation_runtime(runtime_key)
        key = self.get_cache_key()
        call_id = self._call_id()
        call = build_tool_call_states(db, self.thread_id).get(call_id)
        if call is None:
            output = await self.tools.execute_async(
                self.name,
                self.arguments,
                thread_id=self.thread_id,
                db=db,
                initial_model_key=None,
            )
            try:
                files = tuple(
                    _snapshot_output_file(db, self.thread_id, key, path)
                    for path in self.output_files
                )
            except Exception as exc:
                failure = {
                    "schema": "eggopt.thread-tool-file-error.v1",
                    "output": str(output),
                    "error": f"{type(exc).__name__}: {exc}",
                    "expected_files": self.output_files,
                }
                record_synthetic_user_tool_call(
                    db,
                    self.thread_id,
                    self.name,
                    self.arguments,
                    canonical_json(failure, what="ThreadTool file failure receipt"),
                    origin=self.origin,
                    tool_call_id=call_id,
                )
                raise RuntimeError(
                    "tool completed but declared ThreadTool output files could not be "
                    f"snapshotted: {exc}"
                ) from exc
            record_synthetic_user_tool_call(
                db,
                self.thread_id,
                self.name,
                self.arguments,
                _receipt(str(output), files),
                origin=self.origin,
                tool_call_id=call_id,
            )
        elif call.name != self.name or _arguments(call.arguments) != _arguments(
            canonical_json(self.arguments, what="tool arguments")
        ):
            raise RuntimeError("persisted tool call contradicts ThreadTool identity")
        call = build_tool_call_states(db, self.thread_id)[call_id]
        if call.finished_reason != "success" or call.finished_output is None:
            raise RuntimeError(
                f"{self.name} tool call failed: {call.finished_reason or call.state}"
            )
        if not self.output_files:
            return call.finished_output
        result = _parse_receipt(call.finished_output, self.output_files)
        for file in result.files:
            _materialize_output_file(db, self.thread_id, file)
        return result

    def restore(self, value: Any) -> Any:
        if not self.output_files:
            return value
        if not isinstance(value, ThreadToolResult):
            raise TypeError("cached ThreadTool file result has an invalid value")
        runtime_key = str(_current_evaluation()["_runtime_key"])
        db = _evaluation_runtime(runtime_key)
        if tuple(file.path for file in value.files) != self.output_files:
            raise RuntimeError("cached ThreadTool file result contradicts output_files")
        for file in value.files:
            _materialize_output_file(db, self.thread_id, file)
        return value


def _output_paths(values: Any) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("ThreadTool output_files must be an iterable of relative paths")
    paths: list[str] = []
    for value in values or ():
        text = str(value or "").replace("\\", "/")
        path = PurePosixPath(text)
        if (
            not text
            or path.is_absolute()
            or text != path.as_posix()
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.parts[0] == ".egg"
        ):
            raise ValueError(
                "ThreadTool output_files must be normalized workspace-relative paths "
                "outside .egg"
            )
        if text in paths:
            raise ValueError(f"duplicate ThreadTool output file: {text}")
        paths.append(text)
    return tuple(paths)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot_output_file(
    db: Any, thread_id: str, task_key: str, path: str
) -> ThreadToolFile:
    source = authorize_thread_path_read(db, thread_id, path)
    data = source.read_bytes()
    saved = save_provider_output_bytes(
        artifact_workspace_from_db(db),
        thread_id,
        data,
        filename=source.name,
        mime_type=(mimetypes.guess_type(source.name)[0] or "application/octet-stream"),
        presentation="file",
        provenance={
            "kind": "eggopt_thread_tool_file",
            "task_key": task_key,
            "workspace_path": path,
        },
        derived={"workspace_path": path},
    )
    return ThreadToolFile(
        path=path,
        artifact_id=saved.artifact_id,
        sha256=saved.metadata["sha256"],
        size_bytes=saved.metadata["size_bytes"],
        mime_type=saved.metadata["mime_type"],
    )


def _receipt(output: str, files: tuple[ThreadToolFile, ...]) -> str:
    if not files:
        return output
    return canonical_json(
        {
            "schema": "eggopt.thread-tool-result.v1",
            "output": output,
            "files": [file.__dict__ for file in files],
        },
        what="ThreadTool file receipt",
    )


def _parse_receipt(output: str, expected_paths: tuple[str, ...]) -> ThreadToolResult:
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("persisted ThreadTool output has no durable file receipt") from exc
    if (
        not isinstance(value, Mapping)
        or value.get("schema") != "eggopt.thread-tool-result.v1"
    ):
        raise RuntimeError("persisted ThreadTool file receipt has an unsupported schema")
    raw_files = value.get("files")
    if not isinstance(raw_files, list):
        raise TypeError("persisted ThreadTool file receipt has invalid files")
    try:
        files = tuple(
            ThreadToolFile(
                path=str(item["path"]),
                artifact_id=str(item["artifact_id"]),
                sha256=str(item["sha256"]),
                size_bytes=int(item["size_bytes"]),
                mime_type=str(item["mime_type"]),
            )
            for item in raw_files
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("persisted ThreadTool file receipt is malformed") from exc
    if tuple(file.path for file in files) != expected_paths:
        raise RuntimeError("persisted ThreadTool file receipt contradicts output_files")
    if any(
        len(file.sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in file.sha256)
        or file.size_bytes < 0
        or len(file.artifact_id) != 8
        or any(ch not in "0123456789abcdefghijklmnopqrstuvwxyz" for ch in file.artifact_id)
        or not file.mime_type
        for file in files
    ):
        raise RuntimeError("persisted ThreadTool file receipt has invalid metadata")
    return ThreadToolResult(output=str(value.get("output") or ""), files=files)


def _materialize_output_file(db: Any, thread_id: str, file: ThreadToolFile) -> None:
    metadata, data = resolve_provider_output_bytes(
        artifact_workspace_from_db(db), db, thread_id, file.artifact_id
    )
    if (
        metadata.get("sha256") != file.sha256
        or metadata.get("size_bytes") != file.size_bytes
        or _sha256(data) != file.sha256
    ):
        raise RuntimeError(f"durable ThreadTool file contradicts receipt: {file.path}")
    root = get_thread_working_directory(db, thread_id).resolve()
    target = (root / file.path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("ThreadTool output path escaped its workspace") from exc
    if target.is_file():
        current = target.read_bytes()
        if len(current) == file.size_bytes and _sha256(current) == file.sha256:
            return
    elif target.exists():
        raise RuntimeError(f"ThreadTool output path is not a regular file: {file.path}")
    _atomic_write(target, data)
    materialized = target.read_bytes()
    if len(materialized) != file.size_bytes or _sha256(materialized) != file.sha256:
        raise RuntimeError(f"rematerialized ThreadTool file failed verification: {file.path}")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass
        raise


def _arguments(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


__all__ = ["ThreadTool", "ThreadToolFile", "ThreadToolResult"]
