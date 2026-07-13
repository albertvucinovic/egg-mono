"""Deterministic cost-shape tests for chunked streaming state."""
from __future__ import annotations

from collections.abc import Sequence

from eggdisplay import ChunkedText, FullScreenDiffRenderer


class CountingChunkedText(ChunkedText):
    def __init__(self, value: str = "", **kwargs):
        self.chunk_iterations = 0
        super().__init__(value, **kwargs)

    def iter_chunks(self):
        for chunk in super().iter_chunks():
            self.chunk_iterations += 1
            yield chunk


class NoCopyRows(Sequence[str]):
    """Large logical row sequence that records only actually requested rows."""

    def __init__(self, length: int):
        self.length = length
        self.item_reads = 0
        self.iterations = 0

    def __len__(self):
        return self.length

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(self.length)
            return [self[i] for i in range(start, stop, step)]
        if index < 0:
            index += self.length
        if index < 0 or index >= self.length:
            raise IndexError(index)
        self.item_reads += 1
        return f"row-{index}"

    def __iter__(self):
        self.iterations += 1
        raise AssertionError("renderer must not iterate all prior stream rows")


class TinyRenderer(FullScreenDiffRenderer):
    def __init__(self, *, width=80, height=10):
        super().__init__()
        self.width = width
        self.height = height

    def _term_width(self):
        return self.width

    def _term_height(self):
        return self.height

    def _paint(self, width):
        self._prev_viewport = self._compose_visible_viewport(width)
        self._viewport_w = width
        self._viewport_h = self.height


def test_chunked_text_append_does_not_read_prior_blocks():
    text = CountingChunkedText("x" * 1_000_000, block_size=1024)
    before_blocks = len(text._blocks)

    for _ in range(100):
        text.append("delta")

    assert text.chunk_iterations == 0
    assert len(text) == 1_000_500
    assert len(text._blocks) <= before_blocks + 1


def test_renderer_append_does_not_replay_prior_stream_chunks():
    renderer = TinyRenderer()
    renderer.stream_begin()
    stream = CountingChunkedText("x" * 1_000_000, block_size=1024)
    renderer._stream_buffer = stream
    # Represent an already incrementally parsed stream at the current width.
    renderer._stream_rows_state.rows = ["x" * 80] * 12_500
    renderer._stream_rows_state.width = 80

    renderer.stream_append("tail")

    assert stream.chunk_iterations == 0
    assert str(stream).endswith("tail")


def test_scroll_reads_only_visible_stream_row_slice():
    renderer = TinyRenderer(height=10)
    renderer._live_lines = ["LIVE"]
    renderer._stream_buffer = ChunkedText("active")
    rows = NoCopyRows(1_000_000)
    renderer._stream_rows = lambda _width: rows  # type: ignore[method-assign]
    renderer._paint(80)
    initial_reads = rows.item_reads

    renderer.scroll(3)

    # Composition may request the visible page more than once for clamping, but
    # work remains bounded by viewport height rather than total stream rows.
    assert rows.iterations == 0
    assert rows.item_reads - initial_reads <= 2 * renderer.height
    assert renderer._prev_viewport[-1] == "LIVE"


def test_chunked_text_tail_reads_only_newest_blocks():
    class CountingBlocks(list):
        def __init__(self, values):
            super().__init__(values)
            self.reads = 0

        def __reversed__(self):
            for index in range(len(self) - 1, -1, -1):
                self.reads += 1
                yield list.__getitem__(self, index)

    text = ChunkedText()
    blocks = CountingBlocks([str(i).zfill(4) for i in range(10_000)])
    text._blocks = blocks
    text._length = sum(map(len, blocks))

    assert text.tail(6) == "989999"
    assert blocks.reads <= 3
