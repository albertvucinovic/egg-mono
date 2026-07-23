from __future__ import annotations

from rich.style import Style

from egg.syntax_highlighting import (
    infer_tool_output_syntax,
    semantic_syntax_theme,
    syntax_highlight_text,
    tool_argument_syntax_lexer,
)


def _theme():
    return semantic_syntax_theme(
        foreground=Style(color="white"),
        muted=Style(dim=True),
        accent=Style(color="cyan"),
        string=Style(color="yellow"),
        name=Style(color="green"),
        number=Style(color="magenta"),
        error=Style(color="red"),
    )


def test_known_execution_tool_arguments_have_languages():
    assert tool_argument_syntax_lexer("bash", "script") == "bash"
    assert tool_argument_syntax_lexer("bash_repl", "script") == "bash"
    assert tool_argument_syntax_lexer("python_exec", "script") == "python"
    assert tool_argument_syntax_lexer("python_repl", "code") == "python"
    assert tool_argument_syntax_lexer("bash", "query") is None


def test_highlighter_preserves_exact_literal_text_and_trailing_whitespace():
    source = 'print("[red]literal[/red]")  '
    rendered = syntax_highlight_text(source, "python", _theme())

    assert rendered.plain == source
    assert rendered.spans


def test_bash_output_infers_strict_json_but_not_scalar_or_malformed_json():
    assert infer_tool_output_syntax("bash", {"script": "printf json"}, '{"ok": true}').lexer == "json"
    assert infer_tool_output_syntax("bash", {"script": "printf scalar"}, "42") is None
    assert infer_tool_output_syntax("bash", {"script": "printf broken"}, '{"ok":') is None


def test_bash_output_infers_xml_but_rejects_document_types():
    assert infer_tool_output_syntax("bash", {"script": "printf xml"}, "<root><item /></root>").lexer == "xml"
    assert infer_tool_output_syntax(
        "bash",
        {"script": "printf xml"},
        '<!DOCTYPE root [<!ENTITY x "value">]><root>&x;</root>',
    ) is None


def test_bash_output_infers_diff_and_python_traceback_signatures():
    diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new"
    traceback = (
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in <module>\n'
        "    fail()\n"
        "ValueError: bad"
    )

    assert infer_tool_output_syntax("bash", {"script": "echo unknown"}, diff).lexer == "diff"
    assert infer_tool_output_syntax("bash", {"script": "echo unknown"}, traceback).lexer == "pytb"


def test_bash_output_uses_simple_filename_hint_and_abstains_on_compound_scripts():
    python_source = "def answer():\n    return 42"

    hint = infer_tool_output_syntax("bash", {"script": "cat src/example.py"}, python_source)
    assert hint is not None and hint.lexer == "python" and hint.source == "filename"
    assert infer_tool_output_syntax(
        "bash",
        {"script": "cat src/example.py; echo done"},
        python_source,
    ) is None


def test_bash_output_infers_filename_formats_conservatively():
    expected = {
        "head -20 settings.toml": "toml",
        "tail -n 5 payload.yaml": "yaml",
        "sed -n 1,10p script.sh": "bash",
        "cat component.tsx": "tsx",
    }
    for script, lexer in expected.items():
        hint = infer_tool_output_syntax("bash", {"script": script}, "representative output")
        assert hint is not None and hint.lexer == lexer


def test_command_derived_hint_is_not_applied_to_stderr():
    assert infer_tool_output_syntax(
        "bash",
        {"script": "cat src/example.py"},
        "permission denied",
        channel="stderr",
    ) is None


def test_non_bash_results_are_not_inferred():
    assert infer_tool_output_syntax("python_exec", {"script": "print('{}')"}, "{}") is None
