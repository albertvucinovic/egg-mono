from __future__ import annotations

import contextlib
import io

import eggthreads as ts


def _compaction_checkpoint_script() -> str:
    doc = ts.create_default_tools().execute("skill", {"name": "compaction-checkpoint"})
    opening = "```python\n# egg-compaction-narrative-skeleton\n"
    return doc.split(opening, 1)[1].split("\n```", 1)[0]


def _render_compaction_checkpoint(messages: list[dict]) -> tuple[str, dict]:
    namespace = {
        "thread_context": {
            "all_messages": messages,
            "current_prompt_messages": [],
        }
    }
    with contextlib.redirect_stdout(io.StringIO()):
        exec(
            compile(_compaction_checkpoint_script(), "<compaction-checkpoint-test>", "exec"),
            namespace,
            namespace,
        )
    return (
        namespace["compaction_narrative_skeleton_output"],
        namespace["compaction_narrative_skeleton_index"],
    )


def test_skill_registry_loads_description_from_markdown() -> None:
    skills = {skill.name: skill for skill in ts.list_skills()}
    assert "rlm" in skills
    assert "persistent REPL variables" in skills["rlm"].description
    assert "worker-manager" in skills
    assert skills["worker-manager"].description


def test_skill_tool_lists_and_loads_documents() -> None:
    tools = ts.create_default_tools()
    specs = {spec["function"]["name"]: spec for spec in tools.tools_spec()}
    assert "skill" in specs

    listing = tools.execute("skill", {})
    assert "AVAILABLE SKILLS" in listing
    assert "rlm" in listing

    search = tools.execute("skill", {"query": "persistent REPL"})
    assert "SKILL SEARCH RESULTS" in search
    assert "rlm" in search

    doc = tools.execute("skill", {"name": "rlm"})
    assert "# Skill: rlm" in doc
    assert "chunk_text" in doc
    assert "special RLM runtime module" in doc

    worker_doc = tools.execute("skill", {"name": "worker-manager"})
    assert "# Skill: worker-manager" in worker_doc
    assert "Spawn template" in worker_doc


def test_compaction_checkpoint_skill_includes_assistant_notes() -> None:
    tools = ts.create_default_tools()
    doc = tools.execute("skill", {"name": "compaction-checkpoint"})

    assert "Assistant Notes" in doc
    assert "answer_user_preserve_turn" in doc
    assert "source_tool_name" in doc
    assert "omitted_empty_assistant" in doc


def test_compaction_checkpoint_script_transport_is_optional_and_output_is_checkable() -> None:
    tools = ts.create_default_tools()
    doc = tools.execute("skill", {"name": "compaction-checkpoint"})

    assert "Using `extract_tool_output` or creating a file is optional" in doc
    assert "Required complete-map check" in doc

    script = _compaction_checkpoint_script()
    namespace = {"thread_context": {"all_messages": [], "current_prompt_messages": []}}
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        exec(compile(script, "<compaction-checkpoint-test>", "exec"), namespace, namespace)

    output = captured.getvalue()
    assert output.startswith("THREAD NARRATIVE SKELETON FOR COMPACTION v3\n")
    assert output.endswith("END THREAD NARRATIVE SKELETON FOR COMPACTION v3\n")
    assert namespace["compaction_narrative_skeleton_output"] == output
    assert namespace["compaction_narrative_skeleton_index"]["version"] == 3


def test_compaction_checkpoint_script_separates_user_history_from_active_turn() -> None:
    messages = [
        {
            "role": "user",
            "msg_id": "historical-user-id",
            "event_seq": 1,
            "content": "historical user requirement",
        },
        {
            "role": "assistant",
            "msg_id": "historical-assistant-id",
            "event_seq": 2,
            "content": "Historical decision: we should preserve this constraint.",
            "tool_calls": [
                {
                    "id": "historical-call-id",
                    "function": {"name": "bash", "arguments": "historical tool arguments"},
                }
            ],
        },
        {
            "role": "tool",
            "msg_id": "historical-result-id",
            "event_seq": 3,
            "tool_call_id": "historical-call-id",
            "name": "bash",
            "content": "historical tool result",
        },
        {"role": "user", "msg_id": "active-user-1", "event_seq": 4, "content": "active request"},
        {"role": "user", "msg_id": "active-user-2", "event_seq": 5, "content": "active clarification"},
        {"role": "assistant", "msg_id": "active-assistant", "event_seq": 6, "content": "active work"},
        {
            "role": "assistant",
            "msg_id": "active-call-message",
            "event_seq": 7,
            "tool_calls": [
                {
                    "id": "active-call-id",
                    "function": {"name": "bash", "arguments": "active tool arguments"},
                }
            ],
        },
        {
            "role": "tool",
            "msg_id": "active-result-id",
            "event_seq": 8,
            "tool_call_id": "active-call-id",
            "name": "bash",
            "content": "active tool result",
        },
        {
            "role": "user",
            "msg_id": "control-id",
            "event_seq": 9,
            "content": "Use the `compaction-checkpoint` skill. Mode: `summary_only`.",
        },
        {
            "role": "assistant",
            "msg_id": "checkpoint-call-message",
            "event_seq": 10,
            "tool_calls": [
                {
                    "id": "checkpoint-call-id",
                    "function": {"name": "skill", "arguments": "checkpoint machinery"},
                }
            ],
        },
    ]

    output, index = _render_compaction_checkpoint(messages)
    history, active = output.split("=== ACTIVE TURN — CONTINUE FROM HERE ===", 1)

    assert "historical user requirement" in history
    assert "Historical decision" in history
    assert "historical tool arguments" not in history
    assert "historical tool result" not in history
    assert "active request" in active
    assert "active clarification" in active
    assert "active tool arguments" in active
    assert "active tool result" in active
    assert "checkpoint machinery" not in output
    assert "historical-user-id" not in output
    assert index["active_start_message_index"] == 3
    assert index["content_end_message_index"] == 8
    assert index["history_tool_event_count_omitted"] == 2


def test_compaction_checkpoint_script_retains_most_users_under_historical_tool_pressure() -> None:
    messages: list[dict] = []
    for index in range(200):
        messages.extend(
            [
                {
                    "role": "user",
                    "msg_id": f"user-{index}",
                    "event_seq": index * 4 + 1,
                    "content": f"actionable request {index}",
                },
                {
                    "role": "assistant",
                    "msg_id": f"call-message-{index}",
                    "event_seq": index * 4 + 2,
                    "tool_calls": [
                        {
                            "id": f"call-{index}",
                            "function": {
                                "name": "noisy_tool",
                                "arguments": f"historical arguments {index}",
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "msg_id": f"result-{index}",
                    "event_seq": index * 4 + 3,
                    "tool_call_id": f"call-{index}",
                    "name": "noisy_tool",
                    "content": f"historical error-bearing result {index}",
                },
                {
                    "role": "assistant",
                    "msg_id": f"assistant-{index}",
                    "event_seq": index * 4 + 4,
                    "content": f"routine assistant response {index}",
                },
            ]
        )
    messages.extend(
        [
            {"role": "user", "msg_id": "active-1", "event_seq": 1_000, "content": "active request"},
            {"role": "user", "msg_id": "active-2", "event_seq": 1_001, "content": "active clarification"},
        ]
    )

    output, index = _render_compaction_checkpoint(messages)

    for index_value in range(200):
        assert f"actionable request {index_value}" in output
    assert "historical arguments" not in output
    assert "historical error-bearing result" not in output
    assert "active request" in output
    assert "active clarification" in output
    assert len(index["history_users_shown"]) == 200
    assert index["history_tool_event_count_omitted"] == 400
    assert len(output) <= 48_000
    assert len(output.splitlines()) <= 700


def test_compaction_checkpoint_script_keeps_useful_historical_assistant_notes() -> None:
    messages = [
        {"role": "user", "msg_id": "old-user", "event_seq": 1, "content": "old task"},
        {
            "role": "assistant",
            "msg_id": "note-call-message",
            "event_seq": 2,
            "tool_calls": [
                {
                    "id": "note-call",
                    "function": {
                        "name": "answer_user_while_preserving_llm_turn",
                        "arguments": "historical note tool arguments",
                    },
                }
            ],
        },
        {
            "role": "assistant",
            "msg_id": "note-message",
            "event_seq": 3,
            "tool_call_id": "note-call",
            "content": "Progress note: implementation is incomplete and tests still fail.",
        },
        {"role": "user", "msg_id": "active-user", "event_seq": 4, "content": "continue now"},
    ]

    output, index = _render_compaction_checkpoint(messages)

    assert "Assistant Note: Progress note" in output
    assert "historical note tool arguments" not in output
    assert index["history_context_shown"][0]["kind"] == "NOTE"


def test_compaction_checkpoint_script_bounds_an_oversized_active_turn() -> None:
    messages: list[dict] = [
        {"role": "user", "msg_id": "active-user", "event_seq": 1, "content": "active request"}
    ]
    messages.extend(
        {
            "role": "assistant",
            "msg_id": f"active-assistant-{index}",
            "event_seq": index + 2,
            "content": f"active assistant update {index}",
        }
        for index in range(400)
    )

    output, index = _render_compaction_checkpoint(messages)

    assert "active request" in output
    assert "active assistant update 399" in output
    assert index["active_records_omitted"] > 0
    assert len(index["active_records_shown"]) == 220
    assert len(output) <= 48_000
    assert len(output.splitlines()) <= 700


def test_eggtools_exposes_skill_helper_in_memory_repl(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="memory")
    runtime = ts.get_or_create_runtime_thread(db, parent, language="python")
    ts.set_thread_tools_enabled(db, runtime, True)
    ts.set_thread_tool_allowlist(db, runtime, ["skill"])

    out = ts.execute_python_repl(
        db,
        parent,
        "from eggtools import skill\n"
        "print('rlm' in skill())\n"
        "print('# RLM Skill' in skill('rlm'))",
        drive_runtime_tools=True,
        timeout_sec=5,
    )

    assert out.count("True") == 2


def test_skill_commands_defensively_unwrap_shared_structured_render(monkeypatch) -> None:
    from eggthreads.builtin_plugins import skills as skills_plugin
    from eggthreads.tools import ToolExecutionResult

    blocks: list[tuple[str, str]] = []

    class Context:
        log_system = staticmethod(lambda _message: None)
        console_print_block = staticmethod(
            lambda title, text, **_kwargs: blocks.append((title, text))
        )

    monkeypatch.setattr(
        skills_plugin,
        "render_skill_request",
        lambda _args: ToolExecutionResult(
            "canonical\ntext\n",
            publication_presentation=ts.line_number_presentation(),
        ),
    )

    result = skills_plugin.skills_command(Context(), "")

    assert result.clear_input is True
    assert blocks == [("Skills", "1: canonical\n2: text\n")]


def test_direct_numbered_skill_is_presented_but_structured_result_is_canonical() -> None:
    from eggthreads.builtin_plugins.skills import render_skill_request
    from eggthreads.tools import ToolExecutionResult

    registry = ts.create_default_tools()
    direct = registry.execute("skill", {"name": "rlm", "line_numbers": True})
    structured = render_skill_request({"name": "rlm", "line_numbers": True})

    assert direct.startswith("1: # Skill: rlm\n2: ")
    assert isinstance(structured, ToolExecutionResult)
    assert structured.output.startswith("# Skill: rlm\n\n")
    assert not structured.output.startswith("1: ")
    assert structured.presented_output().startswith("1: # Skill: rlm\n2: ")
