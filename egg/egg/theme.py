"""Terminal theme helpers for Egg."""
from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.theme import Theme

THEMES = [
    "dark", "cyberpunk", "forest", "ocean", "sunset", "mono", "midnight",
    "disney", "fruit", "vegetables", "coffee", "matrix", "light", "light-mono",
    "colorful", "colorful-light",
]

BACKGROUND_THEMES = [
    "dark-background", "cyberpunk-background", "forest-background", "ocean-background",
    "sunset-background", "mono-background", "midnight-background", "disney-background",
    "fruit-background", "vegetables-background", "coffee-background", "matrix-background",
    "light-background", "light-mono-background", "colorful-light-background",
]

_THEME_COLORS: dict[str, dict[str, str]] = {
    "dark": {"user": "#58a6ff", "assistant": "#c9d1d9", "system": "#88c0d0", "tool": "#a3be8c", "reasoning": "#d2a8ff", "tool_call": "#f2cc60", "accent": "#58a6ff", "foreground": "#e6edf3", "muted": "#8b949e"},
    "cyberpunk": {"user": "#ff0080", "assistant": "#00ffff", "system": "#ffff00", "tool": "#00ff80", "reasoning": "#ff00ff", "tool_call": "#ff8000", "accent": "#ff0080", "foreground": "#f0e6ff", "muted": "#8866aa"},
    "forest": {"user": "#b7f46a", "assistant": "#7ddf9c", "system": "#a7f36b", "tool": "#e1f75b", "reasoning": "#d6a76d", "tool_call": "#ffd166", "accent": "#9be564", "foreground": "#d8f5d8", "muted": "#8faa8f"},
    "ocean": {"user": "#38bdf8", "assistant": "#7dd3fc", "system": "#22d3ee", "tool": "#5eead4", "reasoning": "#a78bfa", "tool_call": "#fbbf24", "accent": "#06b6d4", "foreground": "#d6f3ff", "muted": "#7aa7b8"},
    "sunset": {"user": "#ff8a65", "assistant": "#8d6e63", "system": "#ffb74d", "tool": "#ffd54f", "reasoning": "#ce93d8", "tool_call": "#fff176", "accent": "#ff7043", "foreground": "#ffe4d4", "muted": "#a1887f"},
    "mono": {"user": "#f5f5f5", "assistant": "#d4d4d4", "system": "#ffffff", "tool": "#bdbdbd", "reasoning": "#9ca3af", "tool_call": "#ffffff", "accent": "#ffffff", "foreground": "#e5e5e5", "muted": "#737373"},
    "midnight": {"user": "#8b8cff", "assistant": "#b6b9ff", "system": "#60a5fa", "tool": "#a5b4fc", "reasoning": "#c4b5fd", "tool_call": "#fbbf24", "accent": "#a5b4fc", "foreground": "#dbe6ff", "muted": "#7c859c"},
    "disney": {"user": "#ffd700", "assistant": "#3b82f6", "system": "#ff69b4", "tool": "#90ee90", "reasoning": "#da70d6", "tool_call": "#ffd700", "accent": "#4169e1", "foreground": "#f0f4ff", "muted": "#7a8caa"},
    "fruit": {"user": "#ffa500", "assistant": "#dc143c", "system": "#ffd700", "tool": "#32cd32", "reasoning": "#9932cc", "tool_call": "#ff7f50", "accent": "#ff6347", "foreground": "#ffe4e8", "muted": "#aa6080"},
    "vegetables": {"user": "#ff8c00", "assistant": "#228b22", "system": "#ff6347", "tool": "#9acd32", "reasoning": "#9370db", "tool_call": "#daa520", "accent": "#6b8e23", "foreground": "#e8f0d8", "muted": "#6b8b6b"},
    "coffee": {"user": "#e7c59a", "assistant": "#d7b98e", "system": "#f5deb3", "tool": "#e7b5a6", "reasoning": "#f0a06b", "tool_call": "#f2c078", "accent": "#cd853f", "foreground": "#f5e6d3", "muted": "#a99278"},
    "matrix": {"user": "#00ff41", "assistant": "#00cc66", "system": "#00ff00", "tool": "#00dd00", "reasoning": "#00aa00", "tool_call": "#66ff66", "accent": "#00ff00", "foreground": "#00ff41", "muted": "#008f11"},
    "light": {"user": "#1d4ed8", "assistant": "#1f2937", "system": "#0e7490", "tool": "#047857", "reasoning": "#7e22ce", "tool_call": "#b45309", "accent": "#1d4ed8", "foreground": "#111827", "muted": "#4b5563"},
    "light-mono": {"user": "#000000", "assistant": "#000000", "system": "#000000", "tool": "#000000", "reasoning": "#000000", "tool_call": "#000000", "accent": "#000000", "foreground": "#000000", "muted": "#000000"},
    "colorful": {"user": "#22c55e", "assistant": "#06b6d4", "system": "#3b82f6", "tool": "#eab308", "reasoning": "#d946ef", "tool_call": "#f59e0b", "accent": "#06b6d4", "foreground": "#e5e5e5", "muted": "#a3a3a3"},
    "colorful-light": {"user": "#16a34a", "assistant": "#0891b2", "system": "#2563eb", "tool": "#ca8a04", "reasoning": "#c026d3", "tool_call": "#d97706", "accent": "#0891b2", "foreground": "#0a0a0a", "muted": "#0a0a0a"},
}

_THEME_MODIFIERS: dict[str, dict[str, str]] = {
    "dark": {"user": "bold", "assistant": "", "system": "bold", "tool": "", "reasoning": "italic", "tool_call": "bold"},
    "cyberpunk": {"user": "bold", "assistant": "bold", "system": "bold", "tool": "bold", "reasoning": "italic", "tool_call": "bold italic"},
    "forest": {"user": "bold", "assistant": "", "system": "bold", "tool": "bold", "reasoning": "italic", "tool_call": "bold"},
    "ocean": {"user": "bold", "assistant": "", "system": "bold", "tool": "italic", "reasoning": "italic", "tool_call": "bold"},
    "sunset": {"user": "bold", "assistant": "", "system": "bold", "tool": "bold", "reasoning": "italic", "tool_call": "bold underline"},
    "mono": {"user": "bold", "assistant": "", "system": "bold underline", "tool": "", "reasoning": "italic", "tool_call": "bold"},
    "midnight": {"user": "bold", "assistant": "", "system": "bold", "tool": "", "reasoning": "italic", "tool_call": "bold"},
    "disney": {"user": "bold", "assistant": "bold", "system": "bold italic", "tool": "", "reasoning": "italic", "tool_call": "bold underline"},
    "fruit": {"user": "bold", "assistant": "bold", "system": "bold", "tool": "italic", "reasoning": "italic", "tool_call": "bold"},
    "vegetables": {"user": "bold", "assistant": "bold", "system": "", "tool": "bold", "reasoning": "italic", "tool_call": "bold"},
    "coffee": {"user": "bold", "assistant": "", "system": "bold", "tool": "", "reasoning": "italic", "tool_call": "bold"},
    "matrix": {"user": "bold", "assistant": "", "system": "bold", "tool": "", "reasoning": "italic", "tool_call": "bold"},
    "light": {"user": "bold", "assistant": "", "system": "bold", "tool": "", "reasoning": "italic", "tool_call": "bold"},
    "light-mono": {"user": "bold", "assistant": "", "system": "bold underline", "tool": "", "reasoning": "italic", "tool_call": "bold"},
    "colorful": {"user": "bold", "assistant": "bold", "system": "bold", "tool": "", "reasoning": "italic", "tool_call": "bold"},
    "colorful-light": {"user": "bold", "assistant": "bold", "system": "bold", "tool": "", "reasoning": "italic", "tool_call": "bold"},
}

_THEME_ALIASES = {name: name.removesuffix("-background") for name in BACKGROUND_THEMES}


def canonical_theme_name(name: str) -> str | None:
    theme = (name or "").strip().lower()
    if theme == "default":
        return theme
    if theme in _THEME_COLORS:
        return theme
    return _THEME_ALIASES.get(theme)


def available_theme_names() -> list[str]:
    return ["default", *THEMES, *BACKGROUND_THEMES]


def rich_theme_for(name: str) -> Theme:
    canonical = canonical_theme_name(name)
    if canonical is None:
        raise KeyError(name)
    colors = _THEME_COLORS[canonical]
    modifiers = _THEME_MODIFIERS.get(canonical, {})

    def style(key: str, *, modifier: str | None = None) -> str:
        parts = [modifier if modifier is not None else modifiers.get(key, ""), colors[key]]
        return " ".join(part for part in parts if part)

    return Theme(
        {
            "green": style("user"),
            "cyan": style("assistant"),
            "blue": style("system"),
            "yellow": style("tool"),
            "magenta": style("reasoning"),
            "bright_magenta": style("reasoning", modifier="bold italic"),
            "white": style("foreground", modifier=""),
            "dim": f"dim {colors['muted']}",
            "egg.user": style("user"),
            "egg.assistant": style("assistant"),
            "egg.system": style("system"),
            "egg.tool": style("tool"),
            "egg.reasoning": style("reasoning"),
            "egg.tool_call": style("tool_call"),
            "egg.tool_call_dim": f"dim {colors['tool_call']}",
            "egg.tool_call_title": style("tool_call", modifier="bold"),
            "egg.accent": style("accent", modifier="bold"),
            "egg.foreground": style("foreground", modifier=""),
            "egg.muted": style("muted", modifier=""),
        }
    )


def apply_theme(app: Any, name: str) -> str:
    canonical = canonical_theme_name(name)
    if canonical is None:
        raise KeyError(name)
    app._theme = canonical
    theme = None if canonical == "default" else rich_theme_for(canonical)
    app._rich_theme = theme
    app.console = themed_console_like(app.console, theme)
    return canonical


def themed_console_like(console: Any, theme: Theme | None) -> Console:
    return Console(
        file=getattr(console, "file", None),
        force_terminal=getattr(console, "_force_terminal", None),
        color_system=getattr(console, "color_system", None),
        theme=theme,
    )


def register_theme_command(registry: Any, app: Any) -> None:
    from eggthreads.command_catalog import CommandResult, CommandSpec

    try:
        registry.get("theme")
        return
    except KeyError:
        pass

    def theme_handler(ctx, arg):
        theme = (arg or "").strip().lower()
        names = available_theme_names()
        if not theme:
            current = getattr(app, "_theme", "default")
            message = f"Available themes: {', '.join(names)}\nUse /theme <name> to switch   (current: {current})"
            app.log_system(message)
            return CommandResult(clear_input=True, message=message)
        try:
            applied = app.apply_theme(theme)
        except KeyError:
            message = f"Unknown theme: {theme}. Available: {', '.join(names)}"
            app.log_system(message)
            return CommandResult(clear_input=False, message=message)
        message = f"Theme changed to: {applied}"
        app.log_system(message)
        try:
            app.redraw_static_view(reason="theme changed")
        except Exception:
            pass
        return CommandResult(clear_input=True, message=message)

    registry.register(
        CommandSpec(
            "theme",
            theme_handler,
            category="display",
            usage="/theme [name]",
            description="List or switch terminal themes.",
            complete=lambda ctx, arg: [
                name for name in available_theme_names()
                if not (arg or "").strip().lower() or (arg or "").strip().lower() in name
            ],
        )
    )
