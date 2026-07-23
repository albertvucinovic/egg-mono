from __future__ import annotations

from rich.style import Style

from egg.syntax_highlighting import semantic_syntax_theme
from egg.tool_presentation import (
    MediumToolStyles,
    bounded_medium_preview,
    format_medium_tool_arguments,
    format_medium_tool_calls,
    format_medium_tool_result,
    medium_tool_calls_text,
    medium_tool_result_text,
)


SEMANTIC_STYLES = MediumToolStyles(
    call="bold cyan",
    call_name="bold yellow",
    argument_key="bold cyan",
    argument_value="white",
    result="white",
    muted="dim",
    command="bold cyan",
)


SYNTAX_THEME = semantic_syntax_theme(
    foreground=Style(color="white"),
    muted=Style(dim=True),
    accent=Style(color="cyan"),
    string=Style(color="yellow"),
    name=Style(color="green"),
    number=Style(color="magenta"),
    error=Style(color="red"),
)


def test_medium_arguments_decode_json_and_preserve_multiline_blocks():
    rendered = format_medium_tool_arguments(
        '{"script":"echo one\\necho two","timeout":120,"options":{"cwd":"/tmp"}}'
    )

    assert rendered == (
        "script:\n"
        "  echo one\n"
        "  echo two\n"
        "timeout: 120\n"
        "options:\n"
        "  {\n"
        '    "cwd": "/tmp"\n'
        "  }"
    )


def test_medium_arguments_keep_malformed_provider_text_inspectable():
    assert format_medium_tool_arguments("{not valid json") == (
        "arguments:\n  {not valid json"
    )


def test_medium_tool_group_keeps_order_and_exact_ids():
    rendered = format_medium_tool_calls([
        {
            "id": "call-a",
            "function": {"name": "bash", "arguments": {"script": "echo a"}},
        },
        {
            "id": "call-b",
            "function": {"name": "bash", "arguments": {"script": "echo b"}},
        },
    ])

    assert rendered.index("1. bash · tool_call_id: call-a") < rendered.index(
        "2. bash · tool_call_id: call-b"
    )
    assert "   script:\n     echo a" in rendered
    assert "   script:\n     echo b" in rendered


def test_medium_tool_group_emits_one_inspection_hint_when_arguments_are_bounded():
    rendered = format_medium_tool_calls([
        {
            "id": "call-long",
            "function": {"name": "bash", "arguments": {"script": "X" * 10_000}},
        },
        {
            "id": "call-short",
            "function": {"name": "bash", "arguments": {"script": "echo done"}},
        },
    ], inspect_message_id="msg-calls")

    assert rendered.count("Inspect complete persisted record: /show msg-calls") == 1


def test_medium_short_and_empty_results_are_explicit():
    assert format_medium_tool_result("27 passed in 1.84s") == "27 passed in 1.84s"
    assert format_medium_tool_result("") == "(no output)"


def test_medium_long_result_keeps_head_tail_and_inspection_hint():
    lines = [f"line-{index:02d}" for index in range(40)]
    rendered = format_medium_tool_result("\n".join(lines), inspect_message_id="msg-result")

    assert "line-00" in rendered
    assert "line-39" in rendered
    assert "line-20" not in rendered
    assert "omitted" in rendered
    assert "Inspect complete persisted record: /show msg-result" in rendered


def test_medium_preview_is_bounded_for_one_extremely_long_line():
    rendered = bounded_medium_preview(
        "A" * 10_000,
        empty_text="(empty)",
        max_chars=4096,
    )

    assert len(rendered) <= 4096
    assert rendered.startswith("A" * 100)
    assert rendered.endswith("A" * 100)
    assert "omitted 1 line" in rendered


def test_medium_preview_preserves_marker_under_tiny_explicit_bound():
    rendered = bounded_medium_preview(
        "Y" * 2_000,
        empty_text="(empty)",
        max_chars=80,
    )

    assert len(rendered) <= 80
    assert "omitted" in rendered


def test_medium_preview_preserves_show_hint_under_small_bound():
    rendered = bounded_medium_preview(
        "Z" * 2_000,
        empty_text="(empty)",
        inspect_message_id="msg-small",
        max_chars=96,
    )

    assert len(rendered) <= 96
    assert "omitted" in rendered
    assert "Inspect complete persisted record: /show msg-small" in rendered


def test_medium_result_keeps_raw_recovery_hint():
    rendered = format_medium_tool_result(
        "short optimized preview",
        recovery_hint="read_long_tool_output('abc123', chunk_number=1)",
    )

    assert "short optimized preview" in rendered
    assert "Raw output: read_long_tool_output('abc123', chunk_number=1)" in rendered


def test_medium_result_does_not_repeat_recovery_hint_already_in_body():
    hint = "read_long_tool_output('abc123', chunk_number=1)"
    rendered = format_medium_tool_result(
        f"Preview complete. Raw output: {hint}",
        recovery_hint=hint,
    )

    assert rendered.count(hint) == 1


def test_medium_long_result_stays_bounded_with_recovery_footer():
    rendered = format_medium_tool_result(
        "X" * 20_000,
        inspect_message_id="msg-long",
        recovery_hint="read_long_tool_output('abc123', chunk_number=1)",
    )

    assert len(rendered) <= 4096
    assert "Inspect complete persisted record: /show msg-long" in rendered
    assert "Raw output: read_long_tool_output('abc123', chunk_number=1)" in rendered


def test_medium_short_result_does_not_add_unnecessary_show_hint():
    rendered = format_medium_tool_result(
        "short result",
        inspect_message_id="msg-short",
    )

    assert rendered == "short result"


def test_medium_tool_group_without_ids_stays_readable_without_guessing():
    rendered = format_medium_tool_calls([
        {"function": {"name": "bash", "arguments": {"script": "echo first"}}},
        {"function": {"name": "bash", "arguments": {"script": "echo second"}}},
    ])

    assert "1. bash\n" in rendered
    assert "2. bash\n" in rendered
    assert "tool_call_id:" not in rendered


def test_medium_tool_call_rich_text_uses_semantic_styles_and_literal_values():
    rendered = medium_tool_calls_text([
        {
            "id": "call-color",
            "function": {
                "name": "bash",
                "arguments": {
                    "script": "echo [red]literal[/red]",
                    "timeout": 30,
                },
            },
        },
    ], styles=SEMANTIC_STYLES)

    assert "echo [red]literal[/red]" in rendered.plain
    styled = [(rendered.plain[span.start:span.end], str(span.style)) for span in rendered.spans]
    assert ("1.", "bold cyan") in styled
    assert ("bash", "bold yellow") in styled
    assert ("script", "bold cyan") in styled
    assert ("tool_call_id:", "dim") in styled
    # User/tool-supplied Rich-looking text is one literal value span, never parsed.
    assert any("[red]literal[/red]" in value and style == "white" for value, style in styled)


def test_medium_tool_result_styles_only_metadata_not_literal_output():
    rendered = medium_tool_result_text(
        "[red]literal result[/red]",
        styles=SEMANTIC_STYLES,
    )

    assert rendered.plain == "[red]literal result[/red]"
    assert len(rendered.spans) == 1
    assert str(rendered.spans[0].style) == "white"


def test_medium_multiline_argument_does_not_reclassify_value_colons_as_keys():
    rendered = medium_tool_calls_text([
        {
            "function": {
                "name": "bash",
                "arguments": {
                    "script": "echo start\nhttps://example.test/path\nlabel: value",
                },
            },
        },
    ], styles=SEMANTIC_STYLES)

    styled = [(rendered.plain[span.start:span.end], str(span.style)) for span in rendered.spans]
    assert any("https://example.test/path" in value and style == "white" for value, style in styled)
    assert any("label: value" in value and style == "white" for value, style in styled)


def test_medium_bash_script_argument_uses_syntax_spans_and_keeps_literal_text():
    script = 'for item in one two; do\n  echo "[red]$item[/red]"\ndone'
    rendered = medium_tool_calls_text([
        {
            "function": {
                "name": "bash",
                "arguments": {"script": script, "timeout": 30},
            },
        },
    ], styles=SEMANTIC_STYLES, syntax_theme=SYNTAX_THEME)

    assert 'for item in one two; do' in rendered.plain
    assert 'echo "[red]$item[/red]"' in rendered.plain
    assert 'done' in rendered.plain
    styled = [(rendered.plain[span.start:span.end], str(span.style)) for span in rendered.spans]
    assert any(value == "for" and "cyan" in style for value, style in styled)
    assert "[red]$item[/red]" in rendered.plain


def test_medium_python_repl_code_uses_python_syntax_spans():
    rendered = medium_tool_calls_text([
        {
            "function": {
                "name": "python_repl",
                "arguments": {"code": "def answer():\n    return 42"},
            },
        },
    ], styles=SEMANTIC_STYLES, syntax_theme=SYNTAX_THEME)

    styled = [(rendered.plain[span.start:span.end], str(span.style)) for span in rendered.spans]
    assert any(value == "def" and "cyan" in style for value, style in styled)
    assert any(value == "answer" and "green" in style for value, style in styled)


def test_medium_bash_result_highlights_sections_without_styling_metadata_as_code():
    rendered = medium_tool_result_text(
        '--- STDOUT ---\n{"ok": true}\n--- STDERR ---\nplain diagnostic',
        styles=SEMANTIC_STYLES,
        tool_name="bash",
        tool_arguments={"script": "jq . result.json"},
        syntax_theme=SYNTAX_THEME,
    )

    assert rendered.plain == '--- STDOUT ---\n{"ok": true}\n--- STDERR ---\nplain diagnostic'
    styled = [(rendered.plain[span.start:span.end], str(span.style)) for span in rendered.spans]
    assert ("--- STDOUT ---", "dim") in styled
    assert ("--- STDERR ---", "dim") in styled
    assert any('"ok"' in value and "yellow" in style for value, style in styled)
    assert any("plain diagnostic" in value and style == "white" for value, style in styled)
