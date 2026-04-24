from __future__ import annotations

import eggthreads
from eggthreads import ThreadRunner


def test_sanitize_terminal_text_strips_escape_sequences_and_controls() -> None:
    raw = "ok\x1b[2Jstill\x1b[Hhere\x1b]52;c;AAAA\x07!\r\x08\x00\tend\nnext"

    safe = eggthreads.sanitize_terminal_text(raw)

    assert safe == "okstillhere!\n��\tend\nnext"
    assert "\x1b" not in safe
    assert "\r" not in safe
    assert "\x08" not in safe
    assert "\x00" not in safe


def test_filter_tool_output_always_applies_terminal_safety() -> None:
    runner = ThreadRunner.__new__(ThreadRunner)

    safe = runner._filter_tool_output("a\x1b[2Jb\x00c", mask_secrets=False)

    assert safe == "ab�c"


def test_default_bash_tool_output_is_terminal_safe(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    tools = eggthreads.create_default_tools()

    result = tools.execute("bash", {"script": "printf 'a\\033[2Jb\\r\\bc'"})

    assert "\x1b" not in result
    assert "\r" not in result
    assert "\x08" not in result
    # Python's text-mode subprocess pipes may translate CR to LF before the
    # sanitizer runs; the important bit is that terminal controls are gone.
    assert "ab" in result
    assert "�c" in result