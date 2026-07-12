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


class _CapturingLLM:
    current_model_key = "projection-test-model"

    def __init__(self) -> None:
        self.seen_messages: list[list[dict]] = []

    async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None, **kwargs):
        self.seen_messages.append([dict(message) for message in messages])
        yield {"type": "done", "message": {"role": "assistant", "content": "done"}}


def _provider_signature(messages):
    return [
        (
            message.get("role"),
            message.get("content"),
            message.get("msg_id"),
            message.get("event_seq"),
            message.get("reasoning_content"),
            message.get("thought_signature"),
            message.get("tool_call_id"),
        )
        for message in messages
    ]


def _run_ra1_and_capture(db: ts.ThreadsDB, thread_id: str) -> list[dict]:
    import asyncio

    llm = _CapturingLLM()
    runner = ts.ThreadRunner(db, thread_id, llm=llm)
    runner._model_thinking_options = lambda _model: {
        "thinking_content_policy": "send all encrypted gemini",
        "thinking_content_key": "reasoning_content",
    }
    assert asyncio.run(runner.run_once()) is True
    assert len(llm.seen_messages) == 1
    return llm.seen_messages[0]


def test_ra1_provider_context_matches_with_no_stale_and_current_snapshot(tmp_path) -> None:
    """Snapshots change only the replay start, never RA1 provider semantics."""

    def build_case(name: str, snapshot_mode: str) -> list[dict]:
        db = _make_db(tmp_path, f"{name}.sqlite")
        thread_id = ts.create_root_thread(db, name=name)
        system_id = ts.append_message(db, thread_id, "system", "standing rules")
        old_user_id = ts.append_message(db, thread_id, "user", "old question")
        assistant_id = ts.append_message(
            db,
            thread_id,
            "assistant",
            "old answer",
            extra={
                "reasoning_content": {"opaque": "thought"},
                "thought_signature": "provider-signature",
                "api_usage": {"input_tokens": 4},
                "provider_usage": {"prompt_tokens": 4},
            },
        )
        if snapshot_mode == "stale":
            ts.create_snapshot(db, thread_id)
        ts.edit_message(db, thread_id, assistant_id, "edited answer")
        deleted_id = ts.append_message(db, thread_id, "assistant", "deleted answer")
        ts.delete_message(db, thread_id, deleted_id)
        ts.append_message(db, thread_id, "user", "queued one")
        trigger_id = ts.append_message(db, thread_id, "user", "queued two")
        if snapshot_mode == "current":
            ts.create_snapshot(db, thread_id)
        messages = _run_ra1_and_capture(db, thread_id)
        assert [message.get("msg_id") for message in messages] == [
            system_id,
            old_user_id,
            assistant_id,
            next(
                message.msg_id
                for message in ts.load_thread_projection(
                    db, thread_id, db.max_event_seq(thread_id), use_snapshot=False
                ).messages
                if message.payload.get("content") == "queued one"
            ),
            trigger_id,
        ]
        assert all("api_usage" not in message for message in messages)
        assert all("provider_usage" not in message for message in messages)
        return messages

    without_snapshot = build_case("no-snapshot", "none")
    stale_snapshot = build_case("stale-snapshot", "stale")
    current_snapshot = build_case("current-snapshot", "current")

    expected_content = [
        "standing rules",
        "old question",
        "edited answer",
        "queued one",
        "queued two",
    ]
    assert [message.get("content") for message in without_snapshot] == expected_content
    assert [message.get("content") for message in stale_snapshot] == expected_content
    assert [message.get("content") for message in current_snapshot] == expected_content
    # IDs/event sequences are thread-local; all provider-bearing values match.
    assert [item[:2] + item[4:] for item in _provider_signature(without_snapshot)] == [
        item[:2] + item[4:] for item in _provider_signature(stale_snapshot)
    ] == [item[:2] + item[4:] for item in _provider_signature(current_snapshot)]


def test_ra1_provider_context_stays_at_captured_projection_watermark(tmp_path, monkeypatch) -> None:
    db = _make_db(tmp_path, "ra1-watermark.sqlite")
    thread_id = ts.create_root_thread(db, name="ra1-watermark")
    trigger = ts.append_message(db, thread_id, "user", "captured trigger")
    runner = ts.ThreadRunner(db, thread_id, llm=_CapturingLLM())
    projection = ts.load_thread_projection(db, thread_id, db.max_event_seq(thread_id))

    def load_then_append():
        ts.append_message(db, thread_id, "user", "after capture")
        return projection

    monkeypatch.setattr(runner, "_load_ra1_provider_projection", load_then_append)
    assert __import__("asyncio").run(runner.run_once()) is True

    messages = runner.llm.seen_messages[0]
    assert [message.get("msg_id") for message in messages] == [trigger]
    assert [message.get("content") for message in messages] == ["captured trigger"]


def test_ra1_projection_preserves_compaction_context_only_and_tool_protocol(tmp_path) -> None:
    db = _make_db(tmp_path, "ra1-compaction.sqlite")
    thread_id = ts.create_root_thread(db, name="ra1-compaction")
    system_id = ts.append_message(db, thread_id, "system", "standing rules")
    old_id = ts.append_message(db, thread_id, "user", "old context")
    summary_id = ts.append_message(db, thread_id, "assistant", "compact summary")
    ts.commit_thread_compaction(db, thread_id, summary_id, created_by="test")
    hidden_id = ts.append_message(
        db, thread_id, "user", "local only", extra={"no_api": True}
    )
    context_only_id = ts.append_message(
        db,
        thread_id,
        "user",
        "manager context",
        extra={"keep_user_turn": True},
    )
    assistant_tool_id = ts.append_message(
        db,
        thread_id,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": "call-projection",
                    "type": "function",
                    "function": {"name": "bash", "arguments": "{}"},
                }
            ]
        },
    )
    tool_id = ts.append_message(
        db,
        thread_id,
        "tool",
        "tool result",
        extra={"tool_call_id": "call-projection"},
    )
    trigger_id = ts.append_message(db, thread_id, "user", "next question")

    messages = _run_ra1_and_capture(db, thread_id)

    ids = [message.get("msg_id") for message in messages]
    assert ids == [system_id, summary_id, context_only_id, assistant_tool_id, tool_id, trigger_id]
    assert old_id not in ids
    assert hidden_id not in ids
    assistant_index = ids.index(assistant_tool_id)
    assert messages[assistant_index]["tool_calls"][0]["id"] == "call-projection"
    assert messages[assistant_index + 1]["role"] == "tool"
    assert messages[assistant_index + 1]["tool_call_id"] == "call-projection"



def _event_types(db: ts.ThreadsDB, thread_id: str) -> list[str]:
    return [
        str(row[0])
        for row in db.conn.execute(
            "SELECT type FROM events WHERE thread_id=? ORDER BY event_seq ASC",
            (thread_id,),
        ).fetchall()
    ]


def _projected_payloads(db: ts.ThreadsDB, thread_id: str) -> list[dict]:
    projection = ts.load_thread_projection(db, thread_id, db.max_event_seq(thread_id))
    return [dict(message.payload) for message in projection.messages]


def test_duplicate_thread_emits_canonical_messages_without_stale_lifecycle(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path, "duplicate.sqlite")
    source = ts.create_root_thread(db, name="source")
    first = ts.append_message(
        db,
        source,
        "assistant",
        "before edit",
        extra={"provider_specific": {"signature": "opaque"}},
    )
    ts.edit_message(db, source, first, "after edit", extra={"edited_field": 7})
    deleted = ts.append_message(db, source, "assistant", "delete me")
    ts.delete_message(db, source, deleted)
    continue_from = ts.append_message(db, source, "user", "continue here")
    skipped = ts.append_message(db, source, "assistant", "skip me")
    db.append_event(
        "duplicate-continue",
        source,
        "control.interrupt",
        {"purpose": "continue", "continue_from_msg_id": continue_from},
    )
    pending = ts.append_message(
        db,
        source,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": "call-pending",
                    "type": "function",
                    "function": {"name": "bash", "arguments": "{}"},
                }
            ]
        },
    )
    db.append_event(
        "pending-started",
        source,
        "tool_call.execution_started",
        {"tool_call_id": "call-pending"},
        invoke_id="old-invocation",
    )
    db.append_event(
        "stale-open",
        source,
        "stream.open",
        {"stream_kind": "llm"},
        msg_id="stale-stream-message",
        invoke_id="old-invocation",
    )
    ts.set_thread_working_directory(db, source, str(tmp_path / "work"))
    ts.set_thread_sandbox_config(
        db,
        source,
        enabled=True,
        provider="srt",
        settings={"provider": "srt", "filesystem": {"denyRead": []}},
        reason="test",
    )
    ts.set_thread_model(
        db,
        source,
        "copy-model",
        concrete_model_info={
            "providers": {
                "test": {
                    "models": {"copy-model": {"model_kind": "chat"}}
                }
            }
        },
    )
    ts.set_thread_tools_enabled(db, source, False)
    watermark = db.max_event_seq(source)
    expected = [dict(message.payload) for message in ts.load_thread_projection(db, source, watermark).messages]

    duplicate = ts.duplicate_thread(db, source, name="copy")

    assert _projected_payloads(db, duplicate) == expected
    assert [payload["content"] for payload in expected if payload.get("content")] == [
        "after edit",
        "continue here",
    ]
    assert skipped not in [message.msg_id for message in ts.load_thread_projection(db, source, watermark).messages]
    assert pending in [message.msg_id for message in ts.load_thread_projection(db, duplicate, db.max_event_seq(duplicate)).messages]
    types = _event_types(db, duplicate)
    assert "stream.open" not in types
    assert "stream.close" not in types
    assert "control.interrupt" not in types
    assert not any(event_type.startswith("tool_call.") for event_type in types)
    assert ts.build_tool_call_states(db, duplicate)["call-pending"].state == "TC1"
    assert ts.current_thread_model(db, duplicate) == "copy-model"
    assert ts.get_thread_working_directory(db, duplicate) == (tmp_path / "work").resolve()
    assert ts.get_thread_sandbox_config(db, duplicate).enabled is True
    assert ts.get_thread_tools_config(db, duplicate).llm_tools_enabled is False


def test_duplicate_completed_history_gets_clean_idle_boundary(tmp_path) -> None:
    db = _make_db(tmp_path, "duplicate-idle.sqlite")
    source = ts.create_root_thread(db, name="source")
    ts.append_message(db, source, "user", "question")
    ts.append_message(db, source, "assistant", "answer")

    duplicate = ts.duplicate_thread(db, source)

    assert _event_types(db, duplicate).count("stream.close") == 1
    assert ts.discover_runner_actionable(db, duplicate) is None


def test_duplicate_child_freezes_inherited_effective_configuration(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path, "duplicate-inherited-config.sqlite")
    parent = ts.create_root_thread(db, name="parent")
    ts.set_thread_working_directory(db, parent, str(tmp_path / "parent-work"))
    ts.set_thread_sandbox_config(
        db,
        parent,
        enabled=True,
        provider="srt",
        settings={"provider": "srt", "filesystem": {"allowWrite": ["out"]}},
        reason="test",
    )
    ts.set_thread_model(
        db,
        parent,
        "parent-model",
        concrete_model_info={
            "providers": {
                "test": {
                    "models": {"parent-model": {"model_kind": "chat"}}
                }
            }
        },
    )
    ts.set_thread_tools_enabled(db, parent, False)
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "user", "child message")

    duplicate = ts.duplicate_thread(db, child)

    assert ts.get_parent(db, duplicate) is None
    assert ts.get_thread_working_directory(db, duplicate) == (tmp_path / "parent-work").resolve()
    assert ts.get_thread_sandbox_config(db, duplicate).enabled is True
    assert ts.current_thread_model(db, duplicate) == "parent-model"
    assert ts.get_thread_tools_config(db, duplicate).llm_tools_enabled is False


def test_duplicate_thread_up_to_uses_target_message_watermark_for_state_and_config(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path, "duplicate-up-to.sqlite")
    source = ts.create_root_thread(db, name="source")
    first = ts.append_message(db, source, "user", "before")
    target = ts.append_message(db, source, "assistant", "target")
    ts.edit_message(db, source, first, "edited before target")
    ts.set_thread_model(
        db,
        source,
        "before-model",
        concrete_model_info={
            "providers": {
                "test": {
                    "models": {"before-model": {"model_kind": "chat"}}
                }
            }
        },
    )
    target_two = ts.append_message(db, source, "user", "target two")
    ts.edit_message(db, source, first, "edit after target two")
    ts.delete_message(db, source, target)
    ts.set_thread_model(
        db,
        source,
        "after-model",
        concrete_model_info={
            "providers": {
                "test": {
                    "models": {"after-model": {"model_kind": "chat"}}
                }
            }
        },
    )
    ts.set_thread_tools_enabled(db, source, False)

    duplicate = ts.duplicate_thread_up_to(db, source, target_two, name="checkpoint")

    assert [payload.get("content") for payload in _projected_payloads(db, duplicate)] == [
        "edited before target",
        "target",
        "target two",
    ]
    assert ts.current_thread_model(db, duplicate) == "before-model"
    # The post-target policy event is outside the selected watermark.
    assert ts.get_thread_tools_config(db, duplicate).llm_tools_enabled is True
    assert "msg.edit" not in _event_types(db, duplicate)
    assert "msg.delete" not in _event_types(db, duplicate)


def test_duplicate_thread_rebases_effective_compaction_boundary(tmp_path) -> None:
    db = _make_db(tmp_path, "duplicate-compaction.sqlite")
    source = ts.create_root_thread(db, name="source")
    ts.append_message(db, source, "user", "old")
    start = ts.append_message(db, source, "assistant", "summary")
    ts.commit_thread_compaction(db, source, start, created_by="test")
    ts.append_message(db, source, "user", "current")

    duplicate = ts.duplicate_thread(db, source)
    compaction = ts.latest_effective_thread_compaction(db, duplicate)
    duplicate_start_seq = db.conn.execute(
        "SELECT event_seq FROM events WHERE thread_id=? AND type='msg.create' "
        "AND msg_id=?",
        (duplicate, start),
    ).fetchone()[0]
    source_compaction = ts.latest_effective_thread_compaction(db, source)

    assert compaction is not None
    assert source_compaction is not None
    assert compaction["start_msg_id"] == start
    assert compaction["start_event_seq"] == duplicate_start_seq
    assert compaction["start_event_seq"] != source_compaction["start_event_seq"]
    assert [
        message.get("content")
        for message in ts.filter_messages_for_compaction_provider_context(
            db,
            duplicate,
            ts.load_thread_projection(
                db, duplicate, db.max_event_seq(duplicate)
            ).message_dicts(),
        )
    ] == ["summary", "current"]


def test_duplicate_thread_up_to_ignores_compaction_after_watermark(tmp_path) -> None:
    db = _make_db(tmp_path, "duplicate-before-compaction.sqlite")
    source = ts.create_root_thread(db, name="source")
    target = ts.append_message(db, source, "user", "checkpoint")
    start = ts.append_message(db, source, "assistant", "later summary")
    ts.commit_thread_compaction(db, source, start, created_by="test")

    duplicate = ts.duplicate_thread_up_to(db, source, target)

    assert ts.latest_effective_thread_compaction(db, duplicate) is None
    assert _event_types(db, duplicate).count("thread.compaction") == 0
