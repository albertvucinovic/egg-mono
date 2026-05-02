from __future__ import annotations

import asyncio
import json

import eggthreads as ts


class _SummaryOnlyLLM:
    current_model_key = "test-model"

    def set_model(self, model_key):
        self.current_model_key = model_key

    def set_model_with_config(self, model_key, config):
        self.current_model_key = model_key

    async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None):
        yield {"type": "reasoning_summary_delta", "text": "display summary"}
        yield {"type": "content_delta", "text": "final"}
        yield {"type": "done", "message": {"role": "assistant", "content": "final"}}


class _ReasoningLLM:
    current_model_key = "test-model"

    def set_model(self, model_key):
        self.current_model_key = model_key

    def set_model_with_config(self, model_key, config):
        self.current_model_key = model_key

    async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None):
        yield {"type": "reasoning_delta", "text": "durable reasoning"}
        yield {"type": "content_delta", "text": "final"}
        yield {
            "type": "done",
            "message": {
                "role": "assistant",
                "content": "final",
                "reasoning_content": "durable reasoning",
            },
        }


def _messages(db: ts.ThreadsDB, thread_id: str):
    ts.create_snapshot(db, thread_id)
    row = db.conn.execute(
        "SELECT snapshot_json FROM threads WHERE thread_id=?",
        (thread_id,),
    ).fetchone()
    return json.loads(row[0])["messages"]


def test_reasoning_summary_delta_streams_but_is_not_persisted(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", "hello")
    ts.create_snapshot(db, tid)

    runner = ts.ThreadRunner(db, tid, llm=_SummaryOnlyLLM())
    assert asyncio.run(runner.run_once()) is True

    deltas = [
        json.loads(r[0])
        for r in db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='stream.delta' ORDER BY event_seq",
            (tid,),
        )
    ]
    assert any(d.get("reasoning_summary") == "display summary" for d in deltas)
    assert not any(d.get("reason") == "display summary" for d in deltas)

    assistant = _messages(db, tid)[-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "final"
    assert "reasoning" not in assistant
    assert "reasoning_content" not in assistant


def test_reasoning_delta_is_still_persisted(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", "hello")
    ts.create_snapshot(db, tid)

    runner = ts.ThreadRunner(db, tid, llm=_ReasoningLLM())
    assert asyncio.run(runner.run_once()) is True

    assistant = _messages(db, tid)[-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "final"
    assert assistant["reasoning"] == "durable reasoning"
