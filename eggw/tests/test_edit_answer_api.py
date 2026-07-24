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
    monkeypatch.setenv("EGGW_API_TOKEN", "test-eggw-token-" + "a" * 48)

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
    return TestClient(app, headers={"Authorization": "Bearer test-eggw-token-" + "a" * 48})


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


def test_editor_command_opens_argument_text(client: TestClient):
    thread_id = _create_thread(client, "Editor Command Args")

    response = client.post(f"/api/threads/{thread_id}/command", json={"command": "/editor write this prompt"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["source_kind"] == "input_message"
    assert payload["data"]["draft"] == "write this prompt"


def test_edit_answer_command_opens_unmatched_selector_as_input_text(client: TestClient):
    from eggthreads import append_message

    thread_id = _create_thread(client, "Edit No Match")
    append_message(core_state.db, thread_id, "assistant", "Only answer")

    response = client.post(f"/api/threads/{thread_id}/command", json={"command": "/editAnswer missing"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["source_kind"] == "input_message"
    assert payload["data"]["draft"] == "missing"


def test_edit_answer_command_selector_can_edit_user_message(client: TestClient):
    from eggthreads import append_message

    thread_id = _create_thread(client, "Edit User Message")
    user_id = append_message(core_state.db, thread_id, "user", "Original user prompt")
    append_message(core_state.db, thread_id, "assistant", "Assistant answer")

    response = client.post(
        f"/api/threads/{thread_id}/command",
        json={"command": f"/editAnswer {user_id[-8:]}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["draft"] == "Original user prompt"
    assert payload["data"]["source_msg_id"] == user_id
    assert payload["data"]["source_kind"] == "message"
    assert payload["data"]["source_label"] == "user message"


def test_edit_answer_endpoint_allows_selected_empty_answer(client: TestClient):
    from eggthreads import append_message

    thread_id = _create_thread(client, "Edit Empty Answer")
    empty_id = append_message(core_state.db, thread_id, "assistant", "")

    response = client.post(
        f"/api/threads/{thread_id}/edit-answer-draft",
        json={"source_msg_id": empty_id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft"] == ""
    assert payload["source_msg_id"] == empty_id
    assert payload["source_kind"] == "assistant_answer"


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
    assert "matched multiple messages" in response.json()["detail"]


def test_editor_command_opens_and_saves_host_file_with_conflict_detection(client: TestClient, tmp_path: Path, monkeypatch):
    from eggthreads import set_thread_working_directory

    monkeypatch.chdir(tmp_path)
    thread_id = _create_thread(client, "Editor File")
    set_thread_working_directory(core_state.db, thread_id, str(tmp_path))
    path = tmp_path / "browser.py"
    path.write_text("print('before')\n", encoding="utf-8")

    opened = client.post(
        f"/api/threads/{thread_id}/command",
        json={"command": "/editor browser.py"},
    ).json()

    assert opened["success"] is True
    data = opened["data"]
    assert data["editor_mode"] == "file"
    assert data["file_path"] == str(path)
    assert data["draft"] == "print('before')\n"

    path.write_text("print('changed elsewhere')\n", encoding="utf-8")
    stale = client.put(
        f"/api/threads/{thread_id}/editor-file",
        json={
            "handle": data["file_handle"],
            "content": "stale\n",
        },
    )
    assert stale.status_code == 409
    assert path.read_text(encoding="utf-8") == "print('changed elsewhere')\n"

    data = client.post(
        f"/api/threads/{thread_id}/command",
        json={"command": "/editor browser.py"},
    ).json()["data"]
    saved = client.put(
        f"/api/threads/{thread_id}/editor-file",
        json={
            "handle": data["file_handle"],
            "content": "print('after')\n",
        },
    )
    assert saved.status_code == 200
    assert path.read_text(encoding="utf-8") == "print('after')\n"

    reused = client.put(
        f"/api/threads/{thread_id}/editor-file",
        json={
            "handle": data["file_handle"],
            "content": "stale\n",
        },
    )
    assert reused.status_code == 409
    assert path.read_text(encoding="utf-8") == "print('after')\n"


def test_editor_file_handle_survives_validation_error_for_retry(client: TestClient, tmp_path: Path, monkeypatch):
    from eggthreads import set_thread_working_directory
    from eggw.editor_files import EDITOR_FILE_MAX_BYTES

    monkeypatch.chdir(tmp_path)
    thread_id = _create_thread(client, "Editor Retry")
    set_thread_working_directory(core_state.db, thread_id, str(tmp_path))
    path = tmp_path / "retry.txt"
    path.write_text("before\n", encoding="utf-8")
    opened = client.post(
        f"/api/threads/{thread_id}/command",
        json={"command": "/editor retry.txt"},
    ).json()["data"]

    rejected = client.put(
        f"/api/threads/{thread_id}/editor-file",
        json={"handle": opened["file_handle"], "content": "x" * (EDITOR_FILE_MAX_BYTES + 1)},
    )
    retried = client.put(
        f"/api/threads/{thread_id}/editor-file",
        json={"handle": opened["file_handle"], "content": "after\n"},
    )

    assert rejected.status_code == 400
    assert retried.status_code == 200
    assert path.read_text(encoding="utf-8") == "after\n"


def test_editor_file_session_can_be_discarded(client: TestClient, tmp_path: Path, monkeypatch):
    from eggthreads import set_thread_working_directory

    monkeypatch.chdir(tmp_path)
    thread_id = _create_thread(client, "Editor Discard")
    set_thread_working_directory(core_state.db, thread_id, str(tmp_path))
    path = tmp_path / "discard.txt"
    path.write_text("before\n", encoding="utf-8")
    opened = client.post(
        f"/api/threads/{thread_id}/command",
        json={"command": "/editor discard.txt"},
    ).json()["data"]

    discarded = client.delete(
        f"/api/threads/{thread_id}/editor-file/{opened['file_handle']}"
    )
    rejected = client.put(
        f"/api/threads/{thread_id}/editor-file",
        json={"handle": opened["file_handle"], "content": "after\n"},
    )

    assert discarded.status_code == 200
    assert discarded.json() == {"discarded": True}
    assert rejected.status_code == 409
    assert path.read_text(encoding="utf-8") == "before\n"


def test_editor_command_can_create_explicit_new_host_file(client: TestClient, tmp_path: Path, monkeypatch):
    from eggthreads import set_thread_working_directory

    monkeypatch.chdir(tmp_path)
    thread_id = _create_thread(client, "Editor New File")
    set_thread_working_directory(core_state.db, thread_id, str(tmp_path))
    path = tmp_path / "new-file.txt"

    opened = client.post(
        f"/api/threads/{thread_id}/command",
        json={"command": "/editor ./new-file.txt"},
    ).json()["data"]
    assert opened["editor_mode"] == "file"
    assert opened["file_handle"]

    saved = client.put(
        f"/api/threads/{thread_id}/editor-file",
        json={
            "handle": opened["file_handle"],
            "content": "created\n",
        },
    )
    assert saved.status_code == 200
    assert path.read_text(encoding="utf-8") == "created\n"


def test_editor_file_handle_cannot_redirect_or_cross_threads(client: TestClient, tmp_path: Path, monkeypatch):
    from eggthreads import set_thread_working_directory

    monkeypatch.chdir(tmp_path)
    owner = _create_thread(client, "Editor Handle Owner")
    other = _create_thread(client, "Editor Handle Other")
    set_thread_working_directory(core_state.db, owner, str(tmp_path))
    set_thread_working_directory(core_state.db, other, str(tmp_path))
    source = tmp_path / "source.txt"
    redirect = tmp_path / "redirect.txt"
    source.write_text("source\n", encoding="utf-8")
    redirect.write_text("redirect\n", encoding="utf-8")

    opened = client.post(
        f"/api/threads/{owner}/command",
        json={"command": "/editor source.txt"},
    ).json()["data"]
    response = client.put(
        f"/api/threads/{other}/editor-file",
        json={
            "handle": opened["file_handle"],
            "content": "hijacked\n",
        },
    )

    assert response.status_code == 409
    assert source.read_text(encoding="utf-8") == "source\n"
    assert redirect.read_text(encoding="utf-8") == "redirect\n"

    reopened = client.post(
        f"/api/threads/{owner}/command",
        json={"command": "/editor source.txt"},
    ).json()["data"]
    redirected = client.put(
        f"/api/threads/{owner}/editor-file",
        json={
            "handle": reopened["file_handle"],
            "content": "hijacked\n",
            "path": str(redirect),
        },
    )
    assert redirected.status_code == 422
    assert source.read_text(encoding="utf-8") == "source\n"
    assert redirect.read_text(encoding="utf-8") == "redirect\n"


def test_editor_file_save_preserves_mode_and_allows_empty_content(client: TestClient, tmp_path: Path, monkeypatch):
    from eggthreads import set_thread_working_directory

    monkeypatch.chdir(tmp_path)
    thread_id = _create_thread(client, "Editor Empty File")
    set_thread_working_directory(core_state.db, thread_id, str(tmp_path))
    path = tmp_path / "executable.sh"
    path.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    path.chmod(0o751)

    opened = client.post(
        f"/api/threads/{thread_id}/command",
        json={"command": "/editor executable.sh"},
    ).json()["data"]
    saved = client.put(
        f"/api/threads/{thread_id}/editor-file",
        json={"handle": opened["file_handle"], "content": ""},
    )

    assert saved.status_code == 200
    assert path.read_text(encoding="utf-8") == ""
    assert path.stat().st_mode & 0o777 == 0o751


def test_editor_file_open_rejects_binary_invalid_utf8_and_oversized_files(client: TestClient, tmp_path: Path, monkeypatch):
    from eggthreads import set_thread_working_directory
    from eggw.editor_files import EDITOR_FILE_MAX_BYTES

    monkeypatch.chdir(tmp_path)
    thread_id = _create_thread(client, "Editor Validation")
    set_thread_working_directory(core_state.db, thread_id, str(tmp_path))
    (tmp_path / "binary.dat").write_bytes(b"a\x00b")
    (tmp_path / "invalid.txt").write_bytes(b"\xff")
    with (tmp_path / "large.txt").open("wb") as handle:
        handle.truncate(EDITOR_FILE_MAX_BYTES + 1)

    for filename, expected in (
        ("binary.dat", "binary"),
        ("invalid.txt", "UTF-8"),
        ("large.txt", "too large"),
    ):
        response = client.post(
            f"/api/threads/{thread_id}/command",
            json={"command": f"/editor {filename}"},
        ).json()
        assert response["success"] is False
        assert expected in response["message"]


def test_editor_file_save_refuses_symlink_replacement(client: TestClient, tmp_path: Path, monkeypatch):
    from eggthreads import set_thread_working_directory

    monkeypatch.chdir(tmp_path)
    thread_id = _create_thread(client, "Editor Symlink Conflict")
    set_thread_working_directory(core_state.db, thread_id, str(tmp_path))
    path = tmp_path / "target.txt"
    other = tmp_path / "other.txt"
    path.write_text("target\n", encoding="utf-8")
    other.write_text("other\n", encoding="utf-8")
    opened = client.post(
        f"/api/threads/{thread_id}/command",
        json={"command": "/editor target.txt"},
    ).json()["data"]
    path.unlink()
    path.symlink_to(other)

    response = client.put(
        f"/api/threads/{thread_id}/editor-file",
        json={"handle": opened["file_handle"], "content": "hijack\n"},
    )

    assert response.status_code == 409
    assert other.read_text(encoding="utf-8") == "other\n"


def test_editor_command_at_file_prepares_draft_without_changing_source(client: TestClient, tmp_path: Path, monkeypatch):
    from eggthreads import set_thread_working_directory

    monkeypatch.chdir(tmp_path)
    thread_id = _create_thread(client, "Editor File Draft")
    set_thread_working_directory(core_state.db, thread_id, str(tmp_path))
    path = tmp_path / "source.txt"
    path.write_text("source body", encoding="utf-8")

    response = client.post(
        f"/api/threads/{thread_id}/command",
        json={"command": "/editor @source.txt"},
    ).json()

    assert response["success"] is True
    assert response["data"]["draft"] == "source body"
    assert response["data"].get("editor_mode") is None
    assert path.read_text(encoding="utf-8") == "source body"


def test_editor_command_selects_show_tool_declaration_as_pretty_json(client: TestClient):
    from eggthreads import append_message

    thread_id = _create_thread(client, "Editor Tool Declaration")
    call_id = "call-editor-pretty-json"
    append_message(
        core_state.db,
        thread_id,
        "assistant",
        "",
        extra={
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "bash", "arguments": '{"script":"echo hi"}'},
            }],
        },
    )

    response = client.post(
        f"/api/threads/{thread_id}/command",
        json={"command": f"/editor {call_id[-10:]}"},
    ).json()

    assert response["success"] is True
    parsed = json.loads(response["data"]["draft"])
    assert parsed["id"] == call_id
    assert parsed["function"]["name"] == "bash"


def test_eggw_rejects_foreground_terminal_prefix_instead_of_running_hidden_bash(client: TestClient):
    thread_id = _create_thread(client, "No Browser PTY")

    response = client.post(
        f"/api/threads/{thread_id}/command",
        json={"command": "$$$ htop"},
    ).json()

    assert response["success"] is False
    assert response["command_name"] == "$$$"
    assert "terminal Egg" in response["message"]


def test_editor_ambiguous_record_requests_existing_completion_without_dialog(client: TestClient):
    thread_id = _create_thread(client, "Editor Ambiguous Completion")
    for prefix in ("alpha", "beta"):
        core_state.db.append_event(
            event_id=f"event-{prefix}-editor-completion",
            thread_id=thread_id,
            type_="msg.create",
            msg_id=f"{prefix}-editor-shared",
            payload={"role": "assistant", "content": prefix},
        )

    response = client.post(
        f"/api/threads/{thread_id}/command",
        json={"command": "/editor editor-shared"},
    ).json()

    assert response["success"] is True
    assert response["data"] == {
        "action": "request_completion",
        "input": "/editor editor-shared",
        "suppress_transcript": True,
    }
