from __future__ import annotations

import eggthreads as ts


def test_skill_registry_loads_description_from_markdown() -> None:
    skills = {skill.name: skill for skill in ts.list_skills()}
    assert "rlm" in skills
    assert "persistent REPL variables" in skills["rlm"].description


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
        "from eggtools import skill\nprint('rlm' in skill())",
        drive_runtime_tools=True,
        bridge_timeout_sec=5,
    )

    assert "True" in out
