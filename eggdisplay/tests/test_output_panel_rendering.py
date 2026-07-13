from __future__ import annotations

from eggdisplay import OutputPanel
from rich.console import Console
import io
import os
import shutil


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


def test_wrapped_output_panel_height_matches_visual_rows_without_blank_slack(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((44, 24)),
    )
    panel = OutputPanel(
        title="Children",
        initial_height=3,
        max_height=24,
        style=OutputPanel.PanelStyle(line_wrap_mode="wrap"),
    )
    panel.set_content("one\ntwo")

    assert panel.calculate_height() == 4

    buf = io.StringIO()
    Console(file=buf, width=44).print(panel.render())
    assert len(buf.getvalue().splitlines()) == 4


def test_wrapped_output_panel_measures_wrapped_identity_before_render(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((44, 24)),
    )
    panel = OutputPanel(
        title="Children",
        initial_height=3,
        max_height=24,
        style=OutputPanel.PanelStyle(line_wrap_mode="wrap", markup=False),
    )
    panel.set_content(
        "Current: full-thread-id | Name: A descriptive thread name | "
        "Description: A useful description that exceeds a narrow row"
    )

    buf = io.StringIO()
    Console(file=buf, width=44).print(panel.render())
    rendered = buf.getvalue().replace("│", " ")
    normalized = " ".join(rendered.split())

    assert panel.calculate_height() > 4
    assert "full-thread-id" in normalized
    assert "A descriptive thread name" in normalized
    assert "A useful description" in normalized


def test_output_panel_max_height_reserves_visible_overflow_notice(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((44, 24)),
    )
    panel = OutputPanel(
        title="Output",
        initial_height=3,
        max_height=5,
        style=OutputPanel.PanelStyle(line_wrap_mode="crop", markup=False),
    )
    panel.set_content("\n".join(f"line {number}" for number in range(10)))

    buf = io.StringIO()
    Console(file=buf, width=44).print(panel.render())
    rendered = buf.getvalue()

    assert "line 9" in rendered
    assert "lines above" in rendered
    assert rendered.index("lines above") < rendered.index("line 9")
    assert len(rendered.splitlines()) == 5


def test_precalculated_height_reuses_visual_segments(monkeypatch) -> None:
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((44, 24)),
    )
    panel = OutputPanel(
        style=OutputPanel.PanelStyle(line_wrap_mode="wrap", markup=False),
    )
    panel.set_content("a long line that wraps at a narrow terminal width")
    calls = 0
    original = panel._content_segments

    def counted(width):
        nonlocal calls
        calls += 1
        return original(width)

    monkeypatch.setattr(panel, "_content_segments", counted)
    panel.calculate_height()
    panel.render()

    assert calls == 1