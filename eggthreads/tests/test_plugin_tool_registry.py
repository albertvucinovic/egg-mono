from __future__ import annotations

import asyncio
import threading
import pytest

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
    assert ctx.cancel_check is not cancel_check
    assert ctx.cancel_check is not None
    assert ctx.cancel_check() is False
    assert ctx.working_dir == "/workspace"
    assert ctx.raw["tool_registry"] is registry


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
    seen_args = seen["args"]
    assert isinstance(seen_args, dict)
    composed_cancel_check = seen_args.pop("_cancel_check")
    assert callable(composed_cancel_check)
    assert composed_cancel_check() is False
    assert seen_args == {
        "_thread_id": "thread-1",
        "_initial_model_key": "model-1",
        "_tool_timeout_sec": 10,
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


def test_tool_registry_adds_canonical_timeout_to_all_tool_schemas() -> None:
    registry = ToolRegistry()
    registry.register(
        "timeout_tool",
        "Timeout tool",
        {"type": "object", "properties": {"value": {"type": "string"}}},
        lambda args: "ok",
    )

    props = registry.tools_spec()[0]["function"]["parameters"]["properties"]
    assert "timeout" in props
    assert "timeout_sec" not in props


def test_execution_tools_advertise_current_capabilities() -> None:
    registry = create_tool_registry()

    bash_capabilities = registry._tools["bash"]["capabilities"]
    assert isinstance(bash_capabilities, ToolCapabilities)
    assert bash_capabilities.supports_streaming is True
    assert bash_capabilities.supports_cancellation is True

    python_capabilities = registry._tools["python_exec"]["capabilities"]
    assert isinstance(python_capabilities, ToolCapabilities)
    assert python_capabilities.supports_streaming is False
    assert python_capabilities.supports_cancellation is True
    assert registry.capabilities("python") == python_capabilities


def test_historical_default_alias_does_not_shadow_an_exact_custom_tool() -> None:
    registry = ToolRegistry()
    registry.register(
        "python",
        "Custom tool using the historical name",
        {"type": "object", "properties": {}},
        lambda _args: "custom-python",
    )

    assert registry.resolve_name("python") == "python"
    assert registry.execute("python", {}) == "custom-python"


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


def test_execute_async_deadline_detaches_noncooperative_sync_tool() -> None:
    registry = ToolRegistry()
    release = threading.Event()
    started = threading.Event()

    def impl(args: dict[str, object]) -> str:
        started.set()
        release.wait()
        return "too-late"

    registry.register(
        "stuck_sync_tool",
        "Stuck sync tool",
        {"type": "object", "properties": {}},
        impl,
    )

    async def run() -> ToolExecutionResult:
        try:
            result = await asyncio.wait_for(
                registry.execute_async(
                    "stuck_sync_tool",
                    {},
                    tool_timeout_sec=0.01,
                    preserve_tool_result=True,
                ),
                timeout=1.0,
            )
            assert isinstance(result, ToolExecutionResult)
            return result
        finally:
            release.set()

    result = asyncio.run(run())

    assert started.is_set()
    assert result.reason == "timeout"
    assert "TIMEOUT" in result.output


def test_execute_async_deadline_composes_sync_cooperative_cancellation() -> None:
    registry = ToolRegistry()
    saw_cancellation = threading.Event()

    def impl(args: dict[str, object]) -> str:
        cancel_check = args["_cancel_check"]
        assert callable(cancel_check)
        while not cancel_check():
            threading.Event().wait(0.001)
        saw_cancellation.set()
        return "cooperatively stopped"

    registry.register(
        "cooperative_sync_tool",
        "Cooperative sync tool",
        {"type": "object", "properties": {}},
        impl,
    )

    result = asyncio.run(
        registry.execute_async(
            "cooperative_sync_tool",
            {},
            tool_timeout_sec=0.01,
            preserve_tool_result=True,
        )
    )

    assert saw_cancellation.is_set()
    assert isinstance(result, ToolExecutionResult)
    assert result.reason == "timeout"


def test_execute_async_deadline_cancels_async_tool_and_runs_cleanup() -> None:
    registry = ToolRegistry()
    cleanup_ran = asyncio.Event()

    async def impl(args: dict[str, object]) -> str:
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_ran.set()

    registry.register(
        "stuck_async_tool",
        "Stuck async tool",
        {"type": "object", "properties": {}},
        impl,
    )

    async def run() -> ToolExecutionResult:
        result = await registry.execute_async(
            "stuck_async_tool",
            {},
            tool_timeout_sec=0.01,
            preserve_tool_result=True,
        )
        assert isinstance(result, ToolExecutionResult)
        await asyncio.wait_for(cleanup_ran.wait(), timeout=0.2)
        return result

    result = asyncio.run(run())

    assert result.reason == "timeout"


def test_execute_async_result_wins_deadline_race() -> None:
    registry = ToolRegistry()

    async def impl(args: dict[str, object]) -> str:
        return "winner"

    registry.register(
        "fast_async_tool",
        "Fast async tool",
        {"type": "object", "properties": {}},
        impl,
    )

    assert asyncio.run(
        registry.execute_async("fast_async_tool", {}, tool_timeout_sec=0.01)
    ) == "winner"


def test_execute_async_without_timeout_remains_unbounded() -> None:
    registry = ToolRegistry()
    release = asyncio.Event()

    async def impl(args: dict[str, object]) -> str:
        await release.wait()
        return "released"

    registry.register(
        "unbounded_async_tool",
        "Unbounded async tool",
        {"type": "object", "properties": {}},
        impl,
    )

    async def run() -> str:
        task = asyncio.create_task(registry.execute_async("unbounded_async_tool", {}))
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        return await task

    assert asyncio.run(run()) == "released"


def test_execute_async_outer_cancellation_reaches_cooperative_sync_tool() -> None:
    registry = ToolRegistry()
    started = threading.Event()
    stopped = threading.Event()

    def impl(args: dict[str, object]) -> str:
        cancel_check = args["_cancel_check"]
        assert callable(cancel_check)
        started.set()
        while not cancel_check():
            threading.Event().wait(0.001)
        stopped.set()
        return "stopped"

    registry.register(
        "cancelled_sync_tool",
        "Cancelled sync tool",
        {"type": "object", "properties": {}},
        impl,
    )

    async def run() -> None:
        task = asyncio.create_task(registry.execute_async("cancelled_sync_tool", {}))
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        deadline = asyncio.get_running_loop().time() + 0.2
        while not stopped.is_set():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("cooperative sync tool did not stop")
            await asyncio.sleep(0.001)

    asyncio.run(run())


def test_execute_async_composes_upstream_cancellation_check() -> None:
    registry = ToolRegistry()
    upstream_cancelled = threading.Event()

    def impl(args: dict[str, object]) -> str:
        cancel_check = args["_cancel_check"]
        assert callable(cancel_check)
        return "cancelled" if cancel_check() else "live"

    registry.register(
        "upstream_cancel_tool",
        "Upstream cancel tool",
        {"type": "object", "properties": {}},
        impl,
    )
    upstream_cancelled.set()

    assert asyncio.run(
        registry.execute_async(
            "upstream_cancel_tool",
            {},
            cancel_check=upstream_cancelled.is_set,
        )
    ) == "cancelled"


def test_sync_tool_admission_queues_healthy_concurrency() -> None:
    import eggthreads.tools as tools_module

    registry = ToolRegistry()
    release = threading.Event()
    started = 0
    started_lock = threading.Lock()
    original_admission = tools_module._SYNC_TOOL_ADMISSION
    tools_module._SYNC_TOOL_ADMISSION = tools_module._SyncToolAdmission(2)

    def healthy(args: dict[str, object]) -> str:
        nonlocal started
        with started_lock:
            started += 1
        release.wait()
        return str(args["value"])

    registry.register("healthy", "Healthy", {"type": "object", "properties": {}}, healthy)

    async def run() -> list[str]:
        tasks = [
            asyncio.create_task(
                registry.execute_async(
                    "healthy",
                    {"value": index},
                    tool_timeout_sec=1,
                )
            )
            for index in range(6)
        ]
        deadline = asyncio.get_running_loop().time() + 0.5
        while started < 2:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("initial healthy workers did not start")
            await asyncio.sleep(0.001)
        assert any(not task.done() for task in tasks[2:])
        release.set()
        return await asyncio.gather(*tasks)

    try:
        results = asyncio.run(run())
        assert results == [str(index) for index in range(6)]
        assert tools_module._SYNC_TOOL_ADMISSION.counts() == (0, 0)
    finally:
        release.set()
        tools_module._SYNC_TOOL_ADMISSION = original_admission


def test_sync_tool_admission_timeout_and_no_timeout_overload() -> None:
    import eggthreads.tools as tools_module

    registry = ToolRegistry()
    release = threading.Event()
    original_admission = tools_module._SYNC_TOOL_ADMISSION
    original_wait = tools_module._SYNC_TOOL_ADMISSION_WAIT_SEC
    tools_module._SYNC_TOOL_ADMISSION = tools_module._SyncToolAdmission(1)
    tools_module._SYNC_TOOL_ADMISSION_WAIT_SEC = 0.02

    registry.register(
        "stuck", "Stuck", {"type": "object", "properties": {}}, lambda args: release.wait()
    )
    registry.register(
        "fast", "Fast", {"type": "object", "properties": {}}, lambda args: "fast"
    )

    async def run() -> tuple[ToolExecutionResult, ToolExecutionResult]:
        stuck = asyncio.create_task(
            registry.execute_async(
                "stuck", {}, tool_timeout_sec=0.01, preserve_tool_result=True
            )
        )
        await asyncio.sleep(0.02)
        timed = await registry.execute_async(
            "fast", {}, tool_timeout_sec=0.02, preserve_tool_result=True
        )
        unbounded = await registry.execute_async(
            "fast", {}, preserve_tool_result=True
        )
        assert (await stuck).reason == "timeout"
        return timed, unbounded

    try:
        timed, unbounded = asyncio.run(run())
    finally:
        release.set()
        deadline = __import__("time").monotonic() + 0.5
        while tools_module._SYNC_TOOL_ADMISSION.counts() != (0, 0):
            if __import__("time").monotonic() >= deadline:
                raise AssertionError("admission did not clean up")
            __import__("time").sleep(0.001)
        tools_module._SYNC_TOOL_ADMISSION = original_admission
        tools_module._SYNC_TOOL_ADMISSION_WAIT_SEC = original_wait

    assert timed.reason == "timeout"
    assert unbounded.reason == "overloaded"


def test_daemon_thread_cap_and_process_exit_are_safe() -> None:
    import subprocess
    import sys
    import textwrap

    package_root = str(__import__("pathlib").Path(__file__).resolve().parents[1])
    script = textwrap.dedent(
        """
        import asyncio
        import threading
        import eggthreads.tools as tools_module
        from eggthreads.tools import ToolExecutionResult, ToolRegistry

        tools_module._SYNC_TOOL_ADMISSION = tools_module._SyncToolAdmission(2)
        tools_module._SYNC_TOOL_ADMISSION_WAIT_SEC = 0.02
        registry = ToolRegistry()
        never_release = threading.Event()
        registry.register(
            "stuck",
            "Stuck",
            {"type": "object", "properties": {}},
            lambda args: never_release.wait(),
        )
        registry.register(
            "fast",
            "Fast",
            {"type": "object", "properties": {}},
            lambda args: "fast-ok",
        )

        async def main():
            first, second = await asyncio.gather(
                *[
                    registry.execute_async(
                        "stuck",
                        {},
                        tool_timeout_sec=0.01,
                        preserve_tool_result=True,
                    )
                    for _ in range(2)
                ]
            )
            overloaded = await registry.execute_async(
                "fast",
                {},
                preserve_tool_result=True,
            )
            assert first.reason == second.reason == "timeout"
            assert isinstance(overloaded, ToolExecutionResult)
            assert overloaded.reason == "overloaded"
            assert tools_module._SYNC_TOOL_ADMISSION.counts() == (2, 2)

        asyncio.run(main())
        print("bounded-and-exited", flush=True)
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        env={**__import__("os").environ, "PYTHONPATH": package_root},
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "bounded-and-exited"


def test_execute_async_sync_tool_propagates_contextvars() -> None:
    import contextvars

    registry = ToolRegistry()
    marker = contextvars.ContextVar("tool-marker", default="missing")
    registry.register(
        "contextvar_tool",
        "Contextvar tool",
        {"type": "object", "properties": {}},
        lambda args: marker.get(),
    )

    async def run() -> str:
        marker.set("propagated")
        return await registry.execute_async("contextvar_tool", {})

    assert asyncio.run(run()) == "propagated"


def test_execute_direct_enforces_sync_tool_deadline() -> None:
    registry = ToolRegistry()
    never_release = threading.Event()
    registry.register(
        "direct_stuck_sync",
        "Direct stuck sync",
        {"type": "object", "properties": {}},
        lambda args: never_release.wait(),
    )

    started_at = __import__("time").monotonic()
    try:
        result = registry.execute(
            "direct_stuck_sync",
            {},
            tool_timeout_sec=0.01,
            preserve_tool_result=True,
        )
        elapsed = __import__("time").monotonic() - started_at
    finally:
        never_release.set()

    assert isinstance(result, ToolExecutionResult)
    assert result.reason == "timeout"
    assert elapsed < 0.75


def test_execute_direct_enforces_async_tool_deadline() -> None:
    registry = ToolRegistry()

    async def stuck(args: dict[str, object]) -> str:
        await asyncio.Event().wait()
        return "unreachable"

    registry.register(
        "direct_stuck_async",
        "Direct stuck async",
        {"type": "object", "properties": {}},
        stuck,
    )

    started_at = __import__("time").monotonic()
    result = registry.execute(
        "direct_stuck_async",
        {},
        tool_timeout_sec=0.01,
        preserve_tool_result=True,
    )
    elapsed = __import__("time").monotonic() - started_at

    assert isinstance(result, ToolExecutionResult)
    assert result.reason == "timeout"
    assert elapsed < 0.75


def test_execute_direct_async_tool_inside_running_loop_keeps_existing_error() -> None:
    registry = ToolRegistry()
    called = False

    async def impl(args: dict[str, object]) -> str:
        nonlocal called
        called = True
        return "unexpected"

    registry.register(
        "direct_async_in_loop",
        "Direct async in loop",
        {"type": "object", "properties": {}},
        impl,
    )

    async def run() -> None:
        try:
            registry.execute("direct_async_in_loop", {}, tool_timeout_sec=0.01)
        except RuntimeError as exc:
            assert "use execute_async" in str(exc)
        else:
            raise AssertionError("execute() must reject async tools in an active loop")

    asyncio.run(run())
    assert called is False


def test_timeout_result_does_not_contain_interrupted_text() -> None:
    registry = ToolRegistry()

    def cooperative(args: dict[str, object]) -> ToolExecutionResult:
        cancel_check = args["_cancel_check"]
        assert callable(cancel_check)
        while not cancel_check():
            threading.Event().wait(0.001)
        return ToolExecutionResult(
            "--- INTERRUPTED ---\nImplementation observed cancellation.",
            reason="interrupted",
        )

    registry.register(
        "contradictory_cleanup",
        "Contradictory cleanup",
        {"type": "object", "properties": {}},
        cooperative,
    )

    result = asyncio.run(
        registry.execute_async(
            "contradictory_cleanup",
            {},
            tool_timeout_sec=0.01,
            preserve_tool_result=True,
        )
    )

    assert isinstance(result, ToolExecutionResult)
    assert result.reason == "timeout"
    assert "TIMEOUT" in result.output
    assert "INTERRUPTED" not in result.output



def test_timed_context_tool_gets_worker_owned_sqlite_connection(tmp_path) -> None:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="root")
    registry = ToolRegistry()
    original_connection = db.conn
    seen: dict[str, object] = {}

    def impl(args: dict[str, object], ctx: ToolContext) -> str:
        seen["db"] = ctx.db
        assert isinstance(ctx.db, ts.ThreadsDB)
        row = ctx.db.get_thread(thread_id)
        assert row is not None
        return row.name or ""

    registry.register(
        "sqlite_context_tool",
        "SQLite context tool",
        {"type": "object", "properties": {}},
        impl,
        accepts_context=True,
    )

    result = registry.execute(
        "sqlite_context_tool",
        {},
        db=db,
        thread_id=thread_id,
        tool_timeout_sec=1,
    )

    assert result == "root"
    worker_db = seen["db"]
    assert isinstance(worker_db, ts.ThreadsDB)
    assert worker_db is not db
    assert worker_db.conn is not original_connection
    with pytest.raises(Exception):
        worker_db.conn.execute("SELECT 1")
    assert db.conn.execute("SELECT 1").fetchone()[0] == 1


def test_timed_context_tool_preserves_custom_db_identity() -> None:
    registry = ToolRegistry()
    custom_db = object()

    def impl(args: dict[str, object], ctx: ToolContext) -> bool:
        return ctx.db is custom_db

    registry.register(
        "custom_db_context_tool",
        "Custom DB context tool",
        {"type": "object", "properties": {}},
        impl,
        accepts_context=True,
    )

    assert registry.execute(
        "custom_db_context_tool",
        {},
        db=custom_db,
        tool_timeout_sec=1,
    ) is True


def test_timed_context_tool_rejects_memory_db_without_relaxing_affinity() -> None:
    registry = ToolRegistry()
    memory_db = ts.ThreadsDB(":memory:")
    memory_db.init_schema()
    called = False

    def impl(args: dict[str, object], ctx: ToolContext) -> bool:
        nonlocal called
        called = True
        return True

    registry.register(
        "memory_db_context_tool",
        "Memory DB context tool",
        {"type": "object", "properties": {}},
        impl,
        accepts_context=True,
    )

    result = registry.execute(
        "memory_db_context_tool",
        {},
        db=memory_db,
        tool_timeout_sec=1,
        preserve_tool_result=True,
    )

    assert isinstance(result, ToolExecutionResult)
    assert result.reason == "unsupported"
    assert "in-memory ThreadsDB" in result.output
    assert called is False

    errors: list[BaseException] = []

    def cross_thread_probe() -> None:
        try:
            memory_db.conn.execute("SELECT 1")
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=cross_thread_probe)
    thread.start()
    thread.join()
    assert errors
    assert "same thread" in str(errors[0]).lower()


def test_direct_execute_admission_and_execution_share_one_deadline(monkeypatch) -> None:
    import eggthreads.tools as tools_module

    registry = ToolRegistry()
    release_slot = threading.Event()
    original_admission = tools_module._SYNC_TOOL_ADMISSION
    tools_module._SYNC_TOOL_ADMISSION = tools_module._SyncToolAdmission(1)
    admission = tools_module._SYNC_TOOL_ADMISSION
    assert admission.try_acquire() is True

    def delayed_release() -> None:
        release_slot.wait(0.04)
        admission.completed(was_detached=False)

    releaser = threading.Thread(target=delayed_release)
    releaser.start()
    registry.register(
        "slow_after_admission",
        "Slow after admission",
        {"type": "object", "properties": {}},
        lambda args: __import__("time").sleep(0.04) or "too-late",
    )

    started_at = __import__("time").monotonic()
    try:
        result = registry.execute(
            "slow_after_admission",
            {},
            tool_timeout_sec=0.06,
            preserve_tool_result=True,
        )
        elapsed = __import__("time").monotonic() - started_at
    finally:
        release_slot.set()
        releaser.join()
        deadline = __import__("time").monotonic() + 0.5
        while admission.counts() != (0, 0):
            if __import__("time").monotonic() >= deadline:
                raise AssertionError("worker accounting did not clean up")
            __import__("time").sleep(0.001)
        tools_module._SYNC_TOOL_ADMISSION = original_admission

    assert isinstance(result, ToolExecutionResult)
    assert result.reason == "timeout"
    assert elapsed < 0.2  # deadline plus the 250 ms grace is not spent twice


def test_execute_async_rejects_timed_sync_tool_with_memory_db() -> None:
    registry = ToolRegistry()
    memory_db = ts.ThreadsDB(":memory:")
    memory_db.init_schema()
    called = False

    def impl(args: dict[str, object], ctx: ToolContext) -> str:
        nonlocal called
        called = True
        return "unexpected"

    registry.register(
        "async_memory_db_context_tool",
        "Async memory DB context tool",
        {"type": "object", "properties": {}},
        impl,
        accepts_context=True,
    )

    result = asyncio.run(
        registry.execute_async(
            "async_memory_db_context_tool",
            {},
            db=memory_db,
            tool_timeout_sec=1,
            preserve_tool_result=True,
        )
    )

    assert isinstance(result, ToolExecutionResult)
    assert result.reason == "unsupported"
    assert "in-memory ThreadsDB" in result.output
    assert called is False
