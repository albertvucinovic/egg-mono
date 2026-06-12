from __future__ import annotations

from pathlib import Path

import eggthreads as ts


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_execute_python_repl_memory_provider_persists_state(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="memory")

    out1 = ts.execute_python_repl(db, parent, "x = 41")
    assert "ERROR" not in out1

    out2 = ts.execute_python_repl(db, parent, "x + 1")
    assert "42" in out2

    runtime = ts.find_runtime_thread(db, parent, language="python")
    assert runtime is not None
    assert runtime.runtime_thread_id in ts.list_children_ids(db, parent)


def test_execute_python_repl_auto_creates_session_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_RLM_SESSION_PROVIDER", "memory")
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")

    out = ts.execute_python_repl(db, parent, "1 + 1")
    assert "2" in out
    runtime = ts.find_runtime_thread(db, parent, language="python")
    assert runtime is not None
    cfg = ts.get_thread_session_config(db, runtime.runtime_thread_id)
    assert cfg.enabled is True
    assert cfg.provider == "memory"


def test_execute_python_repl_reports_disabled_auto_session(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_RLM_AUTO_SESSION", "0")
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")

    out = ts.execute_python_repl(db, parent, "1 + 1")
    assert "auto-create is disabled" in out


def test_python_repl_tool_registered():
    tools = ts.create_default_tools()
    specs = {spec["function"]["name"]: spec for spec in tools.tools_spec()}
    assert "python_repl" in specs
    props = specs["python_repl"]["function"]["parameters"]["properties"]
    assert "timeout" in props
    assert "timeout_sec" not in props
    assert "drive_runtime_tools" not in props
    names = set(specs)
    assert "session_status" not in names
    assert "session_reset" not in names
    assert "session_stop" not in names


def test_python_repl_tool_schema_mentions_hydrated_thread_context():
    tools = ts.create_default_tools()
    spec = {spec["function"]["name"]: spec for spec in tools.tools_spec()}["python_repl"]["function"]
    description = spec["description"]

    assert "thread_context" in description
    assert "older_messages_not_in_prompt" in description
    assert "search_thread" in description
    assert "get_message" in description
    assert "reload_thread_context" in description
    assert "hidden/local-only content is excluded" in description


def test_python_repl_hydrates_thread_context_aliases_helpers_and_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    old = ts.append_message(db, parent, "user", "old visible context")
    summary = ts.append_message(db, parent, "assistant", "compact summary")
    current = ts.append_message(db, parent, "user", "current question")
    hidden = ts.append_message(db, parent, "user", "hidden secret", extra={"no_api": True})
    result = ts.commit_thread_compaction(db, parent, summary, created_by="test")
    assert result.success is True
    ts.enable_thread_session(db, parent, provider="memory")

    code = (
        f"print(thread_context['thread']['thread_id'] == {parent!r})\n"
        "print([m['msg_id'] for m in all_messages])\n"
        "print([m['msg_id'] for m in older_messages_not_in_prompt])\n"
        "print([m['msg_id'] for m in current_prompt_messages])\n"
        f"print(messages_by_id[{old!r}]['content'])\n"
        "print([m['msg_id'] for m in user_messages])\n"
        "print(search_thread('old', role='user', in_prompt=False)[0]['msg_id'])\n"
        f"print(get_message({summary!r})['content'])\n"
        "print(callable(print_message), callable(reload_thread_context))\n"
        "print('jsonl_path' in context_files and 'markdown_path' in context_files)\n"
        "print('old visible context' in open(context_files['markdown_path'], encoding='utf-8').read())\n"
        "thread_context['all_messages'] = []\n"
        "print(len(reload_thread_context()['all_messages']))\n"
        f"print({hidden!r} in messages_by_id)\n"
    )
    out = ts.execute_python_repl(db, parent, code, timeout_sec=5)

    assert "Traceback" not in out
    assert "True" in out
    assert repr([old, summary, current]) in out
    assert repr([old]) in out
    assert repr([summary, current]) in out
    assert "old visible context" in out
    assert repr([old, current]) in out
    assert summary in out
    assert "3" in out  # reload_thread_context rebuilt the three visible messages
    assert "False" in out  # hidden message id is not in messages_by_id
    assert "jsonl_path" not in out  # only printed as a boolean, not full context
    jsonl = tmp_path / ".egg_thread_context" / parent / "thread_context.jsonl"
    markdown = tmp_path / ".egg_thread_context" / parent / "thread_context.md"
    assert jsonl.exists()
    assert markdown.exists()
    jsonl_text = jsonl.read_text(encoding="utf-8")
    markdown_text = markdown.read_text(encoding="utf-8")
    assert old in jsonl_text
    assert "old visible context" in markdown_text
    assert hidden not in jsonl_text
    assert hidden not in ts.build_repl_thread_context(db, parent)["messages_by_id"]


def test_python_repl_thread_context_rebuilds_when_event_seq_is_stale(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    first = ts.append_message(db, parent, "user", "first")
    ts.enable_thread_session(db, parent, provider="memory")

    out1 = ts.execute_python_repl(
        db,
        parent,
        "print(thread_context['thread']['loaded_through_event_seq'])\nprint([m['msg_id'] for m in all_messages])",
        timeout_sec=5,
    )
    seq1 = db.max_event_seq(parent)
    assert "Traceback" not in out1
    assert str(seq1) in out1
    assert repr([first]) in out1

    second = ts.append_message(db, parent, "assistant", "second")
    out2 = ts.execute_python_repl(
        db,
        parent,
        "print(thread_context['thread']['loaded_through_event_seq'])\nprint([m['msg_id'] for m in all_messages])",
        timeout_sec=5,
    )
    seq2 = db.max_event_seq(parent)
    assert "Traceback" not in out2
    assert str(seq2) in out2
    assert repr([first, second]) in out2
    assert seq2 > seq1


def test_shared_session_uses_separate_repl_channel_by_default(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    sid = ts.enable_thread_session(db, parent, provider="memory", share_repl=False)
    child = ts.create_child_thread(db, parent, name="child")
    ts.set_thread_session_config(
        db,
        child,
        enabled=True,
        provider="memory",
        share="session",
        session_id=sid,
        owner_thread_id=parent,
    )

    assert "ERROR" not in ts.execute_python_repl(db, parent, "x = 'parent'")
    child_out = ts.execute_python_repl(db, child, "globals().get('x', 'missing')")
    assert "missing" in child_out


def test_share_repl_true_shares_interpreter_channel(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    sid = ts.enable_thread_session(db, parent, provider="memory", share_repl=True)
    child = ts.create_child_thread(db, parent, name="child")
    ts.set_thread_session_config(
        db,
        child,
        enabled=True,
        provider="memory",
        share="session",
        session_id=sid,
        owner_thread_id=parent,
        share_repl=True,
    )

    assert "ERROR" not in ts.execute_python_repl(db, parent, "shared_value = 99")
    child_out = ts.execute_python_repl(db, child, "shared_value")
    assert "99" in child_out


def test_direct_drive_reports_error_inside_running_loop(tmp_path):
    import asyncio

    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="memory")

    async def run():
        return ts.execute_python_repl(db, parent, "1 + 1", drive_runtime_tools=True)

    out = asyncio.run(run())
    assert "drive_runtime_tools=True cannot be used" in out
