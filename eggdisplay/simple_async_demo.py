#!/usr/bin/env python3
"""
Simple Async Real-time Editor Demo
Shows how easy it is to use the AsyncRealTimeEditor class.
"""

from text_editor import AsyncRealTimeEditor

# That's it! Just create and run
editor = AsyncRealTimeEditor(
    initial_text="Welcome to the async real-time editor!\n\nStart typing...\n"
)

# Run the editor - everything else is handled automatically
editor.run()