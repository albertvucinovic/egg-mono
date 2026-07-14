from eggdisplay import RealTimeEditor


LiveEditorBase = RealTimeEditor.__mro__[1]


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


def test_normalize_key_reassembles_split_shift_enter_csi_u_sequence():
    ed = RealTimeEditor(initial_text="")

    assert ed.normalize_key("\x1b[13;") is None
    assert ed.normalize_key("2u") == "shift-enter"


def test_normalize_key_maps_alt_enter_to_logical_key():
    ed = RealTimeEditor(initial_text="")

    assert ed.normalize_key("\x1b\n") == "alt-enter"


def test_handle_key_treats_logical_shift_enter_as_newline():
    ed = RealTimeEditor(initial_text="hello")
    ed.editor.cursor.row = 0
    ed.editor.cursor.col = 5

    ed._handle_key("shift-enter")

    assert ed.editor.get_text() == "hello\n"


def test_bracketed_paste_strips_terminal_control_sequences():
    ed = RealTimeEditor(initial_text="")

    ed._handle_key(f"{PASTE_START}hello\x1b[2J\x1b]52;c;AAAA\x07world\r!{PASTE_END}")

    text = ed.editor.get_text()
    assert "hello" in text and "world" in text
    assert "\x1b[2J" not in text
    assert "\x1b]52" not in text
    assert "\r" not in text


def test_plain_multichar_paste_strips_terminal_control_sequences():
    ed = RealTimeEditor(initial_text="")

    ed._handle_key("hello\x1b[31mred\x1b[0m\x08!")

    text = ed.editor.get_text()
    assert text.startswith("hellored")
    assert "\x1b[31m" not in text
    assert "\x1b[0m" not in text
    assert "\x08" not in text


def test_read_key_preserves_full_sgr_mouse_sequence():
    """Regression for mouse reports leaking tails like ``72;92M``.

    ``readchar.readkey()`` truncates long CSI sequences after five bytes;
    Egg's reader must instead read until the CSI final byte so the app sees a
    single non-printable mouse key, not printable semicolon-separated numbers.
    """
    chars = iter(b"\x1b[<65;72;92M")

    def read_byte():
        return bytes([next(chars)])

    assert (
        LiveEditorBase._read_key_bytes(read_byte, lambda _timeout: True)
        == "\x1b[<65;72;92M"
    )


def test_read_key_preserves_full_csi_u_shift_enter():
    chars = iter(b"\x1b[13;2u")

    def read_byte():
        return bytes([next(chars)])

    assert (
        LiveEditorBase._read_key_bytes(read_byte, lambda _timeout: True)
        == "\x1b[13;2u"
    )


def test_read_key_preserves_ctrl_alt_sequence_for_app_shortcuts():
    chars = iter(b"\x1b\x01")

    def read_byte():
        return bytes([next(chars)])

    assert LiveEditorBase._read_key_bytes(read_byte, lambda _timeout: True) == "\x1b\x01"


def test_read_key_returns_bare_escape_when_no_tail():
    chars = iter(b"\x1b")

    def read_byte():
        return bytes([next(chars)])

    assert LiveEditorBase._read_key_bytes(read_byte, lambda _timeout: False) == "\x1b"


def test_raw_input_settings_do_not_flush_queued_mouse_bytes():
    """Input setup should not use TCSAFLUSH-style flushing.

    The reader applies these settings with TCSANOW so bytes already queued by
    the terminal (the rest of a mouse report) remain available to assemble.
    """

    class FakeTermios:
        ICANON = 0b001
        ECHO = 0b010
        ISIG = 0b100
        VMIN = 0
        VTIME = 1

    old = [0, 0, 0, FakeTermios.ICANON | FakeTermios.ECHO | FakeTermios.ISIG, 0, 0, [9, 9]]

    new = LiveEditorBase._raw_input_settings(old, FakeTermios)

    assert new[3] == 0
    assert new[6][FakeTermios.VMIN] == 1
    assert new[6][FakeTermios.VTIME] == 0
    # The control-character list is copied, not mutated in place.
    assert old[6] == [9, 9]


def test_read_key_from_fd_keeps_multiple_mouse_reports_separate(monkeypatch):
    """A wheel burst may queue multiple full reports before we read.

    Continuous raw-mode reading must return one complete SGR report per call,
    leaving the next report queued instead of leaking it to the editor.
    """
    stream = bytearray(b"\x1b[<65;55;65M\x1b[<65;55;65M")

    def fake_read(fd, count):
        assert fd == 123
        assert count == 1
        return bytes([stream.pop(0)])

    def fake_select(reads, _writes, _errors, _timeout):
        assert reads == [123]
        return (reads if stream else [], [], [])

    monkeypatch.setattr("os.read", fake_read)
    monkeypatch.setattr("select.select", fake_select)

    assert LiveEditorBase._read_key_from_fd(123) == "\x1b[<65;55;65M"
    assert LiveEditorBase._read_key_from_fd(123) == "\x1b[<65;55;65M"
    assert stream == bytearray()
