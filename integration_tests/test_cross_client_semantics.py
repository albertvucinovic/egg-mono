"""Cross-client contracts that intentionally depend on Egg, EggW, and EggThreads."""
from __future__ import annotations

import asyncio

from egg.approval import ApprovalMixin
from eggthreads import (
    ThreadsDB,
    append_message,
    build_tool_call_states,
    create_root_thread,
    delete_message,
    edit_message,
    load_thread_projection,
)
from eggw import core
from eggw.models import ApprovalRequest
from eggw.routes import messages as web_messages
from eggw.routes import tools as web_tools


def test_terminal_and_web_share_output_finalization_precedence(tmp_path, monkeypatch):
    """Terminal and EggW decisions converge through the same TC4/TC5 authority."""
    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    monkeypatch.setattr(core.state, "db", db)
    monkeypatch.setattr(web_tools, "ensure_scheduler_for", lambda _thread_id: None)

    thread_id = create_root_thread(db, name="Cross-client finalization")
    tool_call_id = "call-cross-client-output"
    append_message(
        db,
        thread_id,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {"name": "bash", "arguments": "{}"},
                }
            ]
        },
    )
    db.append_event(
        "cross-client-approval",
        thread_id,
        "tool_call.approval",
        {"tool_call_id": tool_call_id, "decision": "granted"},
    )
    db.append_event(
        "cross-client-finished",
        thread_id,
        "tool_call.finished",
        {"tool_call_id": tool_call_id, "reason": "success", "output": "inspectable raw output"},
    )

    web_result = asyncio.run(
        web_tools.approve_tool(
            thread_id,
            ApprovalRequest(
                tool_call_id=tool_call_id,
                approved=True,
                output_decision="whole",
            ),
        )
    )
    assert web_result == {"status": "ok"}
    assert build_tool_call_states(db, thread_id)[tool_call_id].output_decision == "whole"

    class InputPanel:
        def __init__(self):
            self.text = "o"

        def clear_text(self):
            self.text = ""

        def increment_message_count(self):
            pass

    class TerminalApprovalClient(ApprovalMixin):
        def __init__(self):
            self.db = db
            self.current_thread = thread_id
            self._pending_prompt = {"kind": "output", "tool_call_ids": [tool_call_id]}
            self.input_panel = InputPanel()
            self.logs = []

        def log_system(self, message):
            self.logs.append(message)

    terminal = TerminalApprovalClient()
    assert terminal.handle_pending_approval_answer("o", source="integration test") is True

    final_state = build_tool_call_states(db, thread_id)[tool_call_id]
    assert final_state.state == "TC5"
    assert final_state.output_decision == "omit"
    assert terminal._pending_prompt == {}
    assert terminal.input_panel.text == ""


def test_eggw_message_envelope_matches_canonical_projection(tmp_path, monkeypatch):
    """EggW renders the same edit/delete result as EggThreads' fixed-watermark view."""
    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    monkeypatch.setattr(core.state, "db", db)
    thread_id = create_root_thread(db, name="Cross-client projection")
    first_id = append_message(db, thread_id, "user", "before edit")
    deleted_id = append_message(db, thread_id, "assistant", "remove me")
    edit_message(db, thread_id, first_id, "after edit")
    delete_message(db, thread_id, deleted_id)

    response = asyncio.run(
        web_messages.get_messages(
            thread_id,
            limit=None,
            before_id=None,
            envelope=True,
            response=None,
        )
    )
    projection = load_thread_projection(
        db,
        thread_id,
        response["snapshot_cursor"],
    )

    assert [(item["id"], item["role"], item["content"]) for item in response["items"]] == [
        (message.msg_id, message.payload.get("role"), message.payload.get("content"))
        for message in projection.messages
    ]
