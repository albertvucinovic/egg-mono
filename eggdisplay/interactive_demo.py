#!/usr/bin/env python3
"""
Interactive demo for the Rich Text Editor.
This version uses a simpler approach for keyboard input.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from text_editor import TextEditor


def main():
    """Main function demonstrating text editor usage."""
    
    # Define a simple autocomplete function
    def autocomplete_function(line: str, row: int, col: int) -> list:
        """Provide autocomplete suggestions for common programming keywords."""
        keywords = [
            "def", "class", "import", "from", "if", "else", "elif",
            "for", "while", "return", "print", "input", "len", "range"
        ]
        
        # Get current word being typed
        current_word = ""
        for char in reversed(line[:col]):
            if char.isalnum() or char == '_':
                current_word = char + current_word
            else:
                break
        
        # Return matching keywords
        return [kw for kw in keywords if kw.startswith(current_word)]
    
    # Create the editor with initial text
    editor = TextEditor(
        initial_text="# Welcome to the Rich Text Editor!\n\n# Try typing 'def' and press Tab for autocomplete\n# Use arrow keys to navigate\n# Press Ctrl+C to exit\n\n",
        autocomplete_callback=autocomplete_function,
        width=80,
        height=20
    )
    
    print("Rich Text Editor Demo")
    print("=====================")
    print("Features demonstrated:")
    print("- Multi-line text editing")
    print("- Arrow key navigation")
    print("- Text insertion/deletion")
    print("- Autocomplete (try typing 'def' and press Tab)")
    print("\nPress Ctrl+C to exit\n")
    
    try:
        # Start the editor
        editor.run()
    except KeyboardInterrupt:
        print("\n\nEditor session ended.")
    
    # Show final text
    print("\nFinal text content:")
    print("-" * 40)
    print(editor.get_text())
    print("-" * 40)


if __name__ == "__main__":
    main()