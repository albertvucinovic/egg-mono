"""
Rich Text Editor - A rich.Live-based text editor component.

This package provides a multi-line text editor with support for:
- Arrow key navigation
- Text insertion, deletion, backspace
- Paste support
- External autocomplete on Tab key
- Event listeners/hooks
"""

from .text_editor import TextEditor, Cursor

__all__ = ['TextEditor', 'Cursor']
__version__ = "0.1.0"