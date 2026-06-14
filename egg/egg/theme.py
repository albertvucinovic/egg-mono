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
    "dark": {"user": "#7cc7ff", "assistant": "#e6edf3", "system": "#88c0d0", "tool": "#a3e635", "reasoning": "#c084fc", "tool_call": "#facc15", "accent": "#58a6ff", "foreground": "#e6edf3", "muted": "#8b949e"},
    "cyberpunk": {"user": "#ff2bd6", "assistant": "#00f5ff", "system": "#f5ff00", "tool": "#39ff14", "reasoning": "#bd00ff", "tool_call": "#ff8c00", "accent": "#ff2bd6", "foreground": "#f8eaff", "muted": "#9b5bb5"},
    "forest": {"user": "#a7f070", "assistant": "#7ee787", "system": "#c5f467", "tool": "#9acd32", "reasoning": "#c49a6c", "tool_call": "#f6c453", "accent": "#76c893", "foreground": "#e0f2df", "muted": "#799f7c"},
    "ocean": {"user": "#38bdf8", "assistant": "#67e8f9", "system": "#22d3ee", "tool": "#2dd4bf", "reasoning": "#818cf8", "tool_call": "#fb7185", "accent": "#06b6d4", "foreground": "#d9f7ff", "muted": "#6da3b7"},
    "sunset": {"user": "#fb923c", "assistant": "#fdba74", "system": "#facc15", "tool": "#f97316", "reasoning": "#c084fc", "tool_call": "#f43f5e", "accent": "#f97316", "foreground": "#fff1e6", "muted": "#b58a7a"},
    "mono": {"user": "#ffffff", "assistant": "#d4d4d4", "system": "#f5f5f5", "tool": "#a3a3a3", "reasoning": "#8a8a8a", "tool_call": "#eeeeee", "accent": "#ffffff", "foreground": "#e5e5e5", "muted": "#737373"},
    "midnight": {"user": "#93c5fd", "assistant": "#c7d2fe", "system": "#60a5fa", "tool": "#a5b4fc", "reasoning": "#c4b5fd", "tool_call": "#fcd34d", "accent": "#818cf8", "foreground": "#dbeafe", "muted": "#6b7280"},
    "disney": {"user": "#ffd700", "assistant": "#60a5fa", "system": "#ff69b4", "tool": "#98fb98", "reasoning": "#da70d6", "tool_call": "#fff176", "accent": "#4169e1", "foreground": "#f0f4ff", "muted": "#7a8caa"},
    "fruit": {"user": "#ff9f1c", "assistant": "#e11d48", "system": "#facc15", "tool": "#84cc16", "reasoning": "#9333ea", "tool_call": "#fb7185", "accent": "#f97316", "foreground": "#ffe4e8", "muted": "#aa6080"},
    "vegetables": {"user": "#f97316", "assistant": "#16a34a", "system": "#dc2626", "tool": "#84cc16", "reasoning": "#7c3aed", "tool_call": "#eab308", "accent": "#65a30d", "foreground": "#e8f5d0", "muted": "#6b8b6b"},
    "coffee": {"user": "#d6a15f", "assistant": "#c8a27a", "system": "#f5deb3", "tool": "#b08968", "reasoning": "#a0522d", "tool_call": "#f1b66a", "accent": "#cd853f", "foreground": "#f5e6d3", "muted": "#9b7c60"},
    "matrix": {"user": "#00ff41", "assistant": "#00c853", "system": "#00ff00", "tool": "#00b300", "reasoning": "#00a000", "tool_call": "#66ff66", "accent": "#00ff00", "foreground": "#00ff41", "muted": "#008f11"},
    "light": {"user": "#1d4ed8", "assistant": "#111827", "system": "#0e7490", "tool": "#047857", "reasoning": "#7e22ce", "tool_call": "#b45309", "accent": "#2563eb", "foreground": "#111827", "muted": "#4b5563"},
    "light-mono": {"user": "#000000", "assistant": "#1f1f1f", "system": "#000000", "tool": "#333333", "reasoning": "#555555", "tool_call": "#000000", "accent": "#000000", "foreground": "#000000", "muted": "#666666"},
    "colorful": {"user": "#22c55e", "assistant": "#06b6d4", "system": "#3b82f6", "tool": "#eab308", "reasoning": "#d946ef", "tool_call": "#f59e0b", "accent": "#06b6d4", "foreground": "#e5e5e5", "muted": "#a3a3a3"},
    "colorful-light": {"user": "#16a34a", "assistant": "#0891b2", "system": "#2563eb", "tool": "#ca8a04", "reasoning": "#c026d3", "tool_call": "#d97706", "accent": "#0891b2", "foreground": "#0a0a0a", "muted": "#0a0a0a"},
}

_THEME_MODIFIERS: dict[str, dict[str, str]] = {
    "dark": {"user": "bold", "assistant": "", "system": "bold", "tool": "", "reasoning": "italic", "tool_call": "bold"},
    "cyberpunk": {"user": "bold", "assistant": "bold", "system": "bold", "tool": "bold", "reasoning": "italic", "tool_call": "bold italic"},
    "forest": {"user": "bold", "assistant": "", "system": "bold", "tool": "bold", "reasoning": "italic", "tool_call": "bold"},
    "ocean": {"user": "bold", "assistant": "", "system": "bold", "tool": "italic", "reasoning": "italic", "tool_call": "bold"},
    "sunset": {"user": "bold", "assistant": "", "system": "bold", "tool": "bold", "reasoning": "italic", "tool_call": "bold"},
    "mono": {"user": "bold", "assistant": "", "system": "bold", "tool": "", "reasoning": "italic", "tool_call": "bold"},
    "midnight": {"user": "bold", "assistant": "", "system": "bold", "tool": "", "reasoning": "italic", "tool_call": "bold"},
    "disney": {"user": "bold", "assistant": "bold", "system": "bold italic", "tool": "", "reasoning": "italic", "tool_call": "bold"},
    "fruit": {"user": "bold", "assistant": "bold", "system": "bold", "tool": "italic", "reasoning": "italic", "tool_call": "bold"},
    "vegetables": {"user": "bold", "assistant": "bold", "system": "", "tool": "bold", "reasoning": "italic", "tool_call": "bold"},
    "coffee": {"user": "bold", "assistant": "", "system": "bold", "tool": "", "reasoning": "italic", "tool_call": "bold"},
    "matrix": {"user": "bold", "assistant": "", "system": "bold", "tool": "", "reasoning": "italic", "tool_call": "bold"},
    "light": {"user": "bold", "assistant": "", "system": "bold", "tool": "", "reasoning": "italic", "tool_call": "bold"},
    "light-mono": {"user": "bold", "assistant": "", "system": "bold", "tool": "", "reasoning": "italic", "tool_call": "bold"},
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
