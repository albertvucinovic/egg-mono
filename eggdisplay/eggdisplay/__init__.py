"""
egg-display - A rich.Live-based text editing and display panels

This package provides a multi-line text editor with support for:
- Arrow key navigation
- Text insertion, deletion, backspace
- Paste support
- External autocomplete on Tab key
- Event listeners/hooks
"""

from .eggdisplay import (
    TextEditor,
    Cursor,
    RealTimeEditor,
    AsyncRealTimeEditor,
    OutputPanel,
    InputPanel,
    HStack,
    VStack,
)

__all__ = [
    'TextEditor',
    'Cursor',
    'RealTimeEditor',
    'AsyncRealTimeEditor',
    'OutputPanel',
    'InputPanel',
    'HStack',
    'VStack',
]
__version__ = "0.1.0"
