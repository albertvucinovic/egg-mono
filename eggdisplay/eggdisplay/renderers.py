"""Flicker-free differential renderers.

Two renderer implementations with a common entry point:

* :class:`InlineDiffRenderer` — HEAD-style inline renderer. Tracks only
  the live region; ``print_above`` emits directly into the terminal's
  natural scrollback so native scroll / mouse wheel / selection all work.

* :class:`FullScreenDiffRenderer` — alternate-screen TUI that owns the
  whole viewport. Maintains an in-memory scrollback model, a transient
  stream buffer (for ``stream_begin``/``stream_append``/``stream_end``),
  in-app scroll (``PageUp``/``PageDown`` + mouse wheel), and row-level
  diffs against the last painted viewport.

* :func:`DiffRenderer` — factory that picks one based on ``mode=`` or
  the ``EGG_DISPLAY_MODE`` environment variable.
"""
from __future__ import annotations

import io
import shutil
import sys
from typing import List, Optional

from rich.console import Console


class _DiffRendererBase:
    """Shared helpers for the inline and full-screen renderers."""

    _SYNC_START = "\x1b[?2026h"
    _SYNC_END = "\x1b[?2026l"

    def __init__(self, *, console: Optional[Console] = None):
        self.console = console or Console()
        self._color_system: Optional[str] = None

    def _term_width(self) -> int:
        return shutil.get_terminal_size(fallback=(100, 24)).columns

    def _term_height(self) -> int:
        return shutil.get_terminal_size(fallback=(100, 24)).lines

    def _render_to_lines(self, renderable) -> tuple:
        width = self._term_width()
        buf = io.StringIO()
        c = Console(
            file=buf,
            width=width,
            force_terminal=True,
            color_system=self._color_system or "truecolor",
        )
        c.print(renderable, end="")
        lines = buf.getvalue().split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return lines, width

    def _rich_print_to_str(self, *objects, **kwargs) -> str:
        width = getattr(self, "_width", 0) or getattr(self, "_viewport_w", 0) or self._term_width()
        buf = io.StringIO()
        c = Console(
            file=buf,
            width=width,
            force_terminal=True,
            color_system=self._color_system or "truecolor",
        )
        c.print(*objects, **kwargs)
        return buf.getvalue()


class InlineDiffRenderer(_DiffRendererBase):
    """HEAD-style inline renderer.

    Tracks only the live region (``_prev_lines``) and writes differential
    updates to the last N rows of the terminal. ``print_above`` emits a
    single message above the live region via the terminal's natural
    scrolling (content ends up in the terminal's real scrollback archive,
    so native scroll-up / mouse wheel work out of the box). Low
    bandwidth, shell-integrated, but transient-content erase is not
    possible once content scrolls past the viewport top.
    """

    def __init__(self, initial=None, *, console: Optional[Console] = None,
                 refresh_per_second: int = 30, screen: bool = False, **_):
        super().__init__(console=console)
        self._initial = initial
        self._prev_lines: List[str] = []
        self._width: int = 0

    def __enter__(self):
        sys.stdout.write("\x1b[?25l")
        sys.stdout.flush()
        try:
            self._color_system = self.console.color_system
        except Exception:
            self._color_system = "truecolor"
        if self._initial is not None:
            self.update(self._initial)
        return self

    def __exit__(self, *_exc):
        buf = ""
        if self._prev_lines:
            buf += "\n"
        buf += "\x1b[?25h"
        sys.stdout.write(buf)
        sys.stdout.flush()
        self._prev_lines = []

    def update(self, renderable) -> None:
        new_lines, new_width = self._render_to_lines(renderable)
        if not self._prev_lines or new_width != self._width:
            self._full_render(new_lines)
        else:
            self._diff_render(new_lines)
        self._prev_lines = new_lines
        self._width = new_width

    def print_above(self, *objects, **kwargs) -> None:
        msg = self._rich_print_to_str(*objects, **kwargs)
        n = len(self._prev_lines)
        parts: List[str] = [self._SYNC_START]
        if n > 0:
            parts.append(f"\x1b[{n}A\r")
            for i in range(n):
                parts.append("\x1b[2K")
                if i < n - 1:
                    parts.append("\n")
            if n > 1:
                parts.append(f"\x1b[{n - 1}A\r")
            else:
                parts.append("\r")
        parts.append(msg)
        if msg and not msg.endswith("\n"):
            parts.append("\n")
        for line in self._prev_lines:
            parts.append(f"\x1b[2K{line}\n")
        parts.append(self._SYNC_END)
        sys.stdout.write("".join(parts))
        sys.stdout.flush()

    def invalidate(self) -> None:
        """Reset diff baseline and clear the viewport.

        Used by the ``/redraw`` path to force a full repaint (and wipe
        stale terminal contents) regardless of which renderer is active.
        The terminal's scrollback archive above the viewport is preserved
        — we only clear what's currently on screen.
        """
        try:
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()
        except Exception:
            pass
        self._prev_lines = []

    def _full_render(self, lines: List[str]) -> None:
        old_n = len(self._prev_lines)
        parts: List[str] = [self._SYNC_START]
        if old_n > 0:
            parts.append(f"\x1b[{old_n}A\r")
        for line in lines:
            parts.append(f"\x1b[2K{line}\n")
        if old_n > len(lines):
            for _ in range(old_n - len(lines)):
                parts.append("\x1b[2K\n")
            parts.append(f"\x1b[{old_n - len(lines)}A")
        parts.append(self._SYNC_END)
        sys.stdout.write("".join(parts))
        sys.stdout.flush()

    def _diff_render(self, new_lines: List[str]) -> None:
        old = self._prev_lines
        old_n = len(old)
        new_n = len(new_lines)
        first = last = -1
        for i in range(max(old_n, new_n)):
            o = old[i] if i < old_n else ""
            n = new_lines[i] if i < new_n else ""
            if o != n:
                if first == -1:
                    first = i
                last = i
        if first == -1:
            return
        parts: List[str] = [self._SYNC_START]
        up = old_n - first
        if up > 0:
            parts.append(f"\x1b[{up}A\r")
        for i in range(first, last + 1):
            line = new_lines[i] if i < new_n else ""
            parts.append(f"\x1b[2K{line}\n")
        if old_n > new_n:
            clear_from = max(last + 1, new_n)
            gap = clear_from - (last + 1)
            if gap > 0:
                parts.append(f"\x1b[{gap}B")
            for _ in range(clear_from, old_n):
                parts.append("\x1b[2K\n")
            back = old_n - new_n
            if back > 0:
                parts.append(f"\x1b[{back}A")
        else:
            forward = new_n - (last + 1)
            if forward > 0:
                parts.append(f"\x1b[{forward}B")
        parts.append(self._SYNC_END)
        sys.stdout.write("".join(parts))
        sys.stdout.flush()


class FullScreenDiffRenderer(_DiffRendererBase):
    """Full-viewport differential renderer.

    Owns the entire terminal viewport via the alternate-screen buffer.
    Maintains an in-memory model of both "scrollback" (content appended
    via :meth:`print_above`) and the current live renderable; on each
    paint it computes the visible viewport (last N rows of scrollback +
    in-flight stream + live) and only writes the rows that actually
    changed relative to what's already on screen.

    Why full viewport: the terminal's scrollback archive is opaque to
    our process — once content scrolls off the viewport top it becomes
    unreachable. By owning the viewport and never emitting raw newlines
    that would scroll content into the archive, we can always replace
    any row, making transient content (e.g. a streaming preview) cleanly
    removable no matter how long it ran.
    """

    # Alternate screen: isolates our canvas from the user's shell scrollback
    # so we can freely rewrite any viewport row without touching their history.
    _ALT_ENTER = "\x1b[?1049h\x1b[H\x1b[2J"
    _ALT_EXIT = "\x1b[?1049l"

    # Mouse tracking (enables wheel-scroll events in alt-screen, where the
    # terminal's native scrollback is gone). Reported in SGR extended format
    # (?1006) so coords can exceed 223. User can hold Shift to bypass and
    # use native text selection in most terminals.
    _MOUSE_ENABLE = "\x1b[?1000;1002;1006h"
    _MOUSE_DISABLE = "\x1b[?1000;1002;1006l"

    # Cap on retained scrollback rows, to keep memory bounded over long sessions.
    _SCROLLBACK_CAP = 10000

    # ANSI escape-sequence regex, used to identify non-printable runs so
    # width-based row splitting doesn't count them toward visual width.
    import re as _re
    _ANSI_RE = _re.compile(
        r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    )
    del _re

    def __init__(self, initial=None, *, console: Optional[Console] = None,
                 refresh_per_second: int = 30, screen: bool = False,
                 alt_screen: bool = True, **_):
        super().__init__(console=console)
        self._initial = initial
        self._alt_screen = bool(alt_screen)
        # Scrollback: permanent history rows appended via print_above.
        # Stream buffer: transient in-flight streaming content, sits between
        # scrollback and live region in the composed viewport; discarded on
        # stream_end so the final formatted message (added via print_above)
        # takes its visual slot with no residue.
        self._scrollback: List[str] = []
        self._stream_buffer: str = ""  # ANSI-rendered bytes so far
        self._live_lines: List[str] = []
        # Last painted viewport: list of exactly `_viewport_h` rows that
        # are currently on the terminal. Used as the diff baseline.
        self._prev_viewport: List[str] = []
        self._viewport_h: int = 0
        self._viewport_w: int = 0
        # In-app scroll offset (alt-screen has no terminal-native scrollback
        # so we implement our own). 0 == stuck to bottom; positive == N rows
        # above bottom. Clamped to the available non-live history size.
        self._scroll_offset: int = 0
        # Number of rows the in-flight stream buffer occupied on the last
        # paint. Used to keep a scrolled-up user's view stable while new
        # tokens arrive (or when the buffer shrinks on stream_end): we
        # bump / reduce scroll_offset by the row-count delta so the
        # visible slice stays fixed on the same content.
        self._last_stream_rows: int = 0

    # -- context manager ----------------------------------------------------

    def __enter__(self):
        out = ""
        if self._alt_screen:
            out += self._ALT_ENTER
            out += self._MOUSE_ENABLE
        out += "\x1b[?25l"  # hide cursor
        sys.stdout.write(out)
        sys.stdout.flush()
        try:
            self._color_system = self.console.color_system
        except Exception:
            self._color_system = "truecolor"
        if self._initial is not None:
            self.update(self._initial)
        return self

    def __exit__(self, *_exc):
        out = "\x1b[?25h"  # show cursor
        if self._alt_screen:
            out += self._MOUSE_DISABLE
            out += self._ALT_EXIT
        else:
            out += "\n"
        sys.stdout.write(out)
        sys.stdout.flush()
        self._scrollback = []
        self._live_lines = []
        self._prev_viewport = []

    # -- public API ---------------------------------------------------------

    def update(self, renderable) -> None:
        """Re-render the live region; repaint the viewport with minimal diff."""
        lines, width = self._render_to_lines(renderable)
        self._live_lines = lines
        self._paint(width)

    def print_above(self, *objects, **kwargs) -> None:
        """Append content to the scrollback model and repaint.

        In full-viewport mode there is no separate "scrollback area" on
        the terminal — scrollback lives in our model, and the paint
        combines it with the live region to fill the viewport (stuck to
        bottom). Older content scrolls out of view (our model retains it
        up to ``_SCROLLBACK_CAP``) without ever entering the terminal's
        own archive.
        """
        ansi = self._rich_print_to_str(*objects, **kwargs)
        if not ansi:
            return
        lines = ansi.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        # If user is scrolled up, preserve their visual position by
        # offsetting to match the amount of new content appended below.
        if self._scroll_offset > 0 and lines:
            self._scroll_offset += len(lines)
        self._scrollback.extend(lines)
        if len(self._scrollback) > self._SCROLLBACK_CAP:
            excess = len(self._scrollback) - self._SCROLLBACK_CAP
            self._scrollback = self._scrollback[excess:]
            # When the cap trims from the front, compensate the offset so
            # the user's view doesn't suddenly jump.
            if self._scroll_offset > 0:
                self._scroll_offset = max(0, self._scroll_offset - excess)
        self._paint(self._viewport_w or self._term_width())

    def clear_scrollback(self) -> None:
        """Drop all accumulated scrollback content and repaint."""
        if not self._scrollback:
            # Still repaint to handle callers that cleared before printing.
            self._paint(self._viewport_w or self._term_width())
            return
        self._scrollback.clear()
        self._paint(self._viewport_w or self._term_width())

    def invalidate(self) -> None:
        """Force the next paint to write every row (used after external clears)."""
        self._prev_viewport = []

    # -- in-app scrolling ---------------------------------------------------

    def scroll(self, delta: int) -> None:
        """Scroll the scrollback/stream area by *delta* rows (positive == up).

        The live region at the bottom of the viewport stays fixed; only
        the history area above it scrolls. Clamped to available content.
        """
        new_offset = max(0, int(self._scroll_offset) + int(delta))
        if new_offset == self._scroll_offset:
            return
        self._scroll_offset = new_offset
        self._paint(self._viewport_w or self._term_width())

    def scroll_to_bottom(self) -> None:
        """Snap back to the most recent content (offset 0)."""
        if self._scroll_offset == 0:
            return
        self._scroll_offset = 0
        self._paint(self._viewport_w or self._term_width())

    # -- streaming (transient content above the live region) ----------------

    def stream_begin(self) -> None:
        """Start a new streaming session; clear any prior in-flight buffer."""
        self._stream_buffer = ""
        self._paint(self._viewport_w or self._term_width())

    def stream_append(self, text) -> None:
        """Append a chunk of text (Rich markup OK) to the stream buffer.

        The chunk is rendered to ANSI, appended to ``_stream_buffer``,
        and the viewport repainted. Stream content sits between the
        permanent scrollback and the live region, visually indistinguishable
        from scrollback — but held only in our model, so it vanishes
        without a trace when stream_end is called.
        """
        if text is None or text == "":
            return
        ansi = self._rich_print_to_str(text, end="")
        if not ansi:
            return
        self._stream_buffer += ansi
        self._paint(self._viewport_w or self._term_width())

    def stream_end(self) -> None:
        """Discard the stream buffer and repaint.

        The caller should follow this with a ``print_above`` of the final,
        formatted message so the viewport slot previously occupied by the
        live stream is replaced with the finalized rendering.
        """
        self._stream_buffer = ""
        self._paint(self._viewport_w or self._term_width())

    # -- internal -----------------------------------------------------------

    def _stream_rows(self, width: int) -> List[str]:
        """Split the in-flight stream buffer into terminal-width visual rows."""
        if not self._stream_buffer or width <= 0:
            return []
        rows: List[str] = []
        for logical in self._stream_buffer.split("\n"):
            if logical == "":
                rows.append("")
                continue
            # Walk the string, advancing `col` only for printable characters.
            current = ""
            col = 0
            i = 0
            n = len(logical)
            while i < n:
                if logical[i] == "\x1b":
                    m = self._ANSI_RE.match(logical, i)
                    if m is not None:
                        current += m.group()
                        i = m.end()
                        continue
                current += logical[i]
                col += 1
                i += 1
                if col >= width:
                    rows.append(current)
                    current = ""
                    col = 0
            if current or not rows or rows[-1] != "":
                rows.append(current)
        return rows

    def _paint(self, width: int) -> None:
        """Compute the visible viewport and emit row-level diff to stdout."""
        vh = self._term_height()
        stream_rows = self._stream_rows(width) if self._stream_buffer else []
        # Keep a scrolled-up user's view stable while the stream buffer
        # grows (or shrinks, e.g. on stream_end). If the user is stuck
        # to the bottom (offset == 0) we let new tokens naturally appear
        # at the bottom; if they're scrolled up we bump offset by the
        # stream-row delta so the visible content slice is unchanged.
        if self._scroll_offset > 0:
            delta = len(stream_rows) - self._last_stream_rows
            if delta:
                self._scroll_offset = max(0, self._scroll_offset + delta)
        self._last_stream_rows = len(stream_rows)
        # The live region is always pinned to the bottom of the viewport;
        # scrolling only affects the history area (scrollback + in-flight
        # stream) above it.
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
        visible = visible[:vh]

        size_changed = (
            not self._prev_viewport
            or len(self._prev_viewport) != vh
            or width != self._viewport_w
        )

        parts: List[str] = [self._SYNC_START]
        if size_changed:
            # Full render: place every row explicitly.
            parts.append("\x1b[H")  # cursor home
            for i, line in enumerate(visible):
                parts.append("\x1b[2K")
                parts.append(line)
                if i < vh - 1:
                    parts.append("\n\r")
        else:
            old = self._prev_viewport
            for i in range(vh):
                if old[i] == visible[i]:
                    continue
                parts.append(f"\x1b[{i + 1};1H\x1b[2K")
                parts.append(visible[i])
        parts.append(self._SYNC_END)
        sys.stdout.write("".join(parts))
        sys.stdout.flush()
        self._prev_viewport = visible
        self._viewport_h = vh
        self._viewport_w = width


def DiffRenderer(*args, mode: Optional[str] = None, **kwargs):
    """Factory that picks a renderer based on *mode* or the environment.

    - ``mode="inline"`` or env ``EGG_DISPLAY_MODE=inline|classic`` →
      :class:`InlineDiffRenderer` — HEAD-style behaviour: live-region
      diff, ``print_above`` writes to the terminal's real scrollback
      archive, native mouse/scroll works, low SSH bandwidth. Streaming
      content goes into the Chat Messages panel (no transient "static"
      preview).
    - ``mode="full"`` or env ``EGG_DISPLAY_MODE=full|tui|altscreen``
      (default) → :class:`FullScreenDiffRenderer` — owns the alt-screen
      viewport, supports ``stream_begin``/``stream_append``/
      ``stream_end`` for transient in-scrollback-area streaming, and
      in-app scroll (``PageUp``/``PageDown``, mouse wheel) since the
      alt-screen has no terminal-native scrollback.
    """
    import os as _os
    if mode is None:
        mode = _os.environ.get("EGG_DISPLAY_MODE", "").strip().lower()
    mode = (mode or "").lower()
    if mode in ("inline", "classic", "head", "legacy"):
        return InlineDiffRenderer(*args, **kwargs)
    # Default: full-screen.
    return FullScreenDiffRenderer(*args, **kwargs)


__all__ = [
    "InlineDiffRenderer",
    "FullScreenDiffRenderer",
    "DiffRenderer",
]
