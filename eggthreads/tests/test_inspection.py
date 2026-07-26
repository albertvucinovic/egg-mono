from __future__ import annotations

from pathlib import Path

import eggthreads as ts
from eggthreads.inspection import SHOW_AMBIGUOUS_LIMIT
from eggthreads.command_catalog import CommandContext, create_default_command_registry, render_command_registry_help


def _db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _append_with_id(db: ts.ThreadsDB, thread_id: str, msg_id: str, role: str, content: str, **extra) -> None:
    db.append_event(
        event_id=f"event-{msg_id}",
        thread_id=thread_id,
        type_="msg.create",
        msg_id=msg_id,
        payload={"role": role, "content": content, **extra},
    )


def test_show_resolves_exact_then_unique_case_insensitive_prefix_or_suffix(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, name="show")
    _append_with_id(db, thread_id, "MsgAlphaTAIL", "assistant", "alpha")
    _append_with_id(db, thread_id, "MsgBetaTAIL", "user", "beta")
    _append_with_id(db, thread_id, "MsgGammaUNIQUE", "assistant", "gamma")

    exact = ts.resolve_show_record(db, thread_id, "MsgAlphaTAIL")
    assert exact.status == "selected"
    assert exact.selected is not None and exact.selected.record_id == "MsgAlphaTAIL"

    prefix = ts.resolve_show_record(db, thread_id, "MsgGam")
    suffix = ts.resolve_show_record(db, thread_id, "UNIQUE")
    assert prefix.selected is not None and prefix.selected.record_id == "MsgGammaUNIQUE"
    assert suffix.selected is not None and suffix.selected.record_id == "MsgGammaUNIQUE"

    assert ts.resolve_show_record(db, thread_id, "unique").selected.record_id == "MsgGammaUNIQUE"


def test_show_does_not_publish_projection_internal_ids_for_identityless_messages(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, name="identityless")
    event_seq = db.append_event(
        event_id="identityless-message",
        thread_id=thread_id,
        type_="msg.create",
        payload={"role": "assistant", "content": "legacy without public ID"},
    )

    assert ts.list_show_record_candidates(db, thread_id) == []
    assert ts.resolve_show_record(db, thread_id, f"event:{event_seq}").status == "missing"


def test_show_exact_identity_wins_even_when_it_is_another_id_prefix(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, name="exact wins")
    _append_with_id(db, thread_id, "ExactId", "assistant", "exact")
    _append_with_id(db, thread_id, "ExactIdLonger", "assistant", "prefix collision")

    resolution = ts.resolve_show_record(db, thread_id, "exactid")

    assert resolution.status == "selected"
    assert resolution.selected is not None
    assert resolution.selected.record_id == "ExactId"


def test_show_ambiguity_is_bounded_and_does_not_select(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, name="ambiguous")
    for index in range(14):
        _append_with_id(db, thread_id, f"record-{index:02d}-same", "assistant", f"message {index}")

    resolution = ts.resolve_show_record(db, thread_id, "same")

    assert resolution.status == "ambiguous"
    assert resolution.selected is None
    assert resolution.total_matches == 14
    assert len(resolution.candidates) == SHOW_AMBIGUOUS_LIMIT
    assert resolution.candidates[0].record_id == "record-13-same"
    assert resolution.candidates[-1].record_id == "record-04-same"
    assert "… and 4 more" in resolution.message


def test_show_candidates_cover_notes_assistant_declarations_and_exact_tool_results(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, name="kinds")
    _append_with_id(
        db,
        thread_id,
        "note-message",
        "assistant",
        "status note",
        answer_user_preserve_turn=True,
    )
    _append_with_id(
        db,
        thread_id,
        "assistant-message",
        "assistant",
        "",
        tool_calls=[{
            "id": "CallExactCase",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"script":"echo hi"}'},
        }],
    )
    _append_with_id(
        db,
        thread_id,
        "tool-message",
        "tool",
        "tool output",
        name="bash",
        tool_call_id="CallExactCase",
    )

    candidates = ts.list_show_record_candidates(db, thread_id)
    by_id_kind = {(candidate.record_id, candidate.kind): candidate for candidate in candidates}
    assert ("note-message", "assistant_note") in by_id_kind
    declaration = by_id_kind[("CallExactCase", "tool_declaration")]
    result = by_id_kind[("tool-message", "tool_result")]
    assert declaration.paired_message_ids == ("tool-message",)
    assert result.paired_message_ids == ("assistant-message",)
    assert ts.resolve_show_record(db, thread_id, "callexactcase").selected.record_id == "CallExactCase"


def test_show_does_not_leak_deleted_skipped_sibling_or_descendant_records(tmp_path: Path) -> None:
    db = _db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    sibling = ts.create_root_thread(db, name="sibling")
    _append_with_id(db, child, "child-secret", "assistant", "descendant")
    _append_with_id(db, sibling, "sibling-secret", "assistant", "sibling")
    _append_with_id(db, root, "deleted-secret", "assistant", "deleted")
    ts.delete_message(db, root, "deleted-secret")
    _append_with_id(db, root, "skipped-secret", "assistant", "skipped")
    db.append_event(
        event_id="skip-event",
        thread_id=root,
        type_="msg.edit",
        msg_id="skipped-secret",
        payload={"skipped_on_continue": True},
    )

    for hint in ("child-secret", "sibling-secret", "deleted-secret", "skipped-secret"):
        result = ts.resolve_show_record(db, root, hint)
        assert result.status == "missing"
        assert result.message == f"No inspectable record matched {hint!r} in the current thread."

    # Explicitly selecting the existing child view uses the same resolver and is allowed.
    assert ts.resolve_show_record(db, child, "child-secret").status == "selected"


def test_show_command_and_completion_share_authoritative_result_shape(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, name="command")
    _append_with_id(db, thread_id, "message-for-show-12345678", "assistant", "full body", reasoning="reason")
    assert ts.build_autocomplete_catalog(db, thread_id).state == "ready"
    registry = create_default_command_registry()
    context = CommandContext(db=db, current_thread=thread_id)

    completion = registry.complete("show", context, "MESSAGE-FOR-SHOW-12345678")
    assert completion == [{
        "display": "[12345678] Assistant · full body",
        "insert": "message-for-show-12345678",
        "replace": 25,
        "meta": "message · message-for-show-12345678",
    }]

    result = registry.execute("show", context, "12345678")
    assert result.clear_input is True
    assert result.data is not None and result.data["action"] == "show_record"
    target = result.data["target"]
    assert target["record_id"] == "message-for-show-12345678"
    assert target["message"]["content"] == "full body"
    assert target["message"]["reasoning"] == "reason"
    assert target["message"]["tokens"] is not None
    assert "/show <id_hint>" in render_command_registry_help(registry)


def test_show_uses_projection_optimizer_metadata_without_rescanning_approval_events(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, name="projection metadata")
    tool_call_id = "call-projection-metadata"
    _append_with_id(
        db,
        thread_id,
        "tool-before-approval",
        "tool",
        "bounded preview",
        name="bash",
        tool_call_id=tool_call_id,
    )
    db.append_event(
        event_id="projection-output-approval",
        thread_id=thread_id,
        type_="tool_call.output_approval",
        payload={
            "tool_call_id": tool_call_id,
            "artifact_path": "/tmp/.egg/egg_outputs/thread/projection123",
            "channels": {
                "raw": {"stored_in_finished_event": True},
                "artifact": "/tmp/.egg/egg_outputs/thread/projection123",
                "optimizer": {"optimized": True, "published": True},
            },
        },
    )

    resolution = ts.resolve_show_record(db, thread_id, "tool-before-approval")

    assert resolution.selected is not None
    assert resolution.selected.message["output_optimizer"]["artifact_id"] == "projection123"


def test_show_target_keeps_bounded_raw_output_recovery_affordance(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, name="artifact")
    tool_call_id = "call-artifact-show"
    _append_with_id(
        db,
        thread_id,
        "tool-artifact-message",
        "tool",
        "optimized bounded preview",
        name="bash",
        tool_call_id=tool_call_id,
    )
    db.append_event(
        event_id="artifact-output-approval",
        thread_id=thread_id,
        type_="tool_call.output_approval",
        payload={
            "tool_call_id": tool_call_id,
            "artifact_path": "/tmp/.egg/egg_outputs/thread/rawabc123",
            "channels": {
                "raw": {"stored_in_finished_event": True},
                "artifact": "/tmp/.egg/egg_outputs/thread/rawabc123",
                "optimizer": {
                    "optimized": True,
                    "fallback": False,
                    "published": True,
                    "savings_pct": 90.0,
                },
            },
        },
    )

    resolution = ts.resolve_show_record(db, thread_id, "tool-artifact-message")
    assert resolution.selected is not None
    target = ts.show_record_target(
        resolution.selected,
        watermark_event_seq=resolution.watermark_event_seq,
    )
    assert target["message"]["content"] == "optimized bounded preview"
    assert target["message"]["output_optimizer"]["artifact_id"] == "rawabc123"
    assert target["message"]["output_optimizer"]["raw_hint"] == (
        "read_long_tool_output('rawabc123', chunk_number=1)"
    )


def test_shortest_unique_record_id_suffix_is_show_compatible_and_case_insensitive():
    from eggthreads.inspection import shortest_unique_record_id_suffix

    ids = ("call_alpha_ABCDEF0012345678", "call_beta_ABCDEF0098765678")
    assert shortest_unique_record_id_suffix(ids[0], ids) == "12345678"
    assert shortest_unique_record_id_suffix(ids[1], ids) == "98765678"


def test_shortest_unique_record_id_suffix_expands_on_collision():
    from eggthreads.inspection import shortest_unique_record_id_suffix

    ids = ("call_alpha_A12345678", "call_beta_B12345678")
    assert shortest_unique_record_id_suffix(ids[0], ids) == "A12345678"
    assert shortest_unique_record_id_suffix(ids[1], ids) == "B12345678"


def test_shortest_unique_record_id_suffix_includes_target_when_catalog_is_empty():
    from eggthreads.inspection import shortest_unique_record_id_suffix

    assert shortest_unique_record_id_suffix("call_target_12345678", ()) == "12345678"


def test_compact_record_id_suffixes_matches_individual_resolution():
    from eggthreads.inspection import (
        compact_record_id_suffixes,
        shortest_unique_record_id_suffix,
    )

    ids = (
        "call_alpha_A12345678",
        "call_beta_B12345678",
        "01KYEXACT12345678",
        "different",
    )
    hints = compact_record_id_suffixes(ids)

    assert {
        record_id: hints[record_id.casefold()]
        for record_id in ids
    } == {
        record_id: shortest_unique_record_id_suffix(record_id, ids)
        for record_id in ids
    }
