from __future__ import annotations

from eggdisplay import OutputPanel
from rich.console import Console
import io


def test_output_panel_title_uses_border_style_as_base_color() -> None:
    panel = OutputPanel(
        title="Output",
        style=OutputPanel.PanelStyle(border_style="cyan", title_style="bold cyan"),
    )

    rendered = panel.render()

    assert rendered.title.plain == "Output"
    assert rendered.title.style == "cyan"
    assert any(span.style == "bold" for span in rendered.title.spans)


def test_output_panel_title_preserves_inline_markup_colors() -> None:
    panel = OutputPanel(
        title="[red]Sandboxing[OFF][/red]  [green]Autoapproval[Off][/green]",
        style=OutputPanel.PanelStyle(border_style="blue", title_style="bold"),
    )

    rendered = panel.render()
    buf = io.StringIO()
    Console(file=buf, width=100, force_terminal=True, color_system="truecolor").print(rendered)
    first_line = buf.getvalue().splitlines()[0]

    assert "\x1b[1;31mSandboxing[OFF]" in first_line
    assert "\x1b[1;32mAutoapproval[Off]" in first_line


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