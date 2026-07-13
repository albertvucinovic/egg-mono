from __future__ import annotations

import re
from pathlib import Path

import pytest

from eggw.autocomplete.autocomplete import THEMES as AUTOCOMPLETE_THEMES
from eggw.commands.utility import THEMES as COMMAND_THEMES
from eggw.theme_contract import (
    COMPATIBILITY_TOKENS,
    ROLE_NAMES,
    SEMANTIC_COLOR_TOKENS,
    STATUS_NAMES,
    composite,
    contrast_ratio,
    mix,
    parse_hex,
    resolve_theme,
)
from eggw.theme_registry import DEFAULT_THEME, THEMES, THEME_METADATA, normalize_theme_name, theme_contract

ROOT = Path(__file__).resolve().parents[2]
GENERATED_CSS = ROOT / "eggw" / "frontend" / "src" / "app" / "themes.generated.css"
GLOBALS_CSS = ROOT / "eggw" / "frontend" / "src" / "app" / "globals.css"


def ratio(values: dict[str, str], foreground: str, background: str) -> float:
    return contrast_ratio(parse_hex(values[foreground]), parse_hex(values[background]))


def assert_pair(theme: str, values: dict[str, str], foreground: str, background: str, minimum: float) -> None:
    actual = ratio(values, foreground, background)
    assert actual >= minimum, f"{theme}: {foreground}/{background} = {actual:.3f}, expected >= {minimum}"


def test_registry_is_unique_complete_and_shared() -> None:
    assert len(THEMES) == len(set(THEMES)) == 31
    assert DEFAULT_THEME == "dark"
    assert tuple(COMMAND_THEMES) == THEMES
    assert tuple(AUTOCOMPLETE_THEMES) == THEMES
    assert set(THEME_METADATA) == set(THEMES)
    assert len(theme_contract()["families"]) == 16
    assert sum(item["treatment"] == "uniform" for item in THEME_METADATA.values()) == 16
    assert sum(item["treatment"] == "tinted" for item in THEME_METADATA.values()) == 15
    assert normalize_theme_name("OCEAN-BACKGROUND") == "ocean-background"
    assert normalize_theme_name("unknown") == DEFAULT_THEME


@pytest.mark.parametrize("theme", THEMES)
def test_every_theme_resolves_the_complete_contract(theme: str) -> None:
    values = resolve_theme(theme)
    assert set(SEMANTIC_COLOR_TOKENS).issubset(values)
    assert set(COMPATIBILITY_TOKENS).issubset(values)
    assert values["theme-polarity"] == THEME_METADATA[theme]["polarity"]
    assert values["theme-treatment"] == THEME_METADATA[theme]["treatment"]
    for token in SEMANTIC_COLOR_TOKENS:
        assert re.fullmatch(r"#[0-9a-f]{6}|rgba\([^)]+\)", values[token]), (theme, token, values[token])


@pytest.mark.parametrize("theme", THEMES)
def test_every_theme_meets_the_semantic_contrast_contract(theme: str) -> None:
    values = resolve_theme(theme)
    text_pairs = (
        ("text-primary", "surface-canvas"), ("text-primary", "surface-panel"), ("text-primary", "surface-raised"),
        ("text-secondary", "surface-canvas"), ("text-secondary", "surface-panel"),
        ("text-secondary", "surface-raised"), ("text-tertiary", "surface-canvas"),
        ("text-tertiary", "surface-panel"), ("text-tertiary", "surface-raised"),
        ("text-heading", "surface-canvas"), ("text-heading", "surface-panel"),
        ("link", "surface-canvas"), ("link", "surface-panel"), ("link-hover", "surface-panel"),
        ("control-text", "control-bg"), ("control-text", "control-bg-hover"),
        ("control-text", "control-bg-active"), ("control-placeholder", "control-bg"),
        ("action-primary-text", "action-primary-bg"), ("action-primary-text", "action-primary-hover"),
        ("action-secondary-text", "action-secondary-bg"), ("action-secondary-text", "action-secondary-hover"),
        ("action-danger-text", "action-danger-bg"), ("action-danger-text", "action-danger-hover"),
        ("action-warning-text", "action-warning-bg"), ("action-warning-text", "action-warning-hover"),
        ("selection-text", "selection-bg"),
        ("code-text", "code-surface"),
    )
    boundary_pairs = (
        ("border-default", "surface-canvas"), ("border-default", "surface-panel"),
        ("border-strong", "surface-canvas"), ("border-strong", "surface-panel"),
        ("control-border", "control-bg"), ("control-border", "surface-panel"),
        ("focus-ring", "surface-panel"), ("focus-ring", "control-bg"),
        ("action-secondary-border", "action-secondary-bg"),
        ("action-danger-border", "action-danger-bg"), ("action-warning-border", "action-warning-bg"),
        ("scrollbar-thumb", "scrollbar-track"), ("scrollbar-thumb-hover", "scrollbar-track"),
    )
    for foreground, background in text_pairs:
        assert_pair(theme, values, foreground, background, 4.5)
    for foreground, background in boundary_pairs:
        assert_pair(theme, values, foreground, background, 3.0)
    for status in STATUS_NAMES:
        assert_pair(theme, values, f"status-{status}-text", f"status-{status}-surface", 4.5)
        assert_pair(theme, values, f"status-{status}-border", f"status-{status}-surface", 3.0)
        assert_pair(theme, values, f"status-{status}-marker", "surface-panel", 3.0)
    for role in ROLE_NAMES:
        assert_pair(theme, values, f"role-{role}-content", f"role-{role}-surface", 4.5)
        assert_pair(theme, values, f"role-{role}-label", f"role-{role}-surface", 4.5)
        assert_pair(theme, values, f"role-{role}-label", "code-surface", 4.5)
        assert_pair(theme, values, f"role-{role}-border", f"role-{role}-surface", 3.0)
        assert_pair(theme, values, f"role-{role}-marker", "surface-panel", 3.0)
    for syntax in ("comment", "keyword", "string", "number", "function", "variable", "operator"):
        assert_pair(theme, values, f"syntax-{syntax}", "code-surface", 4.5)


def test_alpha_compositing_color_mix_and_wcag_known_values() -> None:
    white, black = parse_hex("#fff"), parse_hex("#000")
    assert composite((1.0, 1.0, 1.0, 0.5), black) == pytest.approx((0.5, 0.5, 0.5))
    assert mix(black, white, 0.25) == pytest.approx((0.25, 0.25, 0.25))
    assert contrast_ratio(white, black) == pytest.approx(21.0)
    # WCAG reference-style regression: GitHub blue over its 15% tint on dark canvas.
    dark = parse_hex("#0d1117")
    tinted = composite((*parse_hex("#388bfd"), 0.15), dark)
    assert contrast_ratio(parse_hex("#388bfd"), tinted) == pytest.approx(4.7121, rel=1e-3)


def test_known_light_background_and_colorful_light_regressions_are_fixed() -> None:
    light_background = resolve_theme("light-background")
    assert ratio(light_background, "role-system-label", "role-system-surface") >= 4.5
    assert ratio(light_background, "role-tool-call-label", "role-tool-call-surface") >= 4.5
    colorful_light = resolve_theme("colorful-light")
    assert ratio(colorful_light, "link", "surface-canvas") >= 4.5
    assert ratio(colorful_light, "action-primary-text", "action-primary-bg") >= 4.5


def test_theme_identities_are_distinct_and_background_variants_are_tinted() -> None:
    uniform = [theme for theme in THEMES if THEME_METADATA[theme]["treatment"] == "uniform"]
    signatures = {
        tuple(resolve_theme(theme)[token] for token in (
            "surface-canvas", "surface-panel", "link", "role-user-label",
            "role-assistant-label", "role-reasoning-label",
        ))
        for theme in uniform
    }
    assert len(signatures) == len(uniform) == 16
    for theme in THEMES:
        metadata = THEME_METADATA[theme]
        if metadata["treatment"] != "tinted":
            continue
        tinted, base = resolve_theme(theme), resolve_theme(metadata["family"])
        assert tinted["surface-panel"] != base["surface-panel"]
        assert all(tinted[f"role-{role}-surface"] != base[f"role-{role}-surface"] for role in ROLE_NAMES)


def test_generated_css_is_current_and_has_one_selector_per_theme() -> None:
    import importlib.util

    script = ROOT / "eggw" / "scripts" / "generate_theme_css.py"
    spec = importlib.util.spec_from_file_location("generate_theme_css", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert GENERATED_CSS.read_text(encoding="utf-8") == module.render()

    css = GENERATED_CSS.read_text(encoding="utf-8")
    selectors = re.findall(r'\[data-theme="([^"]+)"\]', css)
    assert selectors == list(THEMES)
    assert len(selectors) == len(set(selectors))
    assert '@import "./themes.generated.css";' in GLOBALS_CSS.read_text(encoding="utf-8")


def test_compatibility_aliases_map_to_semantic_tokens() -> None:
    values = resolve_theme("coffee-background")
    assert values["foreground"] == values["text-primary"]
    assert values["accent"] == values["link"]
    assert values["assistant-msg-bg"] == values["role-assistant-surface"]
    assert values["assistant-msg-text"] == values["role-assistant-label"]
    assert values["tool-call-border"] == values["role-tool-call-border"]
