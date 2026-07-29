from __future__ import annotations

import pytest

from eggflow.eggthreads_tasks import PICRecoveryError, PICTask
from eggthreads import ThreadsDB, append_message, create_root_thread, create_snapshot


def test_pic_recovery_refuses_implicit_history_rewind(tmp_path):
    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = create_root_thread(db, name="pic")
    anchor = append_message(db, thread_id, "user", "anchor")
    first = append_message(db, thread_id, "assistant", "first")
    second = append_message(db, thread_id, "assistant", "valuable tail")
    before = db.max_event_seq(thread_id)

    with pytest.raises(PICRecoveryError, match="explicit recovery"):
        PICTask()._ensure_thread_healthy(db, thread_id)

    assert db.max_event_seq(thread_id) == before
    assert [message["msg_id"] for message in create_snapshot(db, thread_id)["messages"]] == [
        anchor,
        first,
        second,
    ]