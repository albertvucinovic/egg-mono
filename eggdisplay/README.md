# Rich Text Editor

A rich.Live-based text editor component with multi-line editing support.

## Features

- **Multi-line string editing** - Full support for editing text across multiple lines
- **Arrow key navigation** - Move cursor with arrow keys
- **Text operations** - Insert, delete, backspace, and paste support
- **External autocomplete** - Tab key triggers custom autocomplete functionality
- **Event listeners/hooks** - Subscribe to key events, text changes, and cursor movements
- **Initial text support** - Set initial content and modify text programmatically

Inline layout helpers for Rich Live (non-fullscreen):
- HStack: horizontal rows using rich.Columns
- VStack: vertical stack using rich.console.Group

## Installation

```bash
pip install -e .
```

## Quick Start

### Programmatic Usage (Recommended)
```python
from text_editor import TextEditor

# Create editor with initial text
editor = TextEditor(
    initial_text="Hello, World!\nThis is a multi-line editor.",
    width=80,
    height=20
)

# Use programmatically
editor.insert_text(" - Edited")
print(editor.get_text())

# Or simulate key presses
editor.handle_key("a")
editor.handle_key("b")
editor.handle_key("c")
```

### Interactive Usage
```python
from text_editor import TextEditor

editor = TextEditor(initial_text="Hello, World!")

# Start interactive mode (requires terminal support)
editor.run()

# Get the edited text
final_text = editor.get_text()
print(f"Final text: {final_text}")

### Side-by-side Panels (Inline Live)

You can arrange panels horizontally without using Rich Layout (which claims the whole screen) by using HStack and VStack.

```python
from rich.console import Console
from text_editor import OutputPanel, InputPanel, HStack, VStack
from rich.live import Live

console = Console()

left = OutputPanel(title="Left", initial_height=8, max_height=15)
right = OutputPanel(title="Right", initial_height=8, max_height=15)
input_panel = InputPanel(title="Input", initial_height=8, max_height=12)

left.set_content("Left panel content\nMore lines...")
right.set_content("Right panel content\nEven more lines...")

layout = VStack([
    HStack([left, right]).render(),
    input_panel.render(),
]).render()

with Live(layout, refresh_per_second=20, screen=False, console=console) as live:
    # Update your panels and rebuild the layout as needed
    # live.update(VStack([...]).render())
    pass
```
```

## Advanced Usage

### Autocomplete

```python
def my_autocomplete(line: str, row: int, col: int) -> list[str]:
    """Custom autocomplete function."""
    words = ["apple", "banana", "cherry", "date"]
    current_word = line[:col].split()[-1] if line[:col].split() else ""
    return [w for w in words if w.startswith(current_word)]

editor = TextEditor(autocomplete_callback=my_autocomplete)
```

### Event Listeners

```python
def on_text_change(change_type: str, row: int, col: int, data: str):
    print(f"Text changed: {change_type} at line {row}, column {col}")

def on_cursor_move(old_row: int, old_col: int, new_row: int, new_col: int):
    print(f"Cursor moved from ({old_row}, {old_col}) to ({new_row}, {new_col})")

def on_key_press(key: str, row: int, col: int):
    print(f"Key pressed: {key} at ({row}, {col})")

editor = TextEditor()
editor.add_event_listener('text_change', on_text_change)
editor.add_event_listener('cursor_move', on_cursor_move)
editor.add_event_listener('key_press', on_key_press)
```

### Programmatic Text Manipulation

```python
editor = TextEditor()

# Set text programmatically
editor.set_text("New text content\nwith multiple lines")

# Get current text
current_text = editor.get_text()

# Insert text at current cursor position
editor.insert_text("inserted text")

# Delete character at cursor
editor.delete_char()

# Backspace
editor.backspace()

# Move cursor
editor.move_cursor(1, 5)  # Move down 1 line, right 5 columns
```

## Event Types

- `key_press`: Triggered when any key is pressed
- `text_change`: Triggered when text is inserted, deleted, or modified
- `cursor_move`: Triggered when cursor position changes
- `autocomplete`: Triggered when autocomplete is used

## API Reference

### TextEditor Class

#### Constructor
```python
TextEditor(
    initial_text: str = "",
    autocomplete_callback: Optional[Callable[[str, int, int], List[str]]] = None,
    width: int = 80,
    height: int = 20
)
```

#### Key Methods
- `run()`: Start interactive editing session
- `stop()`: Stop the editor
- `get_text() -> str`: Get current text content
- `set_text(text: str)`: Set text content
- `insert_text(text: str)`: Insert text at cursor
- `delete_char()`: Delete character at cursor
- `backspace()`: Delete character before cursor
- `move_cursor(delta_row: int, delta_col: int)`: Move cursor
- `add_event_listener(event_type: str, callback: Callable)`: Add event listener
- `handle_key(key: str) -> bool`: Handle key press programmatically

## Demo

### Programmatic Demo (Works Everywhere)
```bash
python programmatic_demo.py
```

### Interactive Demo (Requires Terminal Support)
```bash
python interactive_demo.py
```

### Simple Tests
```bash
python simple_test.py
```

These demonstrate:
- Basic text editing
- Arrow key navigation  
- Autocomplete with Python keywords
- Event listeners
- Programmatic text manipulation

## License

MIT