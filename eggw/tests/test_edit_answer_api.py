from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eggw.core import state as core_state


GET_USER_TOOL_NAME = "get_user_message_while_preserving_llm_turn"


@pytest.fixture
def test_db_path(tmp_path: Path) -> str:
    db_path = tmp_path / ".egg" / "threads.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return str(db_path)


@pytest.fixture
def app(test_db_path: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EGG_DB_PATH", test_db_path)

    if "eggw.main" in sys.modules:
        del sys.modules["eggw.main"]
    from eggw import main

    core_state.db = None
    core_state.active_schedulers = {}

    from eggthreads import ThreadsDB

    conn = sqlite3.connect(test_db_path, check_same_thread=False, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")

    core_state.db = ThreadsDB.__new__(ThreadsDB)
    core_state.db.path = Path(test_db_path)
    core_state.db.conn = conn
    core_state.db.init_schema()

    return main.app


@pytest.fixture
def client(app):
    return TestClient(app)


def _create_thread(client: TestClient, name: str = "Edit Answer") -> str:
    response = client.post("/api/threads", json={"name": name})
    assert response.status_code == 200
    return response.json()["id"]


def _append_event(db, tid: str, type_: str, payload: dict, *, msg_id: str | None = None, invoke_id: str | None = None) -> None:
    db.append_event(
        event_id=f"{type_}-{db.max_event_seq(tid) + 1}",
        thread_id=tid,
        type_=type_,
        payload=payload,
        msg_id=msg_id,
        invoke_id=invoke_id,
    )


def _start_get_user_wait(thread_id: str, *, note: str = "What title should I use?") -> str:
    from eggthreads import append_message, create_snapshot

    invoke_id = "invoke-edit-answer-get-user-web"
    tool_call_id = "call-edit-answer-get-user-web"
    assert core_state.db.try_open_stream(thread_id, invoke_id, "2999-01-01 00:00:00", owner="test", purpose="tool")
    append_message(
        core_state.db,
        thread_id,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": GET_USER_TOOL_NAME,
                        "arguments": json.dumps({"assistant_note": note}),
                    },
                }
            ]
        },
    )
    _append_event(
        core_state.db,
        thread_id,
        "tool_call.execution_started",
        {"tool_call_id": tool_call_id},
        invoke_id=invoke_id,
    )
    note_msg_id = append_message(
        core_state.db,
        thread_id,
        "assistant",
        note,
        extra={
            "answer_user_preserve_turn": True,
            "source_tool_name": GET_USER_TOOL_NAME,
            "tool_call_id": tool_call_id,
            "awaiting_user_message_tool_call_id": tool_call_id,
        },
    )
    create_snapshot(core_state.db, thread_id)
    return note_msg_id


def test_edit_answer_command_returns_modal_action_and_draft(client: TestClient):
    from eggthreads import append_message

    thread_id = _create_thread(client, "Edit Command")
    source_id = append_message(core_state.db, thread_id, "assistant", "# Heading\n\nBody")

    response = client.post(f"/api/threads/{thread_id}/command", json={"command": "/editAnswer"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["command_name"] == "editAnswer"
    assert payload["message"] == f"Prepared quoted assistant answer {source_id[-8:]}" + "."
    assert payload["data"] == {
        "action": "open_edit_answer_modal",
        "draft": "> # Heading\n>\n> Body",
        "source_msg_id": source_id,
        "source_kind": "assistant_answer",
        "source_suffix": source_id[-8:],
        "source_label": "assistant answer",
        "suppress_transcript": True,
        "message": f"Prepared quoted assistant answer {source_id[-8:]}.",
    }

    messages_response = client.get(f"/api/threads/{thread_id}/messages")
    assert messages_response.status_code == 200
    messages = messages_response.json()
    assert not any(message.get("content") == "/editAnswer" for message in messages)
    assert not any(str(message.get("content") or "").startswith("Prepared quoted") for message in messages)


def test_edit_answer_command_selector_overrides_latest_answer(client: TestClient):
    from eggthreads import append_message

    thread_id = _create_thread(client, "Edit Command Selector")
    selected_id = append_message(core_state.db, thread_id, "assistant", "First answer")
    append_message(core_state.db, thread_id, "assistant", "Second answer")

    response = client.post(
        f"/api/threads/{thread_id}/command",
        json={"command": f"/editAnswer {selected_id[-8:]}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["draft"] == "> First answer"
    assert payload["data"]["source_msg_id"] == selected_id
    assert payload["data"]["source_kind"] == "assistant_answer"


def test_edit_answer_command_defaults_to_waiting_assistant_note(client: TestClient):
    from eggthreads import append_message

    thread_id = _create_thread(client, "Edit Waiting Note Command")
    append_message(core_state.db, thread_id, "assistant", "Older final answer")
    note_id = _start_get_user_wait(thread_id, note="## Waiting note\n\nPlease edit me")
    append_message(core_state.db, thread_id, "assistant", "Later assistant text")

    response = client.post(f"/api/threads/{thread_id}/command", json={"command": "/editAnswer"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["draft"] == "> ## Waiting note\n>\n> Please edit me"
    assert payload["data"]["source_msg_id"] == note_id
    assert payload["data"]["source_kind"] == "assistant_note"
    assert payload["data"]["source_label"] == "assistant note"


def test_edit_answer_command_selector_overrides_waiting_assistant_note(client: TestClient):
    from eggthreads import append_message

    thread_id = _create_thread(client, "Edit Waiting Note Override")
    selected_id = append_message(core_state.db, thread_id, "assistant", "Explicit final answer")
    _start_get_user_wait(thread_id, note="Waiting note")

    response = client.post(
        f"/api/threads/{thread_id}/command",
        json={"command": f"/editAnswer {selected_id[-8:]}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["draft"] == "> Explicit final answer"
    assert payload["data"]["source_msg_id"] == selected_id
    assert payload["data"]["source_kind"] == "assistant_answer"


def test_edit_answer_endpoint_returns_same_draft_as_command(client: TestClient):
    from eggthreads import append_message

    thread_id = _create_thread(client, "Edit Endpoint")
    source_id = append_message(core_state.db, thread_id, "assistant", "Endpoint **answer**")

    endpoint_response = client.post(f"/api/threads/{thread_id}/edit-answer-draft", json={})
    command_response = client.post(f"/api/threads/{thread_id}/command", json={"command": "/editAnswer"})

    assert endpoint_response.status_code == 200
    assert command_response.status_code == 200
    endpoint_payload = endpoint_response.json()
    command_payload = command_response.json()["data"]
    assert endpoint_payload == command_payload
    assert endpoint_payload["source_msg_id"] == source_id
    assert endpoint_payload["draft"] == "> Endpoint **answer**"


def test_edit_answer_endpoint_accepts_exact_source_msg_id(client: TestClient):
    from eggthreads import append_message

    thread_id = _create_thread(client, "Edit Endpoint Source")
    source_id = append_message(core_state.db, thread_id, "assistant", "Source-selected answer")
    append_message(core_state.db, thread_id, "assistant", "Latest answer")

    response = client.post(
        f"/api/threads/{thread_id}/edit-answer-draft",
        json={"source_msg_id": source_id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_msg_id"] == source_id
    assert payload["draft"] == "> Source-selected answer"


def test_edit_answer_endpoint_rejects_selector_and_source_msg_id_together(client: TestClient):
    from eggthreads import append_message

    thread_id = _create_thread(client, "Edit Endpoint Selector Conflict")
    source_id = append_message(core_state.db, thread_id, "assistant", "Answer")

    response = client.post(
        f"/api/threads/{thread_id}/edit-answer-draft",
        json={"selector": source_id[-8:], "source_msg_id": source_id},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Provide either selector or source_msg_id, not both."


def test_edit_answer_endpoint_defaults_to_waiting_assistant_note(client: TestClient):
    from eggthreads import append_message

    thread_id = _create_thread(client, "Edit Waiting Note Endpoint")
    append_message(core_state.db, thread_id, "assistant", "Older final answer")
    note_id = _start_get_user_wait(thread_id, note="Waiting endpoint note")
    append_message(core_state.db, thread_id, "assistant", "Later assistant text")

    response = client.post(f"/api/threads/{thread_id}/edit-answer-draft", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_msg_id"] == note_id
    assert payload["source_kind"] == "assistant_note"
    assert payload["draft"] == "> Waiting endpoint note"


def test_edit_answer_endpoint_reports_no_assistant_answer(client: TestClient):
    thread_id = _create_thread(client, "Edit No Answer")

    response = client.post(f"/api/threads/{thread_id}/edit-answer-draft", json={})

    assert response.status_code == 400
    assert "No assistant answer with textual content" in response.json()["detail"]


def test_edit_answer_command_opens_empty_editor_when_no_answer(client: TestClient):
    thread_id = _create_thread(client, "Edit Empty Fallback Command")

    response = client.post(f"/api/threads/{thread_id}/command", json={"command": "/editAnswer"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["message"] == "Prepared empty input message draft."
    assert payload["data"] == {
        "action": "open_edit_answer_modal",
        "draft": "",
        "source_msg_id": "",
        "source_kind": "input_message",
        "source_suffix": "",
        "source_label": "input message",
        "suppress_transcript": True,
        "message": "Prepared empty input message draft.",
    }


def test_editor_command_opens_empty_editor_draft(client: TestClient):
    thread_id = _create_thread(client, "Editor Command")

    response = client.post(f"/api/threads/{thread_id}/command", json={"command": "/editor"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["source_kind"] == "input_message"
    assert payload["data"]["draft"] == ""
    assert payload["data"]["source_label"] == "input message"


def test_editor_command_rejects_arguments(client: TestClient):
    thread_id = _create_thread(client, "Editor Command Args")

    response = client.post(f"/api/threads/{thread_id}/command", json={"command": "/editor unexpected"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["data"] is None
    assert "/editor does not take arguments" in payload["message"]


def test_edit_answer_command_reports_no_selector_match(client: TestClient):
    from eggthreads import append_message

    thread_id = _create_thread(client, "Edit No Match")
    append_message(core_state.db, thread_id, "assistant", "Only answer")

    response = client.post(f"/api/threads/{thread_id}/command", json={"command": "/editAnswer missing"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["data"] is None
    assert "No assistant answer matched selector 'missing'" in payload["message"]


def test_edit_answer_endpoint_reports_selected_empty_answer(client: TestClient):
    from eggthreads import append_message

    thread_id = _create_thread(client, "Edit Empty Answer")
    empty_id = append_message(core_state.db, thread_id, "assistant", "")

    response = client.post(
        f"/api/threads/{thread_id}/edit-answer-draft",
        json={"source_msg_id": empty_id},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "selected assistant answer is empty."


def test_edit_answer_endpoint_reports_ambiguous_selector(client: TestClient):
    thread_id = _create_thread(client, "Edit Ambiguous")
    core_state.db.append_event(
        event_id="custom-edit-answer-1",
        thread_id=thread_id,
        type_="msg.create",
        payload={"role": "assistant", "content": "custom first"},
        msg_id="01AAAASAME",
    )
    core_state.db.append_event(
        event_id="custom-edit-answer-2",
        thread_id=thread_id,
        type_="msg.create",
        payload={"role": "assistant", "content": "custom second"},
        msg_id="01BBBBSAME",
    )

    response = client.post(f"/api/threads/{thread_id}/edit-answer-draft", json={"selector": "SAME"})

    assert response.status_code == 400
    assert "matched multiple assistant answers" in response.json()["detail"]
