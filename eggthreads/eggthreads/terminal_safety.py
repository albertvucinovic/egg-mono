"""Helpers for keeping untrusted text safe in terminal UIs."""
from __future__ import annotations

import re


_TERMINAL_CONTROL_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"        # CSI (cursor moves, clears, SGR, mode toggles...)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC (title, clipboard, hyperlinks)
    r"|\x1b[P_^][^\x1b]*(?:\x1b\\)"  # DCS / PM / APC
    r"|\x1b[()][0-9A-Za-z]"            # charset selection
    r"|\x1b."                          # any other ESC + final byte
)


def sanitize_terminal_text(text: str) -> str:
    """Return *text* safe to persist and render in a terminal UI.

    Tool output is untrusted terminal input. Even when users enable "raw"
    secret handling, escape/control sequences must not be allowed to move the
    cursor, clear the screen, toggle modes, ring bells, or write to OSC
    channels. Preserve printable text plus newlines and tabs; replace other
    controls with U+FFFD so content boundaries remain visible.
    """
    if not isinstance(text, str) or not text:
        return text

    text = _TERMINAL_CONTROL_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        if ch in ("\n", "\t"):
            out.append(ch)
        elif cp == 0x7F or cp < 0x20 or 0x80 <= cp <= 0x9F:
            out.append("\uFFFD")
        elif 0xD800 <= cp <= 0xDFFF:
            out.append("\uFFFD")
        else:
            out.append(ch)
    return "".join(out)


def looks_like_terminal_control_text(text: str) -> bool:
    """Return True when *text* contains terminal control bytes/sequences."""
    if not isinstance(text, str) or not text:
        return False
    if _TERMINAL_CONTROL_RE.search(text):
        return True
    return any(
        (ch not in ("\n", "\t")) and (ord(ch) == 0x7F or ord(ch) < 0x20 or 0x80 <= ord(ch) <= 0x9F)
        for ch in text
    )


__all__ = ["sanitize_terminal_text", "looks_like_terminal_control_text"]