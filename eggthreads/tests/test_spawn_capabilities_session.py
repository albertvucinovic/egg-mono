from __future__ import annotations

from pathlib import Path

import eggthreads as ts
from eggthreads.tools import create_default_tools


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    # spawn_agent tools use the default .egg/threads.sqlite path, so these
    # tests intentionally use the default DB under the monkeypatched cwd.
    db = ts.ThreadsDB()
    db.init_schema()
    return db


def test_spawn_agent_attenuates_requested_allowed_tools(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.append_message(db, parent, "system", "system")
    ts.set_thread_tool_allowlist(db, parent, ["web_search", "wait"])

    child = create_default_tools().execute(
        "spawn_agent",
        {
            "parent_thread_id": parent,
            "context_text": "do child work",
            "label": "child",
            "allowed_tools": ["web_search", "bash"],
        },
    )

    cfg = ts.get_thread_tools_config(db, child)
    assert cfg.allowed_tools == {"web_search"}
    assert cfg.is_tool_allowed("web_search")
    assert not cfg.is_tool_allowed("bash")
    assert not cfg.is_tool_allowed("wait")


def test_spawn_agent_inherits_parent_allowlist_when_child_omits_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.append_message(db, parent, "system", "system")
    ts.set_thread_tool_allowlist(db, parent, ["web_search", "wait"])

    child = create_default_tools().execute(
        "spawn_agent",
        {
            "parent_thread_id": parent,
            "context_text": "do child work",
            "label": "child",
        },
    )

    cfg = ts.get_thread_tools_config(db, child)
    assert cfg.allowed_tools == {"web_search", "wait"}


def test_spawn_agent_can_disable_child_tool(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.append_message(db, parent, "system", "system")

    child = create_default_tools().execute(
        "spawn_agent",
        {
            "parent_thread_id": parent,
            "context_text": "do child work",
            "label": "child",
            "disabled_tools": ["bash"],
        },
    )

    cfg = ts.get_thread_tools_config(db, child)
    assert not cfg.is_tool_allowed("bash")
    assert cfg.is_tool_allowed("web_search")


def test_spawn_agent_share_session_propagates_parent_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.append_message(db, parent, "system", "system")
    sid = ts.enable_thread_session(db, parent, provider="memory", share="private")

    child = create_default_tools().execute(
        "spawn_agent",
        {
            "parent_thread_id": parent,
            "context_text": "do child work",
            "label": "child",
            "share_session": True,
        },
    )

    cfg = ts.get_thread_session_config(db, child)
    assert cfg.enabled is True
    assert cfg.share == "session"
    assert cfg.session_id == sid
    assert cfg.provider == "memory"


def test_spawn_agent_honors_parent_share_with_children_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.append_message(db, parent, "system", "system")
    sid = ts.enable_thread_session(db, parent, provider="memory", share_with_children_default=True)

    child = create_default_tools().execute(
        "spawn_agent",
        {
            "parent_thread_id": parent,
            "context_text": "do child work",
            "label": "child",
        },
    )

    cfg = ts.get_thread_session_config(db, child)
    assert cfg.enabled is True
    assert cfg.session_id == sid


def test_spawn_agent_share_session_does_not_share_repl_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.append_message(db, parent, "system", "system")
    sid = ts.enable_thread_session(db, parent, provider="memory", share_repl=False)

    child = create_default_tools().execute(
        "spawn_agent",
        {
            "parent_thread_id": parent,
            "context_text": "do child work",
            "label": "child",
            "share_session": True,
        },
    )

    cfg = ts.get_thread_session_config(db, child)
    assert cfg.session_id == sid
    assert cfg.share_repl is False


def test_spawn_agent_share_repl_is_explicit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.append_message(db, parent, "system", "system")
    sid = ts.enable_thread_session(db, parent, provider="memory", share_repl=False)

    child = create_default_tools().execute(
        "spawn_agent",
        {
            "parent_thread_id": parent,
            "context_text": "do child work",
            "label": "child",
            "share_session": True,
            "share_repl": True,
        },
    )

    cfg = ts.get_thread_session_config(db, child)
    assert cfg.session_id == sid
    assert cfg.share_repl is True
