"""Simple test for the text editor."""

from text_editor import TextEditor

# Test basic functionality
editor = TextEditor(initial_text="Hello, World!")

# Test get_text
print("Initial text:", repr(editor.get_text()))

# Test set_text
editor.set_text("New text\nwith multiple\nlines")
print("After set_text:", repr(editor.get_text()))

# Test insert_text
editor.insert_text(" inserted")
print("After insert_text:", repr(editor.get_text()))

# Test cursor movement
print("Cursor before move:", editor.cursor)
editor.move_cursor(1, 0)
print("Cursor after move:", editor.cursor)

print("\nBasic functionality tests passed!")