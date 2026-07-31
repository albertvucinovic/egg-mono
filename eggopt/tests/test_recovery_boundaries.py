from __future__ import annotations

from eggopt.recovery import InteractionRecovery
from eggthreads import ThreadsDB, append_message, create_root_thread, create_snapshot


def test_interaction_recovery_never_uses_older_diagnosis_boundary(tmp_path, monkeypatch):
    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = create_root_thread(db, name="interaction")
    old = append_message(db, thread_id, "user", "old")
    trigger = append_message(db, thread_id, "user", "owned trigger")
    tail = append_message(db, thread_id, "system", "LLM/runner error: retry")

    # If broad diagnosis were still consulted, this would select the older turn.
    monkeypatch.setattr(
        "eggthreads.diagnose_thread",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("broad diagnosis must not choose interaction recovery boundary")
        ),
    )

    assert InteractionRecovery(db, thread_id, trigger).recover() is True
    messages = create_snapshot(db, thread_id)["messages"]
    assert [message["msg_id"] for message in messages] == [old, trigger]
    assert tail not in [message["msg_id"] for message in messages]


def test_interaction_recovery_does_not_claim_a_later_turns_answer(tmp_path):
    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = create_root_thread(db, name="interaction")
    trigger = append_message(db, thread_id, "user", "interrupted turn")
    later = append_message(db, thread_id, "user", "newer turn")
    answer = append_message(db, thread_id, "assistant", "newer answer")

    assert InteractionRecovery(db, thread_id, trigger).recover() is True

    projection = create_snapshot(db, thread_id)
    visible_ids = [message["msg_id"] for message in projection["messages"]]
    assert visible_ids == [trigger]
    assert later not in visible_ids
    assert answer not in visible_ids
