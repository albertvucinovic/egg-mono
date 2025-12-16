from __future__ import annotations

import re

from rich.console import Console

from eggdisplay import InputPanel


def _render_to_text(panel) -> str:
    con = Console(width=60, record=True)
    con.print(panel)
    return con.export_text()


def _visible_editor_lines(rendered: str) -> list[str]:
    # Capture the inner panel rows that have a line prefix like " 1: "
    out: list[str] = []
    for line in rendered.splitlines():
        if re.search(r"\b\d+: ", line):
            out.append(line)
    return out


def test_horizontal_scroll_keeps_cursor_column_visible_and_no_wrapping():
    p = InputPanel(max_height=8)
    # Ensure line is much longer than the render width so horizontal scrolling
    # must occur.
    long_line = ("one two three four five six seven eight nine ten " * 6).strip() + " END"
    p.editor.editor.set_text(long_line)
    p.editor.editor.cursor.row = 0

    # Make InputPanel's width estimate match the render console width.
    p.editor.console = Console(width=60)

    # Start at column 0, should show prefix "one"
    p.editor.editor.cursor.col = 0
    t0 = _render_to_text(p.render())
    lines0 = _visible_editor_lines(t0)
    assert any("one two" in l for l in lines0)

    # Move cursor far right; panel should horizontally scroll so that far-right
    # content becomes visible (and still not wrap).
    p.editor.editor.cursor.col = len(long_line)
    t1 = _render_to_text(p.render())
    lines1 = _visible_editor_lines(t1)

    # Right side of line should appear somewhere in the visible line.
    assert any("END" in l for l in lines1)

    # Still only one editor row for the line (no wrapping into multiple rows).
    assert len(lines1) == 1


def test_all_lines_reachable_and_visible_when_cursor_moves_through_document():
    p = InputPanel(max_height=8)
    payload_lines = [f"this is line {i} with multiple words" for i in range(1, 31)]
    p.editor.editor.set_text("\n".join(payload_lines))

    # Walk the cursor down; each line should become visible when selected.
    for idx, expected in enumerate(payload_lines):
        p.editor.editor.cursor.row = idx
        p.editor.editor.cursor.col = 0
        rendered = _render_to_text(p.render())
        # The current line should be visible somewhere in the panel.
        assert expected in rendered
