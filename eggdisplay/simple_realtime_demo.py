#!/usr/bin/env python3
"""
Simple Real-time Editor Demo
Shows how easy it is to use the RealTimeEditor class.
"""

from text_editor import RealTimeEditor

# That's it! Just create and run
editor = RealTimeEditor(
    initial_text="Welcome to the simple real-time editor!\n\nStart typing...\n"
)

# Run the editor - everything else is handled automatically
editor.run()