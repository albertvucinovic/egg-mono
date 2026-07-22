from __future__ import annotations

import asyncio
import json
from pathlib import Path

import eggthreads as ts
from eggthreads.tools import ToolContext, ToolExecutionResult, ToolRegistry, create_default_tools


TOOL_NAME = "execute_tool_in_other_thread"
SHA = "0123456789abcdef" * 4


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _execute(registry, db, caller, target, tool_name, arguments, **context):
    return asyncio.run(
        registry.execute_async(
            TOOL_NAME,
            {"tool_name": tool_name, "arguments": arguments, "thread_id": target},
            db=db,
            thread_id=caller,
            preserve_tool_result=True,
            **context,
        )
    )


def test_cross_thread_tool_is_registered_with_expected_schema() -> None:
    registry = create_default_tools()
    specs = {spec["function"]["name"]: spec["function"] for spec in registry.tools_spec()}

    spec = specs[TOOL_NAME]
    assert set(spec["parameters"]["required"]) == {"tool_name", "arguments", "thread_id"}
    assert spec["parameters"]["additionalProperties"] is False
    assert registry.capabilities(TOOL_NAME).supports_cancellation is True
    assert registry.capabilities(TOOL_NAME).supports_cross_thread_execution is False

    supported = {
        name
        for name in registry._tools
        if registry.capabilities(name).supports_cross_thread_execution
    }
    assert {"python_repl", "bash_repl", "bash", "python_exec"}.issubset(supported)
    assert {
        TOOL_NAME,
        "compact_thread",
        "extract_tool_output",
        "get_user_message_while_preserving_llm_turn",
        "answer_user_while_preserving_llm_turn",
    }.isdisjoint(supported)


def test_cross_thread_dispatch_uses_deep_descendant_context(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    grandchild = ts.create_child_thread(db, child, name="grandchild")
    workdir = tmp_path / "grandchild-work"
    ts.set_thread_working_directory(db, grandchild, str(workdir))
    ts.set_thread_model(db, grandchild, "descendant-model")

    registry = create_default_tools()
    seen = {}

    def probe(arguments, ctx: ToolContext):
        seen.update(
            arguments=arguments,
            thread_id=ctx.thread_id,
            model=ctx.initial_model_key,
            working_dir=Path(ctx.working_dir),
            origin=ctx.origin,
            invoke_id=ctx.invoke_id,
            tool_call_id=ctx.tool_call_id,
            stream=ctx.stream,
        )
        return "target-ok"

    registry.register(
        "cross_thread_probe",
        "probe",
        {"type": "object", "properties": {"value": {"type": "string"}}},
        probe,
        accepts_context=True,
        capabilities={"supports_cross_thread_execution": True},
    )

    result = _execute(registry, db, root, grandchild, "cross_thread_probe", {"value": "hello"})

    assert result.output == "target-ok"
    assert seen == {
        "arguments": {"value": "hello"},
        "thread_id": grandchild,
        "model": "descendant-model",
        "working_dir": workdir.resolve(),
        "origin": "ancestor_cross_thread",
        "invoke_id": None,
        "tool_call_id": None,
        "stream": None,
    }


def test_cross_thread_dispatch_uses_descendant_working_directory_and_sandbox_lookup(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    workdir = tmp_path / "child-work"
    ts.set_thread_working_directory(db, child, str(workdir))
    ts.set_thread_sandbox_config(db, child, enabled=False)
    registry = create_default_tools()

    result = _execute(registry, db, root, child, "python_exec", {"script": "import os; print(os.getcwd())"})

    assert result.reason == "success"
    assert str(workdir.resolve()) in result.output


def test_cross_thread_dispatch_rejects_non_descendants(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    sibling = ts.create_child_thread(db, root, name="sibling")
    unrelated = ts.create_root_thread(db, name="unrelated")
    registry = create_default_tools()

    for caller, target in (
        (root, root),
        (child, root),
        (child, sibling),
        (root, unrelated),
    ):
        result = _execute(registry, db, caller, target, "skill", {})
        assert result.reason == "denied"
        assert "strict descendant" in result.output


def test_cross_thread_dispatch_rejects_missing_target_before_tool_disclosure(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    registry = create_default_tools()

    result = _execute(registry, db, root, "missing-thread", "not-a-real-tool", {})

    assert result.reason == "error"
    assert "target thread not found" in result.output
    assert "unknown tool" not in result.output


def test_cross_thread_dispatch_enforces_target_policy_and_visibility(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    registry = create_default_tools()

    ts.set_thread_tool_allowlist(db, child, [TOOL_NAME, "python_repl"])
    denied = _execute(registry, db, root, child, "skill", {})
    assert denied.reason == "disabled"
    assert "not allowed" in denied.output

    ts.set_thread_tools_enabled(db, child, False)
    disabled = _execute(registry, db, root, child, "python_repl", {"code": "1"})
    assert disabled.reason == "disabled"
    assert "LLM tools are disabled" in disabled.output

    custom = ToolRegistry()
    from eggthreads.builtin_plugins.cross_thread_execution import register_cross_thread_execution_tool

    register_cross_thread_execution_tool(custom)
    custom.register(
        "local_probe",
        "local",
        {"type": "object", "properties": {}},
        lambda _args: "must-not-run",
        local_only=True,
        capabilities={"supports_cross_thread_execution": True},
    )
    local = _execute(custom, db, root, child, "local_probe", {})
    assert local.reason == "unsupported"
    assert "local-only" in local.output


def test_cross_thread_dispatch_cannot_bypass_calling_ancestor_tool_policy(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    registry = create_default_tools()

    ts.set_thread_tool_allowlist(db, root, [TOOL_NAME])
    result = _execute(registry, db, root, child, "python_repl", {"code": "1"})

    assert result.reason == "disabled"
    assert "not allowed" in result.output
    assert ts.find_runtime_thread(db, child, language="python") is None


def test_cross_thread_legacy_thread_identity_cannot_redirect_target(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    sibling = ts.create_child_thread(db, root, name="sibling")
    registry = create_default_tools()

    result = _execute(
        registry,
        db,
        root,
        child,
        "spawn_agent",
        {"context_text": "must not spawn", "parent_thread_id": sibling},
    )

    assert result.reason == "error"
    assert "may not redirect parent_thread_id" in result.output
    assert ts.list_children_ids(db, child) == []
    assert ts.list_children_ids(db, sibling) == []


def test_cross_thread_dispatch_rejects_unopted_lifecycle_and_reserved_arguments(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    registry = create_default_tools()

    for tool_name in (
        TOOL_NAME,
        "get_user_message_while_preserving_llm_turn",
        "extract_tool_output",
        "compact_thread",
    ):
        result = _execute(registry, db, root, child, tool_name, {})
        assert result.reason == "unsupported"

    reserved = _execute(
        registry,
        db,
        root,
        child,
        "python_repl",
        {"code": "1", "_thread_id": root},
    )
    assert reserved.reason == "error"
    assert "reserved tool context" in reserved.output


def test_cross_thread_nested_timeout_cannot_exceed_outer_timeout(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    registry = create_default_tools()
    seen = {}

    def timeout_probe(arguments, ctx: ToolContext):
        seen["argument_timeout"] = arguments.get("timeout")
        seen["context_timeout"] = ctx.timeout_sec
        return "ok"

    registry.register(
        "timeout_probe",
        "timeout",
        {"type": "object", "properties": {}},
        timeout_probe,
        accepts_context=True,
        capabilities={"supports_cross_thread_execution": True},
    )

    result = _execute(
        registry,
        db,
        root,
        child,
        "timeout_probe",
        {"timeout": 20},
        tool_timeout_sec=5,
    )

    assert result.output == "ok"
    assert seen == {"argument_timeout": 5, "context_timeout": 5}


def test_cross_thread_dispatch_propagates_outer_task_cancellation(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    registry = create_default_tools()
    started = asyncio.Event()

    async def waiting_probe(_arguments, _ctx: ToolContext):
        started.set()
        await asyncio.Event().wait()

    registry.register(
        "waiting_probe",
        "wait forever",
        {"type": "object", "properties": {}},
        waiting_probe,
        accepts_context=True,
        capabilities={"supports_cross_thread_execution": True},
    )

    async def run():
        task = asyncio.create_task(
            registry.execute_async(
                TOOL_NAME,
                {"tool_name": "waiting_probe", "arguments": {}, "thread_id": child},
                db=db,
                thread_id=root,
                preserve_tool_result=True,
            )
        )
        await started.wait()
        task.cancel()
        return await asyncio.gather(task, return_exceptions=True)

    result = asyncio.run(run())
    assert isinstance(result[0], asyncio.CancelledError)


def test_python_repl_reuses_descendant_session_and_hydrates_descendant_history(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EGG_ALLOW_MEMORY_SESSION_WITH_SANDBOX", "1")
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    root_message = ts.append_message(db, root, "user", "ancestor-only-history")
    first_child_message = ts.append_message(db, child, "user", "first descendant historic message")
    ts.enable_thread_session(db, child, provider="memory")
    registry = create_default_tools()

    first = _execute(
        registry,
        db,
        root,
        child,
        "python_repl",
        {
            "code": (
                "persistent_cross_thread_value = 41\n"
                "print(thread_context['thread']['thread_id'])\n"
                "print([m['msg_id'] for m in all_messages])"
            )
        },
    )
    assert first.reason == "success"
    assert child in first.output
    assert first_child_message in first.output
    assert root_message not in first.output

    second_child_message = ts.append_message(db, child, "assistant", "later descendant historic message")
    second = _execute(
        registry,
        db,
        root,
        child,
        "python_repl",
        {
            "code": (
                "print(persistent_cross_thread_value + 1)\n"
                "print(thread_context['thread']['loaded_through_event_seq'])\n"
                "print([m['msg_id'] for m in all_messages])"
            )
        },
    )
    assert "42" in second.output
    assert first_child_message in second.output
    assert second_child_message in second.output

    child_runtime = ts.find_runtime_thread(db, child, language="python")
    assert child_runtime is not None
    assert child_runtime.runtime_thread_id in ts.list_children_ids(db, child)
    assert ts.find_runtime_thread(db, root, language="python") is None


def test_cross_thread_result_is_published_only_in_ancestor(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    call_id = "call-cross-thread"
    ts.append_message(
        db,
        root,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": TOOL_NAME,
                        "arguments": json.dumps(
                            {"tool_name": "skill", "arguments": {"query": "no-such-skill"}, "thread_id": child}
                        ),
                    },
                }
            ]
        },
    )
    ts.approve_tool_calls_for_thread(db, root, decision="granted", tool_call_id=call_id)
    registry = create_default_tools()
    runner = ts.ThreadRunner(db, root, llm=object(), tools=registry)
    child_watermark = db.max_event_seq(child)

    assert asyncio.run(runner.run_once()) is True
    assert asyncio.run(runner.run_once()) is True

    root_messages = ts.create_snapshot(db, root)["messages"]
    child_messages = ts.create_snapshot(db, child)["messages"]
    result = next(message for message in root_messages if message.get("tool_call_id") == call_id)
    assert result["name"] == TOOL_NAME
    assert not any(message.get("tool_call_id") == call_id for message in child_messages)
    assert db.max_event_seq(child) == child_watermark


def test_cross_thread_result_preserves_target_masking_for_ancestor_provider(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    ts.set_thread_allow_raw_tool_output(db, root, True)
    child = ts.create_child_thread(db, root, name="child")
    # A later local descendant restriction must still be honored when the
    # receiving ancestor permits raw provider output.
    ts.set_thread_allow_raw_tool_output(db, child, False)

    registry = create_default_tools()
    registry.register(
        "secret_probe",
        "secret",
        {"type": "object", "properties": {}},
        lambda _args: "OPENAI_API_KEY=secret-value-123",
        capabilities={"supports_cross_thread_execution": True},
    )
    result = _execute(registry, db, root, child, "secret_probe", {})
    assert result.force_provider_output_masking is True

    runner = ts.ThreadRunner(db, root, llm=object(), tools=registry)
    sanitized = runner._sanitize_messages_for_api(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call",
                        "type": "function",
                        "function": {"name": TOOL_NAME, "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "content": result.output,
                "tool_call_id": "call",
                "name": TOOL_NAME,
                "force_provider_output_masking": True,
            }
        ],
        tools_cfg=type("Tools", (), {"allow_raw_tool_output": True})(),
    )
    tool_message = next(message for message in sanitized if message["role"] == "tool")
    assert "secret-value-123" not in tool_message["content"]
    assert "***" in tool_message["content"]


def test_cross_thread_structured_result_keeps_outer_protocol_name_and_content_parts(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    registry = create_default_tools()
    payload = {
        "content_parts": [
            {"type": "text", "text": "Generated in descendant"},
            {
                "type": "artifact",
                "artifact_id": "abc12345",
                "owner_thread_id": child,
                "presentation": "image",
                "mime_type": "image/png",
                "filename": "descendant.png",
                "size_bytes": 10,
                "sha256": SHA,
                "provenance": {"kind": "test"},
                "options": {},
            },
        ]
    }
    registry._tools["generate_image"]["impl"] = lambda _args, _ctx: ToolExecutionResult(
        json.dumps(payload)
    )

    call_id = "call-cross-structured"
    ts.append_message(
        db,
        root,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": TOOL_NAME,
                        "arguments": json.dumps(
                            {"tool_name": "generate_image", "arguments": {"prompt": "egg"}, "thread_id": child}
                        ),
                    },
                }
            ]
        },
    )
    ts.approve_tool_calls_for_thread(db, root, decision="granted", tool_call_id=call_id)
    runner = ts.ThreadRunner(db, root, llm=object(), tools=registry)

    assert asyncio.run(runner.run_once()) is True
    assert ts.build_tool_call_states(db, root)[call_id].transcript_content_tool_name == "generate_image"
    assert asyncio.run(runner.run_once()) is True

    result = next(
        message
        for message in ts.create_snapshot(db, root)["messages"]
        if message.get("tool_call_id") == call_id
    )
    assert result["name"] == TOOL_NAME
    assert isinstance(result["content"], list)
    assert result["content"][1]["artifact_id"] == "abc12345"
    assert result["content"][1]["owner_thread_id"] == child
