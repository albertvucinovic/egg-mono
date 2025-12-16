from __future__ import annotations

import queue
import threading

import pytest


from eggdisplay import InputPanel


PASTE_START_PREFIX = "\x1b[200"  # readchar.readkey() returns this, then "~"
PASTE_END = b"\x1b[201~"


def test_large_paste_30_lines_not_truncated_via_input_worker(monkeypatch):
    """Regression test for large pastes in a small InputPanel.

    Real terminals can lose paste data if it's read too slowly.
    We model the desired behaviour: the input stack must be able to
    ingest a 30-line paste and make all lines reachable.

    This test drives the RealTimeEditor input worker and verifies the
    editor ends up with 30 lines.
    """
    panel = InputPanel(initial_height=8, max_height=12)
    ed = panel.editor  # RealTimeEditor

    payload_lines = [f"this is line {i} with multiple words" for i in range(1, 31)]
    payload = "\n".join(payload_lines)
    payload_bytes = payload.encode("utf-8") + PASTE_END

    # Fake os.read used by the (future) fast paste reader.
    chunks = [payload_bytes[:50], payload_bytes[50:200], payload_bytes[200:]]

    def fake_os_read(_fd: int, _n: int) -> bytes:
        if chunks:
            return chunks.pop(0)
        return b""

    monkeypatch.setattr("os.read", fake_os_read)

    # Fake readchar.readkey: only emits the bracketed paste start prefix and '~',
    # then stops. Without a fast paste reader, the payload never arrives.
    seq = iter([PASTE_START_PREFIX, "~"])

    def fake_readkey() -> str:
        return next(seq)

    monkeypatch.setattr("readchar.readkey", fake_readkey)

    # Run worker briefly
    ed.running = True
    t = threading.Thread(target=ed._input_worker, daemon=True)
    t.start()
    t.join(timeout=1)
    ed.running = False

    # Drain keys from queue and feed to key handler
    while True:
        try:
            k = ed.input_queue.get_nowait()
        except queue.Empty:
            break
        ed._handle_key(k)

    # Expect all 30 lines were pasted.
    assert len(ed.editor.lines) == 30
    assert ed.editor.get_text() == payload
