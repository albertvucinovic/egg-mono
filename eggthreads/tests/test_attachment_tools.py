from __future__ import annotations

import json
from pathlib import Path

import pytest

import eggthreads as ts
from eggthreads.input_artifacts import resolve_input_bytes
from eggthreads.provider_output_artifacts import save_provider_output_bytes


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / ".egg" / "threads.sqlite")
    db.init_schema()
    return db


def test_attachment_tools_are_registered_with_schema_and_help() -> None:
    registry = ts.create_default_tools()
    specs = {spec["function"]["name"]: spec["function"] for spec in registry.tools_spec()}

    for name in (
        "add_local_file_to_model_context",
        "add_provider_artifact_to_model_context",
        "save_provider_artifact_to_file",
    ):
        assert name in specs
        assert specs[name]["parameters"]["additionalProperties"] is False
        help_text = registry.execute("tool_help", {"tool_name": name})
        assert f"Tool: {name}" in help_text
        assert "Detailed description:" in help_text
        assert "Use when:" in help_text
        assert "Examples:" in help_text

    assert specs["add_local_file_to_model_context"]["parameters"]["required"] == ["path"]
    assert specs["add_provider_artifact_to_model_context"]["parameters"]["required"] == ["artifact_id"]
    assert specs["save_provider_artifact_to_file"]["parameters"]["required"] == ["artifact_id"]


def test_add_local_file_to_model_context_tool_ingests_local_file_and_returns_attachment_parts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    source = tmp_path / "note.txt"
    source.write_text("hello attachment", encoding="utf-8")

    output = ts.create_default_tools().execute("add_local_file_to_model_context", {"path": "note.txt"}, db=db, thread_id=tid)
    payload = json.loads(output)

    assert payload["action"] == "stage_attachment"
    assert payload["metadata"]["filename"] == "note.txt"
    assert "blob_relpath" not in payload["metadata"]
    part = payload["content_part"]
    assert part["type"] == "attachment"
    assert part["owner_thread_id"] == tid
    assert part["filename"] == "note.txt"
    assert payload["content_parts"][1] == part
    assert "[Attachment: file note.txt" in payload["content_text"]
    _metadata, data = resolve_input_bytes(tmp_path, db, tid, part["input_id"])
    assert data == b"hello attachment"
    assert "hello attachment" not in output


def test_add_local_file_to_model_context_tool_honors_sandbox_read_policy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    secret = tmp_path / "secret.txt"
    secret.write_text("hidden", encoding="utf-8")
    ts.set_thread_sandbox_config(
        db,
        tid,
        enabled=True,
        provider="srt",
        settings={"provider": "srt", "filesystem": {"denyRead": [str(secret)], "allowWrite": ["."], "denyWrite": []}},
        reason="test",
    )

    output = ts.create_default_tools().execute("add_local_file_to_model_context", {"path": str(secret)}, db=db, thread_id=tid)

    assert output.startswith("Error: ")
    assert "denyRead" in output
    assert not (tmp_path / ".egg" / "egg_inputs" / tid).exists()


def test_add_provider_artifact_to_model_context_tool_promotes_provider_artifact_and_enforces_access(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    source = save_provider_output_bytes(tmp_path, child, b"child generated", filename="generated.txt", mime_type="text/plain", presentation="file")

    missing_selector = ts.create_default_tools().execute("add_provider_artifact_to_model_context", {"artifact_id": source.artifact_id}, db=db, thread_id=parent)
    assert missing_selector.startswith("Error: ")
    assert "not found" in missing_selector.lower()

    output = ts.create_default_tools().execute(
        "add_provider_artifact_to_model_context",
        {"artifact_id": source.artifact_id, "descendant_thread_id": child},
        db=db,
        thread_id=parent,
    )
    payload = json.loads(output)

    part = payload["content_part"]
    assert part["type"] == "attachment"
    assert part["owner_thread_id"] == parent
    assert part["filename"] == "generated.txt"
    assert "[Attachment: file generated.txt" in payload["content_text"]
    metadata, data = resolve_input_bytes(tmp_path, db, parent, part["input_id"])
    assert data == b"child generated"
    assert metadata["provenance"]["source_artifact_id"] == source.artifact_id
    assert metadata["provenance"]["source_owner_thread_id"] == child


def test_save_provider_artifact_to_file_tool_exports_under_workdir_and_honors_sandbox_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    ts.set_thread_working_directory(db, tid, "work")
    source = save_provider_output_bytes(tmp_path, tid, b"generated bytes", filename="generated.txt", mime_type="text/plain", presentation="file")

    output = ts.create_default_tools().execute(
        "save_provider_artifact_to_file",
        {"artifact_id": source.artifact_id, "path": "exports/out.txt"},
        db=db,
        thread_id=tid,
    )
    payload = json.loads(output)

    assert payload["action"] == "save_provider_artifact"
    assert payload["path"] == "exports/out.txt"
    assert (tmp_path / "work" / "exports" / "out.txt").read_bytes() == b"generated bytes"
    assert "blob_relpath" not in payload["metadata"]

    overwrite = ts.create_default_tools().execute(
        "save_provider_artifact_to_file",
        {"artifact_id": source.artifact_id, "path": "exports/out.txt"},
        db=db,
        thread_id=tid,
    )
    assert overwrite.startswith("Error: ")
    assert "overwrite" in overwrite.lower()

    ts.set_thread_sandbox_config(
        db,
        tid,
        enabled=True,
        provider="srt",
        settings={"provider": "srt", "filesystem": {"denyRead": [], "allowWrite": ["allowed"], "denyWrite": []}},
        reason="test",
    )
    denied = ts.create_default_tools().execute(
        "save_provider_artifact_to_file",
        {"artifact_id": source.artifact_id, "path": "blocked/out.txt"},
        db=db,
        thread_id=tid,
    )
    assert denied.startswith("Error: ")
    assert "allowWrite" in denied
    assert not (tmp_path / "work" / "blocked" / "out.txt").exists()


def test_attachment_tool_outputs_publish_attachment_content_parts_in_runner_transcript(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    source = tmp_path / "note.txt"
    source.write_text("runner attachment", encoding="utf-8")
    tool_call_id = "call-attach-file"
    ts.append_message(
        db,
        tid,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {"name": "add_local_file_to_model_context", "arguments": json.dumps({"path": "note.txt"})},
                }
            ]
        },
    )
    db.append_event("approve", tid, "tool_call.approval", {"tool_call_id": tool_call_id, "decision": "granted"})

    runner = ts.ThreadRunner(db, tid, llm=object())
    assert asyncio.run(runner.run_once()) is True
    assert ts.build_tool_call_states(db, tid)[tool_call_id].state == "TC5"
    assert asyncio.run(runner.run_once()) is True

    messages = ts.create_snapshot(db, tid)["messages"]
    tool_message = next(msg for msg in messages if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id)
    assert tool_message["name"] == "add_local_file_to_model_context"
    content = tool_message["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "attachment"
    assert content[1]["filename"] == "note.txt"
    assert "[Attachment: file note.txt" in ts.content_to_plain_text(content)


def test_tool_attachment_result_is_expanded_into_visual_provider_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    source = tmp_path / "pixel.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage-bytes")
    attached = json.loads(ts.create_default_tools().execute("add_local_file_to_model_context", {"path": "pixel.png"}, db=db, thread_id=tid))
    tool_msg_id = "tool-msg-attach-image"
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-attach-image",
                    "type": "function",
                    "function": {"name": "add_local_file_to_model_context", "arguments": json.dumps({"path": "pixel.png"})},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-attach-image",
            "msg_id": tool_msg_id,
            "content": attached["content_parts"],
        },
    ]

    from eggthreads.attachment_lowering import (
        AttachmentLoweringContext,
        expand_tool_attachment_messages_for_provider,
        lower_messages_for_provider,
    )

    expanded = expand_tool_attachment_messages_for_provider(messages)

    assert len(expanded) == 3
    assert expanded[1]["role"] == "tool"
    assert isinstance(expanded[1]["content"], str)
    assert "[Attachment: image pixel.png" in expanded[1]["content"]
    assert expanded[2]["role"] == "user"
    assert expanded[2]["content"] == attached["content_parts"]

    lowered = lower_messages_for_provider(
        expanded,
        AttachmentLoweringContext(
            workspace=tmp_path,
            db=db,
            calling_thread_id=tid,
            model_key="vision-model",
            model_config={"input_modalities": ["text", "image"]},
            provider_api_type="responses",
        ),
        current_msg_id=tool_msg_id,
    )

    assert lowered[1]["role"] == "tool"
    assert isinstance(lowered[1]["content"], str)
    assert lowered[2]["role"] == "user"
    content = lowered[2]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "input_text", "text": attached["message"]}
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/png;base64,")


def test_multiple_tool_attachment_results_keep_protocol_tool_block_before_visual_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    (tmp_path / "one.png").write_bytes(b"\x89PNG\r\n\x1a\none")
    (tmp_path / "two.png").write_bytes(b"\x89PNG\r\n\x1a\ntwo")
    tools = ts.create_default_tools()
    one = json.loads(tools.execute("add_local_file_to_model_context", {"path": "one.png"}, db=db, thread_id=tid))
    two = json.loads(tools.execute("add_local_file_to_model_context", {"path": "two.png"}, db=db, thread_id=tid))

    from eggthreads.attachment_lowering import expand_tool_attachment_messages_for_provider

    expanded = expand_tool_attachment_messages_for_provider(
        [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-one"}, {"id": "call-two"}]},
            {"role": "tool", "tool_call_id": "call-one", "msg_id": "tool-one", "content": one["content_parts"]},
            {"role": "tool", "tool_call_id": "call-two", "msg_id": "tool-two", "content": two["content_parts"]},
        ]
    )

    assert [message["role"] for message in expanded] == ["assistant", "tool", "tool", "user", "user"]
    assert expanded[1]["tool_call_id"] == "call-one"
    assert expanded[2]["tool_call_id"] == "call-two"
    assert isinstance(expanded[1]["content"], str)
    assert isinstance(expanded[2]["content"], str)
    assert expanded[3]["msg_id"] == "tool-one"
    assert expanded[4]["msg_id"] == "tool-two"
    assert expanded[3]["content"] == one["content_parts"]
    assert expanded[4]["content"] == two["content_parts"]


def test_runner_preserves_tool_attachment_parts_until_provider_visual_lowering(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    source = tmp_path / "pixel.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage-bytes")
    tool_call_id = "call-add-image-to-context"
    ts.append_message(
        db,
        tid,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "add_local_file_to_model_context",
                        "arguments": json.dumps({"path": "pixel.png"}),
                    },
                }
            ]
        },
    )
    db.append_event("approve", tid, "tool_call.approval", {"tool_call_id": tool_call_id, "decision": "granted"})

    tool_runner = ts.ThreadRunner(db, tid, llm=object())
    assert asyncio.run(tool_runner.run_once()) is True
    assert asyncio.run(tool_runner.run_once()) is True

    class Registry:
        def get_effective_model_config(self, _model_key):
            return {"api_type": "responses", "input_modalities": ["text", "image"]}

        def merge_parameters(self, _model_key):
            return {}

    class CaptureLLM:
        current_model_key = "vision-model"
        registry = Registry()

        def __init__(self):
            self.seen_messages = []

        async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None, **kwargs):
            self.seen_messages.append(messages)
            yield {"type": "done", "message": {"role": "assistant", "content": "saw it"}}

    llm = CaptureLLM()
    assert asyncio.run(ts.ThreadRunner(db, tid, llm=llm).run_once()) is True
    assert llm.seen_messages
    provider_messages = llm.seen_messages[0]

    tool_message = next(msg for msg in provider_messages if msg.get("role") == "tool")
    assert isinstance(tool_message["content"], str)
    assert "[Attachment: image pixel.png" in tool_message["content"]

    visual_messages = [
        msg
        for msg in provider_messages
        if msg.get("role") == "user"
        and isinstance(msg.get("content"), list)
        and any(isinstance(part, dict) and part.get("type") == "input_image" for part in msg["content"])
    ]
    assert visual_messages
    visual_content = visual_messages[0]["content"]
    assert visual_content[0]["type"] == "input_text"
    assert visual_content[1]["type"] == "input_image"
    assert visual_content[1]["image_url"].startswith("data:image/png;base64,")
