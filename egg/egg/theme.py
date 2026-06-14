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
    "dark": {"user": "#388bfd", "assistant": "#8b949e", "system": "#88c0d0", "tool": "#a3be8c", "reasoning": "#b48ead", "tool_call": "#ebcb8b", "accent": "#58a6ff", "foreground": "#e6edf3", "muted": "#8b949e"},
    "cyberpunk": {"user": "#ff0080", "assistant": "#00ffff", "system": "#ffff00", "tool": "#00ff80", "reasoning": "#ff00ff", "tool_call": "#ff8000", "accent": "#ff0080", "foreground": "#f0e6ff", "muted": "#8866aa"},
    "forest": {"user": "#8fbc3f", "assistant": "#3d7a4a", "system": "#8bc34a", "tool": "#cddc39", "reasoning": "#a67c52", "tool_call": "#ffc107", "accent": "#7cb342", "foreground": "#c8e6c8", "muted": "#6b8e6b"},
    "ocean": {"user": "#29b6f6", "assistant": "#0288d1", "system": "#00bcd4", "tool": "#26c6da", "reasoning": "#7c4dff", "tool_call": "#ffab40", "accent": "#00acc1", "foreground": "#b8e0f0", "muted": "#607d8b"},
    "sunset": {"user": "#ff8a65", "assistant": "#8d6e63", "system": "#ffb74d", "tool": "#ffd54f", "reasoning": "#ce93d8", "tool_call": "#fff176", "accent": "#ff7043", "foreground": "#ffe4d4", "muted": "#a1887f"},
    "mono": {"user": "#e0e0e0", "assistant": "#a0a0a0", "system": "#c0c0c0", "tool": "#909090", "reasoning": "#808080", "tool_call": "#b0b0b0", "accent": "#ffffff", "foreground": "#c0c0c0", "muted": "#606060"},
    "midnight": {"user": "#6366f1", "assistant": "#4c4c8a", "system": "#60a5fa", "tool": "#818cf8", "reasoning": "#a78bfa", "tool_call": "#fbbf24", "accent": "#818cf8", "foreground": "#c8d4e8", "muted": "#6b7280"},
    "disney": {"user": "#ffd700", "assistant": "#3b82f6", "system": "#ff69b4", "tool": "#90ee90", "reasoning": "#da70d6", "tool_call": "#ffd700", "accent": "#4169e1", "foreground": "#f0f4ff", "muted": "#7a8caa"},
    "fruit": {"user": "#ffa500", "assistant": "#dc143c", "system": "#ffd700", "tool": "#32cd32", "reasoning": "#9932cc", "tool_call": "#ff7f50", "accent": "#ff6347", "foreground": "#ffe4e8", "muted": "#aa6080"},
    "vegetables": {"user": "#ff8c00", "assistant": "#228b22", "system": "#ff6347", "tool": "#9acd32", "reasoning": "#9370db", "tool_call": "#daa520", "accent": "#6b8e23", "foreground": "#e8f0d8", "muted": "#6b8b6b"},
    "coffee": {"user": "#d2b48c", "assistant": "#8b7355", "system": "#f5deb3", "tool": "#bc8f8f", "reasoning": "#a0522d", "tool_call": "#deb887", "accent": "#cd853f", "foreground": "#f5e6d3", "muted": "#8b7355"},
    "matrix": {"user": "#00ff00", "assistant": "#008800", "system": "#00cc00", "tool": "#009900", "reasoning": "#006600", "tool_call": "#33ff33", "accent": "#00ff00", "foreground": "#00ff00", "muted": "#006600"},
    "light": {"user": "#2563eb", "assistant": "#4b5563", "system": "#0891b2", "tool": "#059669", "reasoning": "#9333ea", "tool_call": "#d97706", "accent": "#2563eb", "foreground": "#1a1a1a", "muted": "#6b7280"},
    "light-mono": {"user": "#000000", "assistant": "#000000", "system": "#000000", "tool": "#000000", "reasoning": "#000000", "tool_call": "#000000", "accent": "#000000", "foreground": "#000000", "muted": "#000000"},
    "colorful": {"user": "#22c55e", "assistant": "#06b6d4", "system": "#3b82f6", "tool": "#eab308", "reasoning": "#d946ef", "tool_call": "#f59e0b", "accent": "#06b6d4", "foreground": "#e5e5e5", "muted": "#a3a3a3"},
    "colorful-light": {"user": "#16a34a", "assistant": "#0891b2", "system": "#2563eb", "tool": "#ca8a04", "reasoning": "#c026d3", "tool_call": "#d97706", "accent": "#0891b2", "foreground": "#0a0a0a", "muted": "#0a0a0a"},
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
    return Theme(
        {
            "green": colors["user"],
            "cyan": colors["assistant"],
            "blue": colors["system"],
            "yellow": colors["tool"],
            "magenta": colors["reasoning"],
            "bright_magenta": colors["reasoning"],
            "white": colors["foreground"],
            "dim": f"dim {colors['muted']}",
            "egg.user": colors["user"],
            "egg.assistant": colors["assistant"],
            "egg.system": colors["system"],
            "egg.tool": colors["tool"],
            "egg.reasoning": colors["reasoning"],
            "egg.tool_call": colors["tool_call"],
            "egg.accent": colors["accent"],
            "egg.foreground": colors["foreground"],
            "egg.muted": colors["muted"],
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
