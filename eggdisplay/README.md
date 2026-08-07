# eggdisplay

`eggdisplay` is the terminal presentation toolkit used by the Egg CLI. It
provides a multiline editor, resizable Rich panels, horizontal and vertical
layouts, bounded chunked text, and incremental renderers for both native
scrollback and alternate-screen interfaces.

The package is UI-only: it does not know about models, threads, tools, or
SQLite. Those behaviors belong to [`eggthreads`](../eggthreads/README.md) and
the [`egg`](../egg/README.md) client.

## Install

```bash
pip install -e ./eggdisplay
```

Requirements: Python 3.10+, `rich`, and `readchar`.

## Public API

```python
from eggdisplay import (
    AsyncRealTimeEditor,
    ChunkedText,
    FullScreenDiffRenderer,
    HStack,
    InlineDiffRenderer,
    InputPanel,
    OutputPanel,
    RealTimeEditor,
    TextEditor,
    VStack,
)
```

### Text editor

`TextEditor` owns editable lines, cursor state, synchronous or asynchronous
autocomplete, and event hooks:

```python
from eggdisplay import TextEditor

editor = TextEditor(
    initial_text="Hello\nWorld",
    width=80,
    height=20,
    autocomplete_callback=lambda line, row, col: ["hello", "help"],
)
editor.insert_text("!")
print(editor.get_text())
```

Supported event names are `key_press`, `text_change`, `cursor_move`, and
`autocomplete`. Register callbacks with `add_event_listener(...)`.
`RealTimeEditor` and `AsyncRealTimeEditor` wrap the same editing model in
interactive Rich Live loops.

### Panels and layouts

```python
from eggdisplay import HStack, InputPanel, OutputPanel, VStack

chat = OutputPanel(title="Chat", initial_height=8, max_height=20)
status = OutputPanel(title="Status", initial_height=8, max_height=20)
composer = InputPanel(title="Input", initial_height=5, max_height=12)

chat.set_content("Ready")
layout = VStack([HStack([chat, status]).render(), composer.render()]).render()
```

`OutputPanel` and `InputPanel` support dynamic sizing, scrolling, styling, and
wrapped or cropped body text. `HStack` and `VStack` compose their Rich
renderables without owning an application event loop.

### Incremental rendering

`InlineDiffRenderer` updates only changed terminal rows while preserving native
scrollback. `FullScreenDiffRenderer` owns an alternate-screen surface. Both
accept Rich renderables through `update(...)` and expose `print_above(...)` for
static output.

`ChunkedText` stores append-heavy streams in bounded blocks. Use `tail(n)` for a
bounded recent view and `to_string()` only for explicit full materialization.

## Development

```bash
pip install -e "./eggdisplay[dev]"
pytest -q eggdisplay/tests
```

See the [Egg terminal client](../egg/README.md) for the primary integration.
