from __future__ import annotations

from pathlib import Path

import eggthreads as ts
from eggthreads.artifact_completion import current_completion_token
from eggthreads.completion_catalog import global_completion_items, record_id_completion_items, thread_completion_items


def test_global_completion_combines_files_records_and_threads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB(tmp_path / ".egg" / "threads.sqlite")
    db.init_schema()
    current = ts.create_root_thread(db, "current")
    other = ts.create_root_thread(db, "other")
    msg_id = ts.append_message(db, current, "assistant", "completion body")
    assert ts.build_autocomplete_catalog(db, current).state == "ready"
    (tmp_path / "abc-file.txt").write_text("x", encoding="utf-8")

    record_items = global_completion_items(db, current, f"ask {msg_id[-5:]}")
    thread_items = global_completion_items(db, current, f"ask {other[-5:]}")
    file_items = global_completion_items(db, current, "open abc")

    assert any(item["insert"] == msg_id for item in record_items)
    assert any(item["insert"] == other for item in thread_items)
    assert any("abc-file.txt" in item["insert"] for item in file_items)


def test_global_completion_preserves_at_and_quotes_spaces(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB(tmp_path / ".egg" / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, "current")
    (tmp_path / "my file.md").write_text("x", encoding="utf-8")
    (tmp_path / "quote'file.md").write_text("x", encoding="utf-8")

    items = global_completion_items(db, tid, "/editor @my")

    assert any(item["insert"] == "@'./my file.md'" and int(item["replace"]) == 3 for item in items)

    plain = global_completion_items(db, tid, "/editor -- my")
    assert any(item["insert"] == "'./my file.md'" for item in plain)

    short_explicit = global_completion_items(db, tid, "open ./")
    assert any(item["insert"] == "'./my file.md'" for item in short_explicit)

    quoted_name = global_completion_items(db, tid, "open quo")
    assert any(item["insert"] == "'./quote'\"'\"'file.md'" for item in quoted_name)


def test_current_completion_token_retains_quoted_and_escaped_paths() -> None:
    assert current_completion_token('/editor @"my fi') == '@"my fi'
    assert current_completion_token(r"/editor @my\ fi") == r"@my\ fi"
    assert current_completion_token("plain words") == "words"


def test_global_id_minimum_counts_alphanumeric_characters(tmp_path: Path) -> None:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread = ts.create_root_thread(db, "current")
    ts.append_message(db, thread, "assistant", "", extra={"tool_calls": [{"id": "A__bC", "function": {"name": "bash", "arguments": "{}"}}]})
    assert ts.build_autocomplete_catalog(db, thread).state == "ready"

    assert record_id_completion_items(db, thread, "a__") == []
    assert record_id_completion_items(db, thread, "a__bc")[0]["insert"] == "A__bC"


def test_shared_thread_completion_can_search_name_metadata(tmp_path: Path) -> None:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    current = ts.create_root_thread(db, "current")
    target = ts.create_root_thread(db, "Distinctive Completion Name")

    items = thread_completion_items(
        db,
        "distinctive",
        current_thread=current,
        match_metadata=True,
        include_empty=True,
    )

    assert [item["insert"] for item in items] == [target]


def test_shared_thread_completion_accepts_frontend_streaming_state(tmp_path: Path) -> None:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    current = ts.create_root_thread(db, "current")
    target = ts.create_root_thread(db, "target")

    items = thread_completion_items(
        db,
        target[-5:],
        current_thread=current,
        include_streaming=True,
        streaming_thread_ids={target},
    )

    assert len(items) == 1
    assert items[0]["display"].startswith("[STREAMING]")


def test_record_completion_sidecar_ignores_nonsemantic_churn(tmp_path: Path) -> None:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread = ts.create_root_thread(db, "current")
    message_id = ts.append_message(db, thread, "assistant", "body")
    assert ts.build_autocomplete_catalog(db, thread).state == "ready"
    assert record_id_completion_items(db, thread, message_id[-5:])

    db.append_event(
        event_id="heartbeat",
        thread_id=thread,
        type_="provider_request.started",
        payload={},
    )

    assert record_id_completion_items(db, thread, message_id[-5:])


def test_record_completion_never_serves_partial_or_stale_generation(tmp_path: Path) -> None:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread = ts.create_root_thread(db, "current")
    first_id = ts.append_message(db, thread, "assistant", "first")
    assert ts.build_autocomplete_catalog(db, thread).state == "ready"
    assert record_id_completion_items(db, thread, first_id[-5:])

    second_id = ts.append_message(db, thread, "assistant", "second")
    assert record_id_completion_items(db, thread, first_id[-5:]) == []
    assert ts.build_autocomplete_catalog(db, thread).state == "ready"
    assert record_id_completion_items(db, thread, second_id[-5:])
