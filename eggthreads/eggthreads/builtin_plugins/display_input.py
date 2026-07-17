from __future__ import annotations

"""Built-in display and input UI commands."""

from dataclasses import dataclass
from typing import Any

from ..plugins import PluginContext
from ..terminal_safety import sanitize_terminal_text

try:  # pragma: no cover - rich is available in the TUI environment/tests
    from rich import box as rich_box
except Exception:  # pragma: no cover
    rich_box = None  # type: ignore


def _log(context: Any, message: str) -> None:
    if context.log_system is not None:
        context.log_system(message)


def _app(context: Any) -> Any:
    return context.app


def toggle_panel_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    app = _app(context)
    which = (arg or "").strip().lower()
    valid = {"chat", "children", "system"}
    if app is None:
        _log(context, "/togglePanel requires an app context")
        return CommandResult(clear_input=False)
    if not which or which not in valid:
        states = ", ".join(
            f"{key}={'on' if app._panel_visible.get(key, True) else 'off'}" for key in sorted(valid)
        )
        _log(context, f"Usage: /togglePanel (chat|children|system)   (current: {states})")
        return CommandResult(clear_input=False)
    cur = bool(app._panel_visible.get(which, True))
    app._panel_visible[which] = not cur
    _log(context, f"Panel '{which}' is now {'shown' if app._panel_visible[which] else 'hidden'}. ")
    return CommandResult(clear_input=True)


def redraw_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    app = _app(context)
    if app is None:
        _log(context, "/redraw requires an app context")
        return CommandResult(clear_input=False)
    _log(context, "Redrawing transcript (see console).")
    try:
        app.redraw_static_view(reason="manual")
    except Exception as e:
        _log(context, f"/redraw error: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def display_mode_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    app = _app(context)
    if app is None:
        _log(context, "/displayMode requires an app context")
        return CommandResult(clear_input=False)
    which = (arg or "").strip().lower().replace("_", "-")
    aliases = {
        "full-screen": False,
        "fullscreen": False,
        "full": False,
        "tui": False,
        "altscreen": False,
        "alt-screen": False,
        "inline": True,
        "classic": True,
        "head": True,
        "legacy": True,
    }
    if which not in aliases:
        cur = "inline" if getattr(app, "_display_is_inline", False) else "full-screen"
        _log(context, f"Usage: /displayMode (full-screen|inline)   (current: {cur})")
        return CommandResult(clear_input=False)
    want_inline = aliases[which]
    if bool(getattr(app, "_display_is_inline", False)) == want_inline:
        cur = "inline" if want_inline else "full-screen"
        _log(context, f"Display mode already {cur}.")
        return CommandResult(clear_input=True)
    app._display_is_inline = want_inline
    app._pending_mode_change = True
    new_mode = "inline" if want_inline else "full-screen"
    _log(context, f"Display mode switching to {new_mode}…")
    return CommandResult(clear_input=True)


def display_verbosity_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    app = _app(context)
    if app is None:
        _log(context, "/displayVerbosity requires an app context")
        return CommandResult(clear_input=False)
    level = (arg or "").strip().lower()
    allowed = {"max", "medium", "min"}
    current = getattr(app, "_display_verbosity", "min")
    if not level:
        message = f"Usage: /displayVerbosity <max|medium|min>   (current: {current})"
        _log(context, message)
        return CommandResult(clear_input=False, message=message)
    if level not in allowed:
        message = f"Usage: /displayVerbosity <max|medium|min>   (current: {current})"
        _log(context, message)
        return CommandResult(clear_input=False, message=message)
    if level == current:
        message = f"Display verbosity already {level}."
        _log(context, message)
        return CommandResult(clear_input=True, message=message)
    app._display_verbosity = level
    message = f"Display verbosity set to {level}."
    _log(context, message)
    try:
        app.redraw_static_view(
            reason="display verbosity changed",
            reuse_transcript_source=True,
        )
    except Exception as e:
        _log(context, f"/displayVerbosity redraw error: {e}")
        return CommandResult(clear_input=False, message=f"{message} Redraw failed: {e}")
    return CommandResult(clear_input=True, message=message)


def toggle_borders_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    app = _app(context)
    if app is None or rich_box is None:
        _log(context, "/toggleBorders requires an app context")
        return CommandResult(clear_input=False)
    app._borders_visible = not getattr(app, "_borders_visible", True)
    if app._borders_visible:
        original = getattr(app, "_original_box_styles", {})
        app.chat_output.style.box = original.get("chat", rich_box.SQUARE)
        app.system_output.style.box = original.get("system", rich_box.SQUARE)
        app.children_output.style.box = original.get("children", rich_box.SQUARE)
        app.approval_panel.style.box = original.get("approval", rich_box.SQUARE)
    else:
        app.chat_output.style.box = rich_box.MINIMAL
        app.system_output.style.box = rich_box.MINIMAL
        app.children_output.style.box = rich_box.MINIMAL
        app.approval_panel.style.box = rich_box.MINIMAL
    state = "on" if app._borders_visible else "off"
    _log(context, f"Borders are now {state}.")
    try:
        app.redraw_static_view(reason="borders toggled")
    except Exception:
        pass
    return CommandResult(clear_input=True)


def paste_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    app = _app(context)
    if app is None:
        _log(context, "/paste requires an app context")
        return CommandResult(clear_input=False)
    try:
        from egg.commands import utility as utility_mod  # type: ignore

        content = utility_mod.read_clipboard()
    except Exception:
        content = None
    if content is None:
        _log(context, "Failed to read clipboard.")
        return CommandResult(clear_input=False)
    if content == "":
        _log(context, "Clipboard is empty.")
        return CommandResult(clear_input=False)
    safe_content = sanitize_terminal_text(content)
    app.input_panel.editor.editor.set_text(safe_content)
    app.input_panel.editor.editor.cursor.row = 0
    app.input_panel.editor.editor.cursor.col = 0
    app.input_panel.editor.editor._clamp_cursor()
    app.input_panel._scroll_top = 0
    app.input_panel._hscroll_left = 0
    _log(context, f"Pasted {len(safe_content)} characters from clipboard.")
    return CommandResult(clear_input=False)


def enter_mode_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    app = _app(context)
    if app is None:
        _log(context, "/enterMode requires an app context")
        return CommandResult(clear_input=False)
    mode = (arg or "").strip().lower()
    if mode in ("send", "s", "on"):
        app.enter_sends = True
        _log(context, "Enter mode: send (Enter sends, Ctrl+D also sends).")
        return CommandResult(clear_input=True)
    if mode in ("newline", "n", "off"):
        app.enter_sends = False
        _log(context, "Enter mode: newline (Enter inserts newline, Ctrl+D sends).")
        return CommandResult(clear_input=True)
    _log(context, "Usage: /enterMode <send|newline>")
    return CommandResult(clear_input=False)


def _complete_from(options: list[str], arg: str):
    token = (arg or "").strip().lower()
    if not token:
        return options
    pref = [option for option in options if option.startswith(token)]
    cont = [option for option in options if token in option and option not in pref]
    return pref + cont


def register_display_input_commands(registry: Any) -> None:
    from ..command_catalog import CommandSpec

    registry.register(CommandSpec("togglePanel", toggle_panel_command, category="display", usage="/togglePanel <chat|children|system>", description="Show or hide a panel.", complete=lambda ctx, arg: _complete_from(["chat", "children", "system"], arg)))
    registry.register(CommandSpec("toggleBorders", toggle_borders_command, category="display", usage="/toggleBorders", description="Toggle panel borders."))
    registry.register(CommandSpec("redraw", redraw_command, category="display", usage="/redraw", description="Redraw the static transcript."))
    registry.register(CommandSpec("displayMode", display_mode_command, category="display", usage="/displayMode <full-screen|inline>", description="Switch display mode.", complete=lambda ctx, arg: _complete_from(["full-screen", "inline"], arg)))
    registry.register(CommandSpec("displayVerbosity", display_verbosity_command, category="display", usage="/displayVerbosity <max|medium|min>", description="Set transcript display verbosity.", complete=lambda ctx, arg: _complete_from(["max", "medium", "min"], arg)))
    registry.register(CommandSpec("paste", paste_command, category="input", usage="/paste", description="Paste clipboard content into the input panel."))
    registry.register(CommandSpec("enterMode", enter_mode_command, category="input", usage="/enterMode <send|newline>", description="Set Enter key behavior.", complete=lambda ctx, arg: _complete_from(["send", "newline"], arg)))


@dataclass(frozen=True)
class DisplayInputPlugin:
    name: str = "display_input"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.command_registry is not None:
            register_display_input_commands(context.command_registry)


__all__ = [
    "DisplayInputPlugin",
    "display_mode_command",
    "display_verbosity_command",
    "enter_mode_command",
    "paste_command",
    "redraw_command",
    "register_display_input_commands",
    "toggle_borders_command",
    "toggle_panel_command",
]
