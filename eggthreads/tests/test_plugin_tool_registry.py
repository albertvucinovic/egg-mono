from __future__ import annotations

import asyncio
import threading

import eggthreads as ts
from eggthreads.plugins import FunctionPlugin, ToolPluginContext, register_plugins
from eggthreads.tools import ToolCapabilities, ToolContext, ToolExecutionResult, ToolRegistry, create_default_tools, create_tool_registry


def _tool_names(registry: ToolRegistry) -> list[str]:
    return sorted(registry._tools.keys())


def test_create_tool_registry_matches_default_tools() -> None:
    assert _tool_names(create_tool_registry()) == _tool_names(create_default_tools())


def test_function_plugin_registers_tool() -> None:
    registry = ToolRegistry()

    def register(context: ToolPluginContext) -> None:
        context.tool_registry.register(
            "example_tool",
            "Example tool",
            {"type": "object", "properties": {}},
            lambda args: "ok",
        )

    register_plugins(
        ToolPluginContext(tool_registry=registry),
        [FunctionPlugin("example", "0", register)],
    )

    assert registry.execute("example_tool", {}) == "ok"
    assert _tool_names(registry) == ["example_tool"]


def test_context_aware_tool_receives_tool_context() -> None:
    registry = ToolRegistry()
    seen: dict[str, object] = {}

    def impl(args: dict[str, object], ctx: ToolContext) -> str:
        seen["args"] = args
        seen["ctx"] = ctx
        return "context-ok"

    def cancel_check() -> bool:
        return False

    registry.register(
        "context_tool",
        "Context tool",
        {"type": "object", "properties": {}},
        impl,
        accepts_context=True,
    )

    out = registry.execute(
        "context_tool",
        {"timeout_sec": 3},
        thread_id="thread-1",
        invoke_id="invoke-1",
        origin="test",
        initial_model_key="model-1",
        tool_timeout_sec=10,
        cancel_check=cancel_check,
        working_dir="/workspace",
        db="db-handle",
    )

    assert out == "context-ok"
    assert seen["args"] == {"timeout_sec": 3}
    ctx = seen["ctx"]
    assert isinstance(ctx, ToolContext)
    assert ctx.db == "db-handle"
    assert ctx.thread_id == "thread-1"
    assert ctx.invoke_id == "invoke-1"
    assert ctx.origin == "test"
    assert ctx.initial_model_key == "model-1"
    assert ctx.timeout_sec == 3
    assert ctx.cancel_check is cancel_check
    assert ctx.working_dir == "/workspace"


def test_legacy_tool_still_receives_private_context_args() -> None:
    registry = ToolRegistry()
    seen: dict[str, object] = {}

    def impl(args: dict[str, object]) -> str:
        seen["args"] = args
        return "legacy-ok"

    def cancel_check() -> bool:
        return False

    registry.register(
        "legacy_tool",
        "Legacy tool",
        {"type": "object", "properties": {}},
        impl,
    )

    out = registry.execute(
        "legacy_tool",
        {},
        thread_id="thread-1",
        initial_model_key="model-1",
        tool_timeout_sec=10,
        cancel_check=cancel_check,
    )

    assert out == "legacy-ok"
    assert seen["args"] == {
        "_thread_id": "thread-1",
        "_initial_model_key": "model-1",
        "_tool_timeout_sec": 10,
        "_cancel_check": cancel_check,
    }


def test_tool_capabilities_are_registry_metadata_not_tool_schema() -> None:
    registry = ToolRegistry()
    registry.register(
        "capable_tool",
        "Capable tool",
        {"type": "object", "properties": {}},
        lambda args: "ok",
        capabilities={"supports_streaming": True, "supports_cancellation": True, "mode": "test"},
    )

    capabilities = registry._tools["capable_tool"]["capabilities"]
    assert isinstance(capabilities, ToolCapabilities)
    assert capabilities.supports_streaming is True
    assert capabilities.supports_cancellation is True
    assert capabilities.metadata == {"mode": "test"}
    assert capabilities.to_dict() == {
        "mode": "test",
        "supports_streaming": True,
        "supports_cancellation": True,
    }

    spec = registry.tools_spec()[0]["function"]
    assert "capabilities" not in spec


def test_execution_tools_advertise_current_capabilities() -> None:
    registry = create_tool_registry()

    bash_capabilities = registry._tools["bash"]["capabilities"]
    assert isinstance(bash_capabilities, ToolCapabilities)
    assert bash_capabilities.supports_streaming is True
    assert bash_capabilities.supports_cancellation is True

    python_capabilities = registry._tools["python"]["capabilities"]
    assert isinstance(python_capabilities, ToolCapabilities)
    assert python_capabilities.supports_streaming is False
    assert python_capabilities.supports_cancellation is True


def test_execute_async_awaits_async_tool() -> None:
    registry = ToolRegistry()

    async def impl(args: dict[str, object]) -> str:
        await asyncio.sleep(0)
        return f"async-{args['value']}"

    registry.register(
        "async_tool",
        "Async tool",
        {"type": "object", "properties": {"value": {"type": "string"}}},
        impl,
    )

    assert asyncio.run(registry.execute_async("async_tool", {"value": "ok"})) == "async-ok"


def test_execute_async_preserves_context_aware_tools() -> None:
    registry = ToolRegistry()
    seen: dict[str, object] = {}

    async def impl(args: dict[str, object], ctx: ToolContext) -> str:
        await asyncio.sleep(0)
        seen["args"] = args
        seen["ctx"] = ctx
        return "async-context-ok"

    registry.register(
        "async_context_tool",
        "Async context tool",
        {"type": "object", "properties": {}},
        impl,
        accepts_context=True,
    )

    out = asyncio.run(
        registry.execute_async(
            "async_context_tool",
            {},
            thread_id="thread-1",
            tool_timeout_sec=5,
        )
    )

    assert out == "async-context-ok"
    assert seen["args"] == {}
    ctx = seen["ctx"]
    assert isinstance(ctx, ToolContext)
    assert ctx.thread_id == "thread-1"
    assert ctx.timeout_sec == 5


def test_execute_sync_runs_async_tool_without_running_loop() -> None:
    registry = ToolRegistry()

    async def impl(args: dict[str, object]) -> str:
        await asyncio.sleep(0)
        return "sync-bridge-ok"

    registry.register(
        "async_tool",
        "Async tool",
        {"type": "object", "properties": {}},
        impl,
    )

    assert registry.execute("async_tool", {}) == "sync-bridge-ok"


def test_execute_async_runs_sync_tool_in_worker_thread() -> None:
    registry = ToolRegistry()
    main_thread_id = threading.get_ident()
    seen: dict[str, object] = {}

    def impl(args: dict[str, object]) -> str:
        seen["thread_id"] = threading.get_ident()
        return f"sync-{args['value']}"

    registry.register(
        "sync_tool",
        "Sync tool",
        {"type": "object", "properties": {"value": {"type": "string"}}},
        impl,
    )

    assert asyncio.run(registry.execute_async("sync_tool", {"value": "ok"})) == "sync-ok"
    assert seen["thread_id"] != main_thread_id


def test_tool_execution_result_unwraps_by_default_and_can_be_preserved() -> None:
    registry = ToolRegistry()

    def impl(args: dict[str, object]) -> ToolExecutionResult:
        return ToolExecutionResult("structured-output", reason="timeout", streamed=True)

    registry.register(
        "structured_tool",
        "Structured tool",
        {"type": "object", "properties": {}},
        impl,
    )

    assert registry.execute("structured_tool", {}) == "structured-output"

    preserved = registry.execute("structured_tool", {}, preserve_tool_result=True)
    assert preserved == ToolExecutionResult("structured-output", reason="timeout", streamed=True)
