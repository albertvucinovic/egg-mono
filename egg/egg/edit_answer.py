from __future__ import annotations

"""Terminal command for quoting an assistant answer into an external editor."""

import os
import shlex
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from eggthreads.command_catalog import CommandResult, CommandSpec
from eggthreads.content_parts import content_to_plain_text
from eggthreads import sanitize_terminal_text  # type: ignore

from .utils import snapshot_messages


_EDIT_ANSWER_COMMAND = "editAnswer"


def quote_markdown_blockquote(text: str) -> str:
    """Return ``text`` as a markdown blockquote, preserving blank lines.

    This is intentionally a mechanical source transform: the editable buffer is
    raw assistant markdown, not rendered markdown.  Every physical source line
    is prefixed, and blank lines become ``>`` so the quote does not visually
    break when rendered.
    """

    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if normalized == "":
        return ""
    return "\n".join(f"> {line}" if line else ">" for line in normalized.split("\n"))


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


def _message_raw_text(message: Mapping[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return content_to_plain_text(content)


def _assistant_candidates(messages: Sequence[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], str]]:
    candidates: list[tuple[Mapping[str, Any], str]] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        text = _message_raw_text(message)
        if text.strip():
            candidates.append((message, text))
    return candidates


def select_assistant_message(
    messages: Sequence[Mapping[str, Any]],
    selector: str = "",
) -> tuple[Mapping[str, Any], str]:
    """Select an assistant message by msg_id/suffix, or the latest by default."""

    candidates = _assistant_candidates(messages)
    if not candidates:
        raise ValueError("No assistant answer with textual content was found in this thread.")

    wanted = (selector or "").strip()
    if not wanted:
        return candidates[-1]

    matches: list[tuple[Mapping[str, Any], str]] = []
    for message, text in candidates:
        msg_id = str(message.get("msg_id") or message.get("id") or "")
        if msg_id == wanted or (msg_id and msg_id.endswith(wanted)):
            matches.append((message, text))
    if not matches:
        raise ValueError(f"No assistant answer matched selector {wanted!r}.")
    if len(matches) > 1:
        raise ValueError(f"Selector {wanted!r} matched multiple assistant answers; use a longer msg_id.")
    return matches[0]


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

    existing_input = _read_current_input(app)
    if existing_input.strip():
        return CommandResult(clear_input=False, message="/editAnswer refused: input panel is not empty.")

    try:
        create_snapshot = getattr(ctx, "create_snapshot", None)
        if create_snapshot is not None:
            create_snapshot(db, thread_id)
    except Exception:
        pass

    try:
        message, raw_text = select_assistant_message(snapshot_messages(db, thread_id), selector)
    except ValueError as e:
        return CommandResult(clear_input=False, message=f"/editAnswer failed: {e}")

    quoted = quote_markdown_blockquote(raw_text)
    if not quoted.strip():
        return CommandResult(clear_input=False, message="/editAnswer failed: selected assistant answer is empty.")

    temp_path = _write_temp_markdown(quoted)
    try:
        argv = editor_argv_for_path(temp_path)
    except ValueError as e:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return CommandResult(clear_input=False, message=f"/editAnswer failed: {e}")

    try:
        run_external = getattr(app, "run_external_terminal_command", None)
        if run_external is None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
            return CommandResult(clear_input=False, message="/editAnswer failed: terminal handoff is unavailable.")
        returncode = await run_external(argv)
    except Exception as e:
        return CommandResult(clear_input=False, message=f"/editAnswer failed to run editor: {e}; draft left at {temp_path}.")

    if int(returncode or 0) != 0:
        return CommandResult(
            clear_input=False,
            message=f"/editAnswer editor exited with status {returncode}; draft left at {temp_path}.",
        )

    try:
        edited = temp_path.read_text(encoding="utf-8")
    except Exception as e:
        return CommandResult(clear_input=False, message=f"/editAnswer failed to read edited draft: {e}")
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass

    if not edited.strip():
        return CommandResult(clear_input=True, message="/editAnswer cancelled: edited draft was empty.")

    _set_input_panel_text(app, edited.rstrip("\n"))
    msg_id = str(message.get("msg_id") or message.get("id") or "")
    suffix = f" {msg_id[-8:]}" if msg_id else ""
    return CommandResult(
        clear_input=False,
        message=f"Loaded quoted assistant answer{suffix} into the input panel.",
    )


def register_edit_answer_command(registry: Any, app: Any | None = None) -> None:
    """Register the terminal-only /editAnswer command if absent."""

    try:
        registry.get(_EDIT_ANSWER_COMMAND)
        return
    except KeyError:
        pass
    registry.register(
        CommandSpec(
            _EDIT_ANSWER_COMMAND,
            edit_answer_command_async,
            category="input",
            usage="/editAnswer [assistant_msg_id|suffix]",
            description="Quote an assistant answer as raw markdown in $EDITOR, then load it into input.",
        )
    )


__all__ = [
    "edit_answer_command_async",
    "editor_argv_for_path",
    "quote_markdown_blockquote",
    "register_edit_answer_command",
    "select_assistant_message",
]
