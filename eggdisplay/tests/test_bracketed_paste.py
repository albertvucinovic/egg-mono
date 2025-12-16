from eggdisplay import RealTimeEditor


PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"


def test_bracketed_paste_single_key_inserts_all_text():
    ed = RealTimeEditor(initial_text="")
    key = f"{PASTE_START}hello\nworld{PASTE_END}"
    ed._handle_key(key)
    assert ed.editor.get_text() == "hello\nworld"


def test_bracketed_paste_multi_key_sequence_inserts_all_text():
    ed = RealTimeEditor(initial_text="")
    ed._handle_key(PASTE_START)
    ed._handle_key("hello")
    ed._handle_key("\n")
    ed._handle_key("world")
    ed._handle_key(PASTE_END)
    assert ed.editor.get_text() == "hello\nworld"


def test_bracketed_paste_end_marker_in_same_chunk_is_handled():
    ed = RealTimeEditor(initial_text="")
    ed._handle_key(PASTE_START)
    ed._handle_key(f"hello\nworld{PASTE_END}")
    assert ed.editor.get_text() == "hello\nworld"


def test_bracketed_paste_readchar_split_markers_large_payload_not_truncated():
    """Simulate how readchar.readkey() splits ESC[200~ and ESC[201~.

    readchar's POSIX readkey() reads at most 5 chars for escape sequences,
    so ESC[200~ becomes two deliveries:
      - "\x1b[200"  then "~"
    This test ensures we still capture the entire paste payload.
    """
    ed = RealTimeEditor(initial_text="")

    payload_lines = [
        f"this is line {i} with multiple words" for i in range(1, 31)
    ]
    payload = "\n".join(payload_lines)

    # Start marker split (as readchar would deliver)
    ed._handle_key("\x1b[200")
    ed._handle_key("~")

    # Paste payload can arrive in chunks; emulate chunking by sending each line
    # with a trailing newline except the last.
    for i, line in enumerate(payload_lines):
        if i < len(payload_lines) - 1:
            ed._handle_key(line + "\n")
        else:
            ed._handle_key(line)

    # End marker split
    ed._handle_key("\x1b[201")
    ed._handle_key("~")

    assert ed.editor.get_text() == payload
