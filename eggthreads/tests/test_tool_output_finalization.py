from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import eggthreads as ts
from eggthreads.runner import _finalize_auto_tool_output


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _make_tc4(db: ts.ThreadsDB, *, thread_id: str = "thread-output-finalize", tool_call_id: str = "tc-finalize"):
    db.create_thread(thread_id=thread_id, name="output")
    db.append_event(
        event_id=f"parent-{tool_call_id}",
        thread_id=thread_id,
        type_="msg.create",
        msg_id=f"parent-msg-{tool_call_id}",
        payload={
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {"name": "bash", "arguments": "{}"},
                }
            ],
        },
    )
    db.append_event(
        event_id=f"approval-{tool_call_id}",
        thread_id=thread_id,
        type_="tool_call.approval",
        payload={"tool_call_id": tool_call_id, "decision": "granted"},
    )
    db.append_event(
        event_id=f"started-{tool_call_id}",
        thread_id=thread_id,
        type_="tool_call.execution_started",
        invoke_id=f"invoke-{tool_call_id}",
        payload={"tool_call_id": tool_call_id},
    )
    finish_seq = db.append_event(
        event_id=f"finished-{tool_call_id}",
        thread_id=thread_id,
        type_="tool_call.finished",
        invoke_id=f"invoke-{tool_call_id}",
        payload={"tool_call_id": tool_call_id, "reason": "success", "output": "raw output"},
    )
    tc = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert tc.state == "TC4"
    assert tc.state_event_seq == finish_seq
    return thread_id, tool_call_id, tc


def _decision_rows(db: ts.ThreadsDB, thread_id: str):
    rows = db.conn.execute(
        "SELECT event_seq, payload_json FROM events WHERE thread_id=? AND type='tool_call.output_approval' ORDER BY event_seq",
        (thread_id,),
    ).fetchall()
    return [(int(row[0]), json.loads(row[1])) for row in rows]


def test_user_cancel_wins_automatic_policy_race_in_either_order(tmp_path) -> None:
    db = _make_db(tmp_path)
    thread_id, tool_call_id, tc = _make_tc4(db)

    auto = ts.finalize_tool_output(
        db,
        thread_id,
        tool_call_id,
        decision="whole",
        source="automatic_policy",
        reason="Auto: whole",
        expected_event_seq=tc.state_event_seq,
    )
    cancel = ts.finalize_tool_output(
        db,
        thread_id,
        tool_call_id,
        decision="omit",
        source="user_omit",
        reason="Explicitly omitted by user",
        expected_event_seq=auto.state_event_seq,
    )

    assert auto.committed is True
    assert cancel.committed is True
    state = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert state.state == "TC5"
    assert state.output_decision == "omit"
    assert state.last_output_approval_payload["decision_source"] == "user_omit"
    assert state.last_output_approval_payload["supersedes_event_seq"] == auto.event_seq

    # Reversing arrival order produces one event: automatic policy observes the
    # already-authoritative cancellation and returns it idempotently.
    db2 = ts.ThreadsDB(tmp_path / "reverse.sqlite")
    db2.init_schema()
    thread2, call2, tc2 = _make_tc4(db2, thread_id="thread-reverse", tool_call_id="tc-reverse")
    cancel_first = ts.finalize_tool_output(
        db2,
        thread2,
        call2,
        decision="omit",
        source="user_omit",
        reason="Explicitly omitted by user",
        expected_event_seq=tc2.state_event_seq,
    )
    auto_second = ts.finalize_tool_output(
        db2,
        thread2,
        call2,
        decision="whole",
        source="automatic_policy",
        reason="Auto: whole",
        expected_event_seq=tc2.state_event_seq,
    )
    assert cancel_first.committed is True
    assert auto_second.idempotent is True
    assert auto_second.decision == "omit"
    assert len(_decision_rows(db2, thread2)) == 1


def test_auto_vs_manual_is_first_commit_wins_and_metadata_is_preserved(tmp_path) -> None:
    db = _make_db(tmp_path)
    thread_id, tool_call_id, tc = _make_tc4(db)
    manual_plan = ts.ToolOutputPublicationPlan(
        decision="whole",
        preview="manual preview",
        reason="Manual publish",
        channels={"optimizer": {"optimized": True}, "raw": {"stored_in_finished_event": True}},
        metadata={"audit_marker": "kept"},
    )
    manual = ts.finalize_tool_output(
        db,
        thread_id,
        tool_call_id,
        decision="whole",
        source="user",
        reason="Manual publish",
        expected_event_seq=tc.state_event_seq,
        publication_plan=manual_plan,
    )
    auto = ts.finalize_tool_output(
        db,
        thread_id,
        tool_call_id,
        decision="omit",
        source="automatic_policy",
        reason="Auto: omit",
        expected_event_seq=tc.state_event_seq,
    )

    assert manual.committed is True
    assert auto.idempotent is True
    assert auto.decision == "whole"
    state = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    payload = state.last_output_approval_payload
    assert payload["preview"] == "manual preview"
    assert payload["channels"]["optimizer"]["optimized"] is True
    assert payload["channels"]["raw"]["stored_in_finished_event"] is True
    assert payload["audit_marker"] == "kept"
    assert len(_decision_rows(db, thread_id)) == 1

    # If automatic policy commits first, a stale ordinary manual prompt is
    # idempotent; only the explicit user_cancel source may supersede it.
    db2 = ts.ThreadsDB(tmp_path / "auto-first.sqlite")
    db2.init_schema()
    thread2, call2, tc2 = _make_tc4(db2, thread_id="thread-auto-first", tool_call_id="tc-auto-first")
    auto_first = ts.finalize_tool_output(
        db2,
        thread2,
        call2,
        decision="omit",
        source="automatic_policy",
        reason="Auto: omit",
        expected_event_seq=tc2.state_event_seq,
    )
    manual_second = ts.finalize_tool_output(
        db2,
        thread2,
        call2,
        decision="whole",
        source="user",
        reason="Manual publish",
        expected_event_seq=tc2.state_event_seq,
        publication_plan=manual_plan,
    )
    assert auto_first.committed is True
    assert manual_second.idempotent is True
    assert manual_second.decision == "omit"
    assert len(_decision_rows(db2, thread2)) == 1


def test_duplicate_scheduler_finalizers_commit_one_decision(tmp_path) -> None:
    db = _make_db(tmp_path)
    thread_id, tool_call_id, tc = _make_tc4(db)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker() -> None:
        local = ts.ThreadsDB(db.path)
        try:
            barrier.wait(timeout=2)
            results.append(
                ts.finalize_tool_output(
                    local,
                    thread_id,
                    tool_call_id,
                    decision="whole",
                    source="automatic_policy",
                    reason="Auto: duplicate scheduler",
                    expected_event_seq=tc.state_event_seq,
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            local.conn.close()

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert len(results) == 2
    assert sum(result.committed for result in results) == 1
    assert sum(result.idempotent for result in results) == 1
    assert len(_decision_rows(db, thread_id)) == 1
    assert ts.build_tool_call_states(db, thread_id)[tool_call_id].output_decision == "whole"


def test_duplicate_long_whole_finalizers_create_one_artifact(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id, tool_call_id, _tc = _make_tc4(db)
    full_output = "r" * 120_000
    db.append_event(
        event_id="finished-long-concurrent",
        thread_id=thread_id,
        type_="tool_call.finished",
        payload={"tool_call_id": tool_call_id, "reason": "success", "output": full_output},
    )
    tc = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker() -> None:
        local = ts.ThreadsDB(db.path)
        try:
            barrier.wait(timeout=2)
            results.append(
                ts.finalize_tool_output(
                    local,
                    thread_id,
                    tool_call_id,
                    decision="whole",
                    source="automatic_policy",
                    reason="Automatic long output",
                    expected_event_seq=tc.state_event_seq,
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            local.conn.close()

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert len(results) == 2
    assert sum(result.committed for result in results) == 1
    assert sum(result.idempotent for result in results) == 1
    assert len(_decision_rows(db, thread_id)) == 1
    artifact_root = tmp_path / ".egg" / "egg_outputs" / thread_id
    assert len(list(artifact_root.iterdir())) == 1



def test_expected_lifecycle_watermark_rejects_changed_tc4(tmp_path) -> None:
    db = _make_db(tmp_path)
    thread_id, tool_call_id, tc = _make_tc4(db)
    db.append_event(
        event_id="late-summary",
        thread_id=thread_id,
        type_="tool_call.summary",
        payload={"tool_call_id": tool_call_id, "summary": "changed after prompt"},
    )

    with pytest.raises(ts.ToolOutputStateConflict) as exc_info:
        ts.finalize_tool_output(
            db,
            thread_id,
            tool_call_id,
            decision="whole",
            source="user",
            reason="stale UI",
            expected_event_seq=tc.state_event_seq,
        )

    assert exc_info.value.expected_event_seq == tc.state_event_seq
    assert _decision_rows(db, thread_id) == []
    assert ts.build_tool_call_states(db, thread_id)[tool_call_id].state == "TC4"

def test_runner_finalization_requires_live_lease(tmp_path) -> None:
    db = _make_db(tmp_path)
    thread_id, tool_call_id, tc = _make_tc4(db)
    assert db.try_open_stream(thread_id, "old-owner", "2000-01-01 00:00:00", owner="old", purpose="tool")
    stale_writer = db.invocation_writer(thread_id, "old-owner")

    with pytest.raises(ts.LeaseLost):
        ts.finalize_tool_output(
            db,
            thread_id,
            tool_call_id,
            decision="whole",
            source="automatic_policy",
            reason="Auto: stale owner",
            expected_event_seq=tc.state_event_seq,
            invocation_writer=stale_writer,
        )

    assert _decision_rows(db, thread_id) == []
    assert ts.build_tool_call_states(db, thread_id)[tool_call_id].state == "TC4"


def test_append_failure_rolls_back_and_leaves_tc4_retriable(tmp_path, monkeypatch) -> None:
    db = _make_db(tmp_path)
    thread_id, tool_call_id, tc = _make_tc4(db)

    from eggthreads import tool_output as tool_output_module

    def fail_append(*args, **kwargs):
        raise RuntimeError("simulated append failure")

    monkeypatch.setattr(tool_output_module, "_append_decision_with_expected_state", fail_append)
    with pytest.raises(ts.ToolOutputPersistenceError, match="simulated append failure"):
        ts.finalize_tool_output(
            db,
            thread_id,
            tool_call_id,
            decision="whole",
            source="user",
            reason="Manual",
            expected_event_seq=tc.state_event_seq,
        )

    assert _decision_rows(db, thread_id) == []
    state = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert state.state == "TC4"
    assert state.finished_output == "raw output"


def test_partial_artifact_failure_is_detectable_and_retriable(tmp_path, monkeypatch) -> None:
    db = _make_db(tmp_path)
    thread_id, tool_call_id, tc = _make_tc4(db)

    def fail_artifact(*args, **kwargs):
        raise OSError("artifact disk unavailable")

    monkeypatch.setattr("eggthreads.runner.stash_tool_output_and_build_preview", fail_artifact)
    with pytest.raises(ts.ToolOutputPlanError, match="artifact disk unavailable"):
        ts.finalize_tool_output(
            db,
            thread_id,
            tool_call_id,
            decision="partial",
            source="user",
            reason="Manual partial",
            expected_event_seq=tc.state_event_seq,
        )

    assert _decision_rows(db, thread_id) == []
    assert ts.build_tool_call_states(db, thread_id)[tool_call_id].state == "TC4"


def test_manual_whole_long_output_is_routed_to_recoverable_artifact(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id, tool_call_id, tc = _make_tc4(db)
    full_output = "x" * 120_000
    db.append_event(
        event_id="finished-long-whole",
        thread_id=thread_id,
        type_="tool_call.finished",
        payload={"tool_call_id": tool_call_id, "reason": "success", "output": full_output},
    )
    tc = ts.build_tool_call_states(db, thread_id)[tool_call_id]

    result = ts.finalize_tool_output(
        db,
        thread_id,
        tool_call_id,
        decision="whole",
        source="user",
        reason="Manual whole",
        expected_event_seq=tc.state_event_seq,
    )

    payload = dict(result.payload)
    assert payload["requested_decision"] == "whole"
    assert payload["decision"] == "partial"
    assert payload["artifact_path"]
    assert "read_long_tool_output(" in payload["preview"]
    assert len(payload["preview"]) < len(full_output)

    artifact_path = Path(payload["artifact_path"])
    read = ts.create_default_tools().execute(
        "read_long_tool_output",
        {"artifact_id": artifact_path.name, "chunk_number": 1},
        thread_id=thread_id,
        db=db,
    )
    assert read.endswith("x" * 40_000)


def test_custom_whole_long_plan_cannot_bypass_artifact_routing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id, tool_call_id, _tc = _make_tc4(db)
    full_output = "y" * 120_000
    db.append_event(
        event_id="finished-custom-long-whole",
        thread_id=thread_id,
        type_="tool_call.finished",
        payload={"tool_call_id": tool_call_id, "reason": "interrupted", "output": full_output},
    )
    tc = ts.build_tool_call_states(db, thread_id)[tool_call_id]

    result = ts.finalize_tool_output(
        db,
        thread_id,
        tool_call_id,
        decision="whole",
        source="user_cancel",
        reason="Interrupted",
        expected_event_seq=tc.state_event_seq,
        publication_plan=ts.ToolOutputPublicationPlan(
            decision="whole",
            preview=full_output,
            reason="Interrupted",
        ),
    )

    payload = dict(result.payload)
    assert payload["requested_decision"] == "whole"
    assert payload["decision"] == "partial"
    assert Path(payload["artifact_path"]).is_dir()
    assert "read_long_tool_output(" in payload["preview"]


def test_raw_long_output_cannot_shrink_below_threshold_during_sanitization(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id, tool_call_id, _tc = _make_tc4(db)
    full_output = "\x1b[2J" * 30_000
    db.append_event(
        event_id="finished-control-sequence-long-whole",
        thread_id=thread_id,
        type_="tool_call.finished",
        payload={"tool_call_id": tool_call_id, "reason": "success", "output": full_output},
    )
    tc = ts.build_tool_call_states(db, thread_id)[tool_call_id]

    result = ts.finalize_tool_output(
        db,
        thread_id,
        tool_call_id,
        decision="whole",
        source="user",
        reason="Manual whole",
        expected_event_seq=tc.state_event_seq,
    )

    payload = dict(result.payload)
    assert payload["requested_decision"] == "whole"
    assert payload["decision"] == "partial"
    assert Path(payload["artifact_path"]).is_dir()
    assert "read_long_tool_output(" in payload["preview"]
    assert "\x1b" not in payload["preview"]


def test_auto_routed_whole_artifact_failure_leaves_tc4_retriable(tmp_path, monkeypatch) -> None:
    db = _make_db(tmp_path)
    thread_id, tool_call_id, _tc = _make_tc4(db)
    full_output = "f" * 120_000
    db.append_event(
        event_id="finished-failing-long-whole",
        thread_id=thread_id,
        type_="tool_call.finished",
        payload={"tool_call_id": tool_call_id, "reason": "success", "output": full_output},
    )
    tc = ts.build_tool_call_states(db, thread_id)[tool_call_id]

    def fail_artifact(*args, **kwargs):
        raise OSError("artifact disk unavailable")

    monkeypatch.setattr("eggthreads.runner.stash_tool_output_and_build_preview", fail_artifact)
    with pytest.raises(ts.ToolOutputPlanError, match="artifact disk unavailable"):
        ts.finalize_tool_output(
            db,
            thread_id,
            tool_call_id,
            decision="whole",
            source="user",
            reason="Manual whole",
            expected_event_seq=tc.state_event_seq,
        )

    assert _decision_rows(db, thread_id) == []
    state = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    assert state.state == "TC4"
    assert state.finished_output == full_output


def test_user_cancel_reuses_existing_long_output_artifact(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    thread_id, tool_call_id, _tc = _make_tc4(db)
    full_output = "z" * 120_000
    db.append_event(
        event_id="finished-long-cancel",
        thread_id=thread_id,
        type_="tool_call.finished",
        payload={"tool_call_id": tool_call_id, "reason": "interrupted", "output": full_output},
    )
    tc = ts.build_tool_call_states(db, thread_id)[tool_call_id]
    first = ts.finalize_tool_output(
        db,
        thread_id,
        tool_call_id,
        decision="whole",
        source="automatic_policy",
        reason="Automatic long output",
        expected_event_seq=tc.state_event_seq,
    )
    first_path = str(first.payload["artifact_path"])

    cancelled = ts.finalize_tool_output(
        db,
        thread_id,
        tool_call_id,
        decision="whole",
        source="user_cancel",
        reason="Cancelled",
        expected_event_seq=first.state_event_seq,
    )

    payload = dict(cancelled.payload)
    assert payload["decision"] == "partial"
    assert payload["requested_decision"] == "whole"
    assert payload["artifact_path"] == first_path
    assert len(list((tmp_path / ".egg" / "egg_outputs" / thread_id).iterdir())) == 1


def test_automatic_policy_failure_is_detectable_and_retriable(tmp_path, monkeypatch) -> None:
    db = _make_db(tmp_path)
    thread_id, tool_call_id, tc = _make_tc4(db)
    assert db.try_open_stream(thread_id, "live-owner", "2999-01-01 00:00:00", owner="runner", purpose="tool")
    writer = db.invocation_writer(thread_id, "live-owner")

    def fail_policy(*args, **kwargs):
        raise RuntimeError("policy unavailable")

    monkeypatch.setattr("eggthreads.output_policy.decide_output_publication", fail_policy)
    with pytest.raises(ts.ToolOutputPlanError, match="policy unavailable"):
        _finalize_auto_tool_output(
            db,
            thread_id,
            tool_call_id,
            "raw output",
            expected_event_seq=tc.state_event_seq,
            writer=writer,
        )

    assert _decision_rows(db, thread_id) == []
    assert ts.build_tool_call_states(db, thread_id)[tool_call_id].state == "TC4"
