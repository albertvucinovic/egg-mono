"""Incremental text storage shared by streaming UI state and renderers."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any


class ChunkedText:
    """Append-only text with bounded copy work per append.

    Chunks are coalesced up to ``block_size``. Appending therefore never copies
    the complete accumulated stream, while replay/inline presentation can still
    materialize the exact text on demand.
    """

    __slots__ = ("_blocks", "_length", "block_size")

    def __init__(self, value: str = "", *, block_size: int = 64 * 1024) -> None:
        self.block_size = max(1, int(block_size))
        self._blocks: list[str] = []
        self._length = 0
        if value:
            self.append(value)

    def append(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            return
        self._length += len(value)
        remaining = value
        if self._blocks and len(self._blocks[-1]) < self.block_size:
            available = self.block_size - len(self._blocks[-1])
            prefix = remaining[:available]
            if prefix:
                self._blocks[-1] += prefix
                remaining = remaining[len(prefix):]
        while remaining:
            self._blocks.append(remaining[:self.block_size])
            remaining = remaining[self.block_size:]

    def clear(self) -> None:
        self._blocks.clear()
        self._length = 0

    def iter_chunks(self) -> Iterator[str]:
        return iter(self._blocks)

    def extend(self, chunks: Iterable[str]) -> None:
        for chunk in chunks:
            self.append(chunk)

    def tail(self, max_chars: int) -> str:
        """Materialize at most the newest ``max_chars`` without reading old blocks."""
        remaining = max(0, int(max_chars))
        if remaining <= 0 or not self._blocks:
            return ""
        selected: list[str] = []
        for block in reversed(self._blocks):
            if remaining <= 0:
                break
            if len(block) <= remaining:
                selected.append(block)
                remaining -= len(block)
            else:
                selected.append(block[-remaining:])
                remaining = 0
        selected.reverse()
        return "".join(selected)

    def to_string(self) -> str:
        """Materialize the complete value for explicit replay/final presentation."""
        if not self._blocks:
            return ""
        if len(self._blocks) == 1:
            return self._blocks[0]
        return "".join(self._blocks)

    def __len__(self) -> int:
        return self._length

    def __bool__(self) -> bool:
        return self._length > 0

    def __str__(self) -> str:
        return self.to_string()

    def __contains__(self, item: object) -> bool:
        if not isinstance(item, str):
            return False
        return item in self.to_string()

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, ChunkedText):
            return self._blocks == other._blocks
        if isinstance(other, str):
            return self.to_string() == other
        return False

    def __repr__(self) -> str:
        return f"ChunkedText(length={self._length}, blocks={len(self._blocks)})"
