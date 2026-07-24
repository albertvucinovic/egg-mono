from __future__ import annotations

"""Terminal commands for editing answers, transcript records, drafts, and files."""

import os
import shlex
import tempfile
from pathlib import Path
from typing import Any

from eggthreads.command_catalog import CommandResult, CommandSpec
from eggthreads.edit_answer import (
    EditAnswerDraft,
    prepare_edit_answer_draft,
    quote_markdown_blockquote,
    select_assistant_message,
)
from eggthreads.editor_sources import EditorSource, read_editor_draft_file, resolve_editor_source
from eggthreads import get_thread_working_directory, sanitize_terminal_text  # type: ignore

_EDIT_ANSWER_COMMAND = "editAnswer"
_EDITOR_COMMAND = "editor"


def editor_argv_for_path(path: Path) -> list[str]:
    """Build a non-shell editor argv for ``path`` from VISUAL/EDITOR."""

    editor = (os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi").strip()
    try:
        argv = shlex.split(editor)
    except ValueError as e:
        raise ValueError(f"Invalid VISUAL/EDITOR value {editor!r}: {e}") from e
    if not argv:
        argv = ["vi"]
    return [*argv, str(path)]


def set_input_panel_text(app: Any, text: str) -> None:
    safe = sanitize_terminal_text(text)
    editor = app.input_panel.editor.editor
    editor.set_text(safe)
    editor.cursor.row = 0
    editor.cursor.col = 0
    editor._clamp_cursor()
    app.input_panel._scroll_top = 0
    app.input_panel._hscroll_left = 0
    try:
        app.input_panel.mark_dirty()
    except Exception:
        pass


def _read_current_input(app: Any) -> str:
    try:
        return str(app.input_panel.get_text())
    except Exception:
        try:
            return str(app.input_panel.editor.editor.get_text())
        except Exception:
            return ""


def _write_temp_draft(initial_text: str, *, suffix: str = ".md") -> Path:
    safe_suffix = suffix if suffix.startswith(".") and len(suffix) <= 20 else ".md"
    fd, raw_path = tempfile.mkstemp(prefix="egg-editor-", suffix=safe_suffix, text=True)
    path = Path(raw_path)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(initial_text)
        if initial_text and not initial_text.endswith("\n"):
            f.write("\n")
    return path


async def _run_editor(
    app: Any,
    path: Path,
    *,
    command_name: str,
    cwd: Path | None = None,
) -> tuple[int | None, str | None]:
    try:
        argv = editor_argv_for_path(path)
    except ValueError as e:
        return None, f"/{command_name} failed: {e}"
    run_external = getattr(app, "run_external_terminal_command", None)
    if run_external is None:
        return None, f"/{command_name} failed: terminal handoff is unavailable."
    try:
        if cwd is None:
            return int(await run_external(argv) or 0), None
        return int(await run_external(argv, cwd=cwd) or 0), None
    except Exception as e:
        return None, f"/{command_name} failed to run editor: {e}"


async def _open_editor_draft_command_async(
    ctx: Any,
    *,
    command_name: str,
    draft: Any,
    suffix: str = ".md",
    submitted_command: str | None = None,
) -> CommandResult:
    """Open a temporary draft in $EDITOR and load it into the input panel."""

    app = getattr(ctx, "app", None)
    if app is None:
        return CommandResult(clear_input=False, message=f"/{command_name} is available only in terminal Egg.")
    existing_input = _read_current_input(app).strip()
    if existing_input and existing_input != str(submitted_command or "").strip():
        return CommandResult(clear_input=False, message=f"/{command_name} refused: input panel is not empty.")

    try:
        temp_path = _write_temp_draft(draft.draft, suffix=suffix)
    except Exception as e:
        return CommandResult(clear_input=False, message=f"/{command_name} failed to create editor draft: {e}")
    try:
        editor_cwd = get_thread_working_directory(
            getattr(ctx, "db", None),
            str(getattr(ctx, "current_thread", "") or ""),
        )
    except Exception:
        editor_cwd = None
    returncode, error = await _run_editor(
        app,
        temp_path,
        command_name=command_name,
        cwd=editor_cwd,
    )
    if error:
        if error.endswith("terminal handoff is unavailable."):
            temp_path.unlink(missing_ok=True)
            return CommandResult(clear_input=False, message=error)
        return CommandResult(clear_input=False, message=f"{error}; draft left at {temp_path}.")
    if returncode:
        return CommandResult(
            clear_input=False,
            message=f"/{command_name} editor exited with status {returncode}; draft left at {temp_path}.",
        )

    try:
        edited = temp_path.read_text(encoding="utf-8")
    except Exception as e:
        return CommandResult(clear_input=False, message=f"/{command_name} failed to read edited draft: {e}")
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass

    if not edited.strip():
        return CommandResult(clear_input=True, message=f"/{command_name} cancelled: edited draft was empty.")

    set_input_panel_text(app, edited.rstrip("\n"))
    source_suffix = f" {draft.source_suffix}" if draft.source_suffix else ""
    if draft.source_kind == "input_message":
        return CommandResult(clear_input=False, message="Loaded edited input message draft into the input panel.")
    if draft.source_kind == "message":
        label = draft.source_label or "message"
        return CommandResult(clear_input=False, message=f"Loaded edited {label}{source_suffix} into the input panel.")
    label = draft.source_label or ("assistant note" if draft.source_kind == "assistant_note" else "assistant answer")
    return CommandResult(clear_input=False, message=f"Loaded quoted {label}{source_suffix} into the input panel.")


async def edit_answer_command_async(ctx: Any, arg: str) -> CommandResult:
    """Open the latest/selected assistant answer as quoted markdown in $EDITOR."""

    app = getattr(ctx, "app", None)
    db = getattr(ctx, "db", None)
    thread_id = str(getattr(ctx, "current_thread", "") or "").strip()
    selector = (arg or "").strip()
    if app is None:
        return CommandResult(clear_input=False, message="/editAnswer is available only in terminal Egg.")
    if db is None or not thread_id:
        return CommandResult(clear_input=False, message="/editAnswer failed: no current thread.")
    if _read_current_input(app).strip():
        return CommandResult(clear_input=False, message="/editAnswer refused: input panel is not empty.")
    try:
        draft = prepare_edit_answer_draft(
            db,
            thread_id,
            selector,
            prefer_waiting_note=True,
            fallback_to_empty_input=True,
            fallback_unmatched_selector_to_input=True,
        )
    except ValueError as e:
        return CommandResult(clear_input=False, message=f"/editAnswer failed: {e}")
    return await _open_editor_draft_command_async(ctx, command_name="editAnswer", draft=draft)


def _draft_from_editor_source(source: EditorSource, text: str):
    return EditAnswerDraft(
        draft=text,
        source_msg_id=source.record_id,
        source_kind="message" if source.mode in {"record", "file_draft"} else "input_message",
        source_suffix=source.source_suffix,
        source_label=source.source_label,
    )


async def editor_command_async(ctx: Any, arg: str) -> CommandResult:
    """Edit a draft, inspectable record, or host file using the human's editor."""

    app = getattr(ctx, "app", None)
    db = getattr(ctx, "db", None)
    thread_id = str(getattr(ctx, "current_thread", "") or "").strip()
    if app is None:
        return CommandResult(clear_input=False, message="/editor is available only in terminal Egg.")
    if db is None or not thread_id:
        return CommandResult(clear_input=False, message="/editor failed: no current thread.")
    try:
        source = resolve_editor_source(db, thread_id, arg, get_thread_working_directory(db, thread_id))
    except (OSError, ValueError) as e:
        return CommandResult(clear_input=False, message=f"/editor failed: {e}")

    if source.mode == "ambiguous":
        message = source.resolution.message if source.resolution is not None else "Ambiguous editor source."
        return CommandResult(
            clear_input=False,
            message=message,
            data={"action": "request_completion", "input": f"/editor {(arg or '').strip()}"},
        )
    if source.mode == "file" and source.path is not None:
        returncode, error = await _run_editor(
            app,
            source.path,
            command_name="editor",
            cwd=get_thread_working_directory(db, thread_id),
        )
        if error:
            return CommandResult(clear_input=False, message=error)
        if returncode:
            return CommandResult(clear_input=False, message=f"/editor exited with status {returncode}.")
        return CommandResult(clear_input=True, message=f"Finished editing {source.path}.")

    draft_text = source.draft
    suffix = ".md"
    if source.mode == "file_draft" and source.path is not None:
        try:
            draft_text = read_editor_draft_file(source.path)
        except OSError as e:
            return CommandResult(clear_input=False, message=f"/editor failed to read {source.path}: {e}")
        except ValueError as e:
            return CommandResult(clear_input=False, message=f"/editor failed: {e}")
        suffix = source.path.suffix or ".txt"
    draft = _draft_from_editor_source(source, draft_text)
    return await _open_editor_draft_command_async(
        ctx,
        command_name="editor",
        draft=draft,
        suffix=suffix,
        submitted_command=f"/editor{(' ' + arg.strip()) if (arg or '').strip() else ''}",
    )


def register_edit_answer_command(registry: Any, app: Any | None = None) -> None:
    """Register terminal-only editor commands if absent."""

    try:
        registry.get(_EDIT_ANSWER_COMMAND)
    except KeyError:
        registry.register(CommandSpec(
            _EDIT_ANSWER_COMMAND,
            edit_answer_command_async,
            category="input",
            usage="/editAnswer [msg_id|suffix|text]",
            description=(
                "Edit a message by id/suffix or open text in $EDITOR, then load it into input; "
                "without arguments quotes the latest assistant answer or opens an empty editor."
            ),
        ))
    try:
        registry.get(_EDITOR_COMMAND)
    except KeyError:
        registry.register(CommandSpec(
            _EDITOR_COMMAND,
            editor_command_async,
            category="input",
            usage="/editor [id_hint|path|@path|-- text]",
            description="Edit a transcript record/draft or edit a host file in $EDITOR.",
        ))


__all__ = [
    "edit_answer_command_async",
    "editor_command_async",
    "editor_argv_for_path",
    "quote_markdown_blockquote",
    "set_input_panel_text",
    "select_assistant_message",
    "register_edit_answer_command",
]
