from __future__ import annotations

from rich.text import Text

from eggdisplay.eggdisplay.renderers import FullScreenDiffRenderer


def test_rendered_output_strips_terminal_controls_but_keeps_sgr() -> None:
    r = FullScreenDiffRenderer()

    # Use ST-terminated OSC; Rich strips BEL from Text payloads before our
    # renderer sanitizer sees it, so BEL-terminated OSC cannot be recovered
    # without over-stripping following plain text.
    lines, _width = r._render_to_lines(Text("before\x1b[2Jafter\x1b]52;c;AAAA\x1b\\done", style="red"))
    rendered = "\n".join(lines)

    assert "beforeafterdone" in rendered
    assert "\x1b[2J" not in rendered
    assert "\x1b]52" not in rendered
    # Rich styling should survive the safety pass.
    assert "\x1b[31m" in rendered or "\x1b[91m" in rendered


def test_rendered_output_does_not_overstrip_malformed_osc() -> None:
    r = FullScreenDiffRenderer()

    # Rich may remove BEL from Text content before the renderer-level safety
    # pass. If an OSC is therefore malformed by the time we inspect it, drop
    # the introducer rather than deleting all following plain text.
    lines, _width = r._render_to_lines(Text("before\x1b]52;c;AAAA\x07done", style="red"))
    rendered = "\n".join(lines)

    assert "before52;c;AAAAdone" in rendered
    assert "\x1b]" not in rendered


def test_rendered_output_replaces_c0_controls() -> None:
    r = FullScreenDiffRenderer()

    lines, _width = r._render_to_lines(Text("a\rb\x08c\x00d"))
    rendered = "\n".join(lines)

    assert "\r" not in rendered
    assert "\x08" not in rendered
    assert "\x00" not in rendered
    assert "�" in rendered


class TinyTerminalRenderer(FullScreenDiffRenderer):
    def __init__(self, *, width: int = 10, height: int = 6):
        super().__init__()
        self._test_width = width
        self._test_height = height
        self.writes: list[str] = []

    def _term_width(self) -> int:
        return self._test_width

    def _term_height(self) -> int:
        return self._test_height

    def _paint(self, width: int) -> None:
        # Avoid writing terminal control sequences during unit tests while still
        # exercising the same viewport composition and scroll-offset logic.
        vh = self._term_height()
        stream_rows = self._stream_rows(width) if self._stream_buffer else []
        if self._scroll_offset > 0:
            delta = len(stream_rows) - self._last_stream_rows
            if delta:
                self._scroll_offset = max(0, self._scroll_offset + delta)
        self._last_stream_rows = len(stream_rows)

        non_live = list(self._scrollback) + stream_rows
        live = list(self._live_lines)
        live_h = min(len(live), vh)
        non_live_h = max(0, vh - live_h)

        max_offset = max(0, len(non_live) - non_live_h)
        if self._scroll_offset > max_offset:
            self._scroll_offset = max_offset
        offset = self._scroll_offset

        if non_live_h > 0:
            end = len(non_live) - offset
            start = max(0, end - non_live_h)
            non_live_visible = non_live[start:end]
            if len(non_live_visible) < non_live_h:
                non_live_visible = [""] * (non_live_h - len(non_live_visible)) + non_live_visible
        else:
            non_live_visible = []

        visible = non_live_visible + live[:live_h]
        if len(visible) < vh:
            visible = [""] * (vh - len(visible)) + visible
        self._prev_viewport = visible[:vh]
        self._viewport_h = vh
        self._viewport_w = width


def test_stream_rows_uses_terminal_cell_width_for_wide_and_combining_text() -> None:
    r = FullScreenDiffRenderer()

    r._stream_buffer = "ab中de"
    assert r._stream_rows(4) == ["ab中", "de"]

    r._stream_buffer = "e\u0301e\u0301e\u0301"
    assert r._stream_rows(2) == ["e\u0301e\u0301", "e\u0301"]

    r._stream_buffer = "a🙂b"
    assert r._stream_rows(3) == ["a🙂", "b"]


def test_stream_rows_reopens_sgr_style_after_wrap() -> None:
    r = FullScreenDiffRenderer()

    r._stream_buffer = "\x1b[31mabcdef\x1b[0m"

    assert r._stream_rows(3) == [
        "\x1b[31mabc\x1b[0m",
        "\x1b[31mdef\x1b[0m",
    ]


def test_fullscreen_scroll_clamps_and_keeps_live_region_pinned() -> None:
    r = TinyTerminalRenderer(width=20, height=5)
    r._scrollback = [f"h{i}" for i in range(8)]
    r._live_lines = ["LIVE"]

    r._paint(20)
    assert r._prev_viewport == ["h4", "h5", "h6", "h7", "LIVE"]

    r.scroll(2)
    assert r._scroll_offset == 2
    assert r._prev_viewport == ["h2", "h3", "h4", "h5", "LIVE"]

    r.scroll(999)
    assert r._scroll_offset == 4
    assert r._prev_viewport == ["h0", "h1", "h2", "h3", "LIVE"]

    r.scroll_to_bottom()
    assert r._scroll_offset == 0
    assert r._prev_viewport == ["h4", "h5", "h6", "h7", "LIVE"]


def test_fullscreen_scroll_position_stays_stable_when_stream_changes() -> None:
    r = TinyTerminalRenderer(width=4, height=5)
    r._scrollback = [f"h{i}" for i in range(6)]
    r._live_lines = ["LIVE"]
    r._paint(4)

    r.scroll(2)
    before = list(r._prev_viewport)
    assert before == ["h0", "h1", "h2", "h3", "LIVE"]

    r.stream_begin()
    assert r._prev_viewport == before

    r.stream_append("abcd")
    assert r._prev_viewport == before

    r.stream_append("efgh")
    assert r._prev_viewport == before

    r.stream_end()
    assert r._prev_viewport == before
