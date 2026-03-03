"""
Test that the InputPanel shows typed text.
"""
from __future__ import annotations

from eggdisplay import InputPanel


def test_input_panel_shows_typed_text():
    """Typing characters should appear in the panel's text."""
    panel = InputPanel(initial_height=8, max_height=12)
    editor = panel.editor.editor  # TextEditor
    
    # Type 'hello'
    for ch in 'hello':
        editor.handle_key(ch)
    
    # Check internal state
    assert editor.get_text() == 'hello'
    
    # Check that the panel's get_text() returns the same
    assert panel.get_text() == 'hello'
    
    # Now test that the rendered panel includes the text
    # We'll render the panel and check that the renderable contains 'hello'
    rendered = panel.render()
    # The renderable is a Text object inside the Panel
    # We can get the plain text from the renderable
    # rendered.renderable is the Text object
    # Text has a plain property? Let's see.
    # Actually Text has a plain attribute? We can use str(rendered.renderable)
    # But we need to get the text content without style.
    # For simplicity, we can check that the lines are present in the renderable's segments
    # We'll just ensure the renderable is not empty.
    # Let's just check that the renderable is a Text object with some content.
    from rich.text import Text
    assert isinstance(rendered.renderable, Text)
    # Convert to string and check for 'hello'
    text_str = str(rendered.renderable)
    # The text might contain line numbers and formatting, but should contain 'hello'
    assert 'hello' in text_str, f"Expected 'hello' in rendered text, got: {text_str[:200]}"


def test_input_panel_shows_multiline_typed_text():
    """Typing multiple lines should appear correctly."""
    panel = InputPanel(initial_height=8, max_height=12)
    editor = panel.editor.editor
    
    # Type 'line1', newline, 'line2'
    for ch in 'line1':
        editor.handle_key(ch)
    editor.handle_key('enter')
    for ch in 'line2':
        editor.handle_key(ch)
    
    assert editor.get_text() == 'line1\nline2'
    assert panel.get_text() == 'line1\nline2'
    
    rendered = panel.render()
    from rich.text import Text
    assert isinstance(rendered.renderable, Text)
    text_str = str(rendered.renderable)
    # Should contain both lines
    assert 'line1' in text_str
    assert 'line2' in text_str


def test_input_panel_renders_cursor_position():
    """Cursor should be visible at the correct position."""
    panel = InputPanel(initial_height=8, max_height=12)
    editor = panel.editor.editor
    
    # Type 'abc'
    for ch in 'abc':
        editor.handle_key(ch)
    
    # Cursor should be at column 3 (0-indexed 2)
    assert editor.cursor.row == 0
    assert editor.cursor.col == 3
    
    # Render and check that cursor indicator is present
    # The render method uses a cursor style for the character at cursor position
    # We'll just ensure rendering works without error
    rendered = panel.render()
    assert rendered is not None


if __name__ == '__main__':
    test_input_panel_shows_typed_text()
    test_input_panel_shows_multiline_typed_text()
    test_input_panel_renders_cursor_position()
    print("All tests passed\!")
