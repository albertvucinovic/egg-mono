from __future__ import annotations

from eggdisplay import OutputPanel


def test_output_panel_plain_mode_preserves_square_brackets() -> None:
    panel = OutputPanel(style=OutputPanel.PanelStyle(markup=False))
    panel.set_content("[Assistant (streaming)]\nThe [test] token stays visible")

    rendered = panel.render()
    plain = rendered.renderable.plain

    assert "[Assistant (streaming)]" in plain
    assert "The [test] token stays visible" in plain


def test_output_panel_sanitizes_control_characters() -> None:
    panel = OutputPanel(style=OutputPanel.PanelStyle(markup=False))
    panel.set_content("abc\x00\x08\ud83ddef")

    rendered = panel.render()
    plain = rendered.renderable.plain

    assert "\x00" not in plain
    assert "\x08" not in plain
    assert "\ud83d" not in plain
    assert "abc" in plain and "def" in plain
    assert "\uFFFD" in plain


def test_output_panel_recombines_surrogate_pairs() -> None:
    panel = OutputPanel(style=OutputPanel.PanelStyle(markup=False))
    panel.set_content("hi \ud83d\ude00 there")

    rendered = panel.render()
    plain = rendered.renderable.plain

    assert "😀" in plain
    assert "\ud83d" not in plain
    assert "\ude00" not in plain