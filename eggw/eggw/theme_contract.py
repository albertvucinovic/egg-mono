"""Resolve EggW's canonical browser theme data into semantic CSS tokens."""
from __future__ import annotations

from typing import Iterable

from .theme_registry import THEME_METADATA, theme_contract

RGB = tuple[float, float, float]
ROLE_NAMES = ("user", "assistant", "system", "tool", "reasoning", "tool-call")
STATUS_NAMES = ("info", "success", "warning", "danger", "special")

SEMANTIC_COLOR_TOKENS = (
    "surface-canvas", "surface-panel", "surface-raised", "surface-subtle", "surface-inset",
    "surface-overlay", "text-primary", "text-secondary", "text-tertiary", "text-heading",
    "text-inverse", "link", "link-hover", "border-subtle", "border-default", "border-strong",
    "control-bg", "control-bg-hover", "control-bg-active", "control-border", "control-text",
    "control-placeholder", "focus-ring", "action-primary-bg", "action-primary-hover",
    "action-primary-text", "action-secondary-bg", "action-secondary-hover", "action-secondary-text",
    "action-secondary-border", "action-danger-bg", "action-danger-hover", "action-danger-text",
    "action-danger-border", "action-warning-bg", "action-warning-hover", "action-warning-text",
    "action-warning-border", "selection-bg", "selection-text", "code-surface", "code-text",
    "scrollbar-track", "scrollbar-thumb", "scrollbar-thumb-hover", "scrim",
    "syntax-comment", "syntax-keyword", "syntax-string", "syntax-number", "syntax-function",
    "syntax-variable", "syntax-operator",
    *(f"status-{status}-{part}" for status in STATUS_NAMES for part in ("text", "surface", "border", "marker")),
    *(f"role-{role}-{part}" for role in ROLE_NAMES for part in ("content", "label", "surface", "border", "marker")),
)

COMPATIBILITY_TOKENS = (
    "background", "foreground", "panel-bg", "panel-border", "accent", "accent-hover",
    "heading-color", "muted", "code-bg", "scrollbar-thumb", "selection-bg", "error",
    *(f"{role if role in ('reasoning', 'tool-call') else role + '-msg'}-{part}"
      for role in ROLE_NAMES for part in ("bg", "border", "text")),
)


def parse_hex(value: str) -> RGB:
    value = value.strip().removeprefix("#")
    if len(value) == 3:
        value = "".join(character * 2 for character in value)
    if len(value) != 6:
        raise ValueError(f"Expected #rgb or #rrggbb, got {value!r}")
    return tuple(int(value[index:index + 2], 16) / 255 for index in (0, 2, 4))  # type: ignore[return-value]


def to_hex(color: RGB) -> str:
    return "#" + "".join(f"{round(max(0, min(1, channel)) * 255):02x}" for channel in color)


def mix(first: RGB, second: RGB, second_weight: float) -> RGB:
    return tuple(a * (1 - second_weight) + b * second_weight for a, b in zip(first, second))  # type: ignore[return-value]


def composite(foreground: tuple[float, float, float, float], background: RGB) -> RGB:
    alpha = foreground[3]
    return tuple(foreground[index] * alpha + background[index] * (1 - alpha) for index in range(3))  # type: ignore[return-value]


def relative_luminance(color: RGB) -> float:
    linear = tuple(channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in color)
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(first: RGB, second: RGB) -> float:
    light, dark = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def ensure_contrast(color: RGB, backgrounds: Iterable[RGB], minimum: float, polarity: str) -> RGB:
    """Nudge a theme hue only as far as needed toward its contrast pole."""
    backgrounds = tuple(backgrounds)
    pole = (1.0, 1.0, 1.0) if polarity == "dark" else (0.0, 0.0, 0.0)
    if all(contrast_ratio(color, background) >= minimum for background in backgrounds):
        return color
    low, high = 0.0, 1.0
    for _ in range(24):
        weight = (low + high) / 2
        candidate = mix(color, pole, weight)
        if all(contrast_ratio(candidate, background) >= minimum for background in backgrounds):
            high = weight
        else:
            low = weight
    return mix(color, pole, high)


def _status_hues(polarity: str) -> dict[str, RGB]:
    values = {
        "dark": {"info": "#7cc7ff", "success": "#7ee787", "warning": "#f6c453", "danger": "#ff8a8a", "special": "#e2a7ff"},
        "light": {"info": "#1d4ed8", "success": "#137333", "warning": "#854d0e", "danger": "#b42318", "special": "#86198f"},
    }
    return {name: parse_hex(value) for name, value in values[polarity].items()}


def resolve_theme(name: str) -> dict[str, str]:
    metadata = THEME_METADATA[name]
    family = theme_contract()["families"][metadata["family"]]
    polarity = metadata["polarity"]
    canvas = parse_hex(family["background"])
    panel = canvas if metadata["treatment"] == "uniform" else parse_hex(family["panel"])
    raised = parse_hex(family["raised"])
    code = parse_hex(family["code"])
    text = ensure_contrast(parse_hex(family["text"]), (canvas, panel, raised, code), 4.55, polarity)
    secondary = ensure_contrast(parse_hex(family["secondary"]), (canvas, panel, raised), 4.55, polarity)
    tertiary = ensure_contrast(parse_hex(family["tertiary"]), (canvas, panel, raised), 4.55, polarity)
    heading = ensure_contrast(parse_hex(family["heading"]), (canvas, panel), 4.55, polarity)
    boundary = ensure_contrast(parse_hex(family["boundary"]), (canvas, panel, raised), 3.05, polarity)
    strong_boundary = ensure_contrast(mix(boundary, (1, 1, 1) if polarity == "dark" else (0, 0, 0), 0.18), (canvas, panel, raised), 3.05, polarity)
    subtle_boundary = mix(boundary, panel, 0.38)
    accent = ensure_contrast(parse_hex(family["accent"]), (canvas, panel), 4.55, polarity)
    accent_hover = ensure_contrast(mix(accent, (1, 1, 1) if polarity == "dark" else (0, 0, 0), 0.12), (canvas, panel), 4.55, polarity)
    inverse = canvas
    control = raised
    control_hover = mix(raised, accent, 0.10)
    control_active = mix(raised, accent, 0.17)
    selection = mix(canvas, accent, 0.28 if polarity == "dark" else 0.16)
    selection_text = ensure_contrast(text, (selection,), 4.55, polarity)

    values: dict[str, str] = {
        "theme-polarity": polarity,
        "theme-treatment": metadata["treatment"],
        "surface-canvas": to_hex(canvas), "surface-panel": to_hex(panel), "surface-raised": to_hex(raised),
        "surface-subtle": to_hex(mix(panel, text, 0.055)), "surface-inset": to_hex(code), "surface-overlay": to_hex(raised),
        "text-primary": to_hex(text), "text-secondary": to_hex(secondary), "text-tertiary": to_hex(tertiary),
        "text-heading": to_hex(heading), "text-inverse": to_hex(inverse), "link": to_hex(accent), "link-hover": to_hex(accent_hover),
        "border-subtle": to_hex(subtle_boundary), "border-default": to_hex(boundary), "border-strong": to_hex(strong_boundary),
        "control-bg": to_hex(control), "control-bg-hover": to_hex(control_hover), "control-bg-active": to_hex(control_active),
        "control-border": to_hex(boundary), "control-text": to_hex(text), "control-placeholder": to_hex(tertiary),
        "focus-ring": to_hex(accent),
        "action-primary-bg": to_hex(accent), "action-primary-hover": to_hex(accent_hover), "action-primary-text": to_hex(inverse),
        "action-secondary-bg": to_hex(control), "action-secondary-hover": to_hex(control_hover), "action-secondary-text": to_hex(text), "action-secondary-border": to_hex(boundary),
        "selection-bg": to_hex(selection), "selection-text": to_hex(selection_text),
        "code-surface": to_hex(code), "code-text": to_hex(text),
        "scrollbar-track": to_hex(panel), "scrollbar-thumb": to_hex(boundary), "scrollbar-thumb-hover": to_hex(strong_boundary),
        "scrim": "rgba(0, 0, 0, 0.62)" if polarity == "dark" else "rgba(17, 24, 39, 0.42)",
    }

    statuses = _status_hues(polarity)
    for status, raw_hue in statuses.items():
        surface = mix(panel, raw_hue, 0.12 if polarity == "dark" else 0.08)
        label = ensure_contrast(raw_hue, (surface, panel), 4.55, polarity)
        border = ensure_contrast(raw_hue, (surface,), 3.05, polarity)
        marker = ensure_contrast(raw_hue, (panel,), 3.05, polarity)
        for part, color in (("text", label), ("surface", surface), ("border", border), ("marker", marker)):
            values[f"status-{status}-{part}"] = to_hex(color)
    for action, status in (("danger", "danger"), ("warning", "warning")):
        hue = parse_hex(values[f"status-{status}-text"])
        values[f"action-{action}-bg"] = values[f"status-{status}-surface"]
        values[f"action-{action}-hover"] = to_hex(mix(parse_hex(values[f"status-{status}-surface"]), hue, 0.08))
        values[f"action-{action}-text"] = values[f"status-{status}-text"]
        values[f"action-{action}-border"] = values[f"status-{status}-border"]

    role_labels: dict[str, RGB] = {}
    for role in ROLE_NAMES:
        hue = parse_hex(family["roles"][role])
        surface = panel if metadata["treatment"] == "uniform" else mix(panel, hue, 0.10)
        label = ensure_contrast(hue, (surface,), 4.55, polarity)
        border = ensure_contrast(hue, (surface,), 3.05, polarity)
        marker = ensure_contrast(hue, (panel,), 3.05, polarity)
        role_labels[role] = label
        for part, color in (("content", text), ("label", label), ("surface", surface), ("border", border), ("marker", marker)):
            values[f"role-{role}-{part}"] = to_hex(color)

    syntax_sources = {
        "comment": secondary, "keyword": role_labels["reasoning"], "string": role_labels["tool"],
        "number": role_labels["tool-call"], "function": role_labels["assistant"],
        "variable": role_labels["user"], "operator": role_labels["system"],
    }
    for syntax, color in syntax_sources.items():
        values[f"syntax-{syntax}"] = to_hex(ensure_contrast(color, (code,), 4.55, polarity))

    # Compatibility bridge for components migrated in later slices.
    values.update({
        "background": values["surface-canvas"], "foreground": values["text-primary"],
        "panel-bg": values["surface-panel"], "panel-border": values["border-default"],
        "accent": values["link"], "accent-hover": values["link-hover"],
        "heading-color": values["text-heading"], "muted": values["text-secondary"],
        "code-bg": values["code-surface"], "error": values["status-danger-text"],
    })
    for role in ROLE_NAMES:
        legacy = role if role in ("reasoning", "tool-call") else f"{role}-msg"
        values[f"{legacy}-bg"] = values[f"role-{role}-surface"]
        values[f"{legacy}-border"] = values[f"role-{role}-border"]
        values[f"{legacy}-text"] = values[f"role-{role}-label"]
    return values
