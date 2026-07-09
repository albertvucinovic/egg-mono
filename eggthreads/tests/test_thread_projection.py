from __future__ import annotations

import json
import threading
from pathlib import Path

import eggthreads as ts
import eggthreads.api as api_module


def _make_db(tmp_path: Path, name: str = "threads.sqlite") -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / name)
    db.init_schema()
    return db


def _message_signature(projection: ts.ThreadProjection):
    return [
        (
            message.msg_id,
            dict(message.payload),
            message.created_event_seq,
            message.created_event_id,
            message.created_at,
            message.last_event_seq,
            message.last_event_id,
            message.updated_at,
            message.deleted,
            message.skipped_on_continue,
        )
        for message in projection.message_states
    ]


def test_projection_without_snapshot_is_bounded_and_preserves_provider_payload_metadata(tmp_path) -> None:
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="projection")
    first = ts.append_message(
        db,
        thread_id,
        "assistant",
        "first",
        extra={
            "reasoning": "private thought",
            "reasoning_content": "provider thought",
            "provider_specific": {"thought_signature": "sig-1"},
            "api_usage": {"input_tokens": 3},
        },
    )
    watermark = db.max_event_seq(thread_id)
    second = ts.append_message(db, thread_id, "user", "after target")

    projection = ts.load_thread_projection(db, thread_id, watermark)

    assert projection.started_from_snapshot_event_seq == -1
    assert projection.through_event_seq == watermark
    assert [message.msg_id for message in projection.messages] == [first]
    message = projection.messages[0]
    assert message.payload["reasoning_content"] == "provider thought"
    assert message.payload["provider_specific"] == {"thought_signature": "sig-1"}
    assert message.payload["api_usage"] == {"input_tokens": 3}
    assert message.created_event_seq == watermark
    assert message.last_event_seq == watermark
    assert message.created_event_id is not None
    assert message.created_at is not None
    assert second not in [item.msg_id for item in projection.messages]


def test_snapshot_seed_plus_tail_matches_full_replay_for_edit_delete_continue_and_provider_fields(tmp_path) -> None:
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="projection-tail")
    keep = ts.append_message(
        db,
        thread_id,
        "assistant",
        "before",
        extra={"provider_specific": {"signature": "abc"}, "reasoning": "thought"},
    )
    preserved = ts.append_message(
        db,
        thread_id,
        "system",
        "preserve",
        extra={"preserve_on_continue": True, "provider_blob": {"opaque": [1, 2]}},
    )
    ts.create_snapshot(db, thread_id)
    snapshot_seq = db.get_thread(thread_id).snapshot_last_event_seq

    ts.edit_message(db, thread_id, keep, "after", extra={"edit_meta": {"source": "user"}})
    deleted = ts.append_message(db, thread_id, "assistant", "delete me", extra={"vendor": {"id": 7}})
    ts.delete_message(db, thread_id, deleted)
    skipped = ts.append_message(db, thread_id, "assistant", "skip me", extra={"vendor": {"id": 8}})
    db.append_event(
        "continue-projection",
        thread_id,
        "control.interrupt",
        {
            "reason": "continue_thread",
            "purpose": "continue",
            "continue_from_msg_id": keep,
        },
    )
    target = db.max_event_seq(thread_id)

    accelerated = ts.load_thread_projection(db, thread_id, target)
    full = ts.load_thread_projection(db, thread_id, target, use_snapshot=False)

    assert accelerated.started_from_snapshot_event_seq == snapshot_seq
    assert _message_signature(accelerated) == _message_signature(full)
    assert [message.msg_id for message in accelerated.messages] == [keep, preserved]
    keep_view = accelerated.messages[0]
    assert keep_view.payload["content"] == "after"
    assert keep_view.payload["provider_specific"] == {"signature": "abc"}
    assert keep_view.payload["reasoning"] == "thought"
    assert keep_view.payload["edit_meta"] == {"source": "user"}
    assert keep_view.last_event_seq > keep_view.created_event_seq
    assert next(message for message in accelerated.message_states if message.msg_id == deleted).deleted is True
    assert next(message for message in accelerated.message_states if message.msg_id == skipped).skipped_on_continue is True
    assert accelerated.messages[1].payload["provider_blob"] == {"opaque": [1, 2]}


def test_invalid_or_stale_legacy_snapshot_is_optional_and_full_replay_repairs_it(tmp_path) -> None:
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="legacy-snapshot")
    first = ts.append_message(db, thread_id, "user", "one")
    first_seq = db.max_event_seq(thread_id)
    db.conn.execute(
        "UPDATE threads SET snapshot_json=?, snapshot_last_event_seq=? WHERE thread_id=?",
        (json.dumps({"messages": [{"role": "user", "content": "WRONG", "msg_id": first}]}), first_seq, thread_id),
    )
    second = ts.append_message(db, thread_id, "assistant", "two", extra={"provider_field": "kept"})
    target = db.max_event_seq(thread_id)

    projection = ts.load_thread_projection(db, thread_id, target)

    assert projection.started_from_snapshot_event_seq == -1
    assert [message.msg_id for message in projection.messages] == [first, second]
    assert projection.messages[0].payload["content"] == "one"
    assert projection.messages[1].payload["provider_field"] == "kept"

    repaired = ts.create_snapshot(db, thread_id)
    row = db.get_thread(thread_id)
    assert row.snapshot_last_event_seq == target
    assert repaired["messages"] == projection.message_dicts()
    assert "_thread_projection" in repaired
    seeded = ts.load_thread_projection(db, thread_id, target)
    assert seeded.started_from_snapshot_event_seq == target
    assert _message_signature(seeded) == _message_signature(projection)



def test_projection_rejects_watermark_beyond_thread_history(tmp_path) -> None:
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="projection-future")
    ts.append_message(db, thread_id, "user", "one")

    try:
        ts.load_thread_projection(db, thread_id, db.max_event_seq(thread_id) + 1)
    except ts.ThreadProjectionError as exc:
        assert "exceeds" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("future watermark should fail")

def test_snapshot_publication_cas_does_not_regress_newer_two_connection_writer(tmp_path, monkeypatch) -> None:
    db_old = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db_old, name="snapshot-race")
    first = ts.append_message(db_old, thread_id, "user", "first")
    db_new = ts.ThreadsDB(db_old.path)
    old_built = threading.Event()
    allow_old_publish = threading.Event()
    original = api_module._snapshot_from_projection

    initial_seq = db_old.max_event_seq(thread_id)

    def barrier_snapshot(projection):
        snapshot = original(projection)
        if projection.through_event_seq == initial_seq:
            old_built.set()
            assert allow_old_publish.wait(timeout=5)
        return snapshot

    monkeypatch.setattr(api_module, "_snapshot_from_projection", barrier_snapshot)
    old_result = []
    old_error = []

    def old_builder() -> None:
        local = ts.ThreadsDB(db_old.path)
        try:
            old_result.append(ts.create_snapshot(local, thread_id))
        except Exception as exc:  # pragma: no cover - asserted below
            old_error.append(exc)
        finally:
            local.conn.close()

    thread = threading.Thread(target=old_builder)
    thread.start()
    assert old_built.wait(timeout=5)
    second = ts.append_message(db_new, thread_id, "assistant", "second")
    newer = ts.create_snapshot(db_new, thread_id)
    newer_seq = db_new.get_thread(thread_id).snapshot_last_event_seq
    allow_old_publish.set()
    thread.join(timeout=5)

    assert old_error == []
    assert len(old_result) == 1
    assert old_result[0] == newer
    row = db_new.get_thread(thread_id)
    assert row.snapshot_last_event_seq == newer_seq == db_new.max_event_seq(thread_id)
    persisted = json.loads(row.snapshot_json)
    assert persisted == newer
    assert [message["msg_id"] for message in persisted["messages"]] == [first, second]
