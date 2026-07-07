from __future__ import annotations

"""Terminal command for quoting an assistant answer into an external editor."""

import os
import shlex
import tempfile
from pathlib import Path
from typing import Any

from eggthreads.command_catalog import CommandResult, CommandSpec
from eggthreads.edit_answer import (
    empty_input_message_draft,
    prepare_edit_answer_draft,
    quote_markdown_blockquote,
    select_assistant_message,
)
from eggthreads import sanitize_terminal_text  # type: ignore


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


def _set_input_panel_text(app: Any, text: str) -> None:
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


def _write_temp_markdown(initial_text: str) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix="egg-edit-answer-", suffix=".md", text=True)
    path = Path(raw_path)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(initial_text)
        if initial_text and not initial_text.endswith("\n"):
            f.write("\n")
    return path


async def _open_editor_draft_command_async(ctx: Any, arg: str, *, command_name: str, draft: Any) -> CommandResult:
    """Open ``draft`` in $EDITOR and load the edited text into the input panel."""

    app = getattr(ctx, "app", None)
    if app is None:
        return CommandResult(clear_input=False, message=f"/{command_name} is available only in terminal Egg.")

    existing_input = _read_current_input(app)
    if existing_input.strip():
        return CommandResult(clear_input=False, message=f"/{command_name} refused: input panel is not empty.")

    temp_path = _write_temp_markdown(draft.draft)
    try:
        argv = editor_argv_for_path(temp_path)
    except ValueError as e:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return CommandResult(clear_input=False, message=f"/{command_name} failed: {e}")

    try:
        run_external = getattr(app, "run_external_terminal_command", None)
        if run_external is None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
            return CommandResult(clear_input=False, message=f"/{command_name} failed: terminal handoff is unavailable.")
        returncode = await run_external(argv)
    except Exception as e:
        return CommandResult(clear_input=False, message=f"/{command_name} failed to run editor: {e}; draft left at {temp_path}.")

    if int(returncode or 0) != 0:
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

    _set_input_panel_text(app, edited.rstrip("\n"))
    suffix = f" {draft.source_suffix}" if draft.source_suffix else ""
    if draft.source_kind == "input_message":
        return CommandResult(
            clear_input=False,
            message="Loaded edited input message draft into the input panel.",
        )
    if draft.source_kind == "message":
        label = draft.source_label or "message"
        return CommandResult(
            clear_input=False,
            message=f"Loaded edited {label}{suffix} into the input panel.",
        )
    label = draft.source_label or ("assistant note" if draft.source_kind == "assistant_note" else "assistant answer")
    return CommandResult(
        clear_input=False,
        message=f"Loaded quoted {label}{suffix} into the input panel.",
    )


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

    return await _open_editor_draft_command_async(ctx, arg, command_name="editAnswer", draft=draft)


async def editor_command_async(ctx: Any, arg: str) -> CommandResult:
    """Open an empty external editor for composing the input message."""

    return await _open_editor_draft_command_async(
        ctx,
        arg,
        command_name="editor",
        draft=empty_input_message_draft((arg or "").strip()),
    )


def register_edit_answer_command(registry: Any, app: Any | None = None) -> None:
    """Register terminal-only editor commands if absent."""

    try:
        registry.get(_EDIT_ANSWER_COMMAND)
        edit_answer_registered = True
    except KeyError:
        edit_answer_registered = False
    if not edit_answer_registered:
        registry.register(
            CommandSpec(
                _EDIT_ANSWER_COMMAND,
                edit_answer_command_async,
                category="input",
                usage="/editAnswer [msg_id|suffix|text]",
                description=(
                    "Edit a message by id/suffix or open text in $EDITOR, then load it into input; "
                    "without arguments quotes the latest assistant answer or opens an empty editor."
                ),
            )
        )

    try:
        registry.get(_EDITOR_COMMAND)
    except KeyError:
        registry.register(
            CommandSpec(
                _EDITOR_COMMAND,
                editor_command_async,
                category="input",
                usage="/editor [text]",
                description="Open a $EDITOR draft, then load it into input.",
            )
        )


__all__ = [
    "edit_answer_command_async",
    "editor_command_async",
    "editor_argv_for_path",
    "quote_markdown_blockquote",
    "register_edit_answer_command",
    "select_assistant_message",
]
