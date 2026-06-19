from __future__ import annotations

import asyncio
import json

import eggthreads as ts
from eggthreads.runner import RunnerConfig


class _DoneLLM:
    current_model_key = "mock-model"

    def set_model(self, model_key):
        self.current_model_key = model_key

    async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None, **kwargs):
        yield {"type": "done", "message": {"role": "assistant", "content": "ok"}}


def test_runner_emits_provider_request_started_with_timeout(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="provider-timing")
    ts.append_message(db, tid, "user", "hello")
    ts.create_snapshot(db, tid)

    runner = ts.ThreadRunner(
        db,
        tid,
        llm=_DoneLLM(),
        config=RunnerConfig(api_timeout_sec=42, lease_ttl_sec=5),
    )

    assert asyncio.run(runner.run_once()) is True

    rows = db.conn.execute(
        "SELECT event_seq, invoke_id, payload_json FROM events "
        "WHERE thread_id=? AND type='provider_request.started'",
        (tid,),
    ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["timeout"] == 42
    assert payload["model_key"] == "mock-model"
    assert rows[0]["invoke_id"]

    ordered_types = [
        row[0]
        for row in db.conn.execute(
            "SELECT type FROM events WHERE thread_id=? ORDER BY event_seq",
            (tid,),
        ).fetchall()
    ]
    assert ordered_types.index("stream.open") < ordered_types.index("provider_request.started")
    assert ordered_types.index("provider_request.started") < ordered_types.index("stream.close")
