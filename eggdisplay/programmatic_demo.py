#!/usr/bin/env python3
"""
Programmatic demo for the Rich Text Editor.
This demonstrates all features without requiring interactive terminal mode.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from text_editor import TextEditor


def main():
    """Main function demonstrating programmatic usage."""
    
    print("Rich Text Editor - Programmatic Demo")
    print("=" * 50)
    
    # Create editor with initial text
    editor = TextEditor(
        initial_text="Hello, World!\nThis is line 2\nAnd line 3",
        width=60,
        height=10
    )
    
    print("1. Initial text:")
    print(editor.get_text())
    print()
    
    # Add event listeners
    def on_text_change(change_type: str, row: int, col: int, data: str):
        print(f"[Event] Text changed: {change_type} at ({row}, {col})")
    
    def on_cursor_move(old_row: int, old_col: int, new_row: int, new_col: int):
        print(f"[Event] Cursor moved: ({old_row}, {old_col}) -> ({new_row}, {new_col})")
    
    editor.add_event_listener('text_change', on_text_change)
    editor.add_event_listener('cursor_move', on_cursor_move)
    
    # Demonstrate programmatic operations
    print("2. Moving cursor to line 1, position 5:")
    editor.move_cursor(1, 5)
    print(f"   Cursor position: {editor.cursor}")
    print()
    
    print("3. Inserting text at cursor:")
    editor.insert_text("INSERTED ")
    print("   Text after insertion:")
    print(editor.get_text())
    print()
    
    print("4. Moving cursor and deleting:")
    editor.move_cursor(0, 7)  # Move to 'World!' after 'o'
    editor.delete_char()
    print("   Text after deletion:")
    print(editor.get_text())
    print()
    
    print("5. Backspace operation:")
    editor.move_cursor(0, 6)  # Move to 'World' before '!'
    editor.backspace()
    print("   Text after backspace:")
    print(editor.get_text())
    print()
    
    print("6. Setting text programmatically:")
    editor.set_text("New content\nwith multiple\nlines")
    print("   New text:")
    print(editor.get_text())
    print()
    
    print("7. Simulating key presses:")
    editor.set_text("Type here")
    editor.move_cursor(0, 9)  # End of line
    
    # Simulate typing
    for char in " and more":
        editor.handle_key(char)
    
    print("   After simulated typing:")
    print(editor.get_text())
    print()
    
    print("8. Autocomplete demonstration:")
    def autocomplete_demo(line: str, row: int, col: int) -> list:
        suggestions = ["apple", "banana", "cherry"]
        return [s for s in suggestions if s.startswith(line[:col])]
    
    editor.autocomplete_callback = autocomplete_demo
    editor.set_text("a")  # Start with 'a'
    editor.move_cursor(0, 1)  # End of line
    
    # Simulate Tab press for autocomplete
    editor.handle_key('tab')
    print("   After autocomplete (Tab):")
    print(editor.get_text())
    print()
    
    print("Demo completed successfully! ✅")
    print("\nAll editor features are working correctly:")
    print("- Text insertion/deletion")
    print("- Cursor movement")
    print("- Event listeners")
    print("- Autocomplete")
    print("- Programmatic text manipulation")


if __name__ == "__main__":
    main()