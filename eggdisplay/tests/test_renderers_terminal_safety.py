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
        # exercising the renderer's real viewport composition and scroll-offset
        # logic.
        self._prev_viewport = self._compose_visible_viewport(width)
        self._viewport_h = self._term_height()
        self._viewport_w = width


def test_fullscreen_replace_recent_scrollback_updates_local_tail() -> None:
    r = TinyTerminalRenderer(width=20, height=5)
    r._live_lines = ["LIVE"]

    first_rows = r.replace_recent_scrollback(0, "first")
    second_rows = r.replace_recent_scrollback(first_rows, "second")

    assert first_rows == 1
    assert second_rows == 1
    assert r._scrollback == ["second"]
    assert r._prev_viewport[-2:] == ["second", "LIVE"]


def test_fullscreen_replace_recent_scrollback_preserves_scrolled_view() -> None:
    r = TinyTerminalRenderer(width=20, height=5)
    r._scrollback = ["h0", "h1", "h2", "h3", "h4", "summary"]
    r._live_lines = ["LIVE"]
    r._paint(20)
    r.scroll(1)
    before = list(r._prev_viewport)

    r.replace_recent_scrollback(1, "updated\nsummary")

    assert r._scroll_offset == 2
    assert r._scrollback[-2:] == ["updated", "summary"]
    assert r._prev_viewport == before


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


def test_fullscreen_stream_append_defers_repaint_while_scrolled_up() -> None:
    class CountingRenderer(TinyTerminalRenderer):
        def __init__(self):
            super().__init__(width=8, height=4)
            self.paint_calls = 0

        def _paint(self, width: int) -> None:
            self.paint_calls += 1
            super()._paint(width)

    r = CountingRenderer()
    r._scrollback = [f"h{i}" for i in range(6)]
    r._live_lines = ["LIVE"]
    r._paint(8)
    r.scroll(1)
    r.stream_begin()
    before = list(r._prev_viewport)
    calls_before_append = r.paint_calls

    r.stream_append("streaming draft")

    assert r.paint_calls == calls_before_append
    assert r._prev_viewport == before
    assert "streaming draft" in r._stream_buffer

    r.scroll_to_bottom()

    assert r.paint_calls > calls_before_append
    assert any("streaming" in row or "draft" in row for row in r._prev_viewport)


def test_fullscreen_can_scroll_during_stream_without_prior_paint() -> None:
    """Scroll should clamp against current stream rows, not stale paint state."""
    r = TinyTerminalRenderer(width=8, height=4)
    r._live_lines = ["LIVE"]

    # Simulate a fast stream growing before the main UI loop has repainted the
    # live panels. This happens easily while reasoning streams in read-only /
    # NO_API_CALLS scenarios. The scroll call must still see the stream rows.
    r._stream_buffer = "\n".join(f"reason-{i}" for i in range(8))
    assert r._scroll_offset == 0

    r.scroll(3)

    # The first paint observes stream growth from 0 rows and compensates the
    # offset to keep the selected slice stable; the important regression is
    # that the scroll request is not ignored/clamped to zero.
    assert r._scroll_offset > 0
    assert r._prev_viewport[-1] == "LIVE"


def test_fullscreen_scroll_reuses_stream_row_cache() -> None:
    r = TinyTerminalRenderer(width=8, height=4)
    r._live_lines = ["LIVE"]

    r.stream_begin()
    r.stream_append("\n".join(f"reason-{i}" for i in range(50)))
    before = list(r._stream_rows_state.rows)
    r.stream_append("\nmore")
    after_append = list(r._stream_rows_state.rows)

    r.scroll(1)
    r.scroll(1)

    assert len(after_append) > len(before)
    assert r._stream_rows_state.rows == after_append


def test_fullscreen_stream_rows_append_incrementally() -> None:
    r = TinyTerminalRenderer(width=8, height=4)

    def fail_full_reparse(_ansi_text: str, _width: int):  # pragma: no cover - should not be called
        raise AssertionError("stream append should not reparse the full buffer")

    r._stream_rows_from_ansi = fail_full_reparse  # type: ignore[method-assign]
    r.stream_begin()
    r.stream_append("abcd")
    r.stream_append("efgh")

    assert r._stream_rows(4) == ["abcd", "efgh"]


def test_fullscreen_incremental_stream_rows_match_full_rebuild_for_markup() -> None:
    r = TinyTerminalRenderer(width=4, height=4)
    r.stream_begin()
    r.stream_append("[red]abcd[/red]\n")
    r.stream_append("ef")

    assert r._stream_rows(4) == r._stream_rows_from_ansi(r._stream_buffer, 4)


class RecordingScrollbackSource:
    def __init__(self, rows: list[str], *, total: int | None = None):
        self.rows = rows
        self.total = len(rows) if total is None else total
        self.row_count_calls: list[int] = []
        self.requests: list[tuple[int, int, int]] = []

    def row_count(self, width: int) -> int | None:
        self.row_count_calls.append(width)
        return self.total

    def rows_from_bottom(self, width: int, bottom_offset: int, height: int) -> list[str]:
        self.requests.append((width, bottom_offset, height))
        if height <= 0:
            return []
        end = len(self.rows) - max(0, bottom_offset)
        if end <= 0:
            return []
        start = max(0, end - height)
        return self.rows[start:end]


def test_fullscreen_virtual_source_initial_paint_requests_visible_tail_only() -> None:
    r = TinyTerminalRenderer(width=20, height=5)
    source = RecordingScrollbackSource([f"s{i}" for i in range(100)], total=None)
    r.set_scrollback_source(source)
    r._live_lines = ["LIVE"]

    r._paint(20)

    assert r._prev_viewport == ["s96", "s97", "s98", "s99", "LIVE"]
    assert source.requests == [(20, 0, 4)]
    assert source.row_count_calls == []


def test_fullscreen_live_update_reuses_clean_lazy_history_slice() -> None:
    """Typing/status redraws should not refetch lazy transcript rows."""
    class CountingRenderer(TinyTerminalRenderer):
        def __init__(self):
            super().__init__(width=20, height=5)
            self.visible_paints = 0

        def _paint_visible(self, width: int, visible: list[str]) -> None:
            self.visible_paints += 1
            super()._paint_visible(width, visible)

        def _paint(self, width: int) -> None:
            visible = self._compose_visible_viewport(width)
            self._mark_history_source_clean_for_current_view(width)
            self._paint_visible(width, visible)

    r = CountingRenderer()
    source = RecordingScrollbackSource([f"s{i}" for i in range(100)], total=None)
    r.set_scrollback_source(source)
    r._live_lines = ["LIVE"]

    r._paint(20)
    assert source.requests == [(20, 0, 4)]
    source.requests.clear()

    r.update(Text("LIVE changed"))

    assert source.requests == []
    assert r.visible_paints == 2
    assert "LIVE changed" in r._prev_viewport[-1]


def test_fullscreen_live_update_recomposes_when_live_height_changes() -> None:
    """Changed panel height needs a fresh history slice, not reused rows."""
    r = TinyTerminalRenderer(width=20, height=5)
    source = RecordingScrollbackSource([f"s{i}" for i in range(100)], total=None)
    r.set_scrollback_source(source)
    r._live_lines = ["LIVE"]

    r._paint(20)
    source.requests.clear()

    r.update(Text("LIVE one\nLIVE two"))

    assert source.requests == [(20, 0, 3)]
    assert r._prev_viewport[-2:] == ["LIVE one", "LIVE two"]


def test_fullscreen_virtual_source_composes_before_scrollback_stream_and_live() -> None:
    r = TinyTerminalRenderer(width=20, height=6)
    source = RecordingScrollbackSource(["s0", "s1", "s2"], total=3)
    r.set_scrollback_source(source)
    r._scrollback = ["p0", "p1"]
    r._stream_buffer = "t0\nt1"
    r._append_stream_rows(r._stream_buffer, 20)
    r._live_lines = ["LIVE"]

    r._paint(20)

    assert r._prev_viewport == ["s2", "p0", "p1", "t0", "t1", "LIVE"]
    assert source.requests == [(20, 0, 1)]


def test_fullscreen_virtual_source_scrolling_requests_older_rows() -> None:
    r = TinyTerminalRenderer(width=20, height=5)
    source = RecordingScrollbackSource([f"s{i}" for i in range(8)], total=8)
    r.set_scrollback_source(source)
    r._live_lines = ["LIVE"]
    r._scrollback = ["p0", "p1"]

    r._paint(20)
    assert r._prev_viewport == ["s6", "s7", "p0", "p1", "LIVE"]

    r.scroll(3)

    assert r._scroll_offset == 3
    assert r._prev_viewport == ["s3", "s4", "s5", "s6", "LIVE"]
    assert source.requests[-1] == (20, 1, 4)


def test_fullscreen_virtual_source_top_clamps_when_source_is_short() -> None:
    r = TinyTerminalRenderer(width=20, height=5)
    source = RecordingScrollbackSource(["s0", "s1", "s2"], total=None)
    r.set_scrollback_source(source)
    r._live_lines = ["LIVE"]

    r._paint(20)
    assert r._prev_viewport == ["", "s0", "s1", "s2", "LIVE"]

    r.scroll(999)

    assert r._scroll_offset == 0
    assert r._prev_viewport == ["", "s0", "s1", "s2", "LIVE"]
    assert source.row_count_calls == []


def test_fullscreen_source_replacement_keeps_in_session_rows_at_bottom() -> None:
    r = TinyTerminalRenderer(width=20, height=5)
    r._live_lines = ["LIVE"]
    r._scrollback = ["in-session"]
    r.set_scrollback_source(RecordingScrollbackSource(["old0", "old1"], total=2))
    r._paint(20)

    r._scroll_offset = 2
    r.set_scrollback_source(RecordingScrollbackSource(["new0", "new1", "new2"], total=3))

    assert r._scroll_offset == 0
    assert r._scrollback == ["in-session"]
    assert r._prev_viewport == ["new0", "new1", "new2", "in-session", "LIVE"]


def test_fullscreen_stream_end_then_final_print_has_no_stale_stream_rows() -> None:
    r = TinyTerminalRenderer(width=20, height=5)
    r._live_lines = ["LIVE"]
    r.set_scrollback_source(RecordingScrollbackSource(["history"], total=1))

    r.stream_begin()
    r.stream_append("streaming draft")
    assert any("streaming draft" in row for row in r._prev_viewport)

    r.stream_end()
    r.print_above("finalized")

    assert not any("streaming draft" in row for row in r._prev_viewport)
    assert sum("finalized" in row for row in r._prev_viewport) == 1

    # A later transcript source refresh should replace the local finalized row
    # with the sourced transcript copy, not show both.
    r.clear_scrollback()
    r.set_scrollback_source(RecordingScrollbackSource(["history", "finalized"], total=2))

    assert r._scrollback == []
    assert sum("finalized" in row for row in r._prev_viewport) == 1
