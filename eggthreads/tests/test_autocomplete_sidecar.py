from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import eggthreads as ts
import eggthreads.autocomplete_sidecar as sidecar


def _db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / ".egg" / "threads.sqlite")
    db.init_schema()
    return db


def _append(db, thread_id: str, msg_id: str, role: str, content: str, **extra):
    db.append_event(
        event_id=f"event-{msg_id}",
        thread_id=thread_id,
        type_="msg.create",
        msg_id=msg_id,
        payload={"role": role, "content": content, **extra},
    )


def _schema(db: ts.ThreadsDB):
    return tuple(
        db.conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
    )


def test_sidecar_path_is_versioned_deterministic_and_db_specific(tmp_path: Path) -> None:
    first = sidecar.autocomplete_sidecar_path(tmp_path / "a" / "threads.sqlite")
    same = sidecar.autocomplete_sidecar_path(tmp_path / "a" / "." / "threads.sqlite")
    other = sidecar.autocomplete_sidecar_path(tmp_path / "b" / "threads.sqlite")

    assert first == same
    assert first != other
    assert first.parent.name == "cache"
    assert first.name.endswith("-autocomplete-v5.sqlite")


def test_cold_read_is_explicit_and_does_not_build_or_create_sidecar(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "root")
    _append(db, thread_id, "message-1", "user", "hello")
    path = sidecar.autocomplete_sidecar_path(db.path)

    page = sidecar.query_autocomplete_records(db, thread_id, "message")

    assert page.state == "missing"
    assert page.records == ()
    assert page.sidecar_path == path
    assert not path.exists()


def test_build_matches_full_canonical_catalog_and_preserves_pairing(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "root")
    _append(db, thread_id, "user-1", "user", "first")
    _append(db, thread_id, "deleted-2", "assistant", "gone")
    ts.delete_message(db, thread_id, "deleted-2")
    _append(
        db,
        thread_id,
        "assistant-3",
        "assistant",
        "",
        tool_calls=[{
            "id": "CallExactCase",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"script":"echo hi"}'},
        }],
    )
    _append(
        db,
        thread_id,
        "result-4",
        "tool",
        "output",
        name="bash",
        tool_call_id="CallExactCase",
    )
    _append(db, thread_id, "note-5", "assistant", "status", answer_user_preserve_turn=True)

    expected = ts.list_show_record_candidates(db, thread_id)
    result = sidecar.build_autocomplete_catalog(db, thread_id, batch_size=2)
    page = sidecar.query_autocomplete_records(db, thread_id, limit=100)

    assert result.state == "ready"
    assert page.state == "ready"
    assert page.total == len(expected)
    assert [record.record_id for record in page.records] == [candidate.record_id for candidate in expected]
    by_id = {record.record_id: record for record in page.records}
    assert "deleted-2" not in by_id
    assert by_id["CallExactCase"].paired_message_ids == ("result-4",)
    assert by_id["result-4"].paired_message_ids == ("assistant-3",)
    assert by_id["note-5"].kind == "assistant_note"


def test_query_is_case_insensitive_exact_prefix_suffix_and_all_pages_reachable(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "root")
    for index in range(37):
        _append(db, thread_id, f"Record-{index:03d}-Suffix", "assistant", f"body {index}")
    assert sidecar.build_autocomplete_catalog(db, thread_id, batch_size=7).state == "ready"

    exact = sidecar.query_autocomplete_records(db, thread_id, "record-010-suffix")
    prefix = sidecar.query_autocomplete_records(db, thread_id, "ReCoRd-01", limit=100)
    suffix = sidecar.query_autocomplete_records(db, thread_id, "sUfFiX", match="all", limit=100)

    assert [record.record_id for record in exact.records] == ["Record-010-Suffix"]
    assert len(prefix.records) == 10
    assert len(suffix.records) == 37

    seen = []
    cursor = None
    while True:
        page = sidecar.query_autocomplete_records(
            db, thread_id, order="oldest", limit=6, cursor=cursor
        )
        assert page.state == "ready"
        seen.extend(record.record_id for record in page.records)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert seen == [f"Record-{index:03d}-Suffix" for index in range(37)]
    assert len(seen) == page.total


def test_stale_source_is_not_served_and_rebuild_replaces_generation(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "root")
    _append(db, thread_id, "message-one", "user", "one")
    assert sidecar.build_autocomplete_catalog(db, thread_id).state == "ready"

    _append(db, thread_id, "message-two", "assistant", "two")
    stale = sidecar.query_autocomplete_records(db, thread_id, "message")
    assert stale.state == "stale"
    assert stale.records == ()

    rebuilt = sidecar.build_autocomplete_catalog(db, thread_id)
    current = sidecar.query_autocomplete_records(db, thread_id, "message", limit=10)
    assert rebuilt.state == "ready"
    assert current.state == "ready"
    assert {record.record_id for record in current.records} == {"message-one", "message-two"}

    path = sidecar.autocomplete_sidecar_path(db.path)
    conn = sqlite3.connect(path)
    try:
        generations = conn.execute(
            "SELECT COUNT(DISTINCT generation) FROM completion_records WHERE thread_id=?",
            (thread_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert generations == 1


def test_failed_build_preserves_previous_complete_generation(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "root")
    _append(db, thread_id, "stable-message", "user", "stable")
    assert sidecar.build_autocomplete_catalog(db, thread_id).state == "ready"
    original_rows = sidecar._candidate_rows

    def fail_after_materialization(projection):
        original_rows(projection)
        raise RuntimeError("injected build failure")

    monkeypatch.setattr(sidecar, "_candidate_rows", fail_after_materialization)
    failed = sidecar.build_autocomplete_catalog(db, thread_id)
    page = sidecar.query_autocomplete_records(db, thread_id, "stable")

    assert failed.state == "error"
    assert "injected build failure" in (failed.error or "")
    assert page.state == "ready"
    assert [record.record_id for record in page.records] == ["stable-message"]


def test_active_build_lease_returns_busy_without_partial_publication(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "root")
    _append(db, thread_id, "message-one", "user", "one")
    path = sidecar.autocomplete_sidecar_path(db.path)
    conn = sidecar._open_sidecar(path, create=True)
    try:
        conn.execute(
            "INSERT INTO build_leases(thread_id,owner,target_event_seq,lease_until,started_at) VALUES (?,?,?,?,?)",
            (thread_id, "other", db.max_event_seq(thread_id), sidecar.time.time() + 60, sidecar.time.time()),
        )
    finally:
        conn.close()

    result = sidecar.build_autocomplete_catalog(db, thread_id)
    page = sidecar.query_autocomplete_records(db, thread_id)

    assert result.state == "busy"
    assert page.state == "missing"
    assert page.records == ()


def test_sidecar_build_does_not_change_canonical_schema(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "root")
    _append(db, thread_id, "message-one", "user", "one")
    before = _schema(db)

    assert sidecar.build_autocomplete_catalog(db, thread_id).state == "ready"

    assert _schema(db) == before
    assert sidecar.autocomplete_sidecar_path(db.path).is_file()


def test_status_and_clear_are_inspectable_and_disposable(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "root")
    _append(db, thread_id, "message-one", "user", "one")

    missing = sidecar.autocomplete_catalog_status(db, thread_id)
    assert missing.state == "missing"
    assert missing.size_bytes == 0

    built = sidecar.build_autocomplete_catalog(db, thread_id)
    ready = sidecar.autocomplete_catalog_status(db, thread_id)
    assert built.state == "ready"
    assert ready.state == "ready"
    assert ready.through_event_seq == sidecar.autocomplete_semantic_event_seq(db, thread_id)
    assert ready.active_generation
    assert ready.size_bytes > 0

    assert sidecar.clear_autocomplete_catalog(db, thread_id) is True
    assert sidecar.autocomplete_catalog_status(db, thread_id).state == "missing"
    assert sidecar.query_autocomplete_records(db, thread_id).state == "missing"


def test_full_history_content_and_term_indexes_include_oldest_message(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "root")
    _append(db, thread_id, "oldest-message", "user", "AncientNeedleWord begins here")
    for index in range(300):
        _append(db, thread_id, f"newer-{index:03d}", "assistant", f"ordinary content {index}")

    assert sidecar.build_autocomplete_catalog(db, thread_id, batch_size=19).state == "ready"
    content = sidecar.query_autocomplete_content_records(
        db, thread_id, "ancientneedle", order="oldest", limit=10
    )
    term_state, terms = sidecar.query_autocomplete_terms(db, thread_id, "ancient", limit=10)

    assert content.state == "ready"
    assert [record.record_id for record in content.records] == ["oldest-message"]
    assert term_state == "ready"
    assert terms == ("AncientNeedleWord",)


def test_query_plans_use_sidecar_indexes(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "root")
    for index in range(20):
        _append(db, thread_id, f"Record-{index:03d}-Suffix", "assistant", f"needle body {index}")
    assert sidecar.build_autocomplete_catalog(db, thread_id).state == "ready"
    status = sidecar.autocomplete_catalog_status(db, thread_id)
    conn = sqlite3.connect(status.sidecar_path)
    try:
        generation = status.active_generation
        plans = {
            "identity": conn.execute(
                "EXPLAIN QUERY PLAN SELECT record_id FROM completion_records WHERE thread_id=? AND generation=? AND normalized_id=?",
                (thread_id, generation, "record-001-suffix"),
            ).fetchall(),
            "suffix": conn.execute(
                "EXPLAIN QUERY PLAN SELECT record_id FROM completion_records WHERE thread_id=? AND generation=? AND reversed_normalized_id>=? AND reversed_normalized_id<?",
                (thread_id, generation, "xiffus", "xiffus\U0010ffff"),
            ).fetchall(),
            "newest": conn.execute(
                "EXPLAIN QUERY PLAN SELECT record_id FROM completion_records WHERE thread_id=? AND generation=? ORDER BY event_seq DESC,item_order DESC,record_id ASC LIMIT 20",
                (thread_id, generation),
            ).fetchall(),
        }
    finally:
        conn.close()

    assert any("completion_records_identity" in row[3] for row in plans["identity"])
    assert any("completion_records_suffix" in row[3] for row in plans["suffix"])
    assert any("completion_records_newest" in row[3] for row in plans["newest"])


def test_incremental_catch_up_matches_canonical_edits_deletes_and_continue(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "root")
    _append(db, thread_id, "first", "user", "AlphaWord")
    _append(db, thread_id, "second", "assistant", "BetaWord")
    _append(db, thread_id, "third", "assistant", "GammaWord")
    assert sidecar.build_autocomplete_catalog(db, thread_id).state == "ready"

    ts.edit_message(db, thread_id, "first", "AlphaEditedWord")
    ts.delete_message(db, thread_id, "second")
    db.append_event(
        event_id="continue-event",
        thread_id=thread_id,
        type_="control.interrupt",
        payload={"purpose": "continue", "continue_from_msg_id": "first"},
    )
    _append(db, thread_id, "after", "assistant", "DeltaWord")

    result = sidecar.catch_up_autocomplete_catalog(db, thread_id, batch_size=2)
    page = sidecar.query_autocomplete_records(db, thread_id, limit=100)
    expected = ts.list_show_record_candidates(db, thread_id)

    assert result.state == "ready"
    assert page.state == "ready"
    assert [record.record_id for record in page.records] == [candidate.record_id for candidate in expected]
    assert {record.record_id for record in page.records} == {"first", "after"}
    assert sidecar.query_autocomplete_terms(db, thread_id, "alpha") == (
        "ready", ("AlphaEditedWord",)
    )
    assert sidecar.query_autocomplete_terms(db, thread_id, "beta") == ("ready", ())


def test_incremental_tool_result_repairs_both_pairing_directions(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "root")
    _append(
        db,
        thread_id,
        "declaration",
        "assistant",
        "",
        tool_calls=[{
            "id": "call-paired",
            "type": "function",
            "function": {"name": "bash", "arguments": "{}"},
        }],
    )
    assert sidecar.build_autocomplete_catalog(db, thread_id).state == "ready"
    _append(
        db,
        thread_id,
        "result",
        "tool",
        "done",
        name="bash",
        tool_call_id="call-paired",
    )

    assert sidecar.catch_up_autocomplete_catalog(db, thread_id).state == "ready"
    page = sidecar.query_autocomplete_records(db, thread_id, limit=20)
    by_id = {record.record_id: record for record in page.records}
    assert by_id["call-paired"].paired_message_ids == ("result",)
    assert by_id["result"].paired_message_ids == ("declaration",)


def test_incremental_catch_up_does_not_replay_full_canonical_projection(
    tmp_path: Path, monkeypatch
) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "root")
    _append(db, thread_id, "first", "user", "one")
    assert sidecar.build_autocomplete_catalog(db, thread_id).state == "ready"
    _append(db, thread_id, "second", "assistant", "two")

    monkeypatch.setattr(
        sidecar,
        "load_thread_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("incremental catch-up must use sidecar projection state")
        ),
    )

    assert sidecar.catch_up_autocomplete_catalog(db, thread_id).state == "ready"
    assert sidecar.query_autocomplete_records(db, thread_id, "second").state == "ready"


def test_corrupt_sidecar_is_quarantined_and_rebuilt_without_canonical_changes(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "root")
    _append(db, thread_id, "message-one", "user", "one")
    canonical_before = _schema(db)
    path = sidecar.autocomplete_sidecar_path(db.path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not sqlite")

    result = sidecar.build_autocomplete_catalog(db, thread_id)

    assert result.state == "ready"
    assert sidecar.query_autocomplete_records(db, thread_id, "message").state == "ready"
    assert _schema(db) == canonical_before
    quarantined = list(path.parent.glob(path.name + ".corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"not sqlite"


def test_transcript_pages_are_bounded_ordered_and_include_display_metadata(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "transcript")
    first = ts.append_message(db, thread_id, "user", "first")
    second = ts.append_message(
        db,
        thread_id,
        "assistant",
        "second",
        extra={"reasoning": "thinking", "tps": 12.5},
    )
    third = ts.append_message(db, thread_id, "user", "third")
    marker_seq = db.append_event(
        event_id="compaction-event",
        thread_id=thread_id,
        type_="thread.compaction",
        payload={
            "start_msg_id": second,
            "start_event_seq": db.conn.execute(
                "SELECT event_seq FROM events WHERE thread_id=? AND msg_id=? AND type='msg.create'",
                (thread_id, second),
            ).fetchone()[0],
            "selector": "last_llm",
            "created_by": "test",
        },
    )

    assert sidecar.build_autocomplete_catalog(db, thread_id, batch_size=2).state == "ready"

    newest = sidecar.query_transcript_page(db, thread_id, limit=2)
    assert newest.state == "ready"
    assert [entry.kind for entry in newest.entries] == ["message", "message"]
    assert [entry.entry_id for entry in newest.entries] == [second, third]
    assert newest.entries[0].payload["ts"]
    assert newest.entries[0].payload["event_seq"] < newest.entries[1].payload["event_seq"]
    assert newest.entries[0].token_stats["reasoning_tokens"] > 0
    assert newest.next_cursor

    older = sidecar.query_transcript_page(
        db, thread_id, limit=2, cursor=newest.next_cursor
    )
    assert older.state == "ready"
    assert [entry.kind for entry in older.entries] == ["message", "compaction_marker"]
    assert older.entries[0].entry_id == first
    assert older.entries[1].payload["marker_event_seq"] == marker_seq
    assert older.entries[1].payload["selector"] == "last_llm"
    assert older.next_cursor is None


def test_transcript_cursor_survives_boundary_message_deletion(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "cursor")
    ids = [ts.append_message(db, thread_id, "user", f"message {i}") for i in range(5)]
    assert sidecar.build_autocomplete_catalog(db, thread_id).state == "ready"
    page = sidecar.query_transcript_page(db, thread_id, limit=2)
    assert [entry.entry_id for entry in page.entries] == ids[-2:]
    assert page.next_cursor

    ts.delete_message(db, thread_id, ids[-2])
    assert sidecar.catch_up_autocomplete_catalog(db, thread_id).state == "ready"
    older = sidecar.query_transcript_page(db, thread_id, limit=10, cursor=page.next_cursor)
    assert [entry.entry_id for entry in older.entries] == ids[:3]


def test_transcript_page_does_not_materialize_snapshot_json(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "bounded")
    for index in range(200):
        _append(db, thread_id, f"message-{index}", "user", f"body {index}")
    assert sidecar.build_autocomplete_catalog(db, thread_id).state == "ready"
    monkeypatch.setattr(
        db,
        "get_thread",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("transcript page must not load snapshot_json")
        ),
    )

    page = sidecar.query_transcript_page(db, thread_id, limit=3)

    assert page.state == "ready"
    assert [entry.entry_id for entry in page.entries] == [
        "message-197",
        "message-198",
        "message-199",
    ]


def test_transcript_page_query_plan_uses_order_index(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "query plan")
    for index in range(30):
        _append(db, thread_id, f"message-{index}", "user", f"body {index}")
    assert sidecar.build_autocomplete_catalog(db, thread_id).state == "ready"
    path = sidecar.autocomplete_sidecar_path(db.path)
    conn = sqlite3.connect(path)
    try:
        generation = conn.execute(
            "SELECT active_generation FROM thread_authority WHERE thread_id=?",
            (thread_id,),
        ).fetchone()[0]
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT kind,entry_id FROM transcript_entries "
            "WHERE thread_id=? AND generation=? "
            "ORDER BY order_event_seq DESC,kind_order DESC,secondary_order DESC,kind DESC,entry_id DESC LIMIT 10",
            (thread_id, generation),
        ).fetchall()
    finally:
        conn.close()
    assert any("transcript_entries_order" in str(row) for row in plan)


def test_transcript_optimizer_metadata_catches_up_without_token_drift(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "optimizer")
    declaration = ts.append_message(
        db,
        thread_id,
        "assistant",
        "",
        extra={
            "tool_calls": [{
                "id": "call-optimizer",
                "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }]
        },
    )
    result_id = ts.append_message(
        db,
        thread_id,
        "tool",
        "large output",
        extra={"name": "bash", "tool_call_id": "call-optimizer"},
    )
    assert sidecar.build_autocomplete_catalog(db, thread_id).state == "ready"
    before = sidecar.query_transcript_page(db, thread_id, limit=10)
    before_result = next(entry for entry in before.entries if entry.entry_id == result_id)

    db.append_event(
        event_id="approval",
        thread_id=thread_id,
        type_="tool_call.output_approval",
        payload={
            "tool_call_id": "call-optimizer",
            "decision": "partial",
            "artifact_path": "/tmp/artifact123",
            "channels": {
                "raw": {"stored_in_finished_event": True},
                "optimizer": {
                    "optimized": True,
                    "fallback": False,
                    "raw_chars": 12000,
                    "published_chars": 1000,
                    "published_savings_pct": 91.67,
                },
            },
        },
    )
    assert sidecar.catch_up_autocomplete_catalog(db, thread_id).state == "ready"
    after = sidecar.query_transcript_page(db, thread_id, limit=10)
    after_result = next(entry for entry in after.entries if entry.entry_id == result_id)

    assert after_result.payload.get("output_optimizer")
    assert after_result.token_stats == before_result.token_stats
    assert next(entry for entry in after.entries if entry.entry_id == declaration)


def test_compaction_marker_incrementally_joins_its_start_message(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, "marker tail")
    first = ts.append_message(db, thread_id, "user", "first")
    second = ts.append_message(db, thread_id, "assistant", "second")
    assert sidecar.build_autocomplete_catalog(db, thread_id).state == "ready"
    start_seq = db.conn.execute(
        "SELECT event_seq FROM events WHERE thread_id=? AND msg_id=?",
        (thread_id, second),
    ).fetchone()[0]
    marker_seq = db.append_event(
        event_id="marker-tail",
        thread_id=thread_id,
        type_="thread.compaction",
        payload={"start_msg_id": second, "start_event_seq": start_seq},
    )

    assert sidecar.catch_up_autocomplete_catalog(db, thread_id).state == "ready"
    page = sidecar.query_transcript_page(db, thread_id, limit=10)

    assert [entry.entry_id for entry in page.entries] == [
        first,
        f"compaction-{marker_seq}",
        second,
    ]


def test_transcript_cursor_is_thread_bound(tmp_path: Path) -> None:
    db = _db(tmp_path)
    first_thread = ts.create_root_thread(db, "first")
    second_thread = ts.create_root_thread(db, "second")
    for thread_id in (first_thread, second_thread):
        for index in range(3):
            _append(db, thread_id, f"{thread_id}-{index}", "user", str(index))
        assert sidecar.build_autocomplete_catalog(db, thread_id).state == "ready"
    cursor = sidecar.query_transcript_page(db, first_thread, limit=1).next_cursor

    page = sidecar.query_transcript_page(db, second_thread, limit=1, cursor=cursor)

    assert page.state == "error"
    assert "invalid transcript cursor" in (page.error or "")
