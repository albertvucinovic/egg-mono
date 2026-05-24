from __future__ import annotations

"""Built-in long tool output artifact reader."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from ..output_paths import _ARTIFACT_ID_ALPHABET, safe_thread_dir_name
from ..plugins import PluginContext
from ..tools import ToolContext, ToolRegistry


def _context_db(ctx: ToolContext):
    try:
        from .compaction import _context_db as _db

        return _db(ctx)
    except Exception:
        from ..db import ThreadsDB

        db_path = getattr(ctx.db, "path", None)
        return ThreadsDB(db_path) if db_path is not None else ThreadsDB()


def _artifact_error(message: str) -> str:
    return f"Error: {message}"


def _artifact_base_dir(owner_thread_id: str) -> Path:
    return Path.cwd().resolve() / ".egg" / "egg_outputs" / safe_thread_dir_name(owner_thread_id)


def read_long_tool_output_tool(args: Dict[str, Any], ctx: ToolContext) -> str:
    calling_thread_id = ctx.thread_id or str(args.get("_thread_id") or "").strip()
    if not calling_thread_id:
        return _artifact_error("read_long_tool_output requires a calling thread.")

    artifact_id = str(args.get("artifact_id") or "").strip()
    if not artifact_id or any(ch not in _ARTIFACT_ID_ALPHABET for ch in artifact_id):
        return _artifact_error("artifact_id must be lower-case alphanumeric.")

    try:
        chunk_number = int(args.get("chunk_number"))
    except Exception:
        return _artifact_error("chunk_number must be an integer starting at 1.")
    if chunk_number < 1:
        return _artifact_error("chunk_number must be an integer starting at 1.")

    descendant_thread_id = str(args.get("descendant_thread_id") or "").strip()
    owner_thread_id = calling_thread_id
    if descendant_thread_id:
        db = _context_db(ctx)
        try:
            from ..api import is_descendant_thread

            allowed = is_descendant_thread(db, calling_thread_id, descendant_thread_id)
        except Exception:
            allowed = False
        if not allowed:
            return _artifact_error("access denied: descendant_thread_id is not a descendant of the calling thread.")
        owner_thread_id = descendant_thread_id

    artifact_dir = _artifact_base_dir(owner_thread_id) / artifact_id
    metadata_path = artifact_dir / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _artifact_error("artifact not found.")
    except Exception as e:
        return _artifact_error(f"could not read artifact metadata: {e}")
    if not isinstance(metadata, dict):
        return _artifact_error("artifact metadata is invalid.")

    try:
        total_chunks = int(metadata.get("chunk_count") or 0)
    except Exception:
        total_chunks = 0
    if total_chunks < 1:
        return _artifact_error("artifact metadata has no chunks.")
    if chunk_number > total_chunks:
        return _artifact_error(f"bad chunk number: requested {chunk_number}, but artifact has {total_chunks} chunks.")

    chunk_path = artifact_dir / f"chunk-{chunk_number:04d}.txt"
    try:
        chunk = chunk_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _artifact_error("requested chunk is missing.")
    except Exception as e:
        return _artifact_error(f"could not read requested chunk: {e}")

    header = [
        f"artifact_id: {artifact_id}",
        f"owner_thread_id: {owner_thread_id}",
        f"chunk_number: {chunk_number}",
        f"total_chunks: {total_chunks}",
        f"capped: {bool(metadata.get('capped'))}",
    ]
    if "stored_char_count" in metadata:
        header.append(f"stored_char_count: {metadata.get('stored_char_count')}")
    if "original_char_count" in metadata:
        header.append(f"original_char_count: {metadata.get('original_char_count')}")
    return "\n".join(header) + "\n\n" + chunk


def register_long_output_tools(registry: ToolRegistry) -> None:
    registry.register(
        name="read_long_tool_output",
        description=(
            "Read one bounded chunk of a long tool-output artifact. Use the "
            "short artifact_id from a long-output preview, a 1-based "
            "chunk_number, and descendant_thread_id only to read an artifact "
            "owned by a descendant thread."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "Short lower-case alphanumeric artifact id from the preview."},
                "chunk_number": {"type": "integer", "description": "1-based chunk number to read."},
                "descendant_thread_id": {"type": "string", "description": "Optional descendant thread id whose artifact namespace should be read."},
            },
            "required": ["artifact_id", "chunk_number"],
        },
        impl=read_long_tool_output_tool,
        accepts_context=True,
    )


@dataclass(frozen=True)
class LongOutputPlugin:
    name: str = "long_output"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.tool_registry is not None:
            register_long_output_tools(context.tool_registry)


__all__ = ["LongOutputPlugin", "read_long_tool_output_tool", "register_long_output_tools"]
