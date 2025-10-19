#!/usr/bin/env python3
"""
Simple test to verify text editor functionality without interactive mode.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from text_editor import TextEditor


def test_editor():
    """Test the text editor's core functionality."""
    
    print("Testing Text Editor Core Functionality")
    print("=" * 50)
    
    # Test 1: Initial text
    editor = TextEditor(initial_text="Hello, World!")
    assert editor.get_text() == "Hello, World!"
    print("✓ Initial text set correctly")
    
    # Test 2: Set text
    editor.set_text("New text\nwith multiple\nlines")
    assert editor.get_text() == "New text\nwith multiple\nlines"
    print("✓ Set text works correctly")
    
    # Test 3: Insert text
    editor.set_text("Start")
    editor.cursor.row = 0
    editor.cursor.col = 5  # End of line
    editor.insert_text(" end")
    assert editor.get_text() == "Start end"
    print("✓ Insert text works correctly")
    
    # Test 4: Delete character
    editor.set_text("abc")
    editor.cursor.row = 0
    editor.cursor.col = 1  # Position at 'b'
    editor.delete_char()
    assert editor.get_text() == "ac"
    print("✓ Delete character works correctly")
    
    # Test 5: Backspace
    editor.set_text("abc")
    editor.cursor.row = 0
    editor.cursor.col = 1  # Position after 'a', before 'b'
    editor.backspace()
    assert editor.get_text() == "bc"  # 'a' should be deleted
    print("✓ Backspace works correctly")
    
    # Test 6: Newline
    editor.set_text("line1")
    editor.cursor.row = 0
    editor.cursor.col = 3  # Position in middle
    editor.insert_newline()
    assert editor.get_text() == "lin\ne1"
    print("✓ Newline insertion works correctly")
    
    # Test 7: Cursor movement
    editor.set_text("line1\nline2\nline3")
    editor.cursor.row = 0
    editor.cursor.col = 0
    editor.move_cursor(1, 2)  # Move to line2, position 2
    assert editor.cursor.row == 1
    assert editor.cursor.col == 2
    print("✓ Cursor movement works correctly")
    
    # Test 8: Event listeners
    events_captured = []
    
    def capture_event(*args):
        events_captured.append(args)
    
    editor = TextEditor(initial_text="test")
    editor.add_event_listener('text_change', capture_event)
    editor.insert_text("ing")
    
    assert len(events_captured) > 0
    print("✓ Event listeners work correctly")
    
    print("\nAll tests passed! 🎉")
    print("\nNote: The interactive mode requires terminal raw mode support.")
    print("For a working interactive demo, run the example_usage.py script.")


if __name__ == "__main__":
    test_editor()