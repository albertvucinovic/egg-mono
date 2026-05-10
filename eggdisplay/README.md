# eggdisplay

`eggdisplay` contains Rich-based terminal UI primitives used by the Egg terminal
frontend. It is small and independent: a multi-line text editor plus inline live
panels that can be arranged without taking over the whole terminal screen.

## Features

- Multi-line text editing.
- Arrow-key navigation, insert/delete/backspace, and paste support.
- App-provided autocomplete callbacks.
- Text/cursor/key event listeners.
- Scrollable output/input panels rendered with Rich.
- `HStack`/`VStack` helpers for inline non-fullscreen layouts.

## Install

```bash
pip install -e ./eggdisplay
```

Runtime dependencies:

- Python 3.10+
- `rich`
- `readchar`

## Text editor

```python
from eggdisplay import TextEditor

editor = TextEditor(
    initial_text="Hello\nWorld",
    width=80,
    height=20,
)

editor.insert_text("!")
print(editor.get_text())
```

Interactive use:

```python
editor = TextEditor(initial_text="Edit me")
editor.run()
print(editor.get_text())
```

## Inline panels

```python
from rich.console import Console
from rich.live import Live
from eggdisplay import HStack, VStack, InputPanel, OutputPanel

console = Console()
left = OutputPanel(title="Left", initial_height=8, max_height=15)
right = OutputPanel(title="Right", initial_height=8, max_height=15)
input_panel = InputPanel(title="Input", initial_height=6, max_height=12)

left.set_content("Left panel content")
right.set_content("Right panel content")

layout = VStack([
    HStack([left, right]).render(),
    input_panel.render(),
]).render()

with Live(layout, refresh_per_second=20, screen=False, console=console) as live:
    # Rebuild and call live.update(...) when panel content changes.
    pass
```

## Autocomplete

```python
def complete(line: str, row: int, col: int) -> list[str]:
    words = ["apple", "banana", "cherry"]
    prefix = line[:col].split()[-1] if line[:col].split() else ""
    return [word for word in words if word.startswith(prefix)]

editor = TextEditor(autocomplete_callback=complete)
```

## Event listeners

```python
def on_text_change(change_type: str, row: int, col: int, data: str):
    print(change_type, row, col, data)

editor = TextEditor()
editor.add_event_listener("text_change", on_text_change)
```

Supported events:

- `key_press`
- `text_change`
- `cursor_move`
- `autocomplete`

## Key methods

- `run()` / `stop()`
- `get_text()` / `set_text(text)`
- `insert_text(text)`
- `delete_char()` / `backspace()`
- `move_cursor(delta_row, delta_col)`
- `handle_key(key)`
- `add_event_listener(event_type, callback)`

## Tests

```bash
pytest -q eggdisplay/tests
```

## License

MIT
