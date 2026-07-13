from __future__ import annotations

import contextlib
import io

import eggthreads as ts


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

    opening = "```python\n# egg-compaction-narrative-skeleton\n"
    script = doc.split(opening, 1)[1].split("\n```", 1)[0]
    namespace = {"thread_context": {"all_messages": [], "current_prompt_messages": []}}
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        exec(compile(script, "<compaction-checkpoint-test>", "exec"), namespace, namespace)

    output = captured.getvalue()
    assert output.startswith("THREAD NARRATIVE SKELETON FOR COMPACTION v2\n")
    assert output.endswith("END THREAD NARRATIVE SKELETON FOR COMPACTION v2\n")
    assert namespace["compaction_narrative_skeleton_output"] == output


def test_compaction_checkpoint_script_retains_actionable_candidate_text_under_pressure() -> None:
    tools = ts.create_default_tools()
    doc = tools.execute("skill", {"name": "compaction-checkpoint"})
    opening = "```python\n# egg-compaction-narrative-skeleton\n"
    script = doc.split(opening, 1)[1].split("\n```", 1)[0]

    messages = [
        {
            "role": "user",
            "msg_id": f"user-{index}",
            "event_seq": index + 1,
            "content": f"actionable request {index}",
        }
        for index in range(10)
    ]
    messages.extend(
        {
            "role": "tool",
            "msg_id": f"result-{index}",
            "event_seq": index + 100,
            "tool_call_id": f"call-{index}",
            "name": "noisy_tool",
            "content": f"error-bearing result {index}",
        }
        for index in range(900)
    )
    namespace = {
        "thread_context": {
            "all_messages": messages,
            "current_prompt_messages": [],
        }
    }
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(script, "<compaction-checkpoint-pressure-test>", "exec"), namespace, namespace)

    output = namespace["compaction_narrative_skeleton_output"]
    for index in range(2, 10):
        assert f"actionable request {index}" in output


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
