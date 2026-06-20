from __future__ import annotations

"""Terminal attachment staging commands for Egg."""

import shlex
from pathlib import Path
from typing import Any

from eggthreads.attachment_staging import format_staged_attachments, save_local_attachment_for_thread
from eggthreads.content_parts import format_attachment_placeholder
from eggthreads.provider_output_artifacts import promote_provider_output_to_input


def staged_attachments_for_thread(app: Any, thread_id: str) -> list[dict[str, Any]]:
    staged_by_thread = getattr(app, "_staged_attachments_by_thread", None)
    if not isinstance(staged_by_thread, dict):
        staged_by_thread = {}
        setattr(app, "_staged_attachments_by_thread", staged_by_thread)
    staged = staged_by_thread.setdefault(thread_id, [])
    if not isinstance(staged, list):
        staged = []
        staged_by_thread[thread_id] = staged
    return staged


def clear_staged_attachments_for_thread(app: Any, thread_id: str) -> int:
    staged_by_thread = getattr(app, "_staged_attachments_by_thread", None)
    if not isinstance(staged_by_thread, dict):
        return 0
    staged = staged_by_thread.pop(thread_id, [])
    return len(staged) if isinstance(staged, list) else 0


def staged_attachment_count(app: Any, thread_id: str) -> int:
    staged_by_thread = getattr(app, "_staged_attachments_by_thread", None)
    if not isinstance(staged_by_thread, dict):
        return 0
    staged = staged_by_thread.get(thread_id)
    return len(staged) if isinstance(staged, list) else 0


def _parse_attach_path(arg: str) -> str:
    text = (arg or "").strip()
    if not text:
        raise ValueError("Usage: /attach <path>")
    try:
        parts = shlex.split(text)
    except ValueError as e:
        raise ValueError(f"Could not parse path: {e}") from e
    if len(parts) != 1:
        raise ValueError("Usage: /attach <path> (quote paths that contain spaces)")
    return parts[0]


def _parse_attach_output_artifact_id(arg: str) -> str:
    text = (arg or "").strip()
    if not text:
        raise ValueError("Usage: /attachOutput <artifact_id>")
    try:
        parts = shlex.split(text)
    except ValueError as e:
        raise ValueError(f"Could not parse artifact id: {e}") from e
    if len(parts) != 1:
        raise ValueError("Usage: /attachOutput <artifact_id>")
    return parts[0]


def _mark_input_dirty(app: Any) -> None:
    try:
        app.input_panel.mark_dirty()
    except Exception:
        pass


def _complete_attach_path(ctx: Any, arg: str):
    text = str(arg or "")
    try:
        from eggthreads import get_thread_working_directory

        db = getattr(ctx, "db", None)
        thread_id = str(getattr(ctx, "current_thread", "") or "").strip()
        working_dir = get_thread_working_directory(db, thread_id) if db is not None and thread_id else Path.cwd()
        base = Path(text).expanduser()
        if not base.is_absolute():
            base = Path(working_dir) / base
        search_dir = base.parent if text and (text.endswith("/") or base.parent != Path(working_dir)) else Path(working_dir)
        prefix = "" if text.endswith("/") else base.name
        out = []
        for path in sorted(search_dir.glob(prefix + "*"))[:50]:
            display = str(path) + ("/" if path.is_dir() else "")
            out.append(display)
        return out
    except Exception:
        return []


def register_attachment_commands(registry: Any, app: Any) -> None:
    from eggthreads.command_catalog import CommandResult, CommandSpec

    def attach_handler(ctx: Any, arg: str):
        thread_id = str(getattr(ctx, "current_thread", "") or "").strip()
        if not thread_id or getattr(ctx, "db", None) is None:
            message = "/attach failed: no current thread."
            if ctx.log_system is not None:
                ctx.log_system(message)
            return CommandResult(clear_input=False, message=message)
        try:
            source_path = _parse_attach_path(arg)
            _saved, part = save_local_attachment_for_thread(ctx.db, thread_id, source_path)
            staged = staged_attachments_for_thread(app, thread_id)
            staged.append(part)
            message = f"Attached {part.get('filename') or '(unnamed)'} as {part.get('presentation')} ({part.get('mime_type')}); {len(staged)} staged."
            if ctx.log_system is not None:
                ctx.log_system(message)
            _mark_input_dirty(app)
            return CommandResult(clear_input=True, message=message)
        except Exception as e:
            message = f"/attach failed: {e}"
            if ctx.log_system is not None:
                ctx.log_system(message)
            return CommandResult(clear_input=False, message=message)

    def attachments_handler(ctx: Any, arg: str):
        thread_id = str(getattr(ctx, "current_thread", "") or "").strip()
        staged = list(staged_attachments_for_thread(app, thread_id)) if thread_id else []
        message = format_staged_attachments(staged)
        if ctx.console_print_block is not None:
            try:
                ctx.console_print_block("Attachments", message, border_style="green", markup=False)
            except TypeError:
                ctx.console_print_block("Attachments", message, border_style="green")
        if ctx.log_system is not None:
            ctx.log_system(message)
        return CommandResult(clear_input=True, message=message)

    def attach_output_handler(ctx: Any, arg: str):
        thread_id = str(getattr(ctx, "current_thread", "") or "").strip()
        if not thread_id or getattr(ctx, "db", None) is None:
            message = "/attachOutput failed: no current thread."
            if ctx.log_system is not None:
                ctx.log_system(message)
            return CommandResult(clear_input=False, message=message)
        try:
            artifact_id = _parse_attach_output_artifact_id(arg)
            saved, part = promote_provider_output_to_input(Path.cwd(), ctx.db, thread_id, artifact_id)
            staged = staged_attachments_for_thread(app, thread_id)
            staged.append(part)
            placeholder = format_attachment_placeholder(part, validate=False)
            message = (
                f"Promoted provider output {artifact_id} to input {saved.input_id}; "
                f"staged {len(staged)} attachment{'s' if len(staged) != 1 else ''}.\n"
                f"{placeholder}"
            )
            if ctx.log_system is not None:
                ctx.log_system(message)
            _mark_input_dirty(app)
            return CommandResult(clear_input=True, message=message)
        except Exception as e:
            message = f"/attachOutput failed: {e}"
            if ctx.log_system is not None:
                ctx.log_system(message)
            return CommandResult(clear_input=False, message=message)

    def clear_handler(ctx: Any, arg: str):
        thread_id = str(getattr(ctx, "current_thread", "") or "").strip()
        count = clear_staged_attachments_for_thread(app, thread_id) if thread_id else 0
        message = f"Cleared {count} staged attachment{'s' if count != 1 else ''}."
        if ctx.log_system is not None:
            ctx.log_system(message)
        _mark_input_dirty(app)
        return CommandResult(clear_input=True, message=message)

    def register_if_missing(spec: Any) -> None:
        try:
            registry.get(spec.name)
            return
        except KeyError:
            registry.register(spec)

    register_if_missing(
        CommandSpec(
            "attach",
            attach_handler,
            category="input",
            usage="/attach <path>",
            description="Stage a local file attachment for the next user message.",
            complete=_complete_attach_path,
        )
    )
    register_if_missing(
        CommandSpec(
            "attachments",
            attachments_handler,
            category="input",
            usage="/attachments",
            description="List attachments staged for the current thread.",
        )
    )
    register_if_missing(
        CommandSpec(
            "attachOutput",
            attach_output_handler,
            category="input",
            usage="/attachOutput <artifact_id>",
            description="Promote a provider-output artifact and stage it for the next user message.",
        )
    )
    register_if_missing(
        CommandSpec(
            "clearAttachments",
            clear_handler,
            category="input",
            usage="/clearAttachments",
            description="Clear attachments staged for the current thread.",
        )
    )


__all__ = [
    "clear_staged_attachments_for_thread",
    "register_attachment_commands",
    "staged_attachment_count",
    "staged_attachments_for_thread",
]
