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