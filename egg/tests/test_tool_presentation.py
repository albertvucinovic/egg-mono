from __future__ import annotations

from egg.tool_presentation import (
    bounded_medium_preview,
    format_medium_tool_arguments,
    format_medium_tool_calls,
    format_medium_tool_result,
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
