import pytest


from eggdisplay import TextEditor


def test_insert_text_block_multiline_at_end_updates_lines_and_cursor():
    ed = TextEditor(initial_text="abc")
    ed.cursor.row = 0
    ed.cursor.col = 3

    ed.insert_text_block("X\nY\nZ")

    assert ed.lines == ["abcX", "Y", "Z"]
    assert (ed.cursor.row, ed.cursor.col) == (2, 1)


def test_insert_text_block_multiline_in_middle_preserves_after_text():
    ed = TextEditor(initial_text="helloWORLD")
    ed.cursor.row = 0
    ed.cursor.col = 5

    ed.insert_text_block("1\n2")

    assert ed.lines == ["hello1", "2WORLD"]
    assert (ed.cursor.row, ed.cursor.col) == (1, 1)


def test_handle_key_multichar_string_is_treated_as_paste():
    ed = TextEditor(initial_text="")
    assert ed.handle_key("hi") is True
    assert ed.get_text() == "hi"
    assert (ed.cursor.row, ed.cursor.col) == (0, 2)


def test_handle_key_multichar_with_newlines_is_treated_as_paste_block():
    ed = TextEditor(initial_text="")
    assert ed.handle_key("a\nb\nc") is True
    assert ed.lines == ["a", "b", "c"]
    assert (ed.cursor.row, ed.cursor.col) == (2, 1)
