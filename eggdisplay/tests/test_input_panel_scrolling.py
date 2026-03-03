from __future__ import annotations

import re

from rich.console import Console

from eggdisplay import InputPanel


def _render_to_text(panel) -> str:
    con = Console(width=80, record=True)
    con.print(panel)
    return con.export_text()


def _extract_line_numbers(rendered: str) -> list[int]:
    nums: list[int] = []
    for line in rendered.splitlines():
        m = re.search(r"\b(\d+):", line)
        if m:
            nums.append(int(m.group(1)))
    return nums


def test_input_panel_scrolls_to_keep_cursor_visible():
    p = InputPanel(max_height=8)
    # 30 logical lines
    text = "\n".join(f"L{i}" for i in range(1, 31))
    p.editor.editor.set_text(text)

    # Put cursor at the end
    p.editor.editor.cursor.row = 29
    p.editor.editor.cursor.col = 0

    rendered = _render_to_text(p.render())
    nums = _extract_line_numbers(rendered)

    # Ensure we are showing the tail area and include line 30
    assert 30 in nums
    assert min(nums) > 1


def test_input_panel_cursor_near_top_resets_scroll():
    p = InputPanel(max_height=8)
    text = "\n".join(f"L{i}" for i in range(1, 31))
    p.editor.editor.set_text(text)

    # Move cursor down first to make it scroll
    p.editor.editor.cursor.row = 29
    p.render()

    # Now go back near the top
    p.editor.editor.cursor.row = 0
    rendered = _render_to_text(p.render())
    nums = _extract_line_numbers(rendered)

    assert 1 in nums


def test_suggestions_scroll_and_selected_item_visible():
    p = InputPanel(max_height=10)
    ed = p.editor.editor
    ed.set_text("hello")
    ed.cursor.row = 0
    ed.cursor.col = 5

    # Force a big suggestion list
    ed._completion_active = True
    ed._completion_items = [{"display": f"item{i}", "insert": f"item{i}"} for i in range(1, 51)]
    ed._completion_index = 45  # near the end

    rendered = _render_to_text(p.render())

    # Selected item should be visible in output
    assert "item46" in rendered
    # Some items at the top should not be visible (scrolled)
    assert "item1" not in rendered


def test_suggestions_do_not_cut_off_bottom_entries_when_room():
    p = InputPanel(max_height=12)
    ed = p.editor.editor
    ed.set_text("hello")
    ed.cursor.row = 0
    ed.cursor.col = 5

    ed._completion_active = True
    ed._completion_items = [{"display": f"s{i}", "insert": f"s{i}"} for i in range(1, 6)]
    ed._completion_index = 0

    rendered = _render_to_text(p.render())
    # When there are only 5 entries, all should be visible.
    for i in range(1, 6):
        assert f"s{i}" in rendered
