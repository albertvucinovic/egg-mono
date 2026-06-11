from __future__ import annotations

import asyncio
import json

import eggthreads as ts


class _UsageMetadataLLM:
    current_model_key = "test-model"

    def __init__(self):
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    def set_model(self, model_key):
        self.current_model_key = model_key

    def set_model_with_config(self, model_key, config):
        self.current_model_key = model_key

    async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None, **kwargs):
        self.calls += 1
        self.seen_messages.append([dict(m) for m in messages])
        if self.calls == 1:
            yield {
                "type": "done",
                "message": {
                    "role": "assistant",
                    "content": "first answer",
                    "api_usage": {"total_input_tokens": 10, "total_output_tokens": 2},
                    "provider_usage": {"prompt_tokens": 10, "completion_tokens": 2},
                },
            }
        else:
            yield {"type": "done", "message": {"role": "assistant", "content": "second answer"}}


def _msg_payloads(db: ts.ThreadsDB, thread_id: str) -> list[dict]:
    rows = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq ASC",
        (thread_id,),
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


def test_usage_metadata_is_persisted_but_not_sent_to_next_provider_call(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", "hello")
    ts.create_snapshot(db, tid)

    llm = _UsageMetadataLLM()
    runner = ts.ThreadRunner(db, tid, llm=llm)
    assert asyncio.run(runner.run_once()) is True

    persisted_assistant = [p for p in _msg_payloads(db, tid) if p.get("role") == "assistant"][-1]
    assert persisted_assistant["api_usage"] == {"total_input_tokens": 10, "total_output_tokens": 2}
    assert persisted_assistant["provider_usage"] == {"prompt_tokens": 10, "completion_tokens": 2}

    ts.append_message(db, tid, "user", "next")
    ts.create_snapshot(db, tid)
    assert asyncio.run(runner.run_once()) is True

    second_call_messages = llm.seen_messages[1]
    previous_assistant = [
        m for m in second_call_messages
        if m.get("role") == "assistant" and m.get("content") == "first answer"
    ]
    assert previous_assistant
    assert all("api_usage" not in m for m in second_call_messages)
    assert all("provider_usage" not in m for m in second_call_messages)
